"""Dynamic, generic CAPTCHA subsystem with full CapSolver coverage."""

from __future__ import annotations

from app.agent.captcha.bridge import install_captcha_bridge
from app.agent.captcha.probe import probe_page, probe_strict
from app.agent.captcha.registry import all_strategies, detect_captcha, strategy_for
from app.agent.captcha.tools import register_captcha_tools

__all__ = [
    "register_captcha_tools",
    "install_captcha_bridge",
    "detect_captcha",
    "all_strategies",
    "strategy_for",
    "probe_page",
    "probe_strict",
]
