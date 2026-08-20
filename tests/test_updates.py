"""Update checker: version comparison, PyPI polling, install-method detection,
the upgrade runner, and the dashboard endpoints that expose them."""

import base64
import sys
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from openbrowse import updates
from openbrowse.config import settings
from openbrowse.db.models import init_db
from openbrowse.main import app
from openbrowse.updates import UpdateState, is_newer, parse_version


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(updates, "state", UpdateState())


@pytest.mark.parametrize(
    "candidate,current,newer",
    [
        ("1.9.0", "1.8.0", True),
        ("1.10.0", "1.9.9", True),
        ("2.0.0", "1.99.99", True),
        ("1.8.0", "1.8.0", False),
        ("1.7.9", "1.8.0", False),
        ("v1.9.0", "1.8.0", True),
        ("1.9.0rc1", "1.8.0", True),
        ("garbage", "1.8.0", False),
    ],
)
def test_is_newer(candidate, current, newer):
    assert is_newer(candidate, current) is newer


def test_parse_version_lenient():
    assert parse_version("1.10.2") == (1, 10, 2)
    assert parse_version("v2.0.1") == (2, 0, 1)
    assert parse_version("") == (0,)


def _pypi_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_check_once_flags_newer_release(monkeypatch):
    monkeypatch.setattr(updates.state, "current", "1.8.0")

    def handler(request):
        assert str(request.url) == updates.PYPI_URL
        return httpx.Response(200, json={"info": {"version": "1.9.0"}})

    async with _pypi_client(handler) as client:
        state = await updates.check_once(client)

    assert state.latest == "1.9.0"
    assert state.available is True
    assert state.error is None
    assert state.checked_at is not None


async def test_check_once_up_to_date(monkeypatch):
    monkeypatch.setattr(updates.state, "current", "1.9.0")

    def handler(request):
        return httpx.Response(200, json={"info": {"version": "1.9.0"}})

    async with _pypi_client(handler) as client:
        state = await updates.check_once(client)

    assert state.available is False


async def test_check_once_survives_network_failure():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    async with _pypi_client(handler) as client:
        state = await updates.check_once(client)

    assert state.available is False
    assert state.error is not None
    assert state.checked_at is not None


async def test_check_once_survives_http_error():
    def handler(request):
        return httpx.Response(503)

    async with _pypi_client(handler) as client:
        state = await updates.check_once(client)

    assert state.error is not None


def test_detect_checkout(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(updates, "checkout_root", lambda: tmp_path)
    monkeypatch.setattr(updates, "_uv_binary", lambda: "/usr/bin/uv")

    method, commands = updates.detect_install_method()

    assert method == "checkout"
    assert commands[0][:3] == ["git", "-C", str(tmp_path)]
    assert commands[1] == ["/usr/bin/uv", "sync", "--project", str(tmp_path)]


def test_detect_uv_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "checkout_root", lambda: None)
    monkeypatch.setattr(updates, "_uv_binary", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        sys, "prefix", str(tmp_path / ".local/share/uv/tools/openbrowse")
    )

    method, commands = updates.detect_install_method()

    assert method == "uv-tool"
    assert commands == [["/usr/bin/uv", "tool", "upgrade", "openbrowse"]]


@pytest.mark.parametrize(
    "prefix",
    [
        "/home/pi/.local/pipx/venvs/openbrowse",
        "/home/pi/.local/share/pipx/venvs/openbrowse",
    ],
)
def test_detect_pipx(tmp_path, monkeypatch, prefix):
    """pipx owns the app, so pipx upgrades it. Reaching around it with a bare pip
    would leave pipx's records describing a version no longer installed."""
    monkeypatch.setattr(updates, "checkout_root", lambda: None)
    monkeypatch.setattr(updates, "_pipx_binary", lambda: "/usr/bin/pipx")
    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.setattr(sys, "base_prefix", "/usr")

    method, commands = updates.detect_install_method()

    assert method == "pipx"
    assert commands == [["/usr/bin/pipx", "upgrade", "openbrowse"]]


def test_detect_pipx_without_the_binary_offers_no_command(tmp_path, monkeypatch):
    """Better to say there is no automatic path than to invent one that reaches
    around the installer."""
    monkeypatch.setattr(updates, "checkout_root", lambda: None)
    monkeypatch.setattr(updates, "_pipx_binary", lambda: None)
    monkeypatch.setattr(sys, "prefix", "/home/pi/.local/pipx/venvs/openbrowse")
    monkeypatch.setattr(sys, "base_prefix", "/usr")

    method, commands = updates.detect_install_method()

    assert method == "pipx"
    assert commands is None


def test_managed_installs_are_never_mistaken_for_a_plain_venv(monkeypatch):
    """Both uv and pipx put the app in a venv, so sys.prefix != base_prefix is
    true for them too. Whichever manager owns the app has to win that race."""
    monkeypatch.setattr(updates, "checkout_root", lambda: None)
    monkeypatch.setattr(updates, "_uv_binary", lambda: "/usr/bin/uv")
    monkeypatch.setattr(updates, "_pipx_binary", lambda: "/usr/bin/pipx")
    monkeypatch.setattr(sys, "base_prefix", "/usr")

    for prefix, expected in (
        ("/home/pi/.local/share/uv/tools/openbrowse", "uv-tool"),
        ("/home/pi/.local/pipx/venvs/openbrowse", "pipx"),
        ("/home/pi/.local/share/pipx/venvs/openbrowse", "pipx"),
    ):
        monkeypatch.setattr(sys, "prefix", prefix)
        assert updates.detect_install_method()[0] == expected, prefix


def test_installer_lookup_survives_a_systemd_path(tmp_path, monkeypatch):
    """systemd hands the service a minimal PATH that usually omits ~/.local/bin,
    which is where both uv and pipx install themselves."""
    monkeypatch.setattr(updates.shutil, "which", lambda name: None)
    monkeypatch.setattr(updates.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    for name in ("uv", "pipx"):
        (tmp_path / ".local" / "bin" / name).write_text("")

    assert updates._uv_binary() == str(tmp_path / ".local" / "bin" / "uv")
    assert updates._pipx_binary() == str(tmp_path / ".local" / "bin" / "pipx")


def test_detect_pip_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "checkout_root", lambda: None)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(sys, "base_prefix", "/usr")

    method, commands = updates.detect_install_method()

    assert method == "pip"
    assert commands == [
        [sys.executable, "-m", "pip", "install", "--upgrade", "openbrowse"]
    ]


def test_detect_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "checkout_root", lambda: None)
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(updates, "_in_user_site", lambda: False)

    method, commands = updates.detect_install_method()

    assert method == "unknown"
    assert commands is None


def test_detect_ignores_openbrowse_home(tmp_path, monkeypatch):
    """OPENBROWSE_HOME moves the data directory. Keying the upgrade off it would
    run git pull and uv sync against whatever repository it happened to name."""
    stranger = tmp_path / "someone-elses-project"
    (stranger / ".git").mkdir(parents=True)
    (stranger / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr(updates, "settings", SimpleNamespace(home_dir=stranger))
    monkeypatch.setattr(updates, "checkout_root", lambda: None)
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(updates, "_in_user_site", lambda: False)

    method, commands = updates.detect_install_method()

    assert method == "unknown"
    assert commands is None


async def test_install_update_success(monkeypatch):
    monkeypatch.setattr(
        updates,
        "detect_install_method",
        lambda: ("pip", [[sys.executable, "-c", "print('upgraded')"]]),
    )

    ok, log = await updates.install_update()

    assert ok is True
    assert "upgraded" in log
    assert updates.state.installing is False
    assert updates.state.install_log == log


async def test_install_update_failure_reports_exit_code(monkeypatch):
    monkeypatch.setattr(
        updates,
        "detect_install_method",
        lambda: ("pip", [[sys.executable, "-c", "import sys; sys.exit(3)"]]),
    )

    ok, log = await updates.install_update()

    assert ok is False
    assert "exit code 3" in log
    assert updates.state.installing is False


async def test_install_update_unknown_method(monkeypatch):
    monkeypatch.setattr(updates, "detect_install_method", lambda: ("unknown", None))

    ok, log = await updates.install_update()

    assert ok is False
    assert "unknown" in log


async def test_install_update_missing_command(monkeypatch):
    monkeypatch.setattr(
        updates,
        "detect_install_method",
        lambda: ("pip", [["definitely-not-a-real-binary-xyz"]]),
    )

    ok, log = await updates.install_update()

    assert ok is False
    assert "command not found" in log


@pytest.fixture
async def dashboard(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
        api_key="secret-key",
        dashboard_user="admin",
        dashboard_password="",
        allow_insecure_no_auth=False,
    )
    monkeypatch.setattr("openbrowse.config.settings", test_settings)
    monkeypatch.setattr("openbrowse.db.models.settings", test_settings)
    monkeypatch.setattr("openbrowse.auth.settings", test_settings)
    monkeypatch.setattr("openbrowse.dashboard.routes.settings", test_settings)
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _auth() -> dict[str, str]:
    token = base64.b64encode(b"admin:secret-key").decode()
    return {"Authorization": f"Basic {token}"}


async def test_update_status_requires_auth(dashboard):
    resp = await dashboard.get("/api/update")
    assert resp.status_code == 401


async def test_update_status_reports_state(dashboard, monkeypatch):
    monkeypatch.setattr(updates.state, "latest", "9.9.9")
    monkeypatch.setattr(updates.state, "available", True)

    resp = await dashboard.get("/api/update", headers=_auth())

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["latest"] == "9.9.9"


async def test_settings_page_shows_available_update(dashboard, monkeypatch):
    monkeypatch.setattr(updates.state, "latest", "9.9.9")
    monkeypatch.setattr(updates.state, "available", True)

    resp = await dashboard.get("/settings", headers=_auth())

    assert resp.status_code == 200
    assert "v9.9.9 is available" in resp.text


async def test_install_endpoint_restarts_on_success(dashboard, monkeypatch):
    async def fake_install():
        return True, "done"

    restarts: list[bool] = []
    monkeypatch.setattr(updates, "install_update", fake_install)
    monkeypatch.setattr(
        "openbrowse.dashboard.routes._schedule_restart", lambda: restarts.append(True)
    )

    resp = await dashboard.post("/settings/update", headers=_auth())

    assert resp.status_code == 200
    assert "Restarting" in resp.text
    assert restarts == [True]


async def test_install_endpoint_redirects_on_failure(dashboard, monkeypatch):
    async def fake_install():
        return False, "broken"

    monkeypatch.setattr(updates, "install_update", fake_install)

    resp = await dashboard.post("/settings/update", headers=_auth())

    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?update_failed=1"


async def test_install_endpoint_refuses_while_sessions_run(dashboard, monkeypatch):
    monkeypatch.setattr(
        "openbrowse.dashboard.routes.pool",
        SimpleNamespace(active_count=2),
    )

    resp = await dashboard.post("/settings/update", headers=_auth())

    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?update_busy=1"
