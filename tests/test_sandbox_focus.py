"""The sandbox browser must read the page the agent was on, never the code-view
tab that run_code_file keeps focused while a script runs, and the code stream must
never remember one of its own tabs as the page to restore."""

from openbrowse.agent import tools as tools_mod
from openbrowse.agent.code_stream import CodeStreamObserver
from openbrowse.agent.tools import _SandboxBrowser


async def test_sandbox_eval_pins_to_home_target(monkeypatch) -> None:
    calls: list[tuple[str, tuple]] = []

    async def fake_on_target(session, target_id, expression):
        calls.append(("on_target", (target_id, expression)))
        return "pinned"

    async def fake_eval_js(session, expression):
        calls.append(("focused", (expression,)))
        return "focused"

    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_on_target)
    monkeypatch.setattr(tools_mod, "_eval_js", fake_eval_js)

    pinned = _SandboxBrowser(None, home_target="PAGE-TARGET")
    assert await pinned.evaluate("1+1") == "pinned"
    assert calls[-1] == ("on_target", ("PAGE-TARGET", "1+1"))

    unpinned = _SandboxBrowser(None)
    assert await unpinned.evaluate("2+2") == "focused"
    assert calls[-1] == ("focused", ("2+2",))


async def test_sandbox_navigate_pins_to_home_target(monkeypatch) -> None:
    seen: list[str] = []

    async def fake_on_target(session, target_id, expression):
        seen.append(target_id)
        return None

    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_on_target)
    sb = _SandboxBrowser(None, home_target="PAGE-TARGET")
    await sb.navigate("https://example.com/x", settle_s=0)
    assert seen == ["PAGE-TARGET"]


class _FakeSession:
    def __init__(self, focus: str) -> None:
        self.agent_focus_target_id = focus


async def test_code_stream_never_remembers_its_own_tab(monkeypatch) -> None:
    spawned = iter(["CODE-1", "CODE-2"])

    async def fake_spawn(session, url):
        return next(spawned)

    async def fake_focus(session, target_id):
        session.agent_focus_target_id = target_id

    monkeypatch.setattr(tools_mod, "_spawn_tab", fake_spawn)
    monkeypatch.setattr(tools_mod, "_focus_target", fake_focus)

    session = _FakeSession(focus="PAGE-TARGET")
    obs = CodeStreamObserver(session, clipboard={})
    await obs._open_tab()
    assert obs._prev_focus == "PAGE-TARGET"
    assert obs._own_tabs == {"CODE-1"}

    # A mid-generation reset while the code tab holds focus must not poison the
    # remembered page: the next code tab keeps PAGE-TARGET as the focus to
    # restore, and run_code_file's guard resolves reads back to it.
    obs.reset()
    assert obs._prev_focus == "PAGE-TARGET"
    await obs._open_tab()
    assert obs._prev_focus == "PAGE-TARGET"
    assert obs._own_tabs == {"CODE-1", "CODE-2"}

    home = session.agent_focus_target_id
    if home in obs._own_tabs:
        home = obs._prev_focus
    assert home == "PAGE-TARGET"
