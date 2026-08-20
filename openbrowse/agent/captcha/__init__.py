"""Dynamic, generic CAPTCHA subsystem with full CapSolver coverage."""

from __future__ import annotations

from openbrowse.agent.captcha.bridge import install_captcha_bridge
from openbrowse.agent.captcha.probe import probe_page, probe_strict
from openbrowse.agent.captcha.registry import all_strategies, detect_captcha, strategy_for
from openbrowse.agent.captcha.tools import register_captcha_tools

__all__ = [
    "register_captcha_tools",
    "install_captcha_bridge",
    "detect_captcha",
    "all_strategies",
    "strategy_for",
    "probe_page",
    "probe_strict",
]
