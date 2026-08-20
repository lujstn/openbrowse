"""Manage OpenBrowse as a systemd service: the machinery behind
``openbrowse start`` / ``stop`` / ``restart`` / ``status``.

Privileged steps go through ``sudo`` per command rather than re-executing the
whole CLI as root, so the unit is generated with the invoking user's name,
home directory and binary path intact.
"""

from __future__ import annotations

import getpass
import shutil
import subprocess
import sys
from pathlib import Path

from openbrowse.config import settings

SYSTEMD_DIR = Path("/etc/systemd/system")
UNIT_BASENAME = "openbrowse.service"


def systemd_available() -> bool:
    return bool(shutil.which("systemctl")) and Path("/run/systemd/system").exists()


def _openbrowse_binary() -> str:
    found = shutil.which("openbrowse")
    if found:
        return str(Path(found).resolve())
    return f"{sys.executable} -m openbrowse.cli"


def unit_content() -> str:
    return (
        "[Unit]\n"
        "Description=OpenBrowse\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={getpass.getuser()}\n"
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


def start(systemd_dir: Path = SYSTEMD_DIR) -> tuple[bool, str]:
    """Install the unit if none exists, then enable and start it.

    Returns (ok, message). The message says plainly whether OpenBrowse will
    now start automatically on boot.
    """
    unit_path = systemd_dir / UNIT_BASENAME
    created = False
    if not unit_path.exists():
        result = _sudo(["tee", str(unit_path)], input_text=unit_content())
        if result.returncode != 0:
            return False, f"Could not write {unit_path}; is sudo available?"
        _sudo(["systemctl", "daemon-reload"])
        created = True
    result = _sudo(["systemctl", "enable", "--now", UNIT_BASENAME])
    if result.returncode != 0:
        return False, (
            f"systemctl enable --now {UNIT_BASENAME} failed with exit code {result.returncode}."
        )
    lines = []
    if created:
        lines.append(f"Registered OpenBrowse as a systemd service ({unit_path}).")
    lines.append(
        f"OpenBrowse is running on port {settings.port} and will now start "
        "automatically every time this machine boots."
    )
    lines.append(
        f"Open http://<this-host>:{settings.port} in a browser"
        " — a fresh install serves the one-time setup screen there."
    )
    lines.append("Manage it with: openbrowse status | restart | stop")
    return True, "\n".join(lines)


def stop(disable: bool) -> tuple[bool, str]:
    if disable:
        result = _sudo(["systemctl", "disable", "--now", UNIT_BASENAME])
    else:
        result = _sudo(["systemctl", "stop", UNIT_BASENAME])
    if result.returncode != 0:
        return False, f"systemctl failed with exit code {result.returncode}."
    if disable:
        return True, "OpenBrowse is stopped and will no longer start on boot."
    return True, (
        "OpenBrowse is stopped. It will still start automatically on the next "
        "boot; run `openbrowse stop --disable` to turn that off too."
    )


def restart() -> tuple[bool, str]:
    result = _sudo(["systemctl", "restart", UNIT_BASENAME])
    if result.returncode != 0:
        return False, f"systemctl restart {UNIT_BASENAME} failed with exit code {result.returncode}."
    return True, "OpenBrowse restarted."


def status() -> int:
    return subprocess.run(
        ["systemctl", "status", "--no-pager", UNIT_BASENAME]
    ).returncode
