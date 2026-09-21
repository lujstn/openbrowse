"""Browser factory tests -- only test logic, not actual Xvfb/VNC."""

import asyncio

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from openbrowse.browser.factory import DisplayManager, DisplaySlot, launch_chrome, stop_chrome
from openbrowse.config import settings


@pytest.fixture
def manager():
    return DisplayManager()


@patch("openbrowse.browser.factory.subprocess.Popen")
async def test_allocate_display(mock_popen, manager):
    mock_popen.return_value = MagicMock(poll=MagicMock(return_value=None))
    slot = await manager.allocate()
    assert slot.display_num == settings.xvfb_base_display
    assert slot.vnc_port == settings.vnc_base_port + settings.xvfb_base_display
    assert slot.cdp_port == settings.cdp_base_port + 0
    assert mock_popen.call_count == 1  # Xvfb only; VNC starts lazily on first view
    assert slot.vnc_proc is None
    assert slot.novnc_proc is None


@patch("openbrowse.browser.vnc.wait_for_novnc", new_callable=AsyncMock)
@patch("openbrowse.browser.factory.subprocess.Popen")
async def test_ensure_vnc_starts_once_and_is_idempotent(mock_popen, mock_wait, manager):
    mock_popen.return_value = MagicMock(poll=MagicMock(return_value=None))
    mock_wait.return_value = True
    slot = await manager.allocate()
    assert mock_popen.call_count == 1

    assert await manager.ensure_vnc(slot.display_num) is True
    assert mock_popen.call_count == 3  # + x11vnc, websockify
    assert slot.vnc_proc is not None
    assert slot.novnc_proc is not None

    assert await manager.ensure_vnc(slot.display_num) is True
    assert mock_popen.call_count == 3
    mock_wait.assert_awaited_once()


@patch("openbrowse.browser.vnc.wait_for_novnc", new_callable=AsyncMock)
@patch("openbrowse.browser.factory.subprocess.Popen")
async def test_ensure_vnc_false_for_released_slot(mock_popen, mock_wait, manager):
    mock_popen.return_value = MagicMock(poll=MagicMock(return_value=None))
    mock_wait.return_value = True
    with patch("openbrowse.browser.factory.stop_chrome", new=AsyncMock()):
        slot = await manager.allocate()
        await manager.release(slot.display_num)
    assert await manager.ensure_vnc(slot.display_num) is False
    mock_wait.assert_not_awaited()


@patch("openbrowse.browser.vnc.wait_for_novnc", new_callable=AsyncMock)
@patch("openbrowse.browser.factory.subprocess.Popen")
async def test_ensure_vnc_restarts_dead_processes(mock_popen, mock_wait, manager):
    mock_popen.side_effect = lambda *a, **k: MagicMock(poll=MagicMock(return_value=None))
    mock_wait.return_value = True
    slot = await manager.allocate()
    await manager.ensure_vnc(slot.display_num)
    assert mock_popen.call_count == 3

    slot.vnc_proc.poll.return_value = 1  # x11vnc died
    await manager.ensure_vnc(slot.display_num)
    assert mock_popen.call_count == 4


@patch("openbrowse.browser.factory.subprocess.Popen")
async def test_allocate_multiple(mock_popen, manager, monkeypatch):
    from dataclasses import replace

    import openbrowse.browser.factory as factory_mod

    monkeypatch.setattr(
        factory_mod, "settings", replace(settings, max_concurrent_sessions=5)
    )
    mock_popen.return_value = MagicMock(poll=MagicMock(return_value=None))
    s1 = await manager.allocate()
    s2 = await manager.allocate()
    assert s1.display_num != s2.display_num
    assert s1.cdp_port != s2.cdp_port


@patch("openbrowse.browser.vnc.wait_for_novnc", new_callable=AsyncMock)
@patch("openbrowse.browser.factory.subprocess.Popen")
async def test_release_terminates_all_processes(mock_popen, mock_wait, manager):
    mock_proc = MagicMock(poll=MagicMock(return_value=None))
    mock_popen.return_value = mock_proc
    mock_wait.return_value = True

    mock_chrome_proc = AsyncMock()
    mock_chrome_proc.returncode = None

    with patch("openbrowse.browser.factory.stop_chrome", new=AsyncMock()) as mock_stop:
        slot = await manager.allocate()
        await manager.ensure_vnc(slot.display_num)
        slot.chrome_proc = mock_chrome_proc
        await manager.release(slot.display_num)
        mock_stop.assert_called_once_with(slot)

    assert mock_proc.terminate.call_count == 3


@patch("openbrowse.browser.factory.asyncio.create_subprocess_exec", new_callable=AsyncMock)
@patch("openbrowse.browser.factory.wait_for_cdp", new_callable=AsyncMock)
async def test_launch_chrome(mock_wait_cdp, mock_create_subproc):
    mock_wait_cdp.return_value = None

    mock_proc = AsyncMock()
    mock_proc.returncode = None
    mock_create_subproc.return_value = mock_proc

    mock_cloak = MagicMock()
    mock_cloak.ensure_binary.return_value = "/usr/bin/cloakbrowser"
    mock_cloak.get_default_stealth_args.return_value = ["--stealth-flag"]

    slot = DisplaySlot(display_num=10, vnc_port=5910, novnc_port=6080, cdp_port=9222)

    with patch.dict("sys.modules", {"cloakbrowser": mock_cloak}):
        cdp_url = await launch_chrome(slot)

    # Verify ensure_binary was called
    mock_cloak.ensure_binary.assert_called_once()

    # Verify subprocess was launched
    mock_create_subproc.assert_called_once()
    call_args = mock_create_subproc.call_args

    # Check DISPLAY env is set per-session
    env = call_args.kwargs["env"]
    assert env["DISPLAY"] == ":10"

    # Check binary path is first arg
    positional_args = call_args.args
    assert positional_args[0] == "/usr/bin/cloakbrowser"

    args_str = " ".join(positional_args)
    assert "--stealth-flag" in args_str
    assert "--remote-debugging-port=9222" in args_str
    assert "--no-first-run" in args_str
    assert "--no-default-browser-check" in args_str
    assert "--disable-dev-shm-usage" in args_str
    assert "--password-store=basic" in args_str
    assert "--window-size=1920,1080" in args_str

    # Verify chrome_proc stored on slot
    assert slot.chrome_proc is mock_proc

    # Verify cdp_url returned
    assert cdp_url == "http://127.0.0.1:9222"

    # Verify wait_for_cdp was called with the port
    mock_wait_cdp.assert_called_once_with(9222)


@patch("openbrowse.browser.factory.asyncio.create_subprocess_exec", new_callable=AsyncMock)
@patch("openbrowse.browser.factory.wait_for_cdp", new_callable=AsyncMock)
async def test_launch_chrome_light_flags_env_gated(mock_wait_cdp, mock_create_subproc, monkeypatch):
    from dataclasses import replace

    import openbrowse.browser.factory as factory_mod

    mock_wait_cdp.return_value = None
    mock_proc = AsyncMock()
    mock_proc.returncode = None
    mock_create_subproc.return_value = mock_proc

    mock_cloak = MagicMock()
    mock_cloak.ensure_binary.return_value = "/usr/bin/cloakbrowser"
    mock_cloak.get_default_stealth_args.return_value = []

    slot = DisplaySlot(display_num=10, vnc_port=5910, novnc_port=6080, cdp_port=9222)
    monkeypatch.setattr(
        factory_mod, "settings", replace(settings, chrome_light_flags=True)
    )
    with patch.dict("sys.modules", {"cloakbrowser": mock_cloak}):
        await launch_chrome(slot)

    args_str = " ".join(mock_create_subproc.call_args.args)
    assert "--disable-gpu" in args_str
    assert "--renderer-process-limit=4" in args_str
    assert "--enable-low-end-device-mode" in args_str
    assert "--password-store=basic" in args_str
    assert "site-per-process" not in args_str


async def test_stop_chrome_terminates():
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    slot = DisplaySlot(display_num=10, vnc_port=5910, novnc_port=6080, cdp_port=9222)
    slot.chrome_proc = mock_proc

    async def _await_it(awaitable, timeout=None):
        return await awaitable

    with patch("openbrowse.browser.factory.asyncio.wait_for", side_effect=_await_it):
        await stop_chrome(slot)

    mock_proc.terminate.assert_called_once()
    assert slot.chrome_proc is None


async def test_stop_chrome_noop_without_process():
    slot = DisplaySlot(display_num=10, vnc_port=5910, novnc_port=6080, cdp_port=9222)
    assert slot.chrome_proc is None
    # Should not raise
    await stop_chrome(slot)
    assert slot.chrome_proc is None


@patch("openbrowse.browser.factory.subprocess.Popen")
async def test_allocate_waits_for_a_slot_rather_than_failing(mock_popen, manager):
    mock_popen.return_value = MagicMock(poll=MagicMock(return_value=None))
    first = await manager.allocate()

    waiting = asyncio.ensure_future(manager.allocate())
    await asyncio.sleep(0.05)
    assert not waiting.done()

    with patch("openbrowse.browser.factory.stop_chrome", new=AsyncMock()):
        await manager.release(first.display_num)

    second = await asyncio.wait_for(waiting, 3)
    assert second.display_num == first.display_num


@patch("openbrowse.browser.factory.subprocess.Popen")
async def test_allocate_reclaims_a_slot_its_worker_never_gave_back(mock_popen, manager):
    mock_popen.return_value = MagicMock(poll=MagicMock(return_value=None))

    async def worker() -> None:
        await manager.allocate()

    with patch("openbrowse.browser.factory.stop_chrome", new=AsyncMock()):
        await asyncio.create_task(worker())
        slot = await asyncio.wait_for(manager.allocate(), 3)

    assert slot.display_num == settings.xvfb_base_display
    assert len(manager._slots) == 1


@patch("openbrowse.browser.factory.subprocess.Popen")
async def test_allocate_reports_how_full_it_was_when_it_gave_up(
    mock_popen, manager, monkeypatch
):
    import openbrowse.browser.factory as factory_mod

    monkeypatch.setattr(factory_mod, "_ALLOCATE_WAIT_SECONDS", 0.1)
    mock_popen.return_value = MagicMock(poll=MagicMock(return_value=None))

    holding = asyncio.Event()

    async def holder() -> None:
        await manager.allocate()
        await holding.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.6)

    with pytest.raises(
        factory_mod.NoDisplayCapacityError, match="No display slot came free within 0s"
    ):
        await manager.allocate()

    holding.set()
    await task


@patch("openbrowse.browser.factory.os.kill")
@patch("openbrowse.browser.factory.subprocess.run")
async def test_sweep_kills_only_processes_this_server_would_have_launched(
    mock_run, mock_kill, manager
):
    import openbrowse.browser.factory as factory_mod

    display_num = settings.xvfb_base_display
    vnc_port = settings.vnc_base_port + display_num
    novnc_port = settings.novnc_base_port
    xvfb = " ".join(factory_mod._xvfb_argv(display_num))
    # websockify is a script, so the kernel prefixes its interpreter.
    websockify = "/usr/bin/python3 /usr/bin/" + " ".join(
        factory_mod._websockify_argv(novnc_port, vnc_port)
    )
    mock_run.return_value = MagicMock(
        stdout="\n".join(
            [
                f"  4242 {xvfb}",
                f"  4243 {websockify}",
                f"  4244 Xvfb :{display_num} -screen 0 800x600x24",
                "  4245 /usr/bin/some-other-daemon --flag",
                f"  4246 my-own-thing --note {xvfb}-ish",
            ]
        )
    )

    assert manager.sweep_orphans() == 2
    assert [call.args[0] for call in mock_kill.call_args_list] == [4242, 4243]
