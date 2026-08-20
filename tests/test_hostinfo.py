"""Host probe and concurrency recommendation tests — fixture-driven, no root."""

from dataclasses import replace

import openbrowse.hostinfo as hostinfo
from openbrowse.hostinfo import HostInfo, checklist, hard_max, probe, recommend, recommendations, summary


def _meminfo(total_kb: int, available_kb: int) -> str:
    return f"MemTotal: {total_kb} kB\nMemAvailable: {available_kb} kB\nSwapTotal: 0 kB\n"


def _fake_host(tmp_path, monkeypatch, *, cores, meminfo=None, load1=0.0,
               psi=False, pi_model=None, mounts=None, controllers=None,
               systemd=False, override=False):
    monkeypatch.setattr(hostinfo.os, "cpu_count", lambda: cores)
    monkeypatch.setattr(hostinfo.os, "getloadavg", lambda: (load1, load1, load1))

    def _file(name, content):
        p = tmp_path / name
        if content is not None:
            p.write_text(content)
        return str(p)

    monkeypatch.setattr(hostinfo, "_MEMINFO_PATH", _file("meminfo", meminfo))
    monkeypatch.setattr(
        hostinfo, "_PSI_CPU_PATH", _file("psi", "some avg10=0.00\n" if psi else None)
    )
    monkeypatch.setattr(hostinfo, "_DEVICE_TREE_MODEL", _file("model", pi_model))
    monkeypatch.setattr(hostinfo, "_CGROUP_CONTROLLERS", _file("controllers", controllers))
    monkeypatch.setattr(hostinfo, "_MOUNTS_PATH", _file("mounts", mounts))
    sysd = tmp_path / "systemd"
    if systemd:
        sysd.mkdir()
    monkeypatch.setattr(hostinfo, "_SYSTEMD_DIR", str(sysd))
    monkeypatch.setattr(
        hostinfo, "_CAPACITY_OVERRIDE", _file("override.conf", "x" if override else None)
    )


def test_probe_pi5_like(tmp_path, monkeypatch):
    _fake_host(
        tmp_path, monkeypatch,
        cores=4,
        meminfo=_meminfo(16 * 1024 * 1024, 13 * 1024 * 1024),
        load1=0.3,
        pi_model="Raspberry Pi 5 Model B Rev 1.1",
        mounts="/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n",
        controllers="cpuset cpu io memory pids",
        systemd=True,
    )
    info = probe()
    assert info.complete
    assert info.cores == 4
    assert info.is_raspberry_pi
    assert info.systemd
    assert info.cgroup_memory
    assert info.root_on_sd
    assert not info.psi_available
    assert not info.resource_limits_set
    assert hard_max(info) == 4
    assert recommend(info, "all") == 3
    assert recommend(info, "most") == 2
    assert recommend(info, "shared") == 1
    assert "4 cores" in summary(info) and "16GB" in summary(info)


def test_probe_tiny_pi_like(tmp_path, monkeypatch):
    _fake_host(
        tmp_path, monkeypatch,
        cores=1,
        meminfo=_meminfo(512 * 1024, 300 * 1024),
        pi_model="Raspberry Pi Model B Rev 2",
    )
    info = probe()
    assert hard_max(info) == 1
    assert all(v == 1 for v in recommendations(info).values())


def test_probe_vps_like(tmp_path, monkeypatch):
    _fake_host(
        tmp_path, monkeypatch,
        cores=16,
        meminfo=_meminfo(64 * 1024 * 1024, 60 * 1024 * 1024),
        mounts="/dev/vda1 / ext4 rw 0 0\n",
        controllers="memory",
        systemd=True,
    )
    info = probe()
    assert hard_max(info) == 8
    assert recommend(info, "all") == 8
    assert recommend(info, "shared") == 6
    assert not info.is_raspberry_pi
    assert not info.root_on_sd


def test_probe_degrades_off_linux(tmp_path, monkeypatch):
    _fake_host(tmp_path, monkeypatch, cores=8)
    info = probe()
    assert not info.complete
    assert hard_max(info) == 1
    assert recommend(info, "all") == 1
    assert "could not be detected" in summary(info)


def test_busy_host_tempers_recommendation(tmp_path, monkeypatch):
    _fake_host(
        tmp_path, monkeypatch,
        cores=4,
        meminfo=_meminfo(16 * 1024 * 1024, 13 * 1024 * 1024),
        load1=3.0,
    )
    info = probe()
    assert info.load1_per_core == 0.75
    assert recommend(info, "all") == 2
    assert recommend(info, "shared") == 1
    assert "busy" in summary(info)


def test_ram_bound_beats_core_bound(tmp_path, monkeypatch):
    _fake_host(
        tmp_path, monkeypatch,
        cores=8,
        meminfo=_meminfo(4 * 1024 * 1024, 3 * 1024 * 1024),
    )
    info = probe()
    assert hard_max(info) == 2
    assert recommend(info, "all") == 1


def _pi5_info(**over) -> HostInfo:
    base = HostInfo(
        cores=4, mem_total_kb=16 * 1024 * 1024, mem_available_kb=13 * 1024 * 1024,
        load1_per_core=0.1, psi_available=False, is_raspberry_pi=True,
        systemd=True, cgroup_memory=True, root_on_sd=True,
        resource_limits_set=False,
    )
    return replace(base, **over)


def test_checklist_states():
    rows = {r["key"]: r for r in checklist(_pi5_info())}
    assert rows["psi"]["state"] == "action"
    assert rows["limits"]["state"] == "action"
    assert rows["storage"]["state"] == "info"
    assert "cgroup" not in rows

    rows = {r["key"]: r for r in checklist(_pi5_info(psi_available=True, resource_limits_set=True))}
    assert rows["psi"]["state"] == "ok"
    assert rows["limits"]["state"] == "ok"

    rows = {r["key"]: r for r in checklist(_pi5_info(is_raspberry_pi=False, systemd=False, root_on_sd=False))}
    assert rows["psi"]["state"] == "na"
    assert rows["limits"]["state"] == "na"
    assert "storage" not in rows

    rows = {r["key"]: r for r in checklist(_pi5_info(cgroup_memory=False))}
    assert rows["cgroup"]["state"] == "info"


def test_light_flags_recommended_for_constrained_hosts():
    assert hostinfo.light_flags_recommended(_pi5_info()) is True

    big_vps = _pi5_info(
        is_raspberry_pi=False, cores=16,
        mem_total_kb=64 * 1024 * 1024, mem_available_kb=48 * 1024 * 1024,
    )
    assert hostinfo.light_flags_recommended(big_vps) is False

    few_cores = _pi5_info(is_raspberry_pi=False, cores=4, mem_total_kb=64 * 1024 * 1024)
    assert hostinfo.light_flags_recommended(few_cores) is True

    small_ram = _pi5_info(
        is_raspberry_pi=False, cores=8,
        mem_total_kb=8 * 1024 * 1024, mem_available_kb=6 * 1024 * 1024,
    )
    assert hostinfo.light_flags_recommended(small_ram) is True

    assert hostinfo.light_flags_recommended(_pi5_info(cores=0)) is False


def test_capacity_override_path_is_derived_from_the_unit_name():
    """The drop-in directory is named after the systemd unit, so the two must not be
    able to disagree: a rename that missed this would report tuning as still to do."""
    assert hostinfo.UNIT_NAME == "openbrowse.service"
    assert (
        hostinfo._CAPACITY_OVERRIDE
        == f"/etc/systemd/system/{hostinfo.UNIT_NAME}.d/50-capacity.conf"
    )
