"""systemd service management behind openbrowse start/stop/restart/status."""

import getpass
from types import SimpleNamespace

import pytest

from openbrowse import cli, service
from openbrowse.config import settings


def test_unit_name_defaults_to_openbrowse(tmp_path):
    assert service.unit_name(tmp_path) == "openbrowse.service"


def test_unit_name_keeps_legacy_unit(tmp_path):
    (tmp_path / "browser-use.service").write_text("[Unit]\n")
    assert service.unit_name(tmp_path) == "browser-use.service"


def test_unit_name_prefers_new_unit_over_legacy(tmp_path):
    (tmp_path / "browser-use.service").write_text("[Unit]\n")
    (tmp_path / "openbrowse.service").write_text("[Unit]\n")
    assert service.unit_name(tmp_path) == "openbrowse.service"


def test_unit_content_shape(monkeypatch):
    monkeypatch.setattr(service, "_openbrowse_binary", lambda: "/home/pi/.local/bin/openbrowse")
    content = service.unit_content()

    assert f"User={getpass.getuser()}" in content
    assert "ExecStart=/home/pi/.local/bin/openbrowse serve" in content
    assert f"EnvironmentFile=-{settings.env_path}" in content
    assert "WantedBy=multi-user.target" in content
    assert "Restart=on-failure" in content


@pytest.fixture
def sudo_calls(monkeypatch):
    calls: list[tuple[list[str], str | None]] = []

    def fake_sudo(command, *, input_text=None):
        calls.append((command, input_text))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(service, "_sudo", fake_sudo)
    return calls


def test_start_installs_enables_and_says_boot(tmp_path, sudo_calls, monkeypatch):
    monkeypatch.setattr(service, "_openbrowse_binary", lambda: "/usr/local/bin/openbrowse")

    ok, message = service.start(tmp_path)

    assert ok is True
    commands = [c for c, _ in sudo_calls]
    assert commands[0] == ["tee", str(tmp_path / "openbrowse.service")]
    assert sudo_calls[0][1] == service.unit_content()
    assert ["systemctl", "daemon-reload"] in commands
    assert ["systemctl", "enable", "--now", "openbrowse.service"] in commands
    assert "start automatically every time this machine boots" in message
    assert "Registered OpenBrowse as a systemd service" in message


def test_start_reuses_existing_unit(tmp_path, sudo_calls):
    (tmp_path / "browser-use.service").write_text("[Unit]\n")

    ok, message = service.start(tmp_path)

    assert ok is True
    commands = [c for c, _ in sudo_calls]
    assert commands == [["systemctl", "enable", "--now", "browser-use.service"]]
    assert "start automatically every time this machine boots" in message


def test_start_reports_enable_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service, "_sudo", lambda command, *, input_text=None: SimpleNamespace(returncode=1)
    )

    ok, message = service.start(tmp_path)

    assert ok is False


def test_stop_keeps_boot_start_by_default(tmp_path, sudo_calls):
    ok, message = service.stop(disable=False, systemd_dir=tmp_path)

    assert ok is True
    assert [c for c, _ in sudo_calls] == [["systemctl", "stop", "openbrowse.service"]]
    assert "still start automatically on the next boot" in message


def test_stop_disable_removes_boot_start(tmp_path, sudo_calls):
    ok, message = service.stop(disable=True, systemd_dir=tmp_path)

    assert ok is True
    assert [c for c, _ in sudo_calls] == [
        ["systemctl", "disable", "--now", "openbrowse.service"]
    ]
    assert "no longer start on boot" in message


def test_restart(tmp_path, sudo_calls):
    ok, _ = service.restart(tmp_path)

    assert ok is True
    assert [c for c, _ in sudo_calls] == [["systemctl", "restart", "openbrowse.service"]]


def test_cli_start_without_systemd_falls_back_to_serve(monkeypatch, capsys):
    served: list[bool] = []
    monkeypatch.setattr(service, "systemd_available", lambda: False)
    monkeypatch.setattr(cli, "_cmd_serve", lambda args: served.append(True) or 0)

    assert cli.main(["start"]) == 0
    assert served == [True]
    assert "cannot be registered to start automatically" in capsys.readouterr().out


def test_cli_start_with_systemd_uses_service(monkeypatch, capsys):
    monkeypatch.setattr(service, "systemd_available", lambda: True)
    monkeypatch.setattr(service, "start", lambda: (True, "boot message"))

    assert cli.main(["start"]) == 0
    assert "boot message" in capsys.readouterr().out


def test_cli_stop_passes_disable(monkeypatch):
    seen: list[bool] = []
    monkeypatch.setattr(service, "systemd_available", lambda: True)
    monkeypatch.setattr(
        service, "stop", lambda disable: seen.append(disable) or (True, "ok")
    )

    assert cli.main(["stop", "--disable"]) == 0
    assert seen == [True]


def test_cli_status_without_systemd_errors(monkeypatch):
    monkeypatch.setattr(service, "systemd_available", lambda: False)
    assert cli.main(["status"]) == 1
