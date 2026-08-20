"""Live code-writing observation: watch the LLM's partial output as it streams,
and the moment a ``run_code_file`` action starts forming, open the IDE tab and
type the code into it as the model writes it. The writing phase the user sees is
the real generation, not an animation after the fact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from openbrowse.config import settings

logger = logging.getLogger(__name__)

_CODE_KEY_RE = re.compile(r'"code"\s*:\s*"')
_NAME_RE = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"')
_ACTION_KEY_RE = re.compile(r'"run_code_file"\s*:\s*\{')

_PUSH_INTERVAL_S = 0.35

_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
}


def _partial_json_string_prefix(text: str) -> str:
    """Decode as much of a JSON string value as the stream has produced, stopping
    cleanly at the closing quote or a half-received escape sequence.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            break
        if c == "\\":
            if i + 1 >= n:
                break
            e = text[i + 1]
            if e == "u":
                if i + 6 > n:
                    break
                try:
                    out.append(chr(int(text[i + 2 : i + 6], 16)))
                except ValueError:
                    pass
                i += 6
                continue
            out.append(_ESCAPES.get(e, e))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def codeview_url() -> str:
    return f"http://127.0.0.1:{settings.port}/codeview"


class CodeStreamObserver:
    """Fed the LLM's accumulating raw output text during generation. On detecting
    a forming ``run_code_file`` call it opens the IDE tab, announces the writing
    phase to the feed, and pushes the code prefix into the tab as it grows. The
    tab is handed to ``run_code_file`` through the clipboard for the run phase.
    """

    def __init__(
        self,
        browser_session: Any,
        clipboard: dict[str, Any] | None,
        progress: Any = None,
    ) -> None:
        self._session = browser_session
        self._clipboard = clipboard
        self._progress = progress
        self._tab: str | None = None
        self._prev_focus: str | None = None
        self._announced = False
        self._last_push = 0.0
        self._last_len = 0
        self._seen_len = 0
        if clipboard is not None:
            clipboard["_code_stream"] = self

    def reset(self) -> None:
        self._tab = None
        self._prev_focus = None
        self._announced = False
        self._last_push = 0.0
        self._last_len = 0
        self._seen_len = 0
        if self._clipboard is not None:
            self._clipboard.pop("_code_stream_tab", None)

    async def settle(self, has_run_code_file: bool) -> None:
        """Called when a generation finishes, BEFORE any action executes: give
        focus back to the page so actions never land on the code tab. When the
        reply contains no run_code_file after all, the tab closes and state
        resets — no orphans, no stolen focus.
        """
        if self._tab is None:
            return
        try:
            from openbrowse.agent.tools import _close_spawned_tab, _focus_target

            if self._prev_focus:
                await _focus_target(self._session, self._prev_focus)
            if not has_run_code_file:
                tab = self._tab
                await _close_spawned_tab(self._session, tab)
                self.reset()
        except Exception:
            logger.warning("code stream settle failed", exc_info=True)

    async def on_partial(self, text: str) -> None:
        try:
            await self._on_partial(text)
        except Exception:
            logger.debug("code stream observation failed", exc_info=True)

    async def _on_partial(self, text: str) -> None:
        if len(text) < self._seen_len // 2:
            self.reset()
        self._seen_len = len(text)
        key = _ACTION_KEY_RE.search(text)
        if key is None:
            return
        idx = key.start()
        name_match = _NAME_RE.search(text, idx)
        name = name_match.group(1) if name_match else None
        code_match = _CODE_KEY_RE.search(text, idx)
        code_prefix = (
            _partial_json_string_prefix(text[code_match.end() :]) if code_match else ""
        )
        if not self._announced:
            self._announced = True
            if self._progress is not None:
                try:
                    await self._progress(f"⌨️ Writing {name or 'code'}")
                except Exception:
                    logger.debug("code stream progress emit failed", exc_info=True)
            await self._open_tab()
        if self._tab is None or not code_prefix:
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_push < _PUSH_INTERVAL_S or len(code_prefix) <= self._last_len:
            return
        self._last_push = now
        self._last_len = len(code_prefix)
        await self.push(name=name, code=code_prefix, status="Writing")

    async def _open_tab(self) -> None:
        from openbrowse.agent.tools import _focus_target, _spawn_tab

        if self._session is None:
            return
        self._prev_focus = getattr(self._session, "agent_focus_target_id", None)
        self._tab = await _spawn_tab(self._session, codeview_url())
        if self._tab is None:
            logger.warning("code stream: could not open the code tab")
            return
        await _focus_target(self._session, self._tab)
        logger.info("code stream: code tab opened (target %s)", self._tab)
        if self._clipboard is not None:
            self._clipboard["_code_stream_tab"] = self._tab

    async def push(
        self, name: str | None, code: str, status: str, target: str | None = None
    ) -> None:
        """Render the given code and status into the IDE tab via CDP evaluate."""
        tab = target or self._tab
        if self._session is None or tab is None:
            return
        try:
            cdp = await self._session.get_or_create_cdp_session(
                target_id=tab, focus=False
            )
            expr = (
                "window.__setCode && window.__setCode("
                f"{json.dumps(name or 'script.py')}, {json.dumps(code)}, "
                f"{json.dumps(status)})"
            )
            await cdp.cdp_client.send.Runtime.evaluate(
                params={"expression": expr}, session_id=cdp.session_id
            )
        except Exception:
            logger.debug("code stream push failed", exc_info=True)


def completion_has_run_code_file(completion: Any) -> bool:
    try:
        actions = getattr(completion, "action", None) or []
        for entry in actions:
            dump = entry.model_dump(exclude_none=True) if hasattr(entry, "model_dump") else {}
            if "run_code_file" in dump:
                return True
    except Exception:
        logger.debug("completion inspection failed", exc_info=True)
    return False
