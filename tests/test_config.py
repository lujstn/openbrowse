"""Tests for environment-derived settings parsing."""

import pytest

from app.config import _cost_factor


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
