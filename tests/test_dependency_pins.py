import tomllib
from pathlib import Path

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent


def _requirement(name: str) -> Requirement:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    for line in pyproject["project"]["dependencies"]:
        requirement = Requirement(line)
        if requirement.name == name:
            return requirement
    raise AssertionError(f"{name} is not a declared dependency")


def test_cloakbrowser_stays_below_the_licence_key_release():
    # CloakBrowser 0.7.0 refuses to launch a browser without a licence key, so a
    # fresh install that resolves to it cannot run a single session.
    specifier = _requirement("cloakbrowser").specifier

    assert "0.5.8" in specifier
    assert "0.6.5" in specifier
    assert "0.7.0" not in specifier
