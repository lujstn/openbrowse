"""systemd service management behind openbrowse start/stop/restart/status."""

from types import SimpleNamespace

import pytest

from openbrowse import cli, config, hostinfo, service
from openbrowse.config import settings


def test_unit_name_is_the_one_hostinfo_derives_the_tuning_path_from():
    """Two spellings of the unit name is how tuning silently lands on a unit that
    is not running, so service.py must not carry its own."""
    assert service.UNIT_NAME == hostinfo.UNIT_NAME
    assert hostinfo._CAPACITY_OVERRIDE.endswith(f"{service.UNIT_NAME}.d/50-capacity.conf")


def test_unit_content_shape(monkeypatch):
    monkeypatch.setattr(service, "_openbrowse_binary", lambda: "/home/pi/.local/bin/openbrowse")
    content = service.unit_content()

    assert f"User={config.invoking_user()}" in content
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


def test_start_leaves_a_customised_unit_alone(tmp_path, sudo_calls, monkeypatch):
    """Hand-written units carry Tailscale hooks and resource limits this
    generator knows nothing about; "start" must not quietly discard them."""
    monkeypatch.setattr(service, "_openbrowse_binary", lambda: "/usr/local/bin/openbrowse")
    (tmp_path / "openbrowse.service").write_text(
        "[Service]\nExecStart=/home/pi/openbrowse/.venv/bin/openbrowse serve\n"
        "ExecStartPost=+/usr/bin/tailscale funnel --bg 8420\n"
    )

    ok, message = service.start(tmp_path)

    assert ok is True
    assert [c for c, _ in sudo_calls] == [["systemctl", "enable", "--now", "openbrowse.service"]]
    assert "tailscale" in (tmp_path / "openbrowse.service").read_text()
    assert "/home/pi/openbrowse/.venv/bin/openbrowse serve" in message
    assert "--reinstall-unit" in message


def test_start_reinstall_replaces_the_unit(tmp_path, sudo_calls, monkeypatch):
    monkeypatch.setattr(service, "_openbrowse_binary", lambda: "/usr/local/bin/openbrowse")
    (tmp_path / "openbrowse.service").write_text("[Service]\nExecStart=/gone/openbrowse serve\n")

    ok, message = service.start(tmp_path, reinstall=True)

    assert ok is True
    commands = [c for c, _ in sudo_calls]
    assert commands[0] == ["tee", str(tmp_path / "openbrowse.service")]
    assert sudo_calls[0][1] == service.unit_content()
    assert "Rewrote" in message


def test_start_says_nothing_when_the_existing_unit_already_matches(tmp_path, sudo_calls, monkeypatch):
    monkeypatch.setattr(service, "_openbrowse_binary", lambda: "/usr/local/bin/openbrowse")
    (tmp_path / "openbrowse.service").write_text(service.unit_content())

    ok, message = service.start(tmp_path)

    assert ok is True
    assert "--reinstall-unit" not in message


def test_unit_content_names_the_invoking_user_not_root(monkeypatch, tmp_path):
    """`sudo openbrowse start` must not bake User=root and /root/.openbrowse into
    the unit, stranding the config the user actually wrote."""
    monkeypatch.setenv("SUDO_USER", "pi")
    monkeypatch.setattr(service, "_openbrowse_binary", lambda: "/usr/local/bin/openbrowse")

    assert "User=pi" in service.unit_content()


def test_binary_path_is_not_resolved_through_symlinks(monkeypatch, tmp_path):
    """An upgrade repoints the launcher symlink; a unit holding the resolved
    target would start a path that the upgrade just removed."""
    real = tmp_path / "versioned" / "openbrowse"
    real.parent.mkdir()
    real.write_text("")
    link = tmp_path / "openbrowse"
    link.symlink_to(real)
    monkeypatch.setattr(service.shutil, "which", lambda name: str(link))

    assert service._openbrowse_binary() == str(link)


def test_start_reports_enable_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service, "_sudo", lambda command, *, input_text=None: SimpleNamespace(returncode=1)
    )

    ok, message = service.start(tmp_path)

    assert ok is False


def test_stop_keeps_boot_start_by_default(sudo_calls):
    ok, message = service.stop(disable=False)

    assert ok is True
    assert [c for c, _ in sudo_calls] == [["systemctl", "stop", "openbrowse.service"]]
    assert "still start automatically on the next boot" in message


def test_stop_disable_removes_boot_start(sudo_calls):
    ok, message = service.stop(disable=True)

    assert ok is True
    assert [c for c, _ in sudo_calls] == [
        ["systemctl", "disable", "--now", "openbrowse.service"]
    ]
    assert "no longer start on boot" in message


def test_restart(sudo_calls):
    ok, _ = service.restart()

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
    monkeypatch.setattr(service, "start", lambda reinstall=False: (True, "boot message"))

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
