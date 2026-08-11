"""Repair for Anthropic tool calls that leak their action list into a text field.

Under browser-use's non-flash prompt, Claude sometimes serialises its whole
``<action>[...]</action></AgentOutput>`` block as text inside the AgentOutput
``thinking`` field of the forced tool call, leaving the structured ``action``
field empty. The library then raises ``action: Field required``.

These helpers detect that pattern and hoist the leaked action list back into the
structured ``action`` field so the tool call validates, without disturbing the
``thinking`` reasoning we deliberately keep. Pure stdlib so it is unit-testable
without browser-use installed.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

_ACTION_BLOCK_RE = re.compile(r"<action>.*?</action>", re.DOTALL)
_STRAY_TAGS_RE = re.compile(r"</?(?:AgentOutput|thinking|action)>")


def _first_json_array(text: str) -> str | None:
    """Return the first balanced ``[...]`` substring of *text*, or None.

    Bracket- and string-aware so nested arrays/objects in the action payload do
    not truncate the match.
    """
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_action_array(array_str: str) -> list | None:
    try:
        parsed = json.loads(array_str)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, list) and parsed and all(isinstance(x, dict) for x in parsed):
        return parsed
    return None


def _extract_action_array(text: str) -> list | None:
    """Best-effort recovery of a leaked action list from a string: the payload
    right after an ``<action>`` tag when present, else the last balanced ``[...]``
    that parses to a non-empty list of objects (handles a missing opening tag).
    """
    if "<action>" in text:
        after = _first_json_array(text.split("<action>", 1)[1])
        if after:
            parsed = _parse_action_array(after)
            if parsed:
                return parsed
    best = None
    idx = text.find("[")
    while idx != -1:
        arr = _first_json_array(text[idx:])
        if arr:
            parsed = _parse_action_array(arr)
            if parsed:
                best = parsed
        idx = text.find("[", idx + 1)
    return best


def hoist_leaked_action(tool_input: dict) -> bool:
    """Recover a leaked action list into ``tool_input['action']``.

    If *tool_input* has no usable ``action`` but one of its string fields contains
    a leaked action array (typically inside ``<action>...</action>``, but tolerant
    of a missing opening tag), extract it into ``tool_input['action']`` and strip
    the stray tags. Mutates *tool_input* in place; returns True on a repair.
    """
    if not isinstance(tool_input, dict) or tool_input.get("action"):
        return False
    for key, value in list(tool_input.items()):
        if not isinstance(value, str):
            continue
        if "<action>" not in value and "</action>" not in value and "[{" not in value:
            continue
        parsed = _extract_action_array(value)
        if not parsed:
            continue
        tool_input["action"] = parsed
        cleaned = _ACTION_BLOCK_RE.sub("", value)
        cleaned = _STRAY_TAGS_RE.sub("", cleaned).strip()
        tool_input[key] = cleaned
        return True
    return False


def repair_anthropic_message(response: object) -> int:
    """Hoist leaked actions out of every ``tool_use`` block in an Anthropic
    message. Returns the number of blocks repaired.

    Operates purely via ``getattr`` so it needs no anthropic/browser-use imports.
    """
    repaired = 0
    content = getattr(response, "content", None) or []
    for block in content:
        if getattr(block, "type", None) != "tool_use":
            continue
        tool_input = getattr(block, "input", None)
        if not isinstance(tool_input, dict) or tool_input.get("action"):
            continue
        if hoist_leaked_action(tool_input):
            repaired += 1
        else:
            snippet = {
                k: (v[:200] if isinstance(v, str) else v) for k, v in tool_input.items()
            }
            logger.warning("action-leak repair could not salvage tool input: %r", snippet)
    return repaired


def is_missing_action_error(exc: BaseException) -> bool:
    """True if *exc* looks like the AgentOutput ``action: Field required`` failure."""
    text = str(exc).lower()
    return "action" in text and ("field required" in text or "validation error" in text)
