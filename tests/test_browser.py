"""Browser factory tests -- only test logic, not actual Xvfb/VNC."""

import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from app.browser.factory import DisplayManager, DisplaySlot, launch_chrome, stop_chrome, wait_for_cdp
from app.config import settings


@pytest.fixture
def manager():
    return DisplayManager()


@patch("app.browser.factory.subprocess.Popen")
async def test_allocate_display(mock_popen, manager):
    mock_popen.return_value = MagicMock(poll=MagicMock(return_value=None))
    slot = await manager.allocate()
    assert slot.display_num == settings.xvfb_base_display
    assert slot.vnc_port == settings.vnc_base_port + settings.xvfb_base_display
    assert slot.cdp_port == settings.cdp_base_port + 0
    assert mock_popen.call_count == 3  # Xvfb, x11vnc, websockify


@patch("app.browser.factory.subprocess.Popen")
async def test_allocate_multiple(mock_popen, manager):
    mock_popen.return_value = MagicMock(poll=MagicMock(return_value=None))
    s1 = await manager.allocate()
    s2 = await manager.allocate()
    assert s1.display_num != s2.display_num
    assert s1.cdp_port != s2.cdp_port


@patch("app.browser.factory.subprocess.Popen")
async def test_release_terminates_all_processes(mock_popen, manager):
    mock_proc = MagicMock(poll=MagicMock(return_value=None))
    mock_popen.return_value = mock_proc

    mock_chrome_proc = AsyncMock()
    mock_chrome_proc.returncode = None

    with patch("app.browser.factory.stop_chrome", new=AsyncMock()) as mock_stop:
        slot = await manager.allocate()
        slot.chrome_proc = mock_chrome_proc
        await manager.release(slot.display_num)
        mock_stop.assert_called_once_with(slot)

    # Display processes still terminated
    assert mock_proc.terminate.call_count == 3


@patch("app.browser.factory.asyncio.create_subprocess_exec", new_callable=AsyncMock)
@patch("app.browser.factory.wait_for_cdp", new_callable=AsyncMock)
async def test_launch_chrome(mock_wait_cdp, mock_create_subproc):
    mock_wait_cdp.return_value = None

    mock_proc = AsyncMock()
    mock_proc.returncode = None
    mock_create_subproc.return_value = mock_proc

    mock_cloak = MagicMock()
    mock_cloak.ensure_binary = MagicMock()
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

    # Check args include stealth flag and CDP port
    positional_args = call_args.args
    args_str = " ".join(positional_args)
    assert "--stealth-flag" in args_str
    assert "--remote-debugging-port=9222" in args_str
    assert "--no-first-run" in args_str
    assert "--no-default-browser-check" in args_str
    assert "--disable-dev-shm-usage" in args_str
    assert "--window-size=1920,1080" in args_str

    # Verify chrome_proc stored on slot
    assert slot.chrome_proc is mock_proc

    # Verify cdp_url returned
    assert cdp_url == "http://127.0.0.1:9222"

    # Verify wait_for_cdp was called with the port
    mock_wait_cdp.assert_called_once_with(9222)


async def test_stop_chrome_terminates():
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)

    slot = DisplaySlot(display_num=10, vnc_port=5910, novnc_port=6080, cdp_port=9222)
    slot.chrome_proc = mock_proc

    with patch("app.browser.factory.asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
        mock_wait.return_value = None
        await stop_chrome(slot)

    mock_proc.terminate.assert_called_once()
    assert slot.chrome_proc is None


async def test_stop_chrome_noop_without_process():
    slot = DisplaySlot(display_num=10, vnc_port=5910, novnc_port=6080, cdp_port=9222)
    assert slot.chrome_proc is None
    # Should not raise
    await stop_chrome(slot)
    assert slot.chrome_proc is None
