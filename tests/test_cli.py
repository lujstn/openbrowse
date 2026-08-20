"""The openbrowse console script: argument handling and subcommand wiring."""

import os
import sys
from types import SimpleNamespace

import pytest

from openbrowse import __version__, cli, updates
from openbrowse.updates import UpdateState


def test_version_subcommand(capsys):
    assert cli.main(["version"]) == 0
    assert f"openbrowse {__version__}" in capsys.readouterr().out


def test_version_flag_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "openbrowse start" in out
    assert "runs now and on every boot" in out


def test_serve_passes_host_and_port(monkeypatch):
    served: list[tuple] = []
    monkeypatch.setattr(cli, "_cmd_serve", lambda args: served.append((args.host, args.port)) or 0)
    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "9000"]) == 0
    assert served == [("127.0.0.1", 9000)]


def test_check_update_reports_available(monkeypatch, capsys):
    async def fake_check():
        return UpdateState(current="1.0.0", latest="2.0.0", available=True)

    monkeypatch.setattr(updates, "check_once", fake_check)
    assert cli.main(["check-update"]) == 0
    out = capsys.readouterr().out
    assert "Update available: 1.0.0 -> 2.0.0" in out


def test_check_update_up_to_date(monkeypatch, capsys):
    async def fake_check():
        return UpdateState(current="1.0.0", latest="1.0.0", available=False)

    monkeypatch.setattr(updates, "check_once", fake_check)
    assert cli.main(["check-update"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_check_update_failure_exits_nonzero(monkeypatch, capsys):
    async def fake_check():
        return UpdateState(current="1.0.0", error="ConnectError: nope")

    monkeypatch.setattr(updates, "check_once", fake_check)
    assert cli.main(["check-update"]) == 1


def test_update_installs_when_available(monkeypatch, capsys):
    async def fake_check():
        return UpdateState(current="1.0.0", latest="2.0.0", available=True)

    async def fake_install():
        return True, "install log"

    monkeypatch.setattr(updates, "check_once", fake_check)
    monkeypatch.setattr(updates, "install_update", fake_install)
    assert cli.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "install log" in out
    assert "Restart the server" in out


def test_update_skips_when_current(monkeypatch, capsys):
    async def fake_check():
        return UpdateState(current="1.0.0", latest="1.0.0", available=False)

    monkeypatch.setattr(updates, "check_once", fake_check)
    assert cli.main(["update"]) == 0
    assert "already the latest" in capsys.readouterr().out


def test_serve_binds_the_ephemeral_port_it_was_asked_for(monkeypatch):
    """`--port 0` asks the OS for a free port. Falsy-or would serve 8420 instead
    and collide with the running service."""
    import openbrowse.cli as cli_mod

    bound: list[tuple] = []

    class FakeUvicorn:
        @staticmethod
        def run(app, host, port):
            bound.append((host, port))

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    assert cli_mod.main(["serve", "--port", "0"]) == 0
    assert bound == [("0.0.0.0", 0)]


def test_tune_forwards_the_unit_name_and_returns_the_exit_code(monkeypatch):
    """The CLI and the dashboard must tune the same unit; a drop-in for a unit
    that is not running is reported as applied and does nothing."""
    from openbrowse import hostinfo

    seen: list[list[str]] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        cli.subprocess, "run", lambda command: seen.append(command) or SimpleNamespace(returncode=3)
    )

    assert cli.main(["tune", "--share", "most"]) == 3
    assert ["--service", hostinfo.UNIT_NAME] == seen[0][-2:]


def test_tune_without_sudo_reports_rather_than_traces(monkeypatch, capsys):
    def no_sudo(command):
        raise FileNotFoundError(2, "No such file or directory", "sudo")

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli.subprocess, "run", no_sudo)

    assert cli.main(["tune", "--share", "most"]) == 1
    assert "Could not run sudo" in capsys.readouterr().err


def test_start_does_not_offer_flags_it_would_ignore():
    """`openbrowse start` writes a unit with a plain `ExecStart=... serve`, so a
    --port it silently dropped would be a promise the service never keeps."""
    with pytest.raises(SystemExit):
        cli.main(["start", "--port", "9000"])


def _uninstall_env(monkeypatch, tmp_path, method="pipx"):
    """Point every path the uninstall touches at harmless test locations."""
    from dataclasses import replace

    from openbrowse.config import settings

    data_dir = tmp_path / "openbrowse-home"
    data_dir.mkdir()
    (data_dir / ".env").write_text("API_KEY=x\n")
    cache_dir = tmp_path / "cloak-cache"
    cache_dir.mkdir()
    (cache_dir / "chromium-1" ).mkdir()
    cmdline = tmp_path / "cmdline.txt"
    cmdline.write_text("console=serial0 root=PARTUUID=x rw psi=1\n")

    monkeypatch.setenv("CLOAKBROWSER_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("HT_CMDLINE", str(cmdline))
    monkeypatch.setattr(
        "openbrowse.config.settings", replace(settings, home_dir=data_dir)
    )
    monkeypatch.setattr(
        updates, "detect_install_method", lambda: (method, None)
    )
    monkeypatch.setattr(updates, "_pipx_binary", lambda: "/fake/pipx")
    monkeypatch.setattr(updates, "_uv_binary", lambda: "/fake/uv")

    ran: list[list[str]] = []
    monkeypatch.setattr(
        "subprocess.run", lambda cmd, **kw: ran.append(list(cmd)) or SimpleNamespace(returncode=0)
    )
    return data_dir, cache_dir, cmdline, ran


def test_uninstall_removes_everything_with_yes(monkeypatch, tmp_path, capsys):
    data_dir, cache_dir, cmdline, ran = _uninstall_env(monkeypatch, tmp_path)
    assert cli.main(["uninstall", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "fully uninstalled" in out
    assert not data_dir.exists()
    assert not cache_dir.exists()
    assert ["/fake/pipx", "uninstall", "openbrowse"] in ran
    # psi revert went through sudo with the flag stripped from the written content
    psi_cmds = [c for c in ran if c[:2] == ["sudo", "sh"]]
    assert psi_cmds and "psi=1" not in psi_cmds[0][-1].split(">")[0].replace("' >", "")


def test_uninstall_aborts_without_confirmation(monkeypatch, tmp_path, capsys):
    data_dir, cache_dir, _, ran = _uninstall_env(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "no thanks")
    assert cli.main(["uninstall"]) == 1
    assert "nothing was removed" in capsys.readouterr().out
    assert data_dir.exists()
    assert cache_dir.exists()
    assert ran == []


def test_uninstall_honours_keep_flags(monkeypatch, tmp_path):
    data_dir, cache_dir, _, ran = _uninstall_env(monkeypatch, tmp_path)
    assert cli.main(["uninstall", "--yes", "--keep-data", "--keep-browser"]) == 0
    assert data_dir.exists()
    assert cache_dir.exists()
    assert ["/fake/pipx", "uninstall", "openbrowse"] in ran


def test_uninstall_leaves_a_checkout_alone(monkeypatch, tmp_path, capsys):
    _, _, _, ran = _uninstall_env(monkeypatch, tmp_path, method="checkout")
    assert cli.main(["uninstall", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "delete the clone" in out
    assert ["/fake/pipx", "uninstall", "openbrowse"] not in ran
    assert ["/fake/uv", "tool", "uninstall", "openbrowse"] not in ran


def test_uninstall_uses_uv_for_uv_tools(monkeypatch, tmp_path):
    _, _, _, ran = _uninstall_env(monkeypatch, tmp_path, method="uv-tool")
    assert cli.main(["uninstall", "--yes"]) == 0
    assert ["/fake/uv", "tool", "uninstall", "openbrowse"] in ran
