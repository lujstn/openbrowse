"""Browser binary prefetch: state machine, progress mirroring, host checks."""

import asyncio
import logging
import shutil
from types import SimpleNamespace

import pytest

from openbrowse import prefetch


@pytest.fixture(autouse=True)
def reset_prefetch_state(monkeypatch):
    monkeypatch.setattr(prefetch, "_task", None)
    monkeypatch.setattr(
        prefetch, "_state", {"state": "idle", "detail": "", "percent": None}
    )
    event = asyncio.Event()
    event.set()
    monkeypatch.setattr(prefetch, "_settled", event)


async def test_start_reaches_ready(monkeypatch):
    monkeypatch.setattr(prefetch, "_fetch_binary_blocking", lambda: None)
    prefetch.start()
    await prefetch._task
    assert prefetch.status()["state"] == "ready"
    assert prefetch.is_ready()


async def test_start_records_error_and_allows_retry(monkeypatch):
    calls = []

    def failing():
        calls.append(1)
        raise RuntimeError("network unplugged")

    monkeypatch.setattr(prefetch, "_fetch_binary_blocking", failing)
    prefetch.start()
    await prefetch._task
    assert prefetch.status()["state"] == "error"
    assert "network unplugged" in prefetch.status()["detail"]

    monkeypatch.setattr(prefetch, "_fetch_binary_blocking", lambda: None)
    prefetch.start()
    await prefetch._task
    assert prefetch.status()["state"] == "ready"
    assert calls == [1]


async def test_progress_lines_are_mirrored(monkeypatch):
    def chatty():
        cb = logging.getLogger("cloakbrowser")
        cb.warning("Download progress: 49% (97/198 MB)")
        assert prefetch.status()["state"] == "downloading"
        assert prefetch.status()["percent"] == 49
        assert prefetch.status()["detail"] == "97/198 MB"
        cb.warning("Extracting to /tmp/somewhere")
        assert prefetch.status()["state"] == "extracting"

    monkeypatch.setattr(prefetch, "_fetch_binary_blocking", chatty)
    prefetch.start()
    await prefetch._task
    assert prefetch.status()["state"] == "ready"
    assert prefetch.status()["percent"] == 100


async def test_start_is_idempotent_while_running(monkeypatch):
    release = asyncio.Event()
    started = []

    def slow():
        started.append(1)
        # runs in an executor thread; a tiny beat is enough for the second start()
        import time

        time.sleep(0.05)

    monkeypatch.setattr(prefetch, "_fetch_binary_blocking", slow)
    prefetch.start()
    prefetch.start()
    await prefetch._task
    assert started == [1]
    del release


async def test_wait_until_settled_blocks_until_fetch_ends(monkeypatch):
    import time

    monkeypatch.setattr(prefetch, "_fetch_binary_blocking", lambda: time.sleep(0.05))
    prefetch.start()
    await prefetch.wait_until_settled(timeout=5)
    assert prefetch.status()["state"] == "ready"


async def test_wait_until_settled_is_noop_when_nothing_started():
    await prefetch.wait_until_settled(timeout=0.1)


def test_host_checks_flag_low_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: SimpleNamespace(total=0, used=0, free=512 * 1024 * 1024)
    )
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/Xvfb")
    checks = {c["key"]: c for c in prefetch.host_checks(tmp_path)}
    assert not checks["disk"]["ok"]
    assert "2GB" in checks["disk"]["detail"]
    assert checks["xvfb"]["ok"]


def test_host_checks_flag_missing_xvfb(monkeypatch, tmp_path):
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: SimpleNamespace(total=0, used=0, free=64 * 1024**3)
    )
    monkeypatch.setattr(shutil, "which", lambda _: None)
    checks = {c["key"]: c for c in prefetch.host_checks(tmp_path)}
    assert checks["disk"]["ok"]
    assert not checks["xvfb"]["ok"]
    assert "apt install" in checks["xvfb"]["detail"]
