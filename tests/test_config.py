"""Tests for environment-derived settings parsing."""

import pytest

from openbrowse.config import _cost_factor


def test_cost_factor_defaults_to_one_when_unset(monkeypatch):
    monkeypatch.delenv("CLOUD_MAX_COST_FACTOR", raising=False)
    assert _cost_factor() == 1.0


def test_cost_factor_treats_blank_as_unset(monkeypatch):
    for raw in ("", "   "):
        monkeypatch.setenv("CLOUD_MAX_COST_FACTOR", raw)
        assert _cost_factor() == 1.0


def test_cost_factor_accepts_the_valid_range(monkeypatch):
    for raw, expected in (("0.5", 0.5), ("1", 1.0), ("0.01", 0.01)):
        monkeypatch.setenv("CLOUD_MAX_COST_FACTOR", raw)
        assert _cost_factor() == expected


def test_cost_factor_rejects_values_that_would_break_the_cap(monkeypatch):
    for raw in ("0", "-0.5", "5", "O.5", "50%", "inf", "-inf", "nan"):
        monkeypatch.setenv("CLOUD_MAX_COST_FACTOR", raw)
        with pytest.raises(ValueError, match="CLOUD_MAX_COST_FACTOR"):
            _cost_factor()


# OPENBROWSE_HOME picks which .env is read, so it is consulted before that file
# is loaded and can only ever be set in the environment.
_ENV_ONLY = {"OPENBROWSE_HOME"}
# SUDO_USER is set by sudo, not by anyone configuring OpenBrowse.
_NOT_A_SETTING = {"SUDO_USER"}
# Deliberately absent from the dashboard's variable list: the first two are
# owned by the Performance card's own controls, the rest are niche knobs that
# live in .env.example alone so the settings page stays approachable.
_NOT_IN_DASHBOARD = {
    "MAX_CONCURRENT_SESSIONS",
    "CHROME_LIGHT_FLAGS",
    "KEEP_ALIVE_IDLE_TIMEOUT",
    "UPDATE_CHECK_HOURS",
    "ALLOW_INSECURE_NO_AUTH",
}


def _settings_read_from_the_environment() -> set[str]:
    import pathlib
    import re

    source = (pathlib.Path(__file__).resolve().parent.parent / "openbrowse" / "config.py").read_text()
    return set(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', source))


def test_every_setting_is_reachable_from_both_places_a_user_looks():
    """A setting that changes behaviour but appears in neither the example file nor
    the dashboard is one nobody can find."""
    import pathlib

    from openbrowse.dashboard.routes import _ENV_GROUPS

    root = pathlib.Path(__file__).resolve().parent.parent
    example = (root / ".env.example").read_text()
    grouped = {name for _, keys in _ENV_GROUPS for name in keys}

    for name in _settings_read_from_the_environment() - _ENV_ONLY - _NOT_A_SETTING:
        assert f"\n{name}=" in example, f"{name} is missing from .env.example"
        if name not in _NOT_IN_DASHBOARD:
            assert name in grouped, f"{name} is missing from the dashboard's settings groups"


def test_the_dashboard_offers_no_setting_that_nothing_reads():
    """A field a user can set, save and restart for, which changes nothing, is
    worse than an absent one."""
    from openbrowse.dashboard.routes import _ENV_GROUPS

    read = _settings_read_from_the_environment()
    for _, keys in _ENV_GROUPS:
        for name in keys:
            assert name in read, f"the dashboard offers {name}, but config.py never reads it"


def test_env_only_settings_are_not_dashboard_editable():
    from openbrowse.dashboard.routes import _ENV_GROUPS, _ENV_NOT_EDITABLE

    grouped = {name for _, keys in _ENV_GROUPS for name in keys}
    for name in _ENV_ONLY:
        assert name not in grouped
        assert name in _ENV_NOT_EDITABLE


def test_example_file_ships_live_defaults_not_commented_out():
    """A commented-out setting is one a user has to know exists before they can
    find it, which defeats the point of the file."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for line in (root / ".env.example").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and "=" in stripped:
            key = stripped.lstrip("#").strip().split("=", 1)[0]
            assert not key.isupper() or not key.replace("_", "").isalnum(), (
                f"{key} is commented out in .env.example; give it its real default instead"
            )


def test_no_captcha_setting_is_read_by_nothing():
    """A dataclass field with no reader reads as a supported feature and is not."""
    import pathlib

    from openbrowse.config import Settings

    root = pathlib.Path(__file__).resolve().parent.parent
    sources = "\n".join(
        p.read_text()
        for p in (root / "openbrowse").rglob("*.py")
        if p.name != "config.py"
    )
    for name in vars(Settings()):
        if not name.startswith("captcha"):
            continue
        assert name in sources, f"settings.{name} is never read"


def test_update_check_hours_defaults(monkeypatch):
    from openbrowse.config import _update_check_hours

    monkeypatch.delenv("UPDATE_CHECK_HOURS", raising=False)
    assert _update_check_hours() == 6.0


def test_update_check_hours_zero_disables(monkeypatch):
    from openbrowse.config import _update_check_hours

    monkeypatch.setenv("UPDATE_CHECK_HOURS", "0")
    assert _update_check_hours() == 0.0


def test_update_check_hours_rejects_junk(monkeypatch):
    from openbrowse.config import _update_check_hours

    for raw in ("-1", "inf", "nan", "soon"):
        monkeypatch.setenv("UPDATE_CHECK_HOURS", raw)
        with pytest.raises(ValueError, match="UPDATE_CHECK_HOURS"):
            _update_check_hours()


def test_resolve_home_env_override(monkeypatch, tmp_path):
    from openbrowse.config import _resolve_home

    monkeypatch.setenv("OPENBROWSE_HOME", str(tmp_path / "custom"))
    assert _resolve_home() == (tmp_path / "custom").resolve()


def test_resolve_home_checkout_uses_repo_root(monkeypatch):
    import openbrowse.config as config

    from pathlib import Path

    monkeypatch.delenv("OPENBROWSE_HOME", raising=False)
    repo_root = Path(config.__file__).resolve().parent.parent
    assert config._resolve_home() == repo_root


def test_cwd_dotenv_is_not_read(tmp_path, monkeypatch):
    """The console script runs from anywhere, so a walk up from the working
    directory would let an unrelated project's .env supply this server's keys."""
    import importlib

    import openbrowse.config as config

    stranger = tmp_path / "someone-elses-project"
    stranger.mkdir()
    (stranger / ".env").write_text("MAX_CONCURRENT_SESSIONS=99\nAPI_KEY=stolen\n")
    monkeypatch.chdir(stranger)
    monkeypatch.delenv("MAX_CONCURRENT_SESSIONS", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("OPENBROWSE_HOME", str(tmp_path / "home"))

    importlib.reload(config)

    assert config.settings.max_concurrent_sessions == 1
    assert config.settings.api_key == ""


def test_home_follows_the_invoking_user_under_sudo(monkeypatch, tmp_path):
    """Under sudo, Path.home() is /root, and resolving there would run the
    service against an empty root-owned copy of the user's configuration."""
    import pwd

    import openbrowse.config as config

    monkeypatch.delenv("OPENBROWSE_HOME", raising=False)
    monkeypatch.setenv("SUDO_USER", "pi")
    monkeypatch.setattr(config, "checkout_root", lambda: None)
    monkeypatch.setattr(
        config.pwd, "getpwnam", lambda name: pwd.struct_passwd(
            ("pi", "x", 1000, 1000, "", str(tmp_path / "home" / "pi"), "/bin/sh")
        )
    )

    assert config._resolve_home() == tmp_path / "home" / "pi" / ".openbrowse"
    assert config.invoking_user() == "pi"
