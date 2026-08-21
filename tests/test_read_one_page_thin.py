"""A complete page with stable-but-thin text must settle quickly instead of
holding its read-pages wave to the full readiness deadline."""

import time

from openbrowse.agent import tools as tools_mod
from openbrowse.agent.tools import _read_one_page


def _fake_eval(pages: dict[str, str]):
    async def fake(browser_session, target_id, expression):
        if expression == "document.readyState":
            return "complete"
        if expression == tools_mod._BODY_TEXT_JS:
            return pages["text"]
        if expression == tools_mod._IFRAME_HOSTS_JS:
            return pages.get("iframe_hosts", [])
        if expression == "document.title":
            return pages.get("title", "t")
        if expression == tools_mod._JSONLD_JS:
            return pages.get("jsonld")
        if expression == tools_mod._LINKS_JS:
            return []
        return None

    return fake


async def test_thin_complete_page_settles_fast(monkeypatch) -> None:
    thin = "Name: Basil Finch. Role: Glazier. Daily rate: 210. City: Norwich."
    assert len(thin) < tools_mod._MIN_PAGE_TEXT_CHARS
    monkeypatch.setattr(tools_mod, "_eval_on_target", _fake_eval({"text": thin}))

    started = time.monotonic()
    page = await _read_one_page(None, "http://x/detail/2.html", "T1", None, set(), set())
    elapsed = time.monotonic() - started

    assert page.get("error") is None
    assert page["text"] == thin
    # Two stability polls plus the JSON-LD grace — nowhere near the 25s deadline.
    assert elapsed < tools_mod._PAGE_READY_TIMEOUT_S / 2, elapsed


async def test_thin_page_with_pending_embed_still_waits(monkeypatch) -> None:
    monkeypatch.setattr(tools_mod, "_PAGE_READY_TIMEOUT_S", 2.0)
    monkeypatch.setattr(
        tools_mod,
        "_eval_on_target",
        _fake_eval({"text": "loading…", "iframe_hosts": ["records.example.com"]}),
    )

    started = time.monotonic()
    page = await _read_one_page(None, "http://x/detail/11.html", "T1", None, set(), set())
    elapsed = time.monotonic() - started

    # The embed may still render, so the thin early-exit must not fire.
    assert elapsed >= 2.0, elapsed
    assert "embeds its content" in (page.get("error") or "")
