"""Tests for the v3-compatible Sessions API."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from openbrowse.config import settings
from openbrowse.db.models import init_db
from openbrowse.main import app


@pytest.fixture(autouse=True)
async def setup(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
        api_key="",
        allow_insecure_no_auth=True,
        cloud_max_cost_factor=1.0,
    )
    monkeypatch.setattr("openbrowse.config.settings", test_settings)
    monkeypatch.setattr("openbrowse.db.models.settings", test_settings)
    monkeypatch.setattr("openbrowse.auth.settings", test_settings)
    monkeypatch.setattr("openbrowse.api.sessions.settings", test_settings)
    monkeypatch.setattr("openbrowse.profiles.storage.settings", test_settings)
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    await init_db()
    return test_settings


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_create_session_with_task(mock_submit, client):
    resp = await client.post(
        "/v3/sessions",
        json={"task": "Go to google.com", "model": "claude-sonnet-4.6"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    assert data["model"] == "claude-sonnet-4.6"
    mock_submit.assert_called_once()

    from openbrowse.db import crud

    messages, _ = await crud.list_messages(data["id"], limit=10)
    assert [m["summary"] for m in messages if m["type"] == "user_message"] == [
        "Go to google.com"
    ]


async def test_start_returns_while_pool_is_full(client, monkeypatch):
    import asyncio

    import openbrowse.agent.pool as pool_mod
    from openbrowse.agent.pool import SessionPool

    release = asyncio.Event()

    async def fake_run(session_id: str) -> None:
        await release.wait()

    monkeypatch.setattr(pool_mod, "run_agent_session", fake_run)
    busy_pool = SessionPool(max_concurrent=1)
    monkeypatch.setattr("openbrowse.api.sessions.pool", busy_pool)

    first = await client.post("/v3/sessions", json={"task": "one"})
    assert first.status_code == 200
    await asyncio.sleep(0)
    assert busy_pool.active_count == 1

    second = await asyncio.wait_for(
        client.post("/v3/sessions", json={"task": "two"}), timeout=5
    )
    assert second.status_code == 200
    assert busy_pool.queued_count == 1

    release.set()
    await asyncio.sleep(0.01)
    await busy_pool.shutdown()


async def test_create_session_without_task(client):
    resp = await client.post("/v3/sessions", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"


async def test_create_session_resolves_reasoning_default(client):
    """An omitted reasoningEffort runs at the benchmark-backed pick the dashboard
    preselects, not at whatever the provider does unprompted, so the same request
    behaves the same through either door."""
    resp = await client.post("/v3/sessions", json={"model": "claude-sonnet-5"})
    assert resp.status_code == 200
    assert resp.json()["reasoningEffort"] == "high"

    resp = await client.post("/v3/sessions", json={"model": "claude-opus-4-8"})
    assert resp.json()["reasoningEffort"] == "none"

    resp = await client.post("/v3/sessions", json={"model": "gpt-5.6-terra"})
    assert resp.json()["reasoningEffort"] == "none"

    resp = await client.post("/v3/sessions", json={"model": "gpt-5.6-luna"})
    assert resp.json()["reasoningEffort"] == "max"


async def test_create_session_without_a_model_uses_the_configured_default(client, monkeypatch):
    """DEFAULT_MODEL has to mean the same thing through the API as it does on the
    dashboard, effort included, or it is a setting that only half works."""
    from dataclasses import replace

    import openbrowse.api.sessions as sessions_mod
    from openbrowse.config import settings as real_settings

    resp = await client.post("/v3/sessions", json={})
    assert resp.status_code == 200
    assert resp.json()["model"] == "gpt-5.6-terra"
    assert resp.json()["reasoningEffort"] == "none"

    monkeypatch.setattr(
        sessions_mod, "settings", replace(real_settings, default_model="claude-sonnet-5")
    )
    resp = await client.post("/v3/sessions", json={})
    assert resp.json()["model"] == "claude-sonnet-5"
    assert resp.json()["reasoningEffort"] == "high"


async def test_create_session_accepts_either_version_punctuation(client):
    resp = await client.post("/v3/sessions", json={"model": "gpt-5-6-terra"})
    assert resp.status_code == 200
    assert resp.json()["reasoningEffort"] == "none"

    resp = await client.post(
        "/v3/sessions",
        json={"model": "gpt-5-6-terra", "reasoningEffort": "max"},
    )
    assert resp.status_code == 200
    assert resp.json()["reasoningEffort"] == "max"

    resp = await client.post("/v3/sessions", json={"model": "claude-sonnet-4.6"})
    assert resp.status_code == 200
    assert resp.json()["reasoningEffort"] == "none"


async def test_create_session_accepts_reasoning_effort(client):
    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-sonnet-5", "reasoningEffort": "xhigh"},
    )
    assert resp.status_code == 200
    assert resp.json()["reasoningEffort"] == "xhigh"

    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-sonnet-5", "reasoningEffort": "none"},
    )
    assert resp.status_code == 200
    assert resp.json()["reasoningEffort"] == "none"

    resp = await client.post(
        "/v3/sessions",
        json={"model": "gpt-5.6-terra", "reasoningEffort": "max"},
    )
    assert resp.status_code == 200
    assert resp.json()["reasoningEffort"] == "max"


async def test_create_session_rejects_legacy_effort_fields(client):
    for legacy in ("thinkingEffort", "modelThinkingEffort"):
        resp = await client.post(
            "/v3/sessions",
            json={"model": "claude-sonnet-5", legacy: "high"},
        )
        assert resp.status_code == 422
        assert "reasoningEffort" in resp.text


async def test_create_session_maps_thinking_level(client):
    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-sonnet-5", "thinkingLevel": "disabled"},
    )
    assert resp.status_code == 200
    assert resp.json()["reasoningEffort"] == "none"

    for level in ("low", "medium", "high"):
        resp = await client.post(
            "/v3/sessions",
            json={"model": "claude-sonnet-5", "thinkingLevel": level},
        )
        assert resp.status_code == 200
        assert resp.json()["reasoningEffort"] == level


async def test_create_session_thinking_level_disabled_rejected_on_fable(client):
    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-fable-5", "thinkingLevel": "disabled"},
    )
    assert resp.status_code == 422
    assert "cannot be disabled" in resp.json()["detail"]


async def test_create_session_rejects_unmapped_thinking_level(client):
    for bad in ("xhigh", "banana", None):
        resp = await client.post(
            "/v3/sessions",
            json={"model": "claude-sonnet-5", "thinkingLevel": bad},
        )
        assert resp.status_code == 422
        assert "reasoningEffort" in resp.text


async def test_create_session_rejects_both_level_and_effort(client):
    resp = await client.post(
        "/v3/sessions",
        json={
            "model": "claude-sonnet-5",
            "thinkingLevel": "high",
            "reasoningEffort": "high",
        },
    )
    assert resp.status_code == 422
    assert "only one" in resp.text


async def test_create_session_rejects_invalid_effort_per_model(client):
    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-fable-5", "reasoningEffort": "none"},
    )
    assert resp.status_code == 422
    assert "Valid values" in resp.json()["detail"]

    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-sonnet-4-6", "reasoningEffort": "max"},
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/v3/sessions",
        json={"model": "claude-sonnet-4-6", "reasoningEffort": "xhigh"},
    )
    assert resp.status_code == 422


async def test_create_session_rejects_removed_aliases(client):
    for alias in ("bu", "bu-latest", "bu-ultra", "bu-mini", "bu-max"):
        resp = await client.post("/v3/sessions", json={"model": alias})
        assert resp.status_code == 422, alias


async def test_list_sessions(client):
    await client.post("/v3/sessions", json={})
    await client.post("/v3/sessions", json={})
    resp = await client.get("/v3/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2


async def test_get_session(client):
    create_resp = await client.post("/v3/sessions", json={})
    sid = create_resp.json()["id"]
    resp = await client.get(f"/v3/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == sid
    assert resp.json()["failureKind"] is None
    assert resp.json()["failureStatusCode"] is None


async def test_get_nonexistent_session(client):
    resp = await client.get("/v3/sessions/nonexistent")
    assert resp.status_code == 404


async def test_session_response_surfaces_failure_fields(client):
    from openbrowse.db import crud

    create_resp = await client.post("/v3/sessions", json={})
    sid = create_resp.json()["id"]
    await crud.update_session(
        sid,
        status="error",
        failure_kind="provider_rate_limit",
        failure_status_code=429,
    )

    resp = await client.get(f"/v3/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["failureKind"] == "provider_rate_limit"
    assert resp.json()["failureStatusCode"] == 429


async def test_list_messages_empty(client):
    create_resp = await client.post("/v3/sessions", json={})
    sid = create_resp.json()["id"]
    resp = await client.get(f"/v3/sessions/{sid}/messages")
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


async def test_incoming_budget_scaled_to_local_cost(client, setup, monkeypatch):
    monkeypatch.setattr(
        "openbrowse.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.5)
    )
    resp = await client.post("/v3/sessions", json={"maxCostUsd": 6})
    assert resp.status_code == 200
    assert resp.json()["maxCostUsd"] == "3.0"


async def test_budget_untouched_at_default_factor(client):
    resp = await client.post("/v3/sessions", json={"maxCostUsd": 6})
    assert resp.status_code == 200
    assert resp.json()["maxCostUsd"] == "6.0"


async def test_absent_budget_stays_absent(client, setup, monkeypatch):
    monkeypatch.setattr(
        "openbrowse.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.5)
    )
    resp = await client.post("/v3/sessions", json={})
    assert resp.status_code == 200
    assert resp.json()["maxCostUsd"] is None


async def test_scaled_budget_rounds_to_cents(client, setup, monkeypatch):
    monkeypatch.setattr(
        "openbrowse.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.6)
    )
    resp = await client.post("/v3/sessions", json={"maxCostUsd": 6})
    assert resp.status_code == 200
    assert resp.json()["maxCostUsd"] == "3.6"


async def test_tiny_scaled_budget_keeps_a_cent_floor(client, setup, monkeypatch):
    monkeypatch.setattr(
        "openbrowse.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.5)
    )
    resp = await client.post("/v3/sessions", json={"maxCostUsd": 0.01})
    assert resp.status_code == 200
    assert resp.json()["maxCostUsd"] == "0.01"


async def test_rejects_budget_that_would_disable_the_cap(client):
    for bad in (0, -1):
        resp = await client.post("/v3/sessions", json={"maxCostUsd": bad})
        assert resp.status_code == 422, bad


async def test_rejects_non_finite_budget(client):
    for raw in ("Infinity", "-Infinity", "NaN"):
        resp = await client.post(
            "/v3/sessions",
            content=f'{{"maxCostUsd": {raw}}}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, raw


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_rerun_keeps_settings_the_caller_omitted(
    mock_submit, client, setup, monkeypatch
):
    from openbrowse.db import crud

    monkeypatch.setattr(
        "openbrowse.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.5)
    )
    created = await client.post(
        "/v3/sessions",
        json={
            "model": "claude-opus-4-8",
            "maxCostUsd": 6,
            "keepAlive": True,
            "outputSchema": {"type": "object"},
            "systemPromptExtension": "be brief",
        },
    )
    assert created.status_code == 200
    sid = created.json()["id"]
    assert created.json()["maxCostUsd"] == "3.0"

    again = await client.post("/v3/sessions", json={"sessionId": sid, "task": "next"})
    assert again.status_code == 200
    data = again.json()
    assert data["maxCostUsd"] == "3.0"
    assert data["model"] == "claude-opus-4-8"
    assert data["outputSchema"] == {"type": "object"}
    stored = await crud.get_session(sid)
    assert stored["keep_alive"] == 1
    assert stored["system_prompt_extension"] == "be brief"


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_rerun_scales_a_budget_the_caller_resends(
    mock_submit, client, setup, monkeypatch
):
    monkeypatch.setattr(
        "openbrowse.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.5)
    )
    sid = (await client.post("/v3/sessions", json={"maxCostUsd": 6})).json()["id"]
    again = await client.post(
        "/v3/sessions", json={"sessionId": sid, "task": "next", "maxCostUsd": 4}
    )
    assert again.status_code == 200
    assert again.json()["maxCostUsd"] == "2.0"


@pytest.fixture(autouse=True)
def _clear_live_sessions():
    from openbrowse.agent import live

    live._live.clear()
    yield
    live._live.clear()


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_followup_continues_a_parked_session(mock_submit, client):
    from types import SimpleNamespace

    from openbrowse.agent import live
    from openbrowse.db import crud

    session = await crud.create_session(task="first task", keep_alive=True)
    await crud.update_session(session["id"], status="idle")
    entry = live.register(session["id"], SimpleNamespace())
    live.park(entry)

    resp = await client.post(
        "/v3/sessions", json={"sessionId": session["id"], "task": "Is he really PM?"}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    assert entry.inbox.get_nowait() == "Is he really PM?"
    mock_submit.assert_not_called()
    messages, _ = await crud.list_messages(session["id"], limit=10)
    assert [m["summary"] for m in messages if m["type"] == "user_message"] == [
        "Is he really PM?"
    ]


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_followup_changing_the_model_starts_over(mock_submit, client):
    from types import SimpleNamespace

    from openbrowse.agent import live
    from openbrowse.db import crud

    session = await crud.create_session(
        task="first task", model="claude-sonnet-5", keep_alive=True
    )
    await crud.update_session(session["id"], status="idle")
    entry = live.register(session["id"], SimpleNamespace())
    live.park(entry)

    async def worker():
        await entry.release.wait()
        live.unregister(entry)

    released = asyncio.create_task(worker())

    resp = await client.post(
        "/v3/sessions",
        json={
            "sessionId": session["id"],
            "task": "Is he really PM?",
            "model": "claude-opus-4.8",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "created"
    await released
    assert entry.release.is_set()
    assert entry.inbox.empty()
    mock_submit.assert_called_once()


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_followup_keeps_the_agent_when_the_model_is_only_spelled_differently(
    mock_submit, client
):
    from types import SimpleNamespace

    from openbrowse.agent import live
    from openbrowse.db import crud

    session = await crud.create_session(
        task="first task", model="claude-sonnet-4-6", keep_alive=True
    )
    await crud.update_session(session["id"], status="idle")
    entry = live.register(session["id"], SimpleNamespace())
    live.park(entry)

    resp = await client.post(
        "/v3/sessions",
        json={
            "sessionId": session["id"],
            "task": "Is he really PM?",
            "model": "claude-sonnet-4.6",
        },
    )

    assert resp.status_code == 200
    assert entry.inbox.get_nowait() == "Is he really PM?"
    assert not entry.release.is_set()
    mock_submit.assert_not_called()


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_followup_accepted_after_a_keepalive_session_was_released(
    mock_submit, client
):
    from openbrowse.db import crud

    session = await crud.create_session(task="first task", keep_alive=True)
    await crud.update_session(session["id"], status="stopped")

    resp = await client.post(
        "/v3/sessions", json={"sessionId": session["id"], "task": "Is he really PM?"}
    )

    assert resp.status_code == 200
    mock_submit.assert_called_once()


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_followup_rejected_on_a_plain_stopped_session(mock_submit, client):
    from openbrowse.db import crud

    session = await crud.create_session(task="first task", keep_alive=False)
    await crud.update_session(session["id"], status="stopped")

    resp = await client.post(
        "/v3/sessions", json={"sessionId": session["id"], "task": "again please"}
    )

    assert resp.status_code == 422
    mock_submit.assert_not_called()


@patch("openbrowse.api.sessions.pool.cancel", new_callable=AsyncMock)
async def test_stop_task_strategy_cancels_the_run_and_leaves_the_session_usable(
    mock_cancel, client
):
    """Stopping the task ends the run and the browser with it, as it always has,
    and leaves the session addressable so a later call can give it new work."""
    from openbrowse.db import crud

    session = await crud.create_session(task="first task", keep_alive=True)
    await crud.update_session(session["id"], status="running")

    resp = await client.post(f"/v3/sessions/{session['id']}/stop", json={"strategy": "task"})

    assert resp.status_code == 200
    mock_cancel.assert_called_once()
    assert (await crud.get_session(session["id"]))["status"] == "idle"


@patch("openbrowse.api.sessions.pool.cancel", new_callable=AsyncMock)
async def test_stop_session_strategy_releases_the_browser(mock_cancel, client):

    from openbrowse.db import crud

    session = await crud.create_session(task="first task", keep_alive=True)
    await crud.update_session(session["id"], status="idle")

    resp = await client.post(f"/v3/sessions/{session['id']}/stop", json={"strategy": "session"})

    assert resp.status_code == 200
    mock_cancel.assert_called_once()
    assert (await crud.get_session(session["id"]))["status"] == "stopped"


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_a_follow_up_tops_the_budget_up_by_the_session_allowance(
    mock_submit, client, setup, monkeypatch
):
    """maxCostUsd bounds the session, not the turn, so a conversation would
    strangle itself if every follow-up drew from the same fixed pot."""
    from openbrowse.db import crud

    monkeypatch.setattr(
        "openbrowse.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.5)
    )
    sid = (await client.post("/v3/sessions", json={"maxCostUsd": 6})).json()["id"]
    await crud.update_session(sid, total_cost_usd=2.40)

    again = await client.post("/v3/sessions", json={"sessionId": sid, "task": "next"})

    assert again.status_code == 200
    assert again.json()["maxCostUsd"] == "5.4"


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_a_named_budget_on_a_follow_up_is_an_absolute_ceiling(
    mock_submit, client, setup, monkeypatch
):
    from openbrowse.db import crud

    monkeypatch.setattr(
        "openbrowse.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.5)
    )
    sid = (await client.post("/v3/sessions", json={"maxCostUsd": 6})).json()["id"]
    await crud.update_session(sid, total_cost_usd=2.40)

    again = await client.post(
        "/v3/sessions", json={"sessionId": sid, "task": "next", "maxCostUsd": 4}
    )

    assert again.json()["maxCostUsd"] == "2.0"


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_a_named_budget_does_not_become_the_session_allowance(
    mock_submit, client, setup, monkeypatch
):
    """A one-off ceiling for a single dispatch must not resize every later top-up."""
    from openbrowse.db import crud

    monkeypatch.setattr(
        "openbrowse.api.sessions.settings", replace(setup, cloud_max_cost_factor=0.5)
    )
    sid = (await client.post("/v3/sessions", json={"maxCostUsd": 6})).json()["id"]
    await client.post(
        "/v3/sessions", json={"sessionId": sid, "task": "next", "maxCostUsd": 4}
    )
    await crud.update_session(sid, status="idle", total_cost_usd=1.0)

    third = await client.post("/v3/sessions", json={"sessionId": sid, "task": "third"})

    assert third.json()["maxCostUsd"] == "4.0"


@patch("openbrowse.api.sessions.pool.submit_nowait")
async def test_a_session_created_without_a_budget_stays_unbudgeted(
    mock_submit, client
):
    from openbrowse.db import crud

    sid = (await client.post("/v3/sessions", json={})).json()["id"]
    await crud.update_session(sid, total_cost_usd=1.75)

    again = await client.post("/v3/sessions", json={"sessionId": sid, "task": "next"})

    assert again.json()["maxCostUsd"] is None
