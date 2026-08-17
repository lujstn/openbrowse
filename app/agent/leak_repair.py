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

_CARD_TAG_NAMES = (
    "what_i_see",
    "plan_to_goal",
    "next_move",
    "evaluation_previous_goal",
    "memory",
    "next_goal",
    "current_plan_item",
    "plan_update",
    "thinking",
)
# @nonobvious(forced-by): the ``"?`` tolerates the malformed ``<next_move">`` stray-quote
# form Claude emits mid-degeneration; without it those tags survive as literal text.
_TAG_BLEED_RE = re.compile(r'<(/?)\s*(' + "|".join(_CARD_TAG_NAMES) + r')"?\s*>')
_INVOKE_RESTART_RE = re.compile(r"<invoke\b", re.IGNORECASE)
# @nonobvious(deliberately-missing): 'action' is NOT in the junk pattern — hoist_leaked_action
# needs the <action> tags intact to locate a leaked payload before they are stripped.
_TAG_JUNK_RE = re.compile(r"</?(?:invoke|parameter|AgentOutput)\b[^>]*>", re.IGNORECASE)


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


def _clean_bleed_text(text: str) -> str:
    """Drop a restarted tool call and any leftover invoke/parameter or field tags,
    then trim — leaving only the prose that belongs in the host field.
    """
    m = _INVOKE_RESTART_RE.search(text)
    if m:
        text = text[: m.start()]
    text = _TAG_JUNK_RE.sub("", text)
    text = _TAG_BLEED_RE.sub("", text)
    return text.strip()


def scrub_tag_bleed(tool_input: dict) -> bool:
    """Repair Claude's trained tool-call XML idiom bleeding as literal text into the
    forced-JSON AgentOutput fields (seen with reasoningEffort=none, which uses forced
    ``tool_choice``): truncate a restarted ``<invoke ...>`` tool call, split a field's
    value on sibling field tags (tolerating the malformed ``<next_move">`` form), keep
    the pre-tag text as that field's own value, and route each bled-out segment to its
    named sibling field only when that field is still empty. Mutates *tool_input* in
    place; returns True if it changed anything.
    """
    if not isinstance(tool_input, dict):
        return False
    changed = False
    harvested: dict[str, str] = {}
    for key, value in list(tool_input.items()):
        if not isinstance(value, str):
            continue
        if not (
            _TAG_BLEED_RE.search(value)
            or _INVOKE_RESTART_RE.search(value)
            or _TAG_JUNK_RE.search(value)
        ):
            continue
        invoke_m = _INVOKE_RESTART_RE.search(value)
        work = value[: invoke_m.start()] if invoke_m else value
        parts = _TAG_BLEED_RE.split(work)
        for i in range(1, len(parts) - 2, 3):
            slash, name, following = parts[i], parts[i + 1], parts[i + 2]
            if slash:
                continue
            seg = _clean_bleed_text(following)
            if seg and name not in harvested:
                harvested[name] = seg
        cleaned = _clean_bleed_text(parts[0])
        if cleaned != value:
            tool_input[key] = cleaned
            changed = True
    for name, seg in harvested.items():
        existing = tool_input.get(name)
        if not (isinstance(existing, str) and existing.strip()):
            tool_input[name] = seg
            changed = True
    return changed


def merge_parallel_tool_calls(
    response: object,
    output_tool_name: str | None,
    action_names: set[str] | None = None,
) -> int:
    """Fold parallel ``tool_use`` blocks back into one structured output call.

    Claude under auto tool choice (forced by extended thinking) sometimes splits
    its reply into several tool_use blocks — either extra calls to the output
    tool, or calls "invoking" individual actions by name with that action's bare
    parameters as the input. The caller validates only the FIRST block, so the
    split either fails validation or silently drops actions. This merges every
    block's actions, in order, into the first block. Returns the number of
    blocks absorbed (0 when there was nothing to merge).
    """
    content = getattr(response, "content", None) or []
    blocks = [b for b in content if getattr(b, "type", None) == "tool_use"]
    if len(blocks) < 2 and (
        not blocks
        or output_tool_name is None
        or getattr(blocks[0], "name", None) == output_tool_name
    ):
        return 0
    merged: list[dict] = []
    host_fields: dict = {}
    absorbed = 0
    for block in blocks:
        tool_input = getattr(block, "input", None)
        name = getattr(block, "name", None)
        if not isinstance(tool_input, dict):
            return 0
        actions = tool_input.get("action")
        if isinstance(actions, list) and actions:
            for key, value in tool_input.items():
                if key != "action" and key not in host_fields:
                    host_fields[key] = value
            merged.extend(a for a in actions if isinstance(a, dict))
            absorbed += 1
        elif (
            name
            and name != output_tool_name
            and (action_names is None or name in action_names)
        ):
            merged.append({name: tool_input})
            absorbed += 1
        elif name and name != output_tool_name:
            logger.warning(
                "parallel tool_use merge dropped a block invoking unknown "
                "action %r: %r",
                name,
                {k: (v[:200] if isinstance(v, str) else v) for k, v in tool_input.items()},
            )
        else:
            logger.warning(
                "parallel tool_use merge dropped an output-tool block with no "
                "action list (name=%r): %r",
                name,
                {k: (v[:200] if isinstance(v, str) else v) for k, v in tool_input.items()},
            )
    if not merged:
        return 0
    blocks[0].input = {**host_fields, "action": merged}
    return absorbed


def repair_anthropic_message(
    response: object,
    output_tool_name: str | None = None,
    action_names: set[str] | None = None,
    param_kinds: dict[str, dict[str, str]] | None = None,
) -> int:
    """Scrub card-field tag-bleed and hoist leaked actions out of every ``tool_use``
    block in an Anthropic message. Returns the number of blocks whose action was
    hoisted back into place.

    Operates purely via ``getattr`` so it needs no anthropic/browser-use imports.
    """
    repaired = 0
    content = getattr(response, "content", None) or []
    for block in content:
        if getattr(block, "type", None) != "tool_use":
            continue
        tool_input = getattr(block, "input", None)
        if not isinstance(tool_input, dict):
            continue
        scrub_tag_bleed(tool_input)
        if tool_input.get("action"):
            continue
        if hoist_leaked_action(tool_input):
            repaired += 1
    merge_parallel_tool_calls(response, output_tool_name, action_names)
    if param_kinds:
        for block in content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_input = getattr(block, "input", None)
            if isinstance(tool_input, dict):
                coerce_action_param_shapes(tool_input, param_kinds)
    # @nonobvious(forced-by): only the first tool_use block is validated by the
    # caller, so it alone decides whether the reply survives — trailing blocks
    # are ignored either way and warning on them would be pure noise.
    first = next(
        (b for b in content if getattr(b, "type", None) == "tool_use"), None
    )
    tool_input = getattr(first, "input", None) if first else None
    if isinstance(tool_input, dict) and not tool_input.get("action"):
        snippet = {
            k: (v[:200] if isinstance(v, str) else v) for k, v in tool_input.items()
        }
        logger.warning(
            "action-leak repair could not salvage tool input (tool_use name=%r): %r",
            getattr(first, "name", None),
            snippet,
        )
    return repaired


_NESTED_ACTION_LOC_RE = re.compile(
    r"^(action\.\d+[^\n]*)\n\s+([^\n]+?)(?:\s+\[type=[^\]]+\][^\n]*)?$", re.MULTILINE
)


def mistyped_action_params(exc: BaseException) -> str | None:
    """The distilled pydantic detail when the failure is mis-typed action
    ARGUMENTS (``action.0.read_pages.urls: Input should be a valid list``)
    rather than a missing action list, else None. Telling the model it "sent
    no action" when one argument had the wrong type points it at the wrong
    repair and discards the only clue it could act on.
    """
    text = str(exc)
    if "validation error" not in text.lower():
        return None
    pairs = [
        f"{m.group(1)}: {m.group(2).strip()}"
        for m in _NESTED_ACTION_LOC_RE.finditer(text)
    ]
    return "; ".join(pairs) or None


def is_missing_action_error(exc: BaseException) -> bool:
    """True if *exc* looks like the AgentOutput ``action: Field required`` failure."""
    text = str(exc).lower()
    if "action" not in text or (
        "field required" not in text and "validation error" not in text
    ):
        return False
    return mistyped_action_params(exc) is None


def coerce_action_param_shapes(
    tool_input: dict, param_kinds: dict[str, dict[str, str]]
) -> bool:
    """Unwrap JSON-serialised argument values inside an action list, guided by
    the registry's declared types: a param whose annotation wants a list/dict
    but holds a string of that JSON shape is parsed; a nullable non-string
    param holding ``""``/``"null"``/``"none"`` becomes None. Content that does
    not match the declared type's JSON shape is never touched, so a legitimate
    string that merely looks like JSON survives in genuinely-string params.
    Mutates in place; returns True if anything changed.
    """
    actions = tool_input.get("action")
    if not isinstance(actions, list):
        return False
    changed = False
    for entry in actions:
        if not isinstance(entry, dict) or len(entry) != 1:
            continue
        ((name, params),) = entry.items()
        if not isinstance(params, dict):
            continue
        kinds = param_kinds.get(name) or {}
        for pname, value in list(params.items()):
            kind = kinds.get(pname)
            if not isinstance(value, str):
                continue
            text = value.strip()
            if kind in ("list", "dict"):
                open_ch, close_ch = ("[", "]") if kind == "list" else ("{", "}")
                if text.startswith(open_ch) and text.endswith(close_ch):
                    try:
                        parsed = json.loads(text)
                    except ValueError:
                        continue
                    expected = list if kind == "list" else dict
                    if isinstance(parsed, expected):
                        params[pname] = parsed
                        changed = True
            elif kind == "nullable" and text.lower() in ("", "null", "none"):
                params[pname] = None
                changed = True
    return changed
