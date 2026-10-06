"""Shortened output always says nothing failed and names the call that reads the rest.

A Luna run at max effort looped for 40 minutes because a script's printout was cut at
2,000 characters with a note that read like an unfinished audit, and the one reader
it could have used started at the top every time. These pin the contract that closes
that dead end: overflow is saved whole, the note gives the exact next call, and
read_file pages through to the end.
"""

from __future__ import annotations

import json
import types

import pytest
from browser_use import Tools
from browser_use.filesystem.file_system import FileSystem

from openbrowse.agent import tools as tools_mod
from openbrowse.agent.tools import (
    INLINE_BUDGET,
    READ_PAGE_CHARS,
    _exec_in_sandbox,
    continuation_note,
    deliver,
    register_code_tools,
    register_output_guard_overrides,
    register_paged_read_file,
)


@pytest.fixture
def fs(tmp_path):
    return FileSystem(tmp_path)


async def _read(tools: Tools, fs: FileSystem, name: str, start: int | None = None):
    entry = tools.registry.registry.actions["read_file"]
    fields = {"file_name": name} if start is None else {"file_name": name, "start": start}
    return await entry.function(
        params=entry.param_model(**fields), available_file_paths=[], file_system=fs
    )


def _next_start(text: str) -> int | None:
    marker = "start="
    if marker not in text.rsplit("[", 1)[-1]:
        return None
    return int(text.rsplit(marker, 1)[1].split(")")[0])


def _paged_tools() -> Tools:
    tools = Tools()
    register_paged_read_file(tools)
    register_output_guard_overrides(tools)
    return tools


def _roles(n: int = 12, each: int = 5000) -> str:
    return "".join(f"ROLE {i}: " + ("evidence " * (each // 9)) + "\n" for i in range(n))


async def test_a_long_printout_is_saved_whole_and_says_how_to_read_on(fs):
    printed = _roles()
    namespace: dict = {"printed": printed}
    result = await _exec_in_sandbox("print(printed, end='')", namespace, fs)
    text = result.extracted_content
    assert text.startswith(printed[:INLINE_BUDGET])
    assert "Complete, only this reply is shortened" in text
    assert f"read_file('stdout_1.txt', start={INLINE_BUDGET})" in text
    assert "truncated" not in text
    assert (fs.get_dir() / "stdout_1.txt").read_text() == printed


async def test_a_short_printout_comes_back_whole_with_no_file(fs):
    result = await _exec_in_sandbox("print('twelve roles')", {}, fs)
    assert result.extracted_content == "twelve roles\n"
    assert not (fs.get_dir() / "stdout_1.txt").exists()


async def test_each_long_printout_gets_its_own_file(fs):
    namespace: dict = {"a": "a" * 3000, "b": "b" * 3000}
    first = await _exec_in_sandbox("print(a)", namespace, fs)
    second = await _exec_in_sandbox("print(b)", namespace, fs)
    assert "stdout_1.txt" in first.extracted_content
    assert "stdout_2.txt" in second.extracted_content


async def test_read_file_pages_through_to_the_end(fs):
    content = _roles()
    await fs.write_file("pages.txt", content)
    tools = _paged_tools()
    parts, start, reads = [], 0, 0
    while start is not None:
        result = await _read(tools, fs, "pages.txt", start)
        reads += 1
        body, note = result.extracted_content.rsplit("\n[", 1)
        assert len(body) <= READ_PAGE_CHARS
        parts.append(body)
        start = _next_start(result.extracted_content)
    assert "".join(parts) == content
    assert "the end of 'pages.txt'" in note
    assert reads == -(-len(content) // READ_PAGE_CHARS)


async def test_read_file_from_the_top_by_default_and_past_the_end_safely(fs):
    await fs.write_file("small.txt", "hello")
    tools = _paged_tools()
    whole = await _read(tools, fs, "small.txt")
    assert whole.extracted_content.startswith("hello\n[Characters 0 to 5 of 5")
    beyond = await _read(tools, fs, "small.txt", 99)
    assert "of 5: the end" in beyond.extracted_content


async def test_read_file_reports_a_missing_file_as_an_error(fs):
    result = await _read(_paged_tools(), fs, "nope.txt")
    assert result.error and "not found" in result.error


async def test_a_page_read_is_never_cut_again_by_the_output_guard(fs):
    await fs.write_file("big.txt", "x" * 50_000)
    result = await _read(_paged_tools(), fs, "big.txt", 12_000)
    assert result.extracted_content.startswith("x" * READ_PAGE_CHARS + "\n[Complete")
    assert "start=18000" in result.extracted_content


async def test_the_loop_that_hit_luna_max_now_reaches_every_record(fs):
    """A script printing per-role evidence for 12 roles, then reading on as told."""
    tools = Tools()
    register_code_tools(tools, {}, None)
    register_paged_read_file(tools)
    register_output_guard_overrides(tools)
    entry = tools.registry.registry.actions["run_code_file"]
    code = (
        "for i in range(12):\n"
        "    print(f'ROLE {i}: ' + 'visa sponsorship not mentioned; ' * 150)\n"
    )
    result = await entry.function(
        params=entry.param_model(name="audit", code=code),
        browser_session=types.SimpleNamespace(),
        file_system=fs,
    )
    first = result.extracted_content
    assert "ROLE 0" in first and "ROLE 11" not in first
    seen = first.split("\n\n[", 1)[0]
    start = _next_start(first)
    name = first.split("read_file('", 1)[1].split("'", 1)[0]
    while start is not None:
        page = await _read(tools, fs, name, start)
        seen = seen[:start] + page.extracted_content.rsplit("\n[", 1)[0]
        start = _next_start(page.extracted_content)
    assert all(f"ROLE {i}:" in seen for i in range(12))


async def test_the_guards_saved_file_is_readable_from_where_the_preview_stops(fs):
    tools = Tools()
    register_paged_read_file(tools)

    @tools.action("dump")
    async def dump() -> "tools_mod.ActionResult":
        return tools_mod.ActionResult(extracted_content="y" * 20_000)

    guarded = tools_mod._GUARDED_DUMP_ACTIONS
    tools_mod._GUARDED_DUMP_ACTIONS = guarded + ("dump",)
    try:
        register_output_guard_overrides(tools)
    finally:
        tools_mod._GUARDED_DUMP_ACTIONS = guarded
    entry = tools.registry.registry.actions["dump"]
    result = await entry.function(params=entry.param_model(), file_system=fs)
    text = result.extracted_content
    assert "Complete, only this reply is shortened" in text
    name = text.split("read_file('", 1)[1].split("'", 1)[0]
    rest = await _read(tools, fs, name, _next_start(text))
    assert rest.extracted_content.startswith("y" * READ_PAGE_CHARS)


async def test_delivers_pointer_pages_from_the_top(fs):
    result = await deliver(
        {"rows": ["r" * 50] * 200}, note="Found 200 rows.", file_system=fs, filename="rows.json"
    )
    envelope = json.loads(result.extracted_content)
    assert "Complete, only this reply is shortened" in envelope["shortened"]
    assert "read_file('rows.json', start=0)" in envelope["read_with"]


def test_the_note_is_the_same_wherever_output_is_cut():
    assert continuation_note("a.txt", 2000, 9000) == (
        "[Complete, only this reply is shortened: showing characters 0 to 2,000 of "
        "9,000. All of it is saved as 'a.txt'; read on with read_file('a.txt', start=2000).]"
    )
    assert continuation_note("a.txt", 9000, 9000, 6000).endswith("the end of 'a.txt'.]")
    assert "not available" in continuation_note(None, 2000, 9000)


async def test_a_file_saved_from_a_script_reads_back_whole_at_once(fs):
    from openbrowse.agent.tools import _write_fs_file_sync

    _write_fs_file_sync(fs, "rows.json", '{"rows": 12}')
    assert (fs.get_dir() / "rows.json").read_text() == '{"rows": 12}'
    assert fs.get_file("rows.json").read() == '{"rows": 12}'
