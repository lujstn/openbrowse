"""Single command to set OpenBrowse's version everywhere it is declared.

Usage:
    python -m scripts.set_version 1.3.0 [--date YYYY-MM-DD] [--dry-run] [--release]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

PYPROJECT_VERSION_RE = re.compile(r'^version = "[^"]*"$', re.MULTILINE)
CITATION_VERSION_RE = re.compile(r"^version: .*$", re.MULTILINE)
CITATION_DATE_RE = re.compile(r"^date-released: .*$", re.MULTILINE)
MAIN_PY_VERSION_RE = re.compile(r'version="[^"]*"')


def parse_semver(text: str) -> tuple[int, int, int]:
    match = SEMVER_RE.match(text)
    if not match:
        raise ValueError(f"{text!r} is not a valid semver X.Y.Z version")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _replace_exactly_once(pattern: re.Pattern[str], replacement: str, text: str, label: str) -> str:
    count = len(pattern.findall(text))
    if count == 0:
        raise ValueError(f"{label}: pattern matched zero times, refusing to write")
    if count > 1:
        raise ValueError(f"{label}: pattern matched {count} times, refusing to write")
    return pattern.sub(replacement, text)


def apply_version(root: Path, version: str, date_str: str) -> list[str]:
    """Rewrite the three version-bearing files under root. Returns change summary lines."""
    changes: list[str] = []

    pyproject_path = root / "pyproject.toml"
    pyproject_text = pyproject_path.read_text()
    old_match = re.search(r'^version = "([^"]*)"$', pyproject_text, re.MULTILINE)
    old_version = old_match.group(1) if old_match else "?"
    new_pyproject_text = _replace_exactly_once(
        PYPROJECT_VERSION_RE, f'version = "{version}"', pyproject_text, "pyproject.toml"
    )
    changes.append(f"pyproject.toml: version {old_version} -> {version}")

    citation_path = root / "CITATION.cff"
    citation_text = citation_path.read_text()
    old_cit_version = re.search(r"^version: (.*)$", citation_text, re.MULTILINE)
    old_cit_version_str = old_cit_version.group(1) if old_cit_version else "?"
    citation_text = _replace_exactly_once(
        CITATION_VERSION_RE, f"version: {version}", citation_text, "CITATION.cff (version)"
    )
    old_cit_date = re.search(r"^date-released: (.*)$", citation_text, re.MULTILINE)
    old_cit_date_str = old_cit_date.group(1) if old_cit_date else "?"
    new_citation_text = _replace_exactly_once(
        CITATION_DATE_RE, f"date-released: {date_str}", citation_text, "CITATION.cff (date-released)"
    )
    changes.append(f"CITATION.cff: version {old_cit_version_str} -> {version}")
    changes.append(f"CITATION.cff: date-released {old_cit_date_str} -> {date_str}")

    main_py_path = root / "app" / "main.py"
    main_py_text = main_py_path.read_text()
    old_main_match = MAIN_PY_VERSION_RE.search(main_py_text)
    old_main_version = old_main_match.group(0).split('"')[1] if old_main_match else "?"
    new_main_py_text = _replace_exactly_once(
        MAIN_PY_VERSION_RE, f'version="{version}"', main_py_text, "app/main.py"
    )
    changes.append(f"app/main.py: version {old_main_version} -> {version}")

    pyproject_path.write_text(new_pyproject_text)
    citation_path.write_text(new_citation_text)
    main_py_path.write_text(new_main_py_text)

    return changes


def read_current_version(root: Path) -> str:
    pyproject_text = (root / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]*)"$', pyproject_text, re.MULTILINE)
    if not match:
        raise ValueError("pyproject.toml: could not find a version = \"...\" line")
    return match.group(1)


def run_release(root: Path, version: str, notes: str | None) -> None:
    tag = f"v{version}"

    def run(cmd: list[str], **kwargs: object) -> None:
        print("+ " + " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=root, **kwargs)

    run(["git", "add", "pyproject.toml", "CITATION.cff", "app/main.py"])
    run(["git", "commit", "-S", "-m", f"chore: {tag}"])
    run(["git", "tag", "-s", tag, "-m", f"OpenBrowse {tag}"])
    run(["git", "push", "origin", "HEAD"])
    run(["git", "push", "origin", tag])

    release_cmd = ["gh", "release", "create", tag, "--title", f"OpenBrowse {tag}", "--notes-file", "-"]
    print("+ " + " ".join(release_cmd))
    subprocess.run(release_cmd, check=True, cwd=root, input=(notes or ""), text=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set OpenBrowse's version everywhere it is declared.")
    parser.add_argument("version", help="new version, semver X.Y.Z")
    parser.add_argument("--date", default=None, help="release date, YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="print the changes without writing")
    parser.add_argument("--force", action="store_true", help="allow setting an equal (non-increasing) version")
    parser.add_argument("--release", action="store_true", help="commit, tag, push and publish a GitHub release")
    parser.add_argument("--notes", default=None, help="release notes for --release (or pipe via stdin)")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent

    try:
        new_parts = parse_semver(args.version)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    current_version = read_current_version(root)
    current_parts = parse_semver(current_version)

    if new_parts < current_parts:
        print(
            f"Error: {args.version} is lower than the current version {current_version}. "
            "Use --force to override.",
            file=sys.stderr,
        )
        return 1
    if new_parts == current_parts and not args.force:
        print(
            f"Error: {args.version} is the same as the current version {current_version}. "
            "Pass --force to set it anyway.",
            file=sys.stderr,
        )
        return 1

    date_str = args.date or date.today().isoformat()

    if args.dry_run:
        print(f"Dry run: would set version to {args.version} (date-released {date_str})")
        for label, current in (
            ("pyproject.toml: version", current_version),
            ("CITATION.cff: version", current_version),
            ("app/main.py: version", current_version),
        ):
            print(f"  {label} {current} -> {args.version}")
        return 0

    changes = apply_version(root, args.version, date_str)
    print(f"Version set to {args.version}:")
    for change in changes:
        print(f"  {change}")

    if args.release:
        notes = args.notes
        if notes is None and not sys.stdin.isatty():
            notes = sys.stdin.read()
        run_release(root, args.version, notes)

    return 0


if __name__ == "__main__":
    sys.exit(main())
