"""Application configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_BASE = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    # Auth
    api_key: str = field(default_factory=lambda: os.environ.get("API_KEY", ""))
    # Anthropic
    anthropic_api_key: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "")
    )
    # Capsolver
    capsolver_api_key: str = field(
        default_factory=lambda: os.environ.get("CAPSOLVER_API_KEY", "")
    )
    # Paths
    data_dir: Path = field(default_factory=lambda: _BASE / "data")
    db_path: Path = field(default_factory=lambda: _BASE / "data" / "browser_use.db")
    profiles_dir: Path = field(default_factory=lambda: _BASE / "data" / "profiles")
    screenshots_dir: Path = field(
        default_factory=lambda: _BASE / "data" / "screenshots"
    )
    # Limits
    max_concurrent_sessions: int = 3
    default_model: str = "claude-sonnet-4-6"
    # noVNC
    novnc_base_port: int = 6080
    vnc_base_port: int = 5900
    xvfb_base_display: int = 10
    cdp_base_port: int = 9222
    # Server
    host: str = "0.0.0.0"
    port: int = 8420


settings = Settings()

# Ensure directories exist
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.profiles_dir.mkdir(parents=True, exist_ok=True)
settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
