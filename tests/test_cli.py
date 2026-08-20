"""The openbrowse console entry point."""

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


def test_tune_targets_the_openbrowse_unit(monkeypatch):
    execs: list[list[str]] = []
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli.os, "execvp", lambda file, argv: execs.append(list(argv)))
    cli.main(["tune", "--share", "shared", "--dry-run"])
    (argv,) = execs
    assert argv[0] == "bash"
    assert argv[1].endswith("host_tune.sh")
    assert ["--share", "shared"] == argv[2:4]
    assert ["--service", "openbrowse.service"] == argv[4:6]
    assert argv[6] == "--dry-run"


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
