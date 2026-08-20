"""Tests for the first-run /setup screen."""

from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db.models import init_db
from app.main import app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
        api_key="",
        dashboard_password="",
        allow_insecure_no_auth=False,
    )
    for target in (
        "app.config.settings",
        "app.db.models.settings",
        "app.auth.settings",
        "app.dashboard.setup_routes.settings",
    ):
        monkeypatch.setattr(target, test_settings)
    monkeypatch.setattr(
        "app.dashboard.setup_routes._env_path", tmp_path / ".env"
    )
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, tmp_path


async def test_setup_form_served_when_unconfigured(client):
    c, _ = client
    resp = await c.get("/setup")
    assert resp.status_code == 200
    assert "Welcome to OpenBrowse" in resp.text
    assert 'name="api_key"' in resp.text


async def test_setup_writes_env_and_shows_key_once(client):
    c, tmp_path = client
    resp = await c.post(
        "/setup",
        data={
            "api_key": "generated-abc123",
            "anthropic_api_key": "sk-ant-test",
            "openai_api_key": "",
            "dashboard_password": "hunter2",
        },
    )
    assert resp.status_code == 200
    assert "generated-abc123" in resp.text
    env = (tmp_path / ".env").read_text()
    assert "API_KEY=generated-abc123" in env
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env
    assert "DASHBOARD_PASSWORD=hunter2" in env
    assert "OPENAI_API_KEY" not in env


async def test_setup_refuses_to_clobber_existing_env(client):
    c, tmp_path = client
    (tmp_path / ".env").write_text("API_KEY=already-here\n")
    resp = await c.post("/setup", data={"api_key": "new-key"})
    assert resp.status_code == 409
    assert (tmp_path / ".env").read_text() == "API_KEY=already-here\n"


async def test_setup_hidden_once_configured(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
        api_key="configured",
    )
    for target in (
        "app.config.settings",
        "app.db.models.settings",
        "app.auth.settings",
        "app.dashboard.setup_routes.settings",
    ):
        monkeypatch.setattr(target, test_settings)
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/setup", follow_redirects=False)
        assert resp.status_code == 303
        resp = await c.post("/setup", data={"api_key": "x"}, follow_redirects=False)
        assert resp.status_code == 303


async def test_codeview_shell_served_without_auth(client):
    c, _ = client
    resp = await c.get("/codeview")
    assert resp.status_code == 200
    assert "__setCode" in resp.text


def _fixed_info(**over):
    from dataclasses import replace as _replace

    from app.hostinfo import HostInfo

    base = HostInfo(
        cores=4, mem_total_kb=16 * 1024 * 1024, mem_available_kb=13 * 1024 * 1024,
        load1_per_core=0.1, psi_available=False, is_raspberry_pi=True,
        systemd=True, cgroup_memory=True, root_on_sd=True,
        resource_limits_set=False,
    )
    return _replace(base, **over)


async def test_setup_capacity_section_bounded_by_hardware(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr("app.hostinfo.probe", lambda: _fixed_info())
    resp = await c.get("/setup")
    assert resp.status_code == 200
    assert 'max="4"' in resp.text
    assert "4 cores" in resp.text
    assert 'name="share"' in resp.text


async def test_setup_clamps_oversized_concurrency(client, monkeypatch):
    c, tmp_path = client
    monkeypatch.setattr("app.hostinfo.probe", lambda: _fixed_info())
    resp = await c.post(
        "/setup",
        data={"api_key": "k1", "max_concurrent_sessions": "99", "share": "all"},
    )
    assert resp.status_code == 200
    assert "MAX_CONCURRENT_SESSIONS=4" in (tmp_path / ".env").read_text()
    assert "host_tune.sh --share all" in resp.text
    assert "Restart now" in resp.text


async def test_setup_falls_back_to_plain_input_without_probe(client, monkeypatch):
    from app.hostinfo import HostInfo

    c, _ = client
    empty = HostInfo(
        cores=0, mem_total_kb=0, mem_available_kb=0, load1_per_core=0.0,
        psi_available=False, is_raspberry_pi=False, systemd=False,
        cgroup_memory=False, root_on_sd=False, resource_limits_set=False,
    )
    monkeypatch.setattr("app.hostinfo.probe", lambda: empty)
    resp = await c.get("/setup")
    assert resp.status_code == 200
    assert 'name="max_concurrent_sessions" value="1"' in resp.text.replace("\n", " ")
    assert 'type="range"' not in resp.text


async def test_setup_restart_requires_saved_env(client, monkeypatch):
    c, tmp_path = client
    resp = await c.post("/setup/restart")
    assert resp.status_code == 400

    (tmp_path / ".env").write_text("API_KEY=k\n")
    called = []
    monkeypatch.setattr(
        "app.dashboard.setup_routes.schedule_restart", lambda: called.append(1)
    )
    resp = await c.post("/setup/restart")
    assert resp.status_code == 200
    assert called == [1]


async def test_setup_light_browser_preselected_on_constrained_hardware(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr("app.hostinfo.probe", lambda: _fixed_info())
    resp = await c.get("/setup")
    assert resp.status_code == 200
    assert 'name="chrome_light_flags" value="1" checked' in resp.text
    assert "recommended for this machine" in resp.text


async def test_setup_light_browser_unticked_on_big_hardware(client, monkeypatch):
    c, _ = client
    big = _fixed_info(
        is_raspberry_pi=False, cores=16,
        mem_total_kb=64 * 1024 * 1024, mem_available_kb=48 * 1024 * 1024,
    )
    monkeypatch.setattr("app.hostinfo.probe", lambda: big)
    resp = await c.get("/setup")
    assert resp.status_code == 200
    assert 'name="chrome_light_flags"' in resp.text
    assert 'name="chrome_light_flags" value="1" checked' not in resp.text


async def test_setup_save_honours_the_light_browser_choice(client, monkeypatch):
    c, tmp_path = client
    monkeypatch.setattr("app.hostinfo.probe", lambda: _fixed_info())
    resp = await c.post(
        "/setup", data={"api_key": "k1", "chrome_light_flags": "1"}
    )
    assert resp.status_code == 200
    assert "CHROME_LIGHT_FLAGS=1" in (tmp_path / ".env").read_text()


async def test_setup_save_omits_the_light_browser_flag_when_opted_out(client, monkeypatch):
    c, tmp_path = client
    monkeypatch.setattr("app.hostinfo.probe", lambda: _fixed_info())
    resp = await c.post("/setup", data={"api_key": "k1"})
    assert resp.status_code == 200
    assert "CHROME_LIGHT_FLAGS" not in (tmp_path / ".env").read_text()
