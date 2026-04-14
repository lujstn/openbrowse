"""Browser factory tests -- only test logic, not actual Xvfb/VNC."""

import pytest
from unittest.mock import patch, MagicMock

from app.browser.factory import DisplayManager, create_browser_kwargs, DisplaySlot
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
    assert mock_popen.call_count == 3  # Xvfb, x11vnc, websockify


@patch("app.browser.factory.subprocess.Popen")
async def test_allocate_multiple(mock_popen, manager):
    mock_popen.return_value = MagicMock(poll=MagicMock(return_value=None))
    s1 = await manager.allocate()
    s2 = await manager.allocate()
    assert s1.display_num != s2.display_num


@patch("app.browser.factory.subprocess.Popen")
async def test_release_terminates_processes(mock_popen, manager):
    mock_proc = MagicMock(poll=MagicMock(return_value=None))
    mock_popen.return_value = mock_proc
    slot = await manager.allocate()
    await manager.release(slot.display_num)
    assert mock_proc.terminate.call_count == 3


def test_create_browser_kwargs():
    slot = DisplaySlot(display_num=10, vnc_port=5910, novnc_port=6080)
    mock_cloak = MagicMock()
    mock_cloak.executable_path.return_value = "/usr/bin/cloakbrowser"
    with patch.dict("sys.modules", {"cloakbrowser": mock_cloak}):
        kwargs = create_browser_kwargs(slot)
    assert kwargs["headless"] is False
    assert kwargs["env"]["DISPLAY"] == ":10"
    assert kwargs["executable_path"] == "/usr/bin/cloakbrowser"
