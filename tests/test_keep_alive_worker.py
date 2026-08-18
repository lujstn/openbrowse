"""Tests for the keep-alive worker: one agent, one browser, many turns."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.agent import live
from app.agent import runner as runner_mod
from app.config import settings
from app.db import crud
from app.db.models import init_db


class _FakeHistory:
    def __init__(self, steps: list, answer: str, errors: list) -> None:
        self.history = steps
        self._answer = answer
        self._errors = errors

    def final_result(self) -> str:
        return self._answer

    def is_done(self) -> bool:
        return True

    def is_successful(self) -> bool:
        return True

    def errors(self) -> list:
        return self._errors


class _FakeAgent:
    """Stands in for browser-use's Agent: remembers every task it was given."""

    instances: list["_FakeAgent"] = []
    captcha_sink: list | None = None

    def __init__(self, **kwargs) -> None:
        self.task = kwargs["task"]
        self.tasks = [kwargs["task"]]
        self.file_system = None
        self.token_cost_service = SimpleNamespace(usage_history=[])
        self.history = SimpleNamespace(history=[])
        self.state = SimpleNamespace(stopped=False)
        self.errors: list = []
        self.runs = 0
        self.step_caps: list[int] = []
        self.sink_at_start: list[list] = []
        _FakeAgent.instances.append(self)

    def add_new_task(self, text: str) -> None:
        self.task = text
        self.tasks.append(text)

    def stop(self) -> None:
        return None

    async def run(self, max_steps=500, on_step_start=None, on_step_end=None):
        self.runs += 1
        self.step_caps.append(max_steps)
        sink = _FakeAgent.captcha_sink
        if sink is not None:
            self.sink_at_start.append(list(sink))
            if self.runs == 1:
                sink.append(0.01)
        self.history.history.append(
            SimpleNamespace(result=[SimpleNamespace(judgement=None)])
        )
        self.errors.append("boom" if self.runs == 1 else None)
        return _FakeHistory(self.history.history, f"answer {self.runs}", list(self.errors))


class _FakeBrowserSession:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.browser_profile = SimpleNamespace(keep_alive=False)
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
async def worker_env(tmp_path, monkeypatch):
    test_settings = replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        profiles_dir=tmp_path / "data" / "profiles",
    )
    monkeypatch.setattr("app.config.settings", test_settings)
    monkeypatch.setattr("app.db.models.settings", test_settings)
    monkeypatch.setattr(runner_mod, "settings", test_settings)
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    await init_db()

    _FakeAgent.instances.clear()
    _FakeAgent.captcha_sink = None
    slot = SimpleNamespace(display_num=10, novnc_port=6080, vnc_port=5910, cdp_port=9222)

    async def _allocate():
        return slot

    async def _noop(*args, **kwargs):
        return None

    async def _launch(_slot):
        return "ws://cdp"

    monkeypatch.setattr(
        runner_mod,
        "display_manager",
        SimpleNamespace(allocate=_allocate, release=_noop),
    )
    monkeypatch.setattr(runner_mod, "wait_for_novnc", _noop)
    monkeypatch.setattr(runner_mod, "launch_chrome", _launch)
    monkeypatch.setattr(runner_mod, "stop_chrome", _noop)
    monkeypatch.setattr(runner_mod, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(runner_mod, "Agent", _FakeAgent)
    monkeypatch.setattr(runner_mod, "_install_lean_state", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "_north_star_preflight", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "TabManager", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(
        runner_mod,
        "Tools",
        lambda *a, **k: SimpleNamespace(
            registry=SimpleNamespace(registry=SimpleNamespace(actions={}))
        ),
    )
    monkeypatch.setattr(runner_mod, "CodeStreamObserver", lambda *a, **k: SimpleNamespace())

    async def _bridge(*args, **kwargs):
        return None

    monkeypatch.setattr(runner_mod, "install_captcha_bridge", _bridge)
    for name in (
        "register_fetch_tool",
        "register_code_tools",
        "register_clipboard_tools",
        "register_tab_tools",
        "register_output_store_tools",
        "register_completeness_gate",
        "register_output_guard_overrides",
        "register_search_page_flow",
    ):
        monkeypatch.setattr(runner_mod, name, lambda *a, **k: None)

    def _capture_captcha_sink(tools, cost_sink=None, progress=None):
        _FakeAgent.captcha_sink = cost_sink

    monkeypatch.setattr(runner_mod, "register_captcha_tools", _capture_captcha_sink)
    monkeypatch.setattr(
        runner_mod.system_metrics, "mark_baseline", lambda: ("ok", {"load1": 0, "cores": 4})
    )
    monkeypatch.setattr(
        runner_mod, "_build_llm", lambda model, effort: ("anthropic", "claude-sonnet-5", SimpleNamespace())
    )
    yield test_settings
    live._live.clear()


async def _wait_for(predicate, timeout: float = 3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def _summaries(session_id: str) -> list[str]:
    messages, _ = await crud.list_messages(session_id, limit=200)
    return [m["summary"] for m in messages]


async def _types(session_id: str) -> list[str]:
    messages, _ = await crud.list_messages(session_id, limit=200)
    return [m["type"] for m in messages]


async def test_follow_up_continues_the_same_agent_and_browser():
    session = await crud.create_session(task="Summarise the news", keep_alive=True)
    sid = session["id"]
    worker = asyncio.create_task(runner_mod.run_agent_session(sid))

    assert await _wait_for(lambda: live.is_parked(sid))
    first = await crud.get_session(sid)
    assert first["status"] == "idle"
    assert first["output"] == "answer 1"
    assert (await _types(sid)).count("planning") == 1

    assert live.deliver(sid, "Is he really PM?") == live.DELIVERED
    assert await _wait_for(lambda: len(_FakeAgent.instances[0].tasks) > 1)
    assert await _wait_for(lambda: live.is_parked(sid))

    # one agent, one browser, both turns
    assert len(_FakeAgent.instances) == 1
    agent = _FakeAgent.instances[0]
    assert agent.runs == 2
    assert "Is he really PM?" in agent.tasks[1]
    assert "browser is exactly as you left it" in agent.tasks[1]
    # each turn gets its own step allowance on top of what the session has spent
    assert agent.step_caps == [runner_mod._TURN_STEP_BUDGET, runner_mod._TURN_STEP_BUDGET + 1]
    assert (await _types(sid)).count("planning") == 1

    second = await crud.get_session(sid)
    assert second["status"] == "idle"
    assert second["output"] == "answer 2"
    assert second["title"] == "Summarise the news"

    await live.request_release(sid, "Stopped by test")
    await asyncio.wait_for(worker, timeout=3)
    assert (await crud.get_session(sid))["status"] == "stopped"
    assert "Stopped by test" in await _summaries(sid)


async def test_a_turn_is_not_blamed_for_the_previous_turns_errors():
    session = await crud.create_session(task="Summarise the news", keep_alive=True)
    sid = session["id"]
    worker = asyncio.create_task(runner_mod.run_agent_session(sid))

    assert await _wait_for(lambda: live.is_parked(sid))
    assert any("recovered from 1 transient error" in s for s in await _summaries(sid))

    live.deliver(sid, "and the second one?")
    assert await _wait_for(lambda: _FakeAgent.instances[0].runs == 2)
    assert await _wait_for(lambda: live.is_parked(sid))

    completions = [
        m["summary"]
        for m in (await crud.list_messages(sid, limit=200))[0]
        if m["type"] == "completion"
    ]
    assert completions[0] == "Task completed successfully (recovered from 1 transient error)"
    assert completions[1] == "Task completed successfully"

    await live.request_release(sid, "done")
    await asyncio.wait_for(worker, timeout=3)


async def test_a_plain_session_still_ends_after_one_task():
    session = await crud.create_session(task="Summarise the news", keep_alive=False)
    sid = session["id"]

    await asyncio.wait_for(runner_mod.run_agent_session(sid), timeout=3)

    final = await crud.get_session(sid)
    assert final["status"] == "stopped"
    assert final["output"] == "answer 1"
    assert live.is_live(sid) is False


async def test_stopping_mid_task_does_not_park_the_browser():
    session = await crud.create_session(task="Summarise the news", keep_alive=True)
    sid = session["id"]
    worker = asyncio.create_task(runner_mod.run_agent_session(sid))

    assert await _wait_for(lambda: live.is_live(sid))
    await live.request_release(sid, "Stopped from the dashboard", wait=False)
    await asyncio.wait_for(worker, timeout=3)

    assert (await crud.get_session(sid))["status"] == "stopped"
    assert live.is_live(sid) is False



async def test_wait_for_followup_returns_the_message():
    entry = live.register("s1", SimpleNamespace())
    entry.inbox.put_nowait("next thing")

    assert await runner_mod._wait_for_followup(entry) == "next thing"


async def test_wait_for_followup_gives_up_when_released():
    entry = live.register("s1", SimpleNamespace())
    entry.release.set()

    assert await runner_mod._wait_for_followup(entry) is None


async def test_wait_for_followup_keeps_a_message_that_raced_the_release():
    entry = live.register("s1", SimpleNamespace())
    entry.inbox.put_nowait("next thing")
    entry.release.set()

    assert await runner_mod._wait_for_followup(entry) is None
    assert entry.inbox.get_nowait() == "next thing"


async def test_prepare_task_frames_a_follow_up_for_the_agent():
    session = await crud.create_session(task="t", keep_alive=True)
    clipboard: dict = {}
    review_state = {"round": 2, "snapshot": "old"}

    prompt = await runner_mod._prepare_task(
        session_id=session["id"],
        task="Is he really PM?",
        url_text="Is he really PM?",
        clipboard=clipboard,
        review_state=review_state,
        preflight=None,
        output_schema=None,
        output_model=None,
        preamble=runner_mod._CONTINUATION_PREFIX,
    )

    assert prompt.startswith(runner_mod._CONTINUATION_PREFIX)
    assert "Is he really PM?" in prompt
    assert prompt.rstrip().endswith("GOAL: Is he really PM?")
    assert clipboard["northStar"] == "Is he really PM?"
    assert review_state == {"round": 0, "snapshot": None}


async def test_a_turn_that_never_finished_is_reported_as_a_failure():
    """The completion line has to name the real ending, and an unfinished run must
    not have its last action mistaken for the answer."""
    session = await crud.create_session(task="Summarise the news", keep_alive=False)
    agent = _FakeAgent(task="Summarise the news")
    unfinished = _FakeHistory([SimpleNamespace(result=[])], "a page I opened", [])
    unfinished.is_done = lambda: False
    unfinished.is_successful = lambda: None

    await runner_mod._finalise_task(
        session_id=session["id"],
        agent=agent,
        history=unfinished,
        llm=SimpleNamespace(),
        store=None,
        output_model=None,
        output_schema=None,
        clipboard={},
        capsolver_costs=[],
        captcha_spent_before=0.0,
        steps_before=0,
        final_status="stopped",
    )

    stored = await crud.get_session(session["id"])
    assert stored["is_task_successful"] == 0
    assert stored["output"] == ""
    completions = [
        m["summary"]
        for m in (await crud.list_messages(session["id"], limit=50))[0]
        if m["type"] == "completion"
    ]
    assert completions == ["Task failed: ran out of steps before the goal was reached"]


async def test_a_stopped_turn_says_it_was_stopped():
    session = await crud.create_session(task="Summarise the news", keep_alive=False)
    agent = _FakeAgent(task="Summarise the news")
    agent.state.stopped = True
    unfinished = _FakeHistory([SimpleNamespace(result=[])], "", [])
    unfinished.is_done = lambda: False
    unfinished.is_successful = lambda: None

    await runner_mod._finalise_task(
        session_id=session["id"],
        agent=agent,
        history=unfinished,
        llm=SimpleNamespace(),
        store=None,
        output_model=None,
        output_schema=None,
        clipboard={},
        capsolver_costs=[],
        captcha_spent_before=0.0,
        steps_before=0,
        final_status="stopped",
    )

    completions = [
        m["summary"]
        for m in (await crud.list_messages(session["id"], limit=50))[0]
        if m["type"] == "completion"
    ]
    assert completions == ["Task failed: stopped before the goal was reached"]


async def test_captcha_spend_carries_over_but_each_turn_gets_its_own_allowance():
    """The solver stops spending once its own sink passes the cap, so a turn must
    start with an empty sink while the session's bill keeps every earlier solve."""
    session = await crud.create_session(task="Summarise the news", keep_alive=True)
    sid = session["id"]
    worker = asyncio.create_task(runner_mod.run_agent_session(sid))

    assert await _wait_for(lambda: live.is_parked(sid))
    assert (await crud.get_session(sid))["capsolver_cost_usd"] == 0.01

    live.deliver(sid, "and the second one?")
    assert await _wait_for(lambda: _FakeAgent.instances[0].runs == 2)
    assert await _wait_for(lambda: live.is_parked(sid))

    agent = _FakeAgent.instances[0]
    assert agent.sink_at_start == [[], []]
    assert (await crud.get_session(sid))["capsolver_cost_usd"] == 0.01

    await live.request_release(sid, "done")
    await asyncio.wait_for(worker, timeout=3)
