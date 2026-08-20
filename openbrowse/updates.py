"""Background update checking against PyPI, and user-driven upgrades.

The checker polls PyPI's JSON API on an interval and caches the result; the
dashboard reads the cache to show its badge and calls :func:`install_update`
when the user asks for the upgrade. The upgrade command depends on how this
copy was installed: a uv tool, a plain pip/venv install, or a git checkout.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from openbrowse import __version__
from openbrowse.config import settings

logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/openbrowse/json"

_VERSION_PART_RE = re.compile(r"^\d+")


def parse_version(text: str) -> tuple[int, ...]:
    """Lenient numeric version key: '1.10.2' > '1.9.0'; junk parts count as 0."""
    parts: list[int] = []
    for chunk in text.strip().lstrip("v").split("."):
        match = _VERSION_PART_RE.match(chunk)
        parts.append(int(match.group(0)) if match else 0)
    return tuple(parts) if parts else (0,)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


@dataclass
class UpdateState:
    current: str = __version__
    latest: str | None = None
    available: bool = False
    checked_at: float | None = None
    error: str | None = None
    installing: bool = False
    install_log: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "current": self.current,
            "latest": self.latest,
            "available": self.available,
            "checkedAt": self.checked_at,
            "error": self.error,
            "installing": self.installing,
        }


state = UpdateState()
_install_lock = asyncio.Lock()


async def check_once(client: httpx.AsyncClient | None = None) -> UpdateState:
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=15)
    try:
        resp = await client.get(
            PYPI_URL, headers={"Accept": "application/json"}, follow_redirects=True
        )
        resp.raise_for_status()
        latest = str(resp.json()["info"]["version"])
        state.latest = latest
        state.available = is_newer(latest, state.current)
        state.error = None
    except Exception as exc:
        state.error = f"{type(exc).__name__}: {exc}"
        logger.debug("Update check failed: %s", state.error)
    finally:
        state.checked_at = time.time()
        if own_client:
            await client.aclose()
    return state


async def checker_loop() -> None:
    interval = settings.update_check_hours * 3600
    if interval <= 0:
        return
    # @nonobvious(means): the short first delay keeps startup off the critical
    # path while still surfacing the badge within a minute of boot.
    delay = 20.0
    while True:
        try:
            await asyncio.sleep(delay)
            await check_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Update checker iteration failed")
        delay = interval


def _uv_binary() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "uv"
    return str(fallback) if fallback.exists() else None


def detect_install_method() -> tuple[str, list[list[str]] | None]:
    """Return (method, upgrade command list) for this running copy.

    Methods: 'checkout' (git clone), 'uv-tool' (uv tool install), 'pip'
    (pip/uv venv install), 'unknown' (no safe upgrade command).
    """
    home = settings.home_dir
    if (home / "pyproject.toml").exists() and (home / ".git").exists():
        uv = _uv_binary()
        commands: list[list[str]] = [["git", "-C", str(home), "pull", "--ff-only"]]
        if uv:
            commands.append([uv, "sync", "--project", str(home)])
        return "checkout", commands

    parts = Path(sys.prefix).resolve().parts
    if any(parts[i] == "uv" and parts[i + 1] == "tools" for i in range(len(parts) - 1)):
        uv = _uv_binary()
        if uv:
            return "uv-tool", [[uv, "tool", "upgrade", "openbrowse"]]
        return "uv-tool", None

    if sys.prefix != sys.base_prefix or _in_user_site():
        return "pip", [[sys.executable, "-m", "pip", "install", "--upgrade", "openbrowse"]]

    return "unknown", None


def _in_user_site() -> bool:
    try:
        import site

        return Path(__file__).resolve().is_relative_to(Path(site.getusersitepackages()))
    except Exception:
        return False


async def install_update() -> tuple[bool, str]:
    """Run the upgrade command for the detected install method.

    Returns (ok, combined log). The caller decides whether to restart.
    """
    if _install_lock.locked():
        return False, "An update is already installing."
    async with _install_lock:
        method, commands = detect_install_method()
        if not commands:
            return False, (
                f"No automatic upgrade path for install method {method!r}. "
                "Upgrade manually, then restart the service."
            )
        state.installing = True
        log_parts: list[str] = [f"install method: {method}"]
        try:
            for command in commands:
                log_parts.append("$ " + " ".join(command))
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env={**os.environ, "UV_NO_PROGRESS": "1"},
                    )
                    raw, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
                except FileNotFoundError:
                    log_parts.append(f"command not found: {command[0]}")
                    return False, "\n".join(log_parts)
                except asyncio.TimeoutError:
                    proc.kill()
                    log_parts.append("timed out after 600s")
                    return False, "\n".join(log_parts)
                output = raw.decode(errors="replace").strip()
                if output:
                    log_parts.append(output)
                if proc.returncode != 0:
                    log_parts.append(f"exit code {proc.returncode}")
                    return False, "\n".join(log_parts)
            return True, "\n".join(log_parts)
        finally:
            state.installing = False
            state.install_log = "\n".join(log_parts)
