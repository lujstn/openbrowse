import importlib
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


def test_cloakbrowser_stays_on_the_working_release_line():
    # 0.6.2 onwards imports a Playwright internal that current Playwright no
    # longer has, so `import cloakbrowser` fails; 0.7.0 also refuses to launch
    # without a licence key. Either way a fresh install cannot run a session.
    specifier = _requirement("cloakbrowser").specifier

    assert "0.5.8" in specifier
    assert "0.5.12" in specifier
    assert "0.6.2" not in specifier
    assert "0.7.0" not in specifier


def test_installed_cloakbrowser_exposes_what_the_launcher_calls():
    # CI resolves dependencies fresh, so this catches a new upstream release
    # that breaks on import before it reaches anyone's install.
    cloakbrowser = importlib.import_module("cloakbrowser")

    assert callable(cloakbrowser.ensure_binary)
    assert callable(cloakbrowser.get_default_stealth_args)
    assert callable(cloakbrowser.check_for_update)
