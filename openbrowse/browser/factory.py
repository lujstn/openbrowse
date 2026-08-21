"""CloakBrowser factory with Xvfb virtual display management."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

import httpx

from openbrowse.config import settings

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
    vnc_ready: bool = False


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

            # @nonobvious(deliberately-missing): x11vnc and websockify are not
            # started here — they cost continuous framebuffer-polling CPU per
            # session, so ensure_vnc() starts them on the first viewer instead.
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

            self._slots[display_num] = slot
            logger.info(
                "Allocated display :%d (VNC :%d, noVNC :%d, CDP :%d)",
                display_num, vnc_port, novnc_port, cdp_port,
            )
            return slot

    async def ensure_vnc(self, display_num: int) -> bool:
        """Start x11vnc + websockify for a slot if they are not already running,
        then wait until websockify answers. Returns False when the slot is gone
        or the stream never becomes ready. Idempotent; x11vnc runs with -forever
        -shared so the first viewer starts it for the slot's remaining lifetime.
        """
        from openbrowse.browser.vnc import wait_for_novnc

        async with self._lock:
            slot = self._slots.get(display_num)
            if slot is None:
                return False
            if (
                slot.vnc_ready
                and slot.vnc_proc is not None
                and slot.vnc_proc.poll() is None
                and slot.novnc_proc is not None
                and slot.novnc_proc.poll() is None
            ):
                return True
            if slot.vnc_proc is None or slot.vnc_proc.poll() is not None:
                slot.vnc_ready = False
                slot.vnc_proc = subprocess.Popen(
                    [
                        "x11vnc",
                        "-display", f":{display_num}",
                        "-rfbport", str(slot.vnc_port),
                        "-nopw",
                        "-forever",
                        "-shared",
                        "-quiet",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if slot.novnc_proc is None or slot.novnc_proc.poll() is not None:
                slot.vnc_ready = False
                slot.novnc_proc = subprocess.Popen(
                    [
                        "websockify",
                        "--web", "/usr/share/novnc",
                        str(slot.novnc_port),
                        f"localhost:{slot.vnc_port}",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            novnc_port = slot.novnc_port

        # @nonobvious(must-hold): never poll readiness while holding the manager
        # lock — the poll can take 10s and would freeze allocate/release for
        # every session. A release() racing in just makes the poll fail.
        ready = await wait_for_novnc(novnc_port)
        if ready:
            slot.vnc_ready = True
        return ready

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

    from openbrowse import prefetch

    # A fetch in flight owns the disk; launching under it repeats the exact
    # I/O starvation the prefetch exists to prevent.
    await prefetch.wait_until_settled()

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
        # Without this, a renderer killed for malformed IPC dies silently; with
        # it, the browser process logs "Terminating renderer for bad IPC
        # message, reason N" naming the offending validator.
        "--enable-logging=stderr",
    ]
    if settings.chrome_light_flags:
        # @nonobvious(deliberately-missing): no site-isolation collapse
        # (--disable-features=IsolateOrigins,site-per-process) — cross-origin
        # frame reads depend on per-target OOPIFs and would silently break.
        args += [
            "--disable-gpu",
            "--disable-gpu-compositing",
            "--renderer-process-limit=4",
            "--js-flags=--max-old-space-size=256",
            "--enable-low-end-device-mode",
            "--disable-background-networking",
        ]

    # Append mode so a mid-analysis relaunch cannot erase an earlier crash
    # record; the child owns its fd, so closing the parent handle is safe.
    log_path = f"/tmp/bu-chrome-{slot.display_num}.log"
    chrome_log = open(log_path, "ab")
    try:
        slot.chrome_proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=chrome_log,
            stderr=chrome_log,
            env={**os.environ, "DISPLAY": f":{slot.display_num}"},
        )
    finally:
        chrome_log.close()

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
