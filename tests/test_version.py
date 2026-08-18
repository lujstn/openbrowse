import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.set_version import apply_version, parse_semver

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_version(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(), re.MULTILINE)
    assert match, f"no version match in {path}"
    return match.group(1)


def test_all_three_versions_agree():
    pyproject_version = _read_version(REPO_ROOT / "pyproject.toml", r'^version = "([^"]*)"$')
    citation_version = _read_version(REPO_ROOT / "CITATION.cff", r"^version: (.*)$")
    main_py_version = _read_version(REPO_ROOT / "app" / "main.py", r'version="([^"]*)"')

    assert pyproject_version == citation_version == main_py_version


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "1.0.0"\nrequires-python = ">=3.11"\n'
    )
    (tmp_path / "CITATION.cff").write_text(
        "cff-version: 1.2.0\ntitle: Example\nversion: 1.0.0\ndate-released: 2026-01-01\nlicense: MIT\n"
    )
    (tmp_path / "app" / "main.py").write_text(
        'from fastapi import FastAPI\n\napp = FastAPI(\n    title="Example",\n    version="1.0.0",\n)\n'
    )
    return tmp_path


def test_apply_version_updates_all_files(fixture_root: Path):
    changes = apply_version(fixture_root, "1.1.0", "2026-02-02")

    assert len(changes) == 4
    assert 'version = "1.1.0"' in (fixture_root / "pyproject.toml").read_text()
    citation_text = (fixture_root / "CITATION.cff").read_text()
    assert "version: 1.1.0" in citation_text
    assert "date-released: 2026-02-02" in citation_text
    assert 'version="1.1.0"' in (fixture_root / "app" / "main.py").read_text()


def test_apply_version_preserves_other_content(fixture_root: Path):
    apply_version(fixture_root, "1.1.0", "2026-02-02")

    assert 'name = "example"' in (fixture_root / "pyproject.toml").read_text()
    assert "cff-version: 1.2.0" in (fixture_root / "CITATION.cff").read_text()
    assert "from fastapi import FastAPI" in (fixture_root / "app" / "main.py").read_text()


def test_apply_version_zero_matches_fails_loudly(fixture_root: Path):
    (fixture_root / "app" / "main.py").write_text("app = object()\n")

    with pytest.raises(ValueError, match="pattern matched zero times"):
        apply_version(fixture_root, "1.1.0", "2026-02-02")


def test_apply_version_multiple_matches_fails_loudly(fixture_root: Path):
    (fixture_root / "app" / "main.py").write_text(
        'app = FastAPI(version="1.0.0")\nother = FastAPI(version="1.0.0")\n'
    )

    with pytest.raises(ValueError, match="pattern matched 2 times"):
        apply_version(fixture_root, "1.1.0", "2026-02-02")


@pytest.mark.parametrize("bad_version", ["1.3", "v1.3.0", "1.3.0.0", "1.3.x", ""])
def test_parse_semver_rejects_invalid(bad_version: str):
    with pytest.raises(ValueError):
        parse_semver(bad_version)


def test_parse_semver_accepts_valid():
    assert parse_semver("1.3.0") == (1, 3, 0)


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.set_version", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def cli_root(fixture_root: Path) -> Path:
    (fixture_root / "scripts").mkdir()
    (fixture_root / "scripts" / "__init__.py").write_text("")
    set_version_src = (REPO_ROOT / "scripts" / "set_version.py").read_text()
    (fixture_root / "scripts" / "set_version.py").write_text(set_version_src)
    return fixture_root


def test_cli_downgrade_rejected_without_force(cli_root: Path):
    result = _run_cli(["0.9.0"], cwd=cli_root)

    assert result.returncode == 1
    assert "lower than the current version" in result.stderr
    assert 'version = "1.0.0"' in (cli_root / "pyproject.toml").read_text()


def test_cli_equal_version_rejected_without_force(cli_root: Path):
    result = _run_cli(["1.0.0"], cwd=cli_root)

    assert result.returncode == 1
    assert "same as the current version" in result.stderr


def test_cli_equal_version_allowed_with_force(cli_root: Path):
    result = _run_cli(["1.0.0", "--force", "--date", "2026-03-03"], cwd=cli_root)

    assert result.returncode == 0
    assert "date-released: 2026-03-03" in (cli_root / "CITATION.cff").read_text()


def test_cli_dry_run_writes_nothing(cli_root: Path):
    before_pyproject = (cli_root / "pyproject.toml").read_text()
    before_citation = (cli_root / "CITATION.cff").read_text()
    before_main = (cli_root / "app" / "main.py").read_text()

    result = _run_cli(["1.5.0", "--dry-run"], cwd=cli_root)

    assert result.returncode == 0
    assert "Dry run" in result.stdout
    assert (cli_root / "pyproject.toml").read_text() == before_pyproject
    assert (cli_root / "CITATION.cff").read_text() == before_citation
    assert (cli_root / "app" / "main.py").read_text() == before_main


def test_cli_invalid_semver_rejected(cli_root: Path):
    result = _run_cli(["1.5"], cwd=cli_root)

    assert result.returncode == 1
    assert "not a valid semver" in result.stderr


def test_cli_downgrade_allowed_with_force(cli_root: Path):
    result = _run_cli(["0.9.0", "--force", "--date", "2026-03-03"], cwd=cli_root)

    assert result.returncode == 0
    assert 'version = "0.9.0"' in (cli_root / "pyproject.toml").read_text()


# @nonobvious(must-hold): run_release signs a commit, signs a tag, pushes to origin
# and opens a GitHub release, and git searches upward for a repository, so calling
# the real one from a test is only ever one TMPDIR away from publishing a release.
# Every release test stubs it out; none may reach git.
def test_release_dispatches_the_version_and_notes(monkeypatch):
    import scripts.set_version as sv

    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(sv, "read_current_version", lambda root: "1.0.0")
    monkeypatch.setattr(sv, "apply_version", lambda root, version, date_str: [])
    monkeypatch.setattr(
        sv, "run_release", lambda root, version, notes: calls.append((version, notes))
    )

    assert sv.main(["1.1.0", "--release", "--notes", "test notes"]) == 0
    assert calls == [("1.1.0", "test notes")]


def test_dry_run_never_releases(monkeypatch):
    import scripts.set_version as sv

    def _forbidden(*args, **kwargs):
        raise AssertionError("--dry-run must neither write nor release")

    monkeypatch.setattr(sv, "read_current_version", lambda root: "1.0.0")
    monkeypatch.setattr(sv, "apply_version", _forbidden)
    monkeypatch.setattr(sv, "run_release", _forbidden)

    assert sv.main(["1.1.0", "--dry-run", "--release"]) == 0
