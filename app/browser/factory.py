"""CloakBrowser factory with Xvfb virtual display management."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class DisplaySlot:
    """A virtual display slot for a browser session."""

    display_num: int
    vnc_port: int
    novnc_port: int
    cdp_port: int = 0
    xvfb_proc: subprocess.Popen | None = None
    vnc_proc: subprocess.Popen | None = None
    novnc_proc: subprocess.Popen | None = None
    chrome_proc: asyncio.subprocess.Process | None = None
    user_data_dir: str | None = None


class DisplayManager:
    """Allocates and cleans up Xvfb + VNC displays for browser sessions."""

    def __init__(self) -> None:
        self._slots: dict[int, DisplaySlot] = {}
        self._lock = asyncio.Lock()

    async def allocate(self) -> DisplaySlot:
        """Allocate a new virtual display with VNC."""
        async with self._lock:
            # Find first free display number
            for i in range(settings.max_concurrent_sessions):
                display_num = settings.xvfb_base_display + i
                if display_num not in self._slots:
                    break
            else:
                raise RuntimeError("No free display slots")

            vnc_port = settings.vnc_base_port + display_num
            novnc_port = settings.novnc_base_port + i
            cdp_port = settings.cdp_base_port + i

            slot = DisplaySlot(
                display_num=display_num,
                vnc_port=vnc_port,
                novnc_port=novnc_port,
                cdp_port=cdp_port,
            )

            try:
                # Start Xvfb
                slot.xvfb_proc = subprocess.Popen(
                    [
                        "Xvfb",
                        f":{display_num}",
                        "-screen", "0", "1920x1080x24",
                        "-ac",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await asyncio.sleep(0.5)

                # Start x11vnc
                slot.vnc_proc = subprocess.Popen(
                    [
                        "x11vnc",
                        "-display", f":{display_num}",
                        "-rfbport", str(vnc_port),
                        "-nopw",
                        "-forever",
                        "-shared",
                        "-quiet",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                # Start noVNC websockify proxy
                slot.novnc_proc = subprocess.Popen(
                    [
                        "websockify",
                        "--web", "/usr/share/novnc",
                        str(novnc_port),
                        f"localhost:{vnc_port}",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                # Clean up any processes that did start
                for proc_name in ("novnc_proc", "vnc_proc", "xvfb_proc"):
                    proc = getattr(slot, proc_name)
                    if proc and proc.poll() is None:
                        proc.terminate()
                raise

            self._slots[display_num] = slot
            logger.info(
                "Allocated display :%d (VNC :%d, noVNC :%d, CDP :%d)",
                display_num, vnc_port, novnc_port, cdp_port,
            )
            return slot

    async def release(self, display_num: int) -> None:
        """Release a display slot and clean up processes."""
        async with self._lock:
            slot = self._slots.pop(display_num, None)
            if not slot:
                return

            await stop_chrome(slot)

            for proc_name in ("novnc_proc", "vnc_proc", "xvfb_proc"):
                proc = getattr(slot, proc_name)
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()

            if slot.user_data_dir:
                shutil.rmtree(slot.user_data_dir, ignore_errors=True)

            logger.info("Released display :%d", display_num)

    async def cleanup_all(self) -> None:
        """Release all display slots. Called on shutdown."""
        display_nums = list(self._slots.keys())
        for dn in display_nums:
            await self.release(dn)

async def wait_for_cdp(port: int, timeout: float = 30.0) -> None:
    """Poll http://127.0.0.1:{port}/json/version until 200 or timeout."""
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(url, timeout=1.0)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Chrome CDP not ready on port {port} after {timeout}s")
            await asyncio.sleep(0.5)


async def launch_chrome(slot: DisplaySlot) -> str:
    """Launch Chrome with stealth args on the slot's virtual display.

    Returns the CDP URL string (e.g. 'http://127.0.0.1:9222').
    """
    import cloakbrowser

    binary_path = cloakbrowser.ensure_binary()
    stealth_args = cloakbrowser.get_default_stealth_args()

    # @nonobvious(forced-by) Chromium SingletonLocks a shared user-data-dir, so concurrent sessions each need their own or only the first binds its CDP port
    user_data_dir = f"/tmp/bu-chrome-{slot.display_num}"
    shutil.rmtree(user_data_dir, ignore_errors=True)
    slot.user_data_dir = user_data_dir

    args = [binary_path] + list(stealth_args) + [
        f"--remote-debugging-port={slot.cdp_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--window-size=1920,1080",
    ]

    slot.chrome_proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "DISPLAY": f":{slot.display_num}"},
    )

    await wait_for_cdp(slot.cdp_port)

    cdp_url = f"http://127.0.0.1:{slot.cdp_port}"
    logger.info("Chrome launched on display :%d, CDP at %s", slot.display_num, cdp_url)
    return cdp_url


async def stop_chrome(slot: DisplaySlot) -> None:
    """Terminate Chrome process for a slot, if running."""
    proc = slot.chrome_proc
    if proc is None:
        return

    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()

    slot.chrome_proc = None


# Singleton display manager
display_manager = DisplayManager()
