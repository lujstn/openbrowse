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
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

_SAMPLE_INTERVAL_S = 5.0
_HISTORY_MAX = 720

ELEVATED_LOAD_PER_CORE = 1.0
SATURATED_LOAD_PER_CORE = 1.25

# @nonobvious(means): percent of wall time tasks stalled waiting on CPU (PSI
# avg10) — preferred over loadavg, which counts a run's own rendering tabs as
# pressure even when nothing is actually waiting.
ELEVATED_STALL_PCT = 15.0
SATURATED_STALL_PCT = 30.0

# @nonobvious(means): absent unless the kernel booted with psi=1
# (CONFIG_PSI_DEFAULT_DISABLED=y on Raspberry Pi OS), so the loadavg fallback
# is the live path until that cmdline change is deployed.
_PSI_CPU_PATH = "/proc/pressure/cpu"

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


def _psi_cpu_some_avg10() -> float | None:
    try:
        with open(_PSI_CPU_PATH) as fh:
            for line in fh:
                parts = line.split()
                if parts and parts[0] == "some":
                    for field in parts[1:]:
                        key, _, value = field.partition("=")
                        if key == "avg10":
                            return float(value)
    except (OSError, ValueError):
        pass
    return None


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
    stall_pct = _psi_cpu_some_avg10()
    return {
        "t": time.time(),
        "load1": round(load1, 2),
        "cores": cores,
        "loadPerCore": round(load1 / cores, 3),
        "cpuStallPct": round(stall_pct, 2) if stall_pct is not None else None,
        "memUsedPct": (
            round(100.0 * (total_kb - available_kb) / total_kb, 1) if total_kb else None
        ),
        "swapUsedMb": (
            round((swap_total_kb - swap_free_kb) / 1024.0, 1) if swap_total_kb else None
        ),
    }


def stall_fraction() -> float:
    """Fraction of CPU capacity currently stalled, 0.0 to 1.0. PSI when the
    kernel exposes it, else a proxy derived from load per core (load at or
    below 1.0/core maps to 0, 2.0/core and above maps to 1).
    """
    stall_pct = _psi_cpu_some_avg10()
    if stall_pct is not None:
        return min(max(stall_pct / 100.0, 0.0), 1.0)
    s = sample()
    return min(max(s["loadPerCore"] - 1.0, 0.0), 1.0)


def pressure() -> tuple[str, dict[str, Any]]:
    """Current pressure level: 'ok', 'elevated' or 'saturated', with the sample
    that produced it. PSI stall thresholds when available (stall time is what
    actually makes timing-sensitive browser reads miss their windows), else
    load-average per core: above 1.0 runnable tasks queue, above 1.25 reads
    demonstrably miss.
    """
    s = sample()
    stall_pct = s.get("cpuStallPct")
    if stall_pct is not None:
        if stall_pct >= SATURATED_STALL_PCT:
            return "saturated", s
        if stall_pct >= ELEVATED_STALL_PCT:
            return "elevated", s
        return "ok", s
    if s["loadPerCore"] >= SATURATED_LOAD_PER_CORE:
        return "saturated", s
    if s["loadPerCore"] >= ELEVATED_LOAD_PER_CORE:
        return "elevated", s
    return "ok", s


# @nonobvious(means): a ContextVar, not a module global, because sessions run
# concurrently in one process, and a later launch's baseline must not rewrite
# how an earlier session's stamps attribute its own contention.
_baseline_level: ContextVar[str] = ContextVar("pressure_baseline", default="ok")


def mark_baseline() -> tuple[str, dict[str, Any]]:
    """Record the host's pressure at session launch. The mid-run stamps use it
    to tell outside contention apart from the run's own browser work — a wave
    of rendering tabs legitimately saturates a small host by itself, and
    calling that "environmental" would mislead every audit.
    """
    level, s = pressure()
    _baseline_level.set(level)
    return level, s


def pressure_note() -> str:
    """One-line pressure stamp for telemetry, empty when the host is fine."""
    level, s = pressure()
    if level == "ok":
        return ""
    if s.get("cpuStallPct") is not None:
        signal = f"{s['cpuStallPct']}% stall"
    else:
        signal = f"load {s['load1']}"
    if _baseline_level.get() == "ok":
        return (
            f" [host CPU {level} (largely this run's own browser work): {signal} "
            f"on {s['cores']} cores — timing-sensitive embed reads "
            "may need the retry passes]"
        )
    return (
        f" [host CPU {level} since launch: {signal} on {s['cores']} "
        "cores — timing-sensitive embed reads are degraded; treat failures as "
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
