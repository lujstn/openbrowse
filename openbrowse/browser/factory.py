"""CloakBrowser factory with Xvfb virtual display management."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass

import httpx

from openbrowse.config import settings

logger = logging.getLogger(__name__)

# How long a session already admitted by the pool waits for a display before
# giving up. Only ever reached when the pool's cap and the slot count differ.
_ALLOCATE_WAIT_SECONDS = 180.0


class NoDisplayCapacityError(RuntimeError):
    """Every display was busy for the whole wait. Transient: try again later."""


def _xvfb_argv(display_num: int) -> list[str]:
    return ["Xvfb", f":{display_num}", "-screen", "0", "1920x1080x24", "-ac"]


def _x11vnc_argv(display_num: int, vnc_port: int) -> list[str]:
    return [
        "x11vnc",
        "-display", f":{display_num}",
        "-rfbport", str(vnc_port),
        "-nopw",
        "-forever",
        "-shared",
        "-quiet",
    ]


def _websockify_argv(novnc_port: int, vnc_port: int) -> list[str]:
    return [
        "websockify",
        "--web", "/usr/share/novnc",
        str(novnc_port),
        f"localhost:{vnc_port}",
    ]


def _is_ours(args: str, command: list[str]) -> bool:
    """Whether a ps command line is one of ours, however it was resolved.

    The arguments must match exactly and the program must be the right one by
    name. Only the name is compared because a process rarely shows up under the
    argv it was handed: websockify is a script, so the kernel puts the
    interpreter and the script's resolved path in front of it.
    """
    tail = " ".join(command[1:])
    if not args.endswith(tail):
        return False
    program = args[: len(args) - len(tail)].rstrip()
    return os.path.basename(program.rsplit(" ", 1)[-1]) == command[0]


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
    owner: asyncio.Task | None = None


class DisplayManager:
    """Allocates and cleans up Xvfb + VNC displays for browser sessions."""

    def __init__(self) -> None:
        self._slots: dict[int, DisplaySlot] = {}
        self._lock = asyncio.Lock()
        self._waiters: list[asyncio.Future[None]] = []

    async def allocate(self) -> DisplaySlot:
        """Allocate a virtual display, waiting for one if every slot is busy.

        Waiting rather than failing is the point: concurrency is capped before
        a caller ever gets here, so finding nothing free means that cap and the
        real slot count have drifted apart. A caller that has already been
        admitted should queue behind a display, not die for want of one.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ALLOCATE_WAIT_SECONDS
        while True:
            async with self._lock:
                free = self._first_free_locked()
                if free is None and await self._reclaim_abandoned_locked():
                    free = self._first_free_locked()
                if free is not None:
                    return await self._open_locked(*free)
                waiter = loop.create_future()
                self._waiters.append(waiter)

            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.wait_for(waiter, remaining)
            except (TimeoutError, asyncio.TimeoutError):
                raise NoDisplayCapacityError(
                    f"No display slot came free within {_ALLOCATE_WAIT_SECONDS:.0f}s "
                    f"({len(self._slots)} of {settings.max_concurrent_sessions} in use)"
                ) from None
            finally:
                self._discard_waiter(waiter)

    def _first_free_locked(self) -> tuple[int, int] | None:
        """The (index, display number) of the lowest free slot, or None."""
        for i in range(settings.max_concurrent_sessions):
            display_num = settings.xvfb_base_display + i
            if display_num not in self._slots:
                return i, display_num
        return None

    async def _reclaim_abandoned_locked(self) -> bool:
        """Free any slot whose owning task has finished without releasing it.

        A worker is meant to hand its display back on the way out. If its task
        is done and the slot is still here, that hand-back was skipped and the
        slot would otherwise stay held for the life of the process, shrinking
        capacity by one every time it happens.
        """
        abandoned = [
            display_num
            for display_num, slot in self._slots.items()
            if slot.owner is not None and slot.owner.done()
        ]
        for display_num in abandoned:
            slot = self._slots.pop(display_num)
            logger.warning(
                "Reclaimed display :%d — its session ended without releasing it",
                display_num,
            )
            await self._teardown(slot)
        return bool(abandoned)

    async def _open_locked(self, index: int, display_num: int) -> DisplaySlot:
        slot = DisplaySlot(
            display_num=display_num,
            vnc_port=settings.vnc_base_port + display_num,
            novnc_port=settings.novnc_base_port + index,
            cdp_port=settings.cdp_base_port + index,
            owner=asyncio.current_task(),
        )

        # @nonobvious(deliberately-missing): x11vnc and websockify are not
        # started here — they cost continuous framebuffer-polling CPU per
        # session, so ensure_vnc() starts them on the first viewer instead.
        slot.xvfb_proc = subprocess.Popen(
            _xvfb_argv(display_num),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(0.5)

        self._slots[display_num] = slot
        logger.info(
            "Allocated display :%d (VNC :%d, noVNC :%d, CDP :%d)",
            display_num, slot.vnc_port, slot.novnc_port, slot.cdp_port,
        )
        return slot

    def _discard_waiter(self, waiter: asyncio.Future[None]) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass

    def _wake_waiters(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

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
                    _x11vnc_argv(display_num, slot.vnc_port),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if slot.novnc_proc is None or slot.novnc_proc.poll() is not None:
                slot.vnc_ready = False
                slot.novnc_proc = subprocess.Popen(
                    _websockify_argv(slot.novnc_port, slot.vnc_port),
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
            await self._teardown(slot)
            logger.info("Released display :%d", display_num)
            self._wake_waiters()

    async def _teardown(self, slot: DisplaySlot) -> None:
        """Stop everything a slot owns. The caller has already unregistered it."""
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

    async def cleanup_all(self) -> None:
        """Release all display slots. Called on shutdown."""
        display_nums = list(self._slots.keys())
        for dn in display_nums:
            await self.release(dn)

    def sweep_orphans(self) -> int:
        """Kill display processes left behind by a previous run of this server.

        A hard kill leaves Xvfb still owning its display number, and the next
        start would then launch Chrome against an X server it does not control.
        Only processes running one of the exact commands this server launches
        are touched, so anything else on the box — including someone's own Xvfb
        on the same display — is left alone.
        """
        ours: list[list[str]] = []
        for i in range(settings.max_concurrent_sessions):
            display_num = settings.xvfb_base_display + i
            vnc_port = settings.vnc_base_port + display_num
            novnc_port = settings.novnc_base_port + i
            ours.append(_xvfb_argv(display_num))
            ours.append(_x11vnc_argv(display_num, vnc_port))
            ours.append(_websockify_argv(novnc_port, vnc_port))

        try:
            listing = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            logger.warning("Could not list processes to sweep orphaned displays")
            return 0

        killed = 0
        for line in listing.splitlines():
            pid_text, _, args = line.strip().partition(" ")
            args = args.strip()
            if not pid_text.isdigit() or not any(_is_ours(args, c) for c in ours):
                continue
            pid = int(pid_text)
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                continue
            killed += 1
            logger.warning("Killed orphaned display process %d (%s)", pid, args.strip())
        return killed


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
        # @nonobvious(forced-by) a headless Linux host runs no keyring daemon, so Chrome's default Secret Service lookup waits out D-Bus's 25s timeout before it issues its first network request
        "--password-store=basic",
        "--window-size=1920,1080",
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
