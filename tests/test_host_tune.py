"""host_tune.sh behaviour via its HT_* test seams — no root, no real paths."""

import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "host_tune.sh"


def _run(tmp_path, *args, meminfo_kb=16 * 1024 * 1024, pi=True, psi=False):
    env = dict(os.environ)
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir(exist_ok=True)
    cmdline = tmp_path / "cmdline.txt"
    if not cmdline.exists():
        cmdline.write_text("console=tty1 root=PARTUUID=x rootwait\n")
    sudoers_dir = tmp_path / "sudoers.d"
    sudoers_dir.mkdir(exist_ok=True)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: {meminfo_kb} kB\n")
    model = tmp_path / "model"
    if pi:
        model.write_text("Raspberry Pi 5 Model B")
    psi_file = tmp_path / "psi"
    if psi:
        psi_file.write_text("some avg10=0.00\n")
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir(exist_ok=True)
    for tool in ("systemctl", "visudo"):
        stub = stub_bin / tool
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    env.update(
        HT_SYSTEMD_DIR=str(systemd_dir),
        HT_CMDLINE=str(cmdline),
        HT_SUDOERS_DIR=str(sudoers_dir),
        HT_MEMINFO=str(meminfo),
        HT_PSI_CPU=str(psi_file),
        HT_DEVICE_TREE_MODEL=str(model),
        PATH=f"{stub_bin}:/usr/bin:/bin",
    )
    proc = subprocess.run(
        ["bash", str(_SCRIPT), *args], env=env, capture_output=True, text=True
    )
    return proc, systemd_dir, cmdline, sudoers_dir


def test_dry_run_writes_nothing(tmp_path):
    proc, systemd_dir, cmdline, sudoers_dir = _run(tmp_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "would: write" in proc.stdout
    assert "would: append psi=1" in proc.stdout
    assert not list(systemd_dir.rglob("*.conf"))
    assert "psi=1" not in cmdline.read_text()
    assert not list(sudoers_dir.iterdir())


def test_applies_and_is_idempotent(tmp_path):
    proc, systemd_dir, cmdline, sudoers_dir = _run(tmp_path, "--share", "shared")
    assert proc.returncode == 0, proc.stderr
    override = systemd_dir / "openbrowse.service.d" / "50-capacity.conf"
    content = override.read_text()
    assert "CPUWeight=300" in content
    assert "MemoryHigh=6553M" in content
    line = cmdline.read_text()
    assert line.count("psi=1") == 1
    assert line.count("\n") == 1
    sudoers = sudoers_dir / "openbrowse-hosttune"
    assert "NOPASSWD" in sudoers.read_text()
    assert str(_SCRIPT) in sudoers.read_text()

    again, *_ = _run(tmp_path, "--share", "shared")
    assert again.returncode == 0
    assert "ok: resource limits already set" in again.stdout
    assert "ok: psi=1 already" in again.stdout
    assert "ok: sudoers entry already present" in again.stdout
    assert cmdline.read_text().count("psi=1") == 1


def test_share_changes_rewrite_override(tmp_path):
    _run(tmp_path, "--share", "shared")
    proc, systemd_dir, _, _ = _run(tmp_path, "--share", "all")
    assert proc.returncode == 0
    content = (systemd_dir / "openbrowse.service.d" / "50-capacity.conf").read_text()
    assert "MemoryHigh=14745M" in content


def test_psi_skipped_when_present(tmp_path):
    proc, _, cmdline, _ = _run(tmp_path, psi=True)
    assert proc.returncode == 0
    assert "ok: PSI already available" in proc.stdout
    assert "psi=1" not in cmdline.read_text()


def test_psi_skipped_when_not_pi(tmp_path):
    proc, _, cmdline, _ = _run(tmp_path, pi=False)
    assert proc.returncode == 0
    assert "not a Raspberry Pi" in proc.stdout
    assert "psi=1" not in cmdline.read_text()


def test_unknown_share_rejected(tmp_path):
    proc, *_ = _run(tmp_path, "--share", "everything")
    assert proc.returncode == 2


def test_explicit_service_flag_still_wins(tmp_path):
    proc, systemd_dir, _, _ = _run(tmp_path, "--service", "custom.service")
    assert proc.returncode == 0, proc.stderr
    assert (systemd_dir / "custom.service.d" / "50-capacity.conf").exists()
