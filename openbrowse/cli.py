"""The ``openbrowse`` command: serve the app, check for and install updates,
and run the host tuning script, from any install method."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from openbrowse import __version__


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from openbrowse.config import settings

    uvicorn.run(
        "openbrowse.main:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
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
        return _cmd_serve(args)
    ok, message = service.start()
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


def _cmd_tune(args: argparse.Namespace) -> int:
    from openbrowse import service

    script = Path(__file__).resolve().parent / "scripts" / "host_tune.sh"
    command = ["bash", str(script), "--share", args.share, "--service", service.UNIT_BASENAME]
    if args.dry_run:
        command.append("--dry-run")
    if os.geteuid() != 0:
        command = ["sudo", *command]
    os.execvp(command[0], command)


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
    start.add_argument("--host", default=None, help=argparse.SUPPRESS)
    start.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)
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
