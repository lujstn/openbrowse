"""Cross-origin but same-SITE iframes never get their own CDP target, so both the
read_pages pipeline and the sandbox frame helpers must fall back to an isolated
world inside the frame instead of waiting for a panel that can never attach."""

import time

from openbrowse.agent import browser_cdp as cdp_mod
from openbrowse.agent import tools as tools_mod
from openbrowse.agent.tools import _SandboxBrowser, _read_one_page

FRAME_TEXT = "Name: Verity Lockwood\nRole: Project Manager\nDaily rate: £380\nCity: Edinburgh"


class _FakeCdpClient:
    def __init__(self, tree, world_context=7):
        self._tree = tree
        self._world_context = world_context
        self.send = self

    @property
    def Page(self):
        return self

    async def getFrameTree(self, session_id=None):
        return {"frameTree": self._tree}

    async def createIsolatedWorld(self, params=None, session_id=None):
        return {"executionContextId": self._world_context}


class _FakeSession:
    def __init__(self, tree):
        self.cdp_client = _FakeCdpClient(tree)
        self.session_id = "s1"
        self.agent_focus_target_id = "PAGE"

    async def get_or_create_cdp_session(self, target_id=None, focus=True):
        return self


def _tree_with_child(url: str) -> dict:
    return {
        "frame": {"id": "MAIN", "url": "http://127.0.0.1:8621/detail/11.html"},
        "childFrames": [{"frame": {"id": "CHILD", "url": url}}],
    }


async def test_same_process_frame_finds_child_by_substring() -> None:
    session = _FakeSession(_tree_with_child("http://127.0.0.1:8622/frame/detail/11.html"))
    hit = await cdp_mod._same_process_frame(session, "PAGE", "127.0.0.1:8622")
    assert hit == ("http://127.0.0.1:8622/frame/detail/11.html", 7)
    # The main frame must never match, even when the needle appears in its URL.
    miss = await cdp_mod._same_process_frame(session, "PAGE", "no-such-host")
    assert miss is None


async def test_read_one_page_reads_same_site_frame_via_world(monkeypatch) -> None:
    monkeypatch.setattr(tools_mod, "_FRAME_MATCH_GRACE_S", 0.2)

    async def no_oopif(*args, **kwargs):
        return None

    async def fake_probe(session, target_id, needle):
        return ("http://127.0.0.1:8622/frame/detail/11.html", 7)

    async def fake_world_eval(session, target_id, context_id, expression):
        assert context_id == 7
        if expression == tools_mod._BODY_TEXT_JS:
            return FRAME_TEXT
        if expression == tools_mod._JSONLD_JS:
            return None
        if expression == tools_mod._LINKS_JS:
            return []
        return None

    async def fake_target_eval(session, target_id, expression):
        if expression == "document.readyState":
            return "complete"
        if expression == "document.title":
            return "Staff profile 11"
        if expression == tools_mod._BODY_TEXT_JS:
            return "Profile below is embedded from our records system."
        if expression == tools_mod._IFRAME_SRC_JS:
            return ["http://127.0.0.1:8622/frame/detail/11.html"]
        if expression == tools_mod._JSONLD_JS:
            return None
        return None

    monkeypatch.setattr(tools_mod, "_match_frame_target", no_oopif)
    monkeypatch.setattr(tools_mod, "_same_process_frame", fake_probe)
    monkeypatch.setattr(tools_mod, "_eval_in_frame_world", fake_world_eval)
    monkeypatch.setattr(tools_mod, "_eval_on_target", fake_target_eval)

    started = time.monotonic()
    page = await _read_one_page(
        None, "http://127.0.0.1:8621/detail/11.html", "PAGE",
        "127.0.0.1:8622", set(), set(),
    )
    elapsed = time.monotonic() - started

    assert page.get("error") is None
    assert page.get("frame_matched") is True
    assert page["text"] == FRAME_TEXT
    assert elapsed < tools_mod._PAGE_READY_TIMEOUT_S / 2, elapsed


async def test_sandbox_frame_evaluate_falls_back_to_world(monkeypatch) -> None:
    async def fake_probe(session, target_id, needle):
        return ("http://127.0.0.1:8622/frame/detail/11.html", 7)

    async def fake_world_eval(session, target_id, context_id, expression):
        return FRAME_TEXT

    monkeypatch.setattr(tools_mod, "_same_process_frame", fake_probe)
    monkeypatch.setattr(tools_mod, "_eval_in_frame_world", fake_world_eval)

    sb = _SandboxBrowser(None, home_target="PAGE")

    async def no_oopifs():
        return []

    sb.frames = no_oopifs
    assert await sb.frame_text("127.0.0.1:8622") == FRAME_TEXT
