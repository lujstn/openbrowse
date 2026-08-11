"""Import BU Cloud profile storage-state JSON into local profiles.

Usage (from the repo root, under the venv):
  python -m scripts.import_profiles FILE.json [FILE2.json ...] \
      [--profile-id ID] [--name NAME] [--no-backup]

A single Playwright storage_state file ({"cookies": [...], "origins": [...]}) needs a target
id: pass --profile-id, or name the file after a known profile (personal_profile*.json /
yc_profile*.json). A bundle (a JSON list, or {"profiles": [...]}) carries an id per entry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.models import init_db  # noqa: E402
from app.profiles.importer import ProfileImportError, import_bundle  # noqa: E402

_KNOWN_ID = {
    "personal_profile": "0bee43b4-d8c4-4741-8f1e-6576749a81b0",
    "yc_profile": "9ebd2c83-6790-426f-8575-e3c9164a37bc",
}
_KNOWN_NAME = {
    "personal_profile": "Personal Profile",
    "yc_profile": "YC Profile",
}


def _guess(path: Path) -> tuple[str | None, str | None]:
    stem = path.name.split(".")[0].lower()
    for key, pid in _KNOWN_ID.items():
        if stem.startswith(key):
            return pid, _KNOWN_NAME[key]
    return None, None


async def _run(args: argparse.Namespace) -> int:
    await init_db()
    results: list[dict] = []
    for f in args.files:
        path = Path(f)
        if not path.exists():
            print(f"! not found: {path}", file=sys.stderr)
            return 2
        data = json.loads(path.read_text())
        default_id, default_name = args.profile_id, args.name
        if default_id is None and isinstance(data, dict) and ("cookies" in data or "origins" in data):
            gid, gname = _guess(path)
            default_id = default_id or gid
            default_name = default_name or gname
        try:
            results.extend(
                await import_bundle(
                    data,
                    default_id=default_id,
                    default_name=default_name,
                    backup=not args.no_backup,
                )
            )
        except ProfileImportError as exc:
            print(f"! {path.name}: {exc}", file=sys.stderr)
            return 2

    print(f"{'ID':38}  {'NAME':18}  {'COOKIES':>7}  {'ORIGINS':>7}  DOMAINS")
    for r in results:
        print(
            f"{r['id']:38}  {(r['name'] or '—'):18}  {r['cookie_count']:>7}  "
            f"{r['origin_count']:>7}  {', '.join(r['domains'])}"
        )
    print(f"\n{len(results)} profile(s) imported.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Import BU Cloud profile cookies into local profiles.")
    ap.add_argument("files", nargs="+", help="storage_state JSON file(s) or a bundle")
    ap.add_argument("--profile-id", default=None, help="target id for a single-profile file")
    ap.add_argument("--name", default=None, help="profile name to set")
    ap.add_argument("--no-backup", action="store_true", help="do not write a .import-bak first")
    return asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
