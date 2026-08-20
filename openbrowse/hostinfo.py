"""Host hardware probe and concurrency recommendation for onboarding.

Concurrency that suits a Raspberry Pi 1 strands a 16-core VPS and vice versa,
so the setup screen and the settings capacity card read the machine itself:
how many cores and how much memory it has, how busy it already is, and which
host-level tuning (PSI, systemd resource limits) is available or still to do.
Every reader degrades to a safe default off Linux.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MEMINFO_PATH = "/proc/meminfo"
_PSI_CPU_PATH = "/proc/pressure/cpu"
_DEVICE_TREE_MODEL = "/proc/device-tree/model"
_CGROUP_CONTROLLERS = "/sys/fs/cgroup/cgroup.controllers"
_MOUNTS_PATH = "/proc/mounts"
_SYSTEMD_DIR = "/run/systemd/system"
_CAPACITY_OVERRIDE = "/etc/systemd/system/openbrowse.service.d/50-capacity.conf"

SHARE_PRESETS: dict[str, float] = {"all": 0.9, "most": 0.7, "shared": 0.4}

_SESSION_RAM_KB = 2 * 1024 * 1024
_HARD_MAX_CEILING = 8
_BUSY_LOAD_PER_CORE = 0.5
_LIGHT_FLAGS_MAX_CORES = 4
_LIGHT_FLAGS_MAX_MEM_KB = 8 * 1024 * 1024


@dataclass(frozen=True)
class HostInfo:
    cores: int
    mem_total_kb: int
    mem_available_kb: int
    load1_per_core: float
    psi_available: bool
    is_raspberry_pi: bool
    systemd: bool
    cgroup_memory: bool
    root_on_sd: bool
    resource_limits_set: bool

    @property
    def complete(self) -> bool:
        """True when enough was read to bound and recommend concurrency."""
        return self.cores > 0 and self.mem_total_kb > 0


def _read_text(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def _meminfo_kb() -> tuple[int, int]:
    total = available = 0
    raw = _read_text(_MEMINFO_PATH)
    if raw:
        for line in raw.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if not parts:
                continue
            try:
                value = int(parts[0])
            except ValueError:
                continue
            if key == "MemTotal":
                total = value
            elif key == "MemAvailable":
                available = value
    return total, available


def probe() -> HostInfo:
    cores = os.cpu_count() or 0
    mem_total_kb, mem_available_kb = _meminfo_kb()
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    controllers = _read_text(_CGROUP_CONTROLLERS) or ""
    model = _read_text(_DEVICE_TREE_MODEL) or ""
    root_device = ""
    mounts = _read_text(_MOUNTS_PATH) or ""
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "/":
            root_device = parts[0]
            break
    return HostInfo(
        cores=cores,
        mem_total_kb=mem_total_kb,
        mem_available_kb=mem_available_kb,
        load1_per_core=round(load1 / cores, 3) if cores else 0.0,
        psi_available=os.path.exists(_PSI_CPU_PATH),
        is_raspberry_pi="raspberry pi" in model.lower(),
        systemd=os.path.isdir(_SYSTEMD_DIR),
        cgroup_memory="memory" in controllers.split(),
        root_on_sd=root_device.startswith("/dev/mmcblk"),
        resource_limits_set=Path(_CAPACITY_OVERRIDE).exists(),
    )


def hard_max(info: HostInfo) -> int:
    """Upper bound for the concurrency slider: one core and 2GB per session,
    never above the port/display slot sanity ceiling.
    """
    if not info.complete:
        return 1
    by_ram = info.mem_total_kb // _SESSION_RAM_KB
    return max(1, min(info.cores, by_ram, _HARD_MAX_CEILING))


def recommend(info: HostInfo, share: str) -> int:
    """Suggested concurrency for a share preset, tempered by how busy the box
    already is at probe time (a host running other services gets one less).
    """
    fraction = SHARE_PRESETS.get(share, SHARE_PRESETS["most"])
    if not info.complete:
        return 1
    by_cores = int(info.cores * fraction)
    by_ram = int(info.mem_available_kb * fraction) // _SESSION_RAM_KB
    value = min(by_cores, by_ram)
    if info.load1_per_core > _BUSY_LOAD_PER_CORE:
        value -= 1
    return max(1, min(value, hard_max(info)))


def recommendations(info: HostInfo) -> dict[str, int]:
    return {share: recommend(info, share) for share in SHARE_PRESETS}


def light_flags_recommended(info: HostInfo) -> bool:
    """True where the lighter browser profile is worth pre-selecting: small
    boards and small hosts, where Chromium's GPU process and per-site renderer
    fan-out cost more under a virtual display than they give back.
    """
    if not info.complete:
        return False
    return (
        info.is_raspberry_pi
        or info.cores <= _LIGHT_FLAGS_MAX_CORES
        or info.mem_total_kb <= _LIGHT_FLAGS_MAX_MEM_KB
    )


def summary(info: HostInfo) -> str:
    if not info.complete:
        return "hardware could not be detected on this platform"
    gb = round(info.mem_total_kb / (1024 * 1024))
    busy = "busy" if info.load1_per_core > _BUSY_LOAD_PER_CORE else "quiet"
    pi = " · Raspberry Pi" if info.is_raspberry_pi else ""
    return f"{info.cores} cores · {gb}GB RAM · currently {busy}{pi}"


def checklist(info: HostInfo) -> list[dict[str, Any]]:
    """Host-tuning status rows for the setup and settings screens. State is
    'ok' (done), 'action' (host_tune.sh fixes it), 'info' (worth knowing),
    'na' (not applicable here).
    """
    rows: list[dict[str, Any]] = []
    if info.psi_available:
        rows.append(
            {
                "key": "psi",
                "label": "CPU pressure metrics (PSI)",
                "state": "ok",
                "detail": "Kernel pressure stall information is active.",
            }
        )
    elif info.is_raspberry_pi:
        rows.append(
            {
                "key": "psi",
                "label": "CPU pressure metrics (PSI)",
                "state": "action",
                "detail": (
                    "Available but disabled on this kernel. The tuning script "
                    "adds psi=1 to the boot configuration; takes effect after "
                    "a reboot. Until then load average is used instead."
                ),
            }
        )
    else:
        rows.append(
            {
                "key": "psi",
                "label": "CPU pressure metrics (PSI)",
                "state": "na",
                "detail": "Not exposed by this kernel; load average is used instead.",
            }
        )
    if not info.systemd:
        rows.append(
            {
                "key": "limits",
                "label": "Service resource limits",
                "state": "na",
                "detail": "Not running under systemd; limits are not managed here.",
            }
        )
    elif info.resource_limits_set:
        rows.append(
            {
                "key": "limits",
                "label": "Service resource limits",
                "state": "ok",
                "detail": "CPU weight and memory ceiling are configured.",
            }
        )
    else:
        rows.append(
            {
                "key": "limits",
                "label": "Service resource limits",
                "state": "action",
                "detail": (
                    "CPU weight keeps sessions responsive when the machine is "
                    "shared, and a memory ceiling protects the host. The "
                    "tuning script sets both."
                ),
            }
        )
    if info.systemd and not info.cgroup_memory:
        rows.append(
            {
                "key": "cgroup",
                "label": "Memory controller",
                "state": "info",
                "detail": (
                    "The cgroup memory controller is not active, so a memory "
                    "ceiling would not be enforced. Check the kernel boot "
                    "options if you want it."
                ),
            }
        )
    if info.root_on_sd:
        rows.append(
            {
                "key": "storage",
                "label": "Storage",
                "state": "info",
                "detail": (
                    "The system runs from an SD card. Browsing caches already "
                    "stay in memory; heavy use still ages the card slowly."
                ),
            }
        )
    return rows
