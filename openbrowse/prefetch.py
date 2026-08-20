"""Browser binary readiness, shared by the setup wizard and the runtime.

The stealth Chromium build is ~200MB, and on SD-card hardware a download
racing a session launch starves Chrome of I/O until its debug port misses the
readiness window. Everything that fetches the binary therefore funnels through
here: the wizard starts a prefetch so the download happens during onboarding,
the server prewarms at boot, and launches wait for whichever is in flight to
settle before starting Chrome.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Compressed archive plus the extracted build, with working room to spare.
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024

_PROGRESS_RE = re.compile(r"Download progress:\s*(\d+)%(?:\s*\((.*?)\))?")

_state: dict[str, Any] = {"state": "idle", "detail": "", "percent": None}
_task: asyncio.Task[None] | None = None
_settled = asyncio.Event()
_settled.set()


class _ProgressHandler(logging.Handler):
    """Mirrors cloakbrowser's own progress log lines into ``_state``.

    cloakbrowser exposes no progress callback, but it logs every download
    step; parsing those lines is the only non-invasive window into a transfer
    that can take minutes on a small board.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            return
        match = _PROGRESS_RE.search(message)
        if match:
            _state["state"] = "downloading"
            _state["percent"] = int(match.group(1))
            _state["detail"] = match.group(2) or ""
        elif "Extracting" in message:
            _state["state"] = "extracting"
            _state["percent"] = None
            _state["detail"] = "Unpacking the browser"


def _fetch_binary_blocking() -> None:
    import cloakbrowser

    cloakbrowser.ensure_binary()
    # ensure_binary answers from cache; the blocking check is what actually
    # pulls a newer build down, so nothing is left to download at launch time.
    cloakbrowser.check_for_update()


def status() -> dict[str, Any]:
    return dict(_state)


def is_ready() -> bool:
    return _state["state"] == "ready"


def start(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Begin fetching the browser binary in the background, once."""
    global _task
    if _task is not None and not _task.done():
        return
    if is_ready():
        return
    _state.update({"state": "starting", "detail": "", "percent": None})
    _settled.clear()

    async def _run() -> None:
        handler = _ProgressHandler()
        cb_logger = logging.getLogger("cloakbrowser")
        cb_logger.addHandler(handler)
        try:
            await asyncio.get_running_loop().run_in_executor(None, _fetch_binary_blocking)
            _state.update({"state": "ready", "detail": "", "percent": 100})
        except Exception as exc:
            logger.warning("Browser prefetch failed: %s", exc)
            _state.update({"state": "error", "detail": str(exc), "percent": None})
        finally:
            cb_logger.removeHandler(handler)
            _settled.set()

    _task = (loop or asyncio.get_running_loop()).create_task(_run())


async def wait_until_settled(timeout: float = 900.0) -> None:
    """Block a session launch while a fetch is in flight.

    Settling means ready OR error: a failed update check must not brick
    launches, because ensure_binary still answers from whatever the cache
    holds. When nothing was ever started there is nothing to wait for.
    """
    try:
        await asyncio.wait_for(_settled.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Browser prefetch still running after %.0fs; launching anyway", timeout)


def host_checks(home: Path | None = None) -> list[dict[str, Any]]:
    """Cheap preflight checks shown by the wizard's get-ready step."""
    checks: list[dict[str, Any]] = []

    target = home or Path.home()
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        free = None
    if free is None:
        checks.append(
            {
                "key": "disk",
                "label": "Disk space",
                "ok": True,
                "detail": "Could not be measured on this platform.",
            }
        )
    else:
        gb = free / (1024**3)
        checks.append(
            {
                "key": "disk",
                "label": "Disk space",
                "ok": free >= MIN_FREE_BYTES,
                "detail": (
                    f"{gb:.1f}GB free."
                    if free >= MIN_FREE_BYTES
                    else f"Only {gb:.1f}GB free; the browser needs about 2GB. Free some space, then re-check."
                ),
            }
        )

    xvfb = shutil.which("Xvfb") is not None
    checks.append(
        {
            "key": "xvfb",
            "label": "Virtual display (Xvfb)",
            "ok": xvfb,
            "detail": (
                "Installed."
                if xvfb
                else "Missing; sessions need it. Install with: sudo apt install -y xvfb"
            ),
        }
    )
    return checks
