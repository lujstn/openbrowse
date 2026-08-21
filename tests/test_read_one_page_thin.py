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


async def test_thin_host_page_recovers_frame_content_in_one_pass(monkeypatch) -> None:
    # A host page whose text is thin and whose content sits in an embedded
    # panel must come back with the panel's text from a single read — telling
    # the model to re-run the sweep with a frame filter it could apply itself
    # is an instruction a no-reasoning model reliably improvises around.
    monkeypatch.setattr(tools_mod, "_FRAME_MATCH_GRACE_S", 0.2)
    frame_text = "Name: Sylvia Thorne. Role: Botanical Illustrator. Rate: 300. " * 4

    async def no_oopif(*args, **kwargs):
        return None

    async def fake_probe(browser_session, target_id, needle):
        return ("http://127.0.0.1:8622/frame/detail/11.html", 7)

    async def fake_world_eval(browser_session, target_id, ctx, expression):
        if expression == tools_mod._BODY_TEXT_JS:
            return frame_text
        if expression == tools_mod._LINKS_JS:
            return []
        return None

    monkeypatch.setattr(tools_mod, "_match_frame_target", no_oopif)
    monkeypatch.setattr(tools_mod, "_same_process_frame", fake_probe)
    monkeypatch.setattr(tools_mod, "_eval_in_frame_world", fake_world_eval)
    monkeypatch.setattr(
        tools_mod,
        "_eval_on_target",
        _fake_eval(
            {
                "text": "Profile below is embedded from our records system.",
                "iframe_hosts": ["127.0.0.1:8622"],
            }
        ),
    )

    started = time.monotonic()
    page = await _read_one_page(
        None, "http://127.0.0.1:8621/detail/11.html", "T1", None, set(), set()
    )
    elapsed = time.monotonic() - started

    assert page.get("error") is None
    assert page.get("frame_matched") is True
    assert page["text"] == frame_text
    assert elapsed < tools_mod._PAGE_READY_TIMEOUT_S / 2, elapsed


async def test_thin_page_with_unrecoverable_embed_reports_the_filter(monkeypatch) -> None:
    # When the frame recovery itself cannot read the panel, the instruction
    # error survives as the last resort — and the total time stays bounded by
    # the two passes, not an unbounded retry loop.
    monkeypatch.setattr(tools_mod, "_PAGE_READY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(tools_mod, "_FRAME_MATCH_GRACE_S", 0.2)
    monkeypatch.setattr(tools_mod, "_JSONLD_GRACE_S", 0.2)
    monkeypatch.setattr(
        tools_mod,
        "_eval_on_target",
        _fake_eval({"text": "loading…", "iframe_hosts": ["records.example.com"]}),
    )

    started = time.monotonic()
    page = await _read_one_page(None, "http://x/detail/11.html", "T1", None, set(), set())
    elapsed = time.monotonic() - started

    assert "embeds its content" in (page.get("error") or "")
    assert elapsed < 5.0, elapsed
