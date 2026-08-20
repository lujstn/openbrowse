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


def test_every_captcha_setting_is_reachable_from_both_places_a_user_looks():
    """A setting that changes behaviour but appears in neither the example file nor
    the dashboard is one nobody can find, spend ceiling included."""
    import pathlib

    from openbrowse.dashboard.routes import _ENV_GROUPS

    root = pathlib.Path(__file__).resolve().parent.parent
    example = (root / ".env.example").read_text()
    group = dict(_ENV_GROUPS)["CAPTCHA solving"]
    for name in ("CAPSOLVER_API_KEY", "CAPTCHA_MAX_COST_USD"):
        assert name in example, f"{name} is missing from .env.example"
        assert name in group, f"{name} is missing from the dashboard's CAPTCHA group"


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
