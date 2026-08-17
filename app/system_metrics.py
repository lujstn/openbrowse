"""Host pressure sampling — load, memory and swap history for the dashboard
monitor and for stamping environmental context onto timing-sensitive failures.

Timing-sensitive browser work (OOPIF attach, embed href rewriting, consent
persistence) degrades invisibly when the host CPU is contended, and the
resulting failures masquerade as page errors. One shared sampler gives every
surface the same answer to "was the box struggling at that moment?".
"""

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

_SAMPLE_INTERVAL_S = 5.0
_HISTORY_MAX = 720

ELEVATED_LOAD_PER_CORE = 1.0
SATURATED_LOAD_PER_CORE = 1.25

_history: deque[dict[str, Any]] = deque(maxlen=_HISTORY_MAX)


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    values[key] = int(parts[0])
    except OSError:
        pass
    return values


def sample() -> dict[str, Any]:
    cores = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    mem = _meminfo()
    total_kb = mem.get("MemTotal", 0)
    available_kb = mem.get("MemAvailable", 0)
    swap_total_kb = mem.get("SwapTotal", 0)
    swap_free_kb = mem.get("SwapFree", 0)
    return {
        "t": time.time(),
        "load1": round(load1, 2),
        "cores": cores,
        "loadPerCore": round(load1 / cores, 3),
        "memUsedPct": (
            round(100.0 * (total_kb - available_kb) / total_kb, 1) if total_kb else None
        ),
        "swapUsedMb": (
            round((swap_total_kb - swap_free_kb) / 1024.0, 1) if swap_total_kb else None
        ),
    }


def pressure() -> tuple[str, dict[str, Any]]:
    """Current pressure level: 'ok', 'elevated' or 'saturated', with the sample
    that produced it. Thresholds are load-average per core: above 1.0 runnable
    tasks queue, above 1.25 timing-sensitive browser reads demonstrably miss
    their windows.
    """
    s = sample()
    if s["loadPerCore"] >= SATURATED_LOAD_PER_CORE:
        return "saturated", s
    if s["loadPerCore"] >= ELEVATED_LOAD_PER_CORE:
        return "elevated", s
    return "ok", s


def pressure_note() -> str:
    """One-line environmental stamp for telemetry, empty when the host is fine."""
    level, s = pressure()
    if level == "ok":
        return ""
    return (
        f" [host CPU {level}: load {s['load1']} on {s['cores']} cores — "
        "timing-sensitive embed reads are degraded; treat failures as "
        "environmental, not site changes]"
    )


def history() -> list[dict[str, Any]]:
    return list(_history)


async def sampler_loop() -> None:
    while True:
        try:
            _history.append(sample())
        except Exception:
            logger.debug("system metrics sample failed", exc_info=True)
        await asyncio.sleep(_SAMPLE_INTERVAL_S)
