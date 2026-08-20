"""Strategy registry and detection dispatcher.

Adding a captcha type is one strategy class plus @register; nothing else in the
tree changes. Detection runs every strategy's pure detect over one probe snapshot
and ranks whatever claims it. The probe names a single challenge today, so the
ranking has nothing to choose between; it is what keeps file order from deciding
if a probe ever reports more than one candidate.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from browser_use import BrowserSession

from openbrowse.agent.captcha.base import CaptchaStrategy, Detection
from openbrowse.agent.captcha.probe import probe_page

logger = logging.getLogger(__name__)

_STRATEGIES: list[CaptchaStrategy] = []
_BY_KIND: dict[str, CaptchaStrategy] = {}
_LOADED = False


def register(cls):
    inst = cls()
    if not inst.kind:
        raise ValueError(f"{cls.__name__} has no kind")
    if inst.kind in _BY_KIND:
        raise ValueError(f"duplicate captcha kind: {inst.kind}")
    _STRATEGIES.append(inst)
    _BY_KIND[inst.kind] = inst
    return cls


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    importlib.import_module("openbrowse.agent.captcha.strategies")
    _LOADED = True


def all_strategies() -> list[CaptchaStrategy]:
    _ensure_loaded()
    return list(_STRATEGIES)


def strategy_for(kind: str) -> CaptchaStrategy | None:
    _ensure_loaded()
    return _BY_KIND.get(kind)


def detect_from_probe(probe: dict[str, Any]) -> Detection | None:
    """Rank every strategy's detection of one probe snapshot; highest wins."""
    _ensure_loaded()
    hits: list[Detection] = []
    for strat in _STRATEGIES:
        try:
            det = strat.detect(probe)
        except Exception:
            logger.debug("%s.detect raised", strat.kind, exc_info=True)
            det = None
        if det is not None:
            hits.append(det)
    if not hits:
        return None
    hits.sort(
        key=lambda d: (d.confidence, getattr(_BY_KIND.get(d.kind), "priority", 0)),
        reverse=True,
    )
    return hits[0]


async def detect_captcha(browser_session: BrowserSession) -> Detection | None:
    probe = await probe_page(browser_session)
    if not probe:
        return None
    return detect_from_probe(probe)
