"""upload_file must never hand Chromium a relative path. browser-use treats a
cdp_url session as a remote browser and forwards the model's path into
DOM.setFileInputFiles verbatim; a relative path there means the read grant can
never match the form submission's body, and the browser kills the renderer
with ILLEGAL_UPLOAD_PARAMS (bad_message reason 170) while the tool chain
reports success."""

import asyncio
from pathlib import Path

from browser_use import ActionResult, Tools
from browser_use.filesystem.file_system import FileSystem
from browser_use.tools.views import UploadFileAction

from openbrowse.agent.tools import (
    _resolve_upload_path,
    register_upload_path_resolution,
)


def _fs_with_file(tmp_path: Path, name: str = "greeting.txt") -> FileSystem:
    fs = FileSystem(tmp_path)
    asyncio.run(fs.write_file(name, "hello from openbrowse"))
    return fs


def test_managed_name_resolves_to_absolute_real_path(tmp_path: Path) -> None:
    fs = _fs_with_file(tmp_path)
    resolved = _resolve_upload_path("greeting.txt", fs)
    assert resolved is not None
    assert Path(resolved).is_absolute()
    assert Path(resolved).name == "greeting.txt"
    assert Path(resolved).read_text() == "hello from openbrowse"


def test_absolute_path_passes_through_untouched(tmp_path: Path) -> None:
    fs = _fs_with_file(tmp_path)
    outside = tmp_path / "elsewhere.bin"
    outside.write_bytes(b"x")
    assert _resolve_upload_path(str(outside), fs) == str(outside)


def test_unknown_relative_name_is_unresolvable(tmp_path: Path) -> None:
    fs = _fs_with_file(tmp_path)
    assert _resolve_upload_path("missing.txt", fs) is None


def test_traversal_name_cannot_escape_the_managed_directory(tmp_path: Path) -> None:
    # get_file matches by sanitised basename, so a traversal name resolves to
    # the managed file inside the directory — never to a sibling outside it.
    fs = _fs_with_file(tmp_path)
    resolved = _resolve_upload_path("../greeting.txt", fs)
    assert resolved is not None
    fs_dir = Path(str(fs.get_dir())).resolve()
    assert fs_dir in Path(resolved).parents


def test_relative_name_without_file_system_is_unresolvable() -> None:
    assert _resolve_upload_path("greeting.txt", None) is None


def _swap_in_fake_builtin(tools: Tools, seen: dict) -> None:
    entry = tools.registry.registry.actions["upload_file"]

    async def fake_builtin(params=None, **kwargs):
        seen["path"] = params.path
        return ActionResult(extracted_content="ok")

    entry.function = fake_builtin


def test_wrapper_rewrites_managed_name_before_the_builtin(tmp_path: Path) -> None:
    fs = _fs_with_file(tmp_path)
    tools = Tools()
    seen: dict = {}
    _swap_in_fake_builtin(tools, seen)
    register_upload_path_resolution(tools)
    entry = tools.registry.registry.actions["upload_file"]
    result = asyncio.run(
        entry.function(
            params=UploadFileAction(index=3, path="greeting.txt"),
            file_system=fs,
            browser_session=None,
            available_file_paths=[],
        )
    )
    assert isinstance(result, ActionResult) and result.error is None
    assert Path(seen["path"]).is_absolute()
    assert Path(seen["path"]).name == "greeting.txt"


def test_wrapper_refuses_unresolvable_path_with_tool_error(tmp_path: Path) -> None:
    fs = _fs_with_file(tmp_path)
    tools = Tools()
    seen: dict = {}
    _swap_in_fake_builtin(tools, seen)
    register_upload_path_resolution(tools)
    entry = tools.registry.registry.actions["upload_file"]
    result = asyncio.run(
        entry.function(
            params=UploadFileAction(index=3, path="missing.txt"),
            file_system=fs,
            browser_session=None,
            available_file_paths=[],
        )
    )
    assert isinstance(result, ActionResult)
    assert result.error and "could not resolve" in result.error
    assert "path" not in seen, "the builtin must not run for an unresolvable path"


def test_wrapper_passes_absolute_path_through(tmp_path: Path) -> None:
    fs = _fs_with_file(tmp_path)
    target = tmp_path / "direct.txt"
    target.write_text("d")
    tools = Tools()
    seen: dict = {}
    _swap_in_fake_builtin(tools, seen)
    register_upload_path_resolution(tools)
    entry = tools.registry.registry.actions["upload_file"]
    asyncio.run(
        entry.function(
            params=UploadFileAction(index=1, path=str(target)),
            file_system=fs,
            browser_session=None,
            available_file_paths=[],
        )
    )
    assert seen["path"] == str(target)
