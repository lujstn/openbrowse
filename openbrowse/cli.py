"""The ``openbrowse`` command: serve the app, check for and install updates,
and run the host tuning script, from any install method."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from openbrowse import __version__


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from openbrowse.config import settings

    uvicorn.run(
        "openbrowse.main:app",
        # @nonobvious(must-hold): `is None`, not `or`. Port 0 is the request for
        # an ephemeral port, and `or` would quietly serve the default instead.
        host=settings.host if args.host is None else args.host,
        port=settings.port if args.port is None else args.port,
    )
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    from openbrowse import service

    if not service.systemd_available():
        print(
            "systemd is not available on this machine, so OpenBrowse cannot be "
            "registered to start automatically on boot. Running in the "
            "foreground instead (Ctrl+C stops it)."
        )
        return _cmd_serve(argparse.Namespace(host=None, port=None))
    ok, message = service.start(reinstall=args.reinstall_unit)
    print(message)
    return 0 if ok else 1


def _cmd_stop(args: argparse.Namespace) -> int:
    from openbrowse import service

    if not service.systemd_available():
        print("systemd is not available; stop the foreground process with Ctrl+C.")
        return 1
    ok, message = service.stop(disable=args.disable)
    print(message)
    return 0 if ok else 1


def _cmd_restart(_: argparse.Namespace) -> int:
    from openbrowse import service

    if not service.systemd_available():
        print("systemd is not available; restart the foreground process by hand.")
        return 1
    ok, message = service.restart()
    print(message)
    return 0 if ok else 1


def _cmd_status(_: argparse.Namespace) -> int:
    from openbrowse import service

    if not service.systemd_available():
        print("systemd is not available; there is no managed service to inspect.")
        return 1
    return service.status()


def _cmd_version(_: argparse.Namespace) -> int:
    print(f"openbrowse {__version__}")
    return 0


def _cmd_check_update(_: argparse.Namespace) -> int:
    from openbrowse import updates

    state = asyncio.run(updates.check_once())
    if state.error:
        print(f"Update check failed: {state.error}", file=sys.stderr)
        return 1
    if state.available:
        print(f"Update available: {state.current} -> {state.latest}")
        print("Run `openbrowse update` to install it.")
        return 0
    print(f"openbrowse {state.current} is up to date (latest: {state.latest}).")
    return 0


def _cmd_update(_: argparse.Namespace) -> int:
    from openbrowse import updates

    state = asyncio.run(updates.check_once())
    if state.error:
        print(f"Warning: update check failed ({state.error}); trying anyway.", file=sys.stderr)
    elif not state.available:
        print(f"openbrowse {state.current} is already the latest release.")
        return 0
    ok, log = asyncio.run(updates.install_update())
    print(log)
    if ok:
        print("Updated. Restart the server (or systemd service) to run the new version.")
        return 0
    return 1


def _cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove everything OpenBrowse put on this machine, then the package itself."""
    import shutil
    import subprocess as sp

    from openbrowse import updates
    from openbrowse.config import settings
    from openbrowse.hostinfo import UNIT_NAME

    unit_path = Path("/etc/systemd/system") / UNIT_NAME
    dropin_dir = Path(f"/etc/systemd/system/{UNIT_NAME}.d")
    sudoers_file = (
        Path(os.environ.get("HT_SUDOERS_DIR", "/etc/sudoers.d")) / "openbrowse-hosttune"
    )
    cmdline_path = Path(os.environ.get("HT_CMDLINE", "/boot/firmware/cmdline.txt"))
    data_dir = settings.home_dir
    cache_dir = Path(os.environ.get("CLOAKBROWSER_CACHE_DIR", "") or (Path.home() / ".cloakbrowser"))

    method, _ = updates.detect_install_method()
    if method == "checkout":
        package_line = "the checkout itself is left alone; delete the clone yourself"
    else:
        package_line = f"the installed package (via {method})"

    # @nonobvious(forced-by): /etc/sudoers.d is 0750 root-only, so for a normal
    # user even asking whether the grant file exists raises PermissionError.
    # None means "cannot tell"; removal handles it with an unconditional rm -f.
    unit_present = _maybe_exists(unit_path)
    dropin_present = _maybe_exists(dropin_dir)
    sudoers_present = _maybe_exists(sudoers_file)

    def _planned(present: bool | None, label: str) -> str | None:
        if present is False:
            return None
        return label + (" (if present)" if present is None else "")

    plan = [
        _planned(unit_present, f"systemd service and unit ({unit_path})"),
        _planned(dropin_present, f"capacity drop-in ({dropin_dir})"),
        _planned(sudoers_present, f"tuning sudoers grant ({sudoers_file})"),
        "psi=1 boot flag" if _cmdline_has_psi(cmdline_path) else None,
        None if args.keep_data else f"all data including .env and profiles ({data_dir})",
        None if args.keep_browser else f"the downloaded browser ({cache_dir})",
        package_line,
    ]
    print("This removes:")
    for item in plan:
        if item:
            print(f"  - {item}")
    if not args.yes:
        answer = input("Type 'uninstall' to continue: ").strip().lower()
        if answer != "uninstall":
            print("Aborted; nothing was removed.")
            return 1

    def run(cmd: list[str]) -> bool:
        try:
            result = sp.run(cmd)
        except OSError as exc:
            print(f"  failed: {' '.join(cmd)} ({exc})", file=sys.stderr)
            return False
        if result.returncode != 0:
            print(f"  failed ({result.returncode}): {' '.join(cmd)}", file=sys.stderr)
            return False
        return True

    failures = 0
    if unit_present is not False or dropin_present is not False or sudoers_present is not False:
        print("Removing the service (sudo may prompt for your password)...")
        if unit_present is not False:
            run(["sudo", "systemctl", "disable", "--now", UNIT_NAME])
            if not run(["sudo", "rm", "-f", str(unit_path)]):
                failures += 1
        if sudoers_present is not False and not run(["sudo", "rm", "-f", str(sudoers_file)]):
            failures += 1
        if dropin_present is not False and not run(["sudo", "rm", "-rf", str(dropin_dir)]):
            failures += 1
        run(["sudo", "systemctl", "daemon-reload"])
    if _cmdline_has_psi(cmdline_path):
        print(f"Reverting psi=1 in {cmdline_path}...")
        content = cmdline_path.read_text()
        reverted = content.replace(" psi=1", "", 1)
        if not run(["sudo", "sh", "-c", f"printf %s {_shquote(reverted)} > {_shquote(str(cmdline_path))}"]):
            failures += 1

    if not args.keep_data and data_dir.exists():
        print(f"Deleting {data_dir}...")
        shutil.rmtree(data_dir, ignore_errors=True)
    if not args.keep_browser and cache_dir.exists():
        print(f"Deleting {cache_dir}...")
        shutil.rmtree(cache_dir, ignore_errors=True)

    remove_cmd = _package_remove_command(method)
    if method == "checkout":
        print("This copy runs from a git checkout; delete the clone directory yourself when ready.")
    elif remove_cmd is None:
        print(
            "Could not work out how this copy was installed; remove the package "
            "yourself (e.g. pipx uninstall openbrowse or pip uninstall openbrowse).",
            file=sys.stderr,
        )
        failures += 1
    else:
        print(f"Removing the package: {' '.join(remove_cmd)}")
        if not run(remove_cmd):
            failures += 1

    if failures:
        print(f"Done, with {failures} step(s) failed; see above.", file=sys.stderr)
        return 1
    print("OpenBrowse is fully uninstalled.")
    return 0


def _maybe_exists(path: Path) -> bool | None:
    try:
        return path.exists()
    except OSError:
        return None


def _cmdline_has_psi(cmdline_path: Path) -> bool:
    try:
        return " psi=1" in cmdline_path.read_text()
    except OSError:
        return False


def _shquote(text: str) -> str:
    import shlex

    return shlex.quote(text)


def _package_remove_command(method: str) -> list[str] | None:
    from openbrowse.updates import _pip_available, _pipx_binary, _uv_binary

    if method == "pipx":
        pipx = _pipx_binary()
        return [pipx, "uninstall", "openbrowse"] if pipx else None
    if method == "uv-tool":
        uv = _uv_binary()
        return [uv, "tool", "uninstall", "openbrowse"] if uv else None
    if method == "pip" and _pip_available():
        return [sys.executable, "-m", "pip", "uninstall", "-y", "openbrowse"]
    return None


def _cmd_tune(args: argparse.Namespace) -> int:
    from openbrowse.hostinfo import UNIT_NAME

    script = Path(__file__).resolve().parent / "scripts" / "host_tune.sh"
    command = ["bash", str(script), "--share", args.share, "--service", UNIT_NAME]
    if args.dry_run:
        command.append("--dry-run")
    if os.geteuid() != 0:
        command = ["sudo", *command]
    try:
        # @nonobvious(forced-by): output is left uncaptured so sudo's password
        # prompt reaches the terminal.
        return subprocess.run(command).returncode
    except OSError as exc:
        print(f"Could not run {command[0]}: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openbrowse",
        description="Self-hosted AI browser automation server with a live dashboard.",
    )
    parser.add_argument(
        "--version", action="version", version=f"openbrowse {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser(
        "start",
        help="run as a service that starts automatically on boot (systemd)",
    )
    start.add_argument(
        "--reinstall-unit",
        action="store_true",
        help="overwrite the existing systemd unit with a freshly generated one",
    )
    start.set_defaults(func=_cmd_start)

    stop = sub.add_parser("stop", help="stop the service")
    stop.add_argument(
        "--disable", action="store_true", help="also stop it starting on boot"
    )
    stop.set_defaults(func=_cmd_stop)

    restart = sub.add_parser("restart", help="restart the service")
    restart.set_defaults(func=_cmd_restart)

    status = sub.add_parser("status", help="show the service status")
    status.set_defaults(func=_cmd_status)

    serve = sub.add_parser("serve", help="run the server in the foreground")
    serve.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    serve.add_argument("--port", type=int, default=None, help="port (default 8420)")
    serve.set_defaults(func=_cmd_serve)

    version = sub.add_parser("version", help="print the installed version")
    version.set_defaults(func=_cmd_version)

    check = sub.add_parser("check-update", help="check PyPI for a newer release")
    check.set_defaults(func=_cmd_check_update)

    update = sub.add_parser("update", help="upgrade to the latest release")
    update.set_defaults(func=_cmd_update)

    uninstall = sub.add_parser(
        "uninstall",
        help="remove the service, data, browser and package from this machine",
    )
    uninstall.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    uninstall.add_argument("--keep-data", action="store_true", help="keep .env, the database and profiles")
    uninstall.add_argument("--keep-browser", action="store_true", help="keep the downloaded Chromium build")
    uninstall.set_defaults(func=_cmd_uninstall)

    tune = sub.add_parser("tune", help="size the host for OpenBrowse (Linux, needs sudo)")
    tune.add_argument("--share", default="most", choices=["all", "most", "shared"])
    tune.add_argument("--dry-run", action="store_true")
    tune.set_defaults(func=_cmd_tune)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        print(
            "\nMost people want: openbrowse start"
            "  (runs now and on every boot)"
        )
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
