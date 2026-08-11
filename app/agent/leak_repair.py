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
import re

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


def hoist_leaked_action(tool_input: dict) -> bool:
    """Recover a leaked action list into ``tool_input['action']``.

    If *tool_input* has no usable ``action`` but one of its string fields
    contains a ``<action>[...]</action>`` block, extract that JSON array into
    ``tool_input['action']`` and strip the stray tags from the field. Mutates
    *tool_input* in place and returns True when a repair was made.
    """
    if not isinstance(tool_input, dict) or tool_input.get("action"):
        return False
    for key, value in list(tool_input.items()):
        if not isinstance(value, str) or "<action>" not in value:
            continue
        after = value.split("<action>", 1)[1]
        array_str = _first_json_array(after)
        if not array_str:
            continue
        try:
            parsed = json.loads(array_str)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, list) or not parsed:
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
        if isinstance(tool_input, dict) and hoist_leaked_action(tool_input):
            repaired += 1
    return repaired


def is_missing_action_error(exc: BaseException) -> bool:
    """True if *exc* looks like the AgentOutput ``action: Field required`` failure."""
    text = str(exc).lower()
    return "action" in text and ("field required" in text or "validation error" in text)
