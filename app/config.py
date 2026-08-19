"""Application configuration from environment variables."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_BASE = Path(__file__).resolve().parent.parent


def _cost_factor() -> float:
    raw = os.environ.get("CLOUD_MAX_COST_FACTOR", "")
    if not raw.strip():
        return 1.0
    limits = "must be a number greater than 0 and at most 1"
    try:
        factor = float(raw)
    except ValueError:
        raise ValueError(f"CLOUD_MAX_COST_FACTOR {limits}; got {raw!r}.") from None
    if not math.isfinite(factor) or not 0 < factor <= 1:
        raise ValueError(f"CLOUD_MAX_COST_FACTOR {limits}; got {raw!r}.")
    return factor


def _keep_alive_idle_timeout() -> int:
    """Seconds a finished keep-alive session waits, browser still open, for the
    next follow-up before it closes itself. 0 parks indefinitely — until the
    session is stopped or its display slot is claimed by a new session.
    """
    raw = os.environ.get("KEEP_ALIVE_IDLE_TIMEOUT", "").strip()
    if not raw:
        return 600
    try:
        seconds = int(raw)
    except ValueError:
        raise ValueError(
            f"KEEP_ALIVE_IDLE_TIMEOUT must be a whole number of seconds; got {raw!r}."
        ) from None
    if seconds < 0:
        raise ValueError(f"KEEP_ALIVE_IDLE_TIMEOUT must not be negative; got {raw!r}.")
    return seconds


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default_factory=lambda: os.environ.get("API_KEY", ""))
    dashboard_user: str = field(
        default_factory=lambda: os.environ.get("DASHBOARD_USER", "admin")
    )
    dashboard_password: str = field(
        default_factory=lambda: os.environ.get("DASHBOARD_PASSWORD", "")
    )
    allow_insecure_no_auth: bool = field(
        default_factory=lambda: os.environ.get("ALLOW_INSECURE_NO_AUTH", "").lower()
        in ("1", "true", "yes")
    )
    anthropic_api_key: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "")
    )
    openai_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENAI_API_KEY", "")
    )
    capsolver_api_key: str = field(
        default_factory=lambda: os.environ.get("CAPSOLVER_API_KEY", "")
    )
    # @nonobvious(must-hold): the address a solve is billed against comes from the
    # page, which can name any site it likes, so spending is capped by default and
    # only an explicit setting may widen or remove the ceiling.
    # @nonobvious(means): the dearest tier the solver publishes is $3 per thousand,
    # so the default buys ten solves in a session and no task costs more than a
    # thirtieth of it.
    captcha_cost_cap_usd: float = field(
        default_factory=lambda: float(os.environ.get("CAPTCHA_MAX_COST_USD") or 0.03)
    )
    data_dir: Path = field(default_factory=lambda: _BASE / "data")
    db_path: Path = field(default_factory=lambda: _BASE / "data" / "browser_use.db")
    profiles_dir: Path = field(default_factory=lambda: _BASE / "data" / "profiles")
    screenshots_dir: Path = field(
        default_factory=lambda: _BASE / "data" / "screenshots"
    )
    max_concurrent_sessions: int = field(
        default_factory=lambda: int(os.environ.get("MAX_CONCURRENT_SESSIONS", "1"))
    )
    keep_alive_idle_timeout: int = field(default_factory=_keep_alive_idle_timeout)
    cloud_max_cost_factor: float = field(default_factory=_cost_factor)
    default_model: str = "claude-sonnet-5"
    stale_session_minutes: int = 15
    reconcile_interval_seconds: int = 60
    novnc_base_port: int = 6080
    vnc_base_port: int = 5900
    xvfb_base_display: int = 10
    cdp_base_port: int = 9222
    host: str = "0.0.0.0"
    port: int = 8420


settings = Settings()

settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.profiles_dir.mkdir(parents=True, exist_ok=True)
settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
