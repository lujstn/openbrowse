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
    script = Path(__file__).resolve().parent / "scripts" / "host_tune.sh"
    command = ["bash", str(script), "--share", args.share]
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

    serve = sub.add_parser("serve", help="run the server (the default)")
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
        args.host = None
        args.port = None
        return _cmd_serve(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
