"""A tab that stops answering CDP must not wedge a session on step one.

browser-use's navigate treats a timed-out DOM capture as an empty page and
re-navigates the same, still-loading tab; once the tab answers nothing at all,
every later state capture times out as well and the agent never takes a step.
The guard probes the tab directly, never navigates the same tab twice, and
carries on in a fresh tab when the first one is dead."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from browser_use import ActionResult, Tools
from browser_use.browser.events import NavigateToUrlEvent
from browser_use.tools.views import NavigateAction

import openbrowse.agent.tools as tools_mod
from openbrowse.agent.tools import register_navigation_guard


class _Event:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def __await__(self):
        yield from asyncio.sleep(0).__await__()
        return self

    async def event_result(self, raise_if_any: bool = False, raise_if_none: bool = False):
        if self._error and raise_if_any:
            raise self._error
        return None


class _Session:
    """``urls`` is what the browser process reports as each tab's committed URL;
    it answers even when the tab itself does not."""

    def __init__(
        self,
        focus: str = "tab-1",
        nav_error: Exception | None = None,
        urls: dict[str, str] | None = None,
    ) -> None:
        self.agent_focus_target_id = focus
        self.dispatched: list = []
        self._nav_error = nav_error
        self.urls = urls if urls is not None else {"tab-1": "chrome://newtab/"}
        self.event_bus = SimpleNamespace(dispatch=self._dispatch)

        async def get_target_info(params):
            return {"targetInfo": {"url": self.urls.get(params["targetId"], "")}}

        self._cdp_client_root = SimpleNamespace(
            send=SimpleNamespace(Target=SimpleNamespace(getTargetInfo=get_target_info))
        )

    def _dispatch(self, event):
        self.dispatched.append(event)
        return _Event(self._nav_error)


def _same_tab_navigations(session: _Session) -> int:
    return sum(
        1 for e in session.dispatched if isinstance(e, NavigateToUrlEvent) and not e.new_tab
    )


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(tools_mod, "_NAV_PROBE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(tools_mod, "_NAV_READY_DEADLINE_S", 1.2)
    monkeypatch.setattr(tools_mod, "_NAV_UNCOMMITTED_DEAD_S", 0.1)
    monkeypatch.setattr(tools_mod, "_NAV_COMMITTED_DEAD_S", 0.6)
    monkeypatch.setattr(tools_mod, "_NAV_BLANK_AFTER_S", 0.1)
    monkeypatch.setattr(tools_mod, "_NAV_POLL_S", 0.01)


def _script_tabs(monkeypatch, scripts: dict[str, list]) -> dict:
    """Each tab replays its script of probe answers: a (readyState, text length)
    pair, or None for no answer; the last entry repeats."""
    calls: dict[str, int] = {}

    async def fake_eval(browser_session, target_id, expression):
        seq = scripts[target_id]
        i = calls.get(target_id, 0)
        calls[target_id] = i + 1
        answer = seq[min(i, len(seq) - 1)]
        if answer is None:
            await asyncio.sleep(10)
        return json.dumps(list(answer))

    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_eval)
    return calls


def _recovery_spies(monkeypatch, fresh: str | None = "tab-2") -> dict:
    seen: dict = {"spawned": [], "focused": [], "closed": []}

    async def fake_spawn(browser_session, url):
        seen["spawned"].append(url)
        return fresh

    async def fake_focus(browser_session, target_id):
        seen["focused"].append(target_id)
        browser_session.agent_focus_target_id = target_id

    async def fake_close(browser_session, target_id):
        seen["closed"].append(target_id)

    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    monkeypatch.setattr(tools_mod, "_focus_target", fake_focus)
    monkeypatch.setattr(tools_mod, "_close_spawned_tab", fake_close)
    return seen


def _guarded(original=None):
    tools = Tools()
    entry = tools.registry.registry.actions["navigate"]
    if original is not None:
        entry.function = original
    register_navigation_guard(tools)
    return entry.function


def _navigate(fn, session, url="https://example.com/jobs", new_tab=False):
    return asyncio.run(fn(params=NavigateAction(url=url, new_tab=new_tab), browser_session=session))


def test_ready_page_navigates_once(fast, monkeypatch) -> None:
    _script_tabs(monkeypatch, {"tab-1": [("complete", 500)]})
    seen = _recovery_spies(monkeypatch)
    session = _Session()
    result = _navigate(_guarded(), session)
    assert isinstance(result, ActionResult) and result.error is None
    assert _same_tab_navigations(session) == 1
    assert seen["spawned"] == []


def test_slow_first_load_is_waited_for_not_reloaded(fast, monkeypatch) -> None:
    _script_tabs(monkeypatch, {"tab-1": [None, None, ("loading", 0), ("complete", 800)]})
    seen = _recovery_spies(monkeypatch)
    session = _Session()
    result = _navigate(_guarded(), session)
    assert result.error is None
    assert _same_tab_navigations(session) == 1
    assert seen["spawned"] == []


def test_blank_page_reports_empty_without_reloading(fast, monkeypatch) -> None:
    _script_tabs(monkeypatch, {"tab-1": [("complete", 0)]})
    seen = _recovery_spies(monkeypatch)
    session = _Session()
    result = _navigate(_guarded(), session)
    assert result.error and "empty content" in result.error
    assert _same_tab_navigations(session) == 1
    assert seen["spawned"] == []


def test_dead_tab_is_replaced_by_a_fresh_one(fast, monkeypatch) -> None:
    _script_tabs(monkeypatch, {"tab-1": [None], "tab-2": [("complete", 900)]})
    seen = _recovery_spies(monkeypatch, fresh="tab-2")
    session = _Session()
    result = _navigate(_guarded(), session)
    assert result.error is None
    assert "replaced" in (result.long_term_memory or "")
    assert _same_tab_navigations(session) == 1
    assert seen["spawned"] == ["https://example.com/jobs"]
    assert seen["focused"] == ["tab-2"]
    assert seen["closed"] == ["tab-1"]


def test_dead_fresh_tab_gives_a_clear_error_instead_of_hanging(fast, monkeypatch) -> None:
    _script_tabs(monkeypatch, {"tab-1": [None], "tab-2": [None]})
    _recovery_spies(monkeypatch, fresh="tab-2")
    session = _Session()
    result = _navigate(_guarded(), session)
    assert result.error and "stopped responding" in result.error
    assert _same_tab_navigations(session) == 1


def test_failed_tab_spawn_gives_a_clear_error(fast, monkeypatch) -> None:
    _script_tabs(monkeypatch, {"tab-1": [None]})
    _recovery_spies(monkeypatch, fresh=None)
    result = _navigate(_guarded(), _Session())
    assert result.error and "stopped responding" in result.error


def test_navigation_failure_is_reported_without_a_second_attempt(fast, monkeypatch) -> None:
    _script_tabs(monkeypatch, {"tab-1": [("complete", 500)]})
    seen = _recovery_spies(monkeypatch)
    session = _Session(nav_error=RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
    result = _navigate(_guarded(), session)
    assert result.error and "ERR_NAME_NOT_RESOLVED" in result.error
    assert _same_tab_navigations(session) == 1
    assert seen["spawned"] == []


@pytest.mark.parametrize(
    "url,new_tab", [("https://example.com", True), ("about:blank", False), ("file:///tmp/x", False)]
)
def test_new_tabs_and_non_http_urls_use_the_builtin(fast, monkeypatch, url, new_tab) -> None:
    ran: list = []

    async def builtin(params=None, **kwargs):
        ran.append(params.url)
        return ActionResult(extracted_content="builtin")

    session = _Session()
    result = _navigate(_guarded(builtin), session, url=url, new_tab=new_tab)
    assert ran == [url]
    assert result.extracted_content == "builtin"
    assert session.dispatched == []


def test_dead_uncommitted_tab_is_caught_on_the_short_limit(fast, monkeypatch) -> None:
    """A silent tab still on its old URL is dead well before the long limit."""
    _script_tabs(monkeypatch, {"tab-1": [None], "tab-2": [("complete", 900)]})
    seen = _recovery_spies(monkeypatch, fresh="tab-2")
    session = _Session(urls={"tab-1": "chrome://newtab/"})
    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        result = loop.run_until_complete(
            _guarded()(params=NavigateAction(url="https://example.com/jobs"), browser_session=session)
        )
        elapsed = loop.time() - started
    finally:
        loop.close()
    assert result.error is None and seen["closed"] == ["tab-1"]
    assert elapsed < tools_mod._NAV_COMMITTED_DEAD_S


def test_empty_target_url_counts_as_uncommitted(fast, monkeypatch) -> None:
    _script_tabs(monkeypatch, {"tab-1": [None], "tab-2": [("complete", 900)]})
    seen = _recovery_spies(monkeypatch, fresh="tab-2")
    result = _navigate(_guarded(), _Session(urls={"tab-1": ""}))
    assert result.error is None and seen["spawned"] == ["https://example.com/jobs"]


def test_committed_but_busy_tab_is_given_time_before_being_called_dead(fast, monkeypatch) -> None:
    """Moved to the new URL but not answering means the page's scripts are busy:
    it must outlast the short limit and load without a replacement tab."""
    busy_polls = 10
    _script_tabs(monkeypatch, {"tab-1": [None] * busy_polls + [("complete", 700)]})
    seen = _recovery_spies(monkeypatch)
    session = _Session(urls={"tab-1": "chrome://newtab/"})

    async def committed_after_dispatch(params):
        return {"targetInfo": {"url": "https://example.com/jobs" if session.dispatched else "chrome://newtab/"}}

    session._cdp_client_root.send.Target.getTargetInfo = committed_after_dispatch
    result = _navigate(_guarded(), session)
    assert result.error is None
    assert seen["spawned"] == []


def test_old_page_answering_is_not_mistaken_for_the_new_one(fast, monkeypatch) -> None:
    """The tab answers with the previous page's text while the navigation is
    still in flight; that must not be reported as the new page having loaded."""
    _script_tabs(monkeypatch, {"tab-1": [("complete", 300)]})
    _recovery_spies(monkeypatch)
    session = _Session(urls={"tab-1": "https://old.example/"}, nav_error=RuntimeError("net::ERR_CONNECTION_REFUSED"))
    result = _navigate(_guarded(), session)
    assert result.error and "ERR_CONNECTION_REFUSED" in result.error
