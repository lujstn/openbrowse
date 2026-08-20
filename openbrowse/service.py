"""Manage OpenBrowse as a systemd service: the machinery behind
``openbrowse start`` / ``stop`` / ``restart`` / ``status``.

Privileged steps go through ``sudo`` per command rather than re-executing the
whole CLI as root, so the unit is generated with the invoking user's name,
home directory and binary path intact.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from openbrowse.config import invoking_user, settings
from openbrowse.hostinfo import UNIT_NAME

SYSTEMD_DIR = Path("/etc/systemd/system")

_EXEC_START_RE = re.compile(r"^ExecStart=(.*)$", re.MULTILINE)


def systemd_available() -> bool:
    return bool(shutil.which("systemctl")) and Path("/run/systemd/system").exists()


def _openbrowse_binary() -> str:
    found = shutil.which("openbrowse")
    if found:
        # @nonobvious(deliberately-missing): no .resolve(). Where the launcher is
        # a symlink into a versioned directory, an upgrade repoints it, and a
        # unit holding the resolved target would start a path that no longer
        # exists after exactly the upgrade that moved it.
        return found
    return f"{sys.executable} -m openbrowse.cli"


def _exec_start(unit_text: str) -> str | None:
    match = _EXEC_START_RE.search(unit_text)
    return match.group(1).strip() if match else None


def unit_content() -> str:
    return (
        "[Unit]\n"
        "Description=OpenBrowse\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={invoking_user()}\n"
        f"WorkingDirectory={settings.home_dir}\n"
        f"EnvironmentFile=-{settings.env_path}\n"
        f"ExecStart={_openbrowse_binary()} serve\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _sudo(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    # @nonobvious(forced-by): sudo may need the user's password, so it must own
    # the terminal; captured pipes would leave the prompt invisible and hung.
    return subprocess.run(["sudo", *command], input=input_text, text=True)


def start(systemd_dir: Path = SYSTEMD_DIR, *, reinstall: bool = False) -> tuple[bool, str]:
    """Install the unit if none exists, then enable and start it.

    Returns (ok, message). The message says plainly whether OpenBrowse will
    now start automatically on boot.
    """
    unit_path = systemd_dir / UNIT_NAME
    existing = None
    if unit_path.exists():
        try:
            existing = unit_path.read_text()
        except OSError:
            existing = ""
    notes: list[str] = []
    if existing is None or reinstall:
        result = _sudo(["tee", str(unit_path)], input_text=unit_content())
        if result.returncode != 0:
            return False, f"Could not write {unit_path}; is sudo available?"
        _sudo(["systemctl", "daemon-reload"])
        notes.append(
            f"{'Rewrote' if existing is not None else 'Registered OpenBrowse as'}"
            f" a systemd service ({unit_path})."
        )
    else:
        # @nonobvious(deliberately-missing): a differing unit is reported, never
        # rewritten. Hand-written units carry Tailscale hooks and resource limits
        # this generator knows nothing about, and "start" must not discard them.
        live, expected = _exec_start(existing), _exec_start(unit_content())
        if live is not None and expected is not None and live != expected:
            notes.append(
                f"Note: {unit_path} starts `{live}`, but this copy of OpenBrowse "
                f"would start `{expected}`. Keeping your unit. Run "
                "`openbrowse start --reinstall-unit` to replace it."
            )
    result = _sudo(["systemctl", "enable", "--now", UNIT_NAME])
    if result.returncode != 0:
        return (
            False,
            f"systemctl enable --now {UNIT_NAME} failed with exit code {result.returncode}.",
        )
    lines = notes
    lines.append(
        f"OpenBrowse is running on port {settings.port} and will now start "
        "automatically every time this machine boots."
    )
    lines.append(
        f"Open http://<this-host>:{settings.port} in a browser, where a fresh "
        "install serves the one-time setup screen."
    )
    lines.append("Manage it with: openbrowse status | restart | stop")
    return True, "\n".join(lines)


def stop(disable: bool) -> tuple[bool, str]:
    if disable:
        result = _sudo(["systemctl", "disable", "--now", UNIT_NAME])
    else:
        result = _sudo(["systemctl", "stop", UNIT_NAME])
    if result.returncode != 0:
        return False, f"systemctl failed with exit code {result.returncode}."
    if disable:
        return True, "OpenBrowse is stopped and will no longer start on boot."
    return True, (
        "OpenBrowse is stopped. It will still start automatically on the next "
        "boot; run `openbrowse stop --disable` to turn that off too."
    )


def restart() -> tuple[bool, str]:
    result = _sudo(["systemctl", "restart", UNIT_NAME])
    if result.returncode != 0:
        return False, f"systemctl restart {UNIT_NAME} failed with exit code {result.returncode}."
    return True, "OpenBrowse restarted."


def status() -> int:
    return subprocess.run(["systemctl", "status", "--no-pager", UNIT_NAME]).returncode
