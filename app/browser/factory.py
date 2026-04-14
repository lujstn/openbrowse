"""CloakBrowser factory with Xvfb virtual display management."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class DisplaySlot:
    """A virtual display slot for a browser session."""

    display_num: int
    vnc_port: int
    novnc_port: int
    xvfb_proc: subprocess.Popen | None = None
    vnc_proc: subprocess.Popen | None = None
    novnc_proc: subprocess.Popen | None = None


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

            slot = DisplaySlot(
                display_num=display_num,
                vnc_port=vnc_port,
                novnc_port=novnc_port,
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
                "Allocated display :%d (VNC :%d, noVNC :%d)",
                display_num, vnc_port, novnc_port,
            )
            return slot

    async def release(self, display_num: int) -> None:
        """Release a display slot and clean up processes."""
        async with self._lock:
            slot = self._slots.pop(display_num, None)
            if not slot:
                return

            for proc_name in ("novnc_proc", "vnc_proc", "xvfb_proc"):
                proc = getattr(slot, proc_name)
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()

            logger.info("Released display :%d", display_num)

    async def cleanup_all(self) -> None:
        """Release all display slots. Called on shutdown."""
        display_nums = list(self._slots.keys())
        for dn in display_nums:
            await self.release(dn)

    def get_live_url(self, slot: DisplaySlot, base_url: str) -> str:
        """Get the noVNC URL for a display slot."""
        return f"{base_url}:{slot.novnc_port}/vnc.html?autoconnect=true&resize=scale"


def create_browser_kwargs(slot: DisplaySlot) -> dict:
    """Build kwargs for browser-use Browser() using CloakBrowser on a virtual display."""
    import cloakbrowser

    return {
        "executable_path": cloakbrowser.executable_path(),
        "headless": False,
        "env": {"DISPLAY": f":{slot.display_num}"},
        "window_size": {"width": 1920, "height": 1080},
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }


# Singleton display manager
display_manager = DisplayManager()
