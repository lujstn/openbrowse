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
    captcha_proxy: str = field(
        default_factory=lambda: os.environ.get("CAPTCHA_PROXY", "")
    )
    # @nonobvious(must-hold): the address a solve is billed against comes from the
    # page, which can name any site it likes, so spending is capped by default and
    # only an explicit setting may widen or remove the ceiling.
    captcha_cost_cap_usd: float = field(
        default_factory=lambda: float(os.environ.get("CAPTCHA_MAX_COST_USD") or 1.0)
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
