"""Session plumbing for the live suite: env gating, fixture servers, preflight,
model matrix, budget ceiling and the end-of-run cost table.

Nothing here spends money until a scenario runs; both preflights exist so a
misconfigured environment fails in seconds instead of after a paid session.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import uvicorn

from tests.live.harness import LiveClient

BASE_URL = os.environ.get("OPENBROWSE_LIVE_URL", "")
API_KEY = os.environ.get("OPENBROWSE_LIVE_API_KEY", "")
BUDGET_USD = float(os.environ.get("LIVE_BUDGET_USD", "2.00"))
FIXTURE_PORT = int(os.environ.get("LIVE_FIXTURE_PORT", "8621"))
FRAMES_PORT = int(os.environ.get("LIVE_FIXTURE_PORT2", "8622"))

DEFAULT_MODELS = "gpt-5.6-terra:none"
SONNET_SMOKE_MODEL = ("claude-sonnet-5", "high")


def _parse_models(raw: str) -> list[tuple[str, str]]:
    pairs = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        model, _, effort = chunk.partition(":")
        pairs.append((model, effort or "default"))
    return pairs


BASE_MODELS = _parse_models(os.environ.get("LIVE_MODELS", DEFAULT_MODELS))
SONNET_ENABLED = os.environ.get("LIVE_SONNET", "1") != "0"


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _lan_ip() -> str:
    """The address the OpenBrowse host can plausibly reach this machine on."""
    target = urlparse(BASE_URL).hostname or "8.8.8.8"
    if _is_loopback(target):
        return "127.0.0.1"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def pytest_generate_tests(metafunc):
    if "model_effort" not in metafunc.fixturenames:
        return
    params = list(BASE_MODELS)
    if SONNET_ENABLED and metafunc.definition.get_closest_marker("sonnet_smoke"):
        if SONNET_SMOKE_MODEL not in params:
            params.append(SONNET_SMOKE_MODEL)
    metafunc.parametrize(
        "model_effort", params, ids=[f"{m}-{e}" for m, e in params]
    )


@pytest.fixture(scope="session")
def live_env() -> None:
    if not BASE_URL or not API_KEY:
        pytest.skip(
            "live suite needs OPENBROWSE_LIVE_URL and OPENBROWSE_LIVE_API_KEY "
            "pointing at a running OpenBrowse server"
        )


class _Server:
    def __init__(self, app, port: int) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fixture server failed to start")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture(scope="session")
def fixture_urls(live_env) -> tuple[str, str]:
    """(main site base URL, frames base URL) as the OpenBrowse host will see them."""
    from tests.live.fixture_site.app import build_app

    lan = _lan_ip()
    main_public = os.environ.get("LIVE_FIXTURE_PUBLIC_URL", f"http://{lan}:{FIXTURE_PORT}")
    frames_public = os.environ.get("LIVE_FIXTURE_PUBLIC_URL2", f"http://{lan}:{FRAMES_PORT}")

    server_host = urlparse(BASE_URL).hostname or ""
    if not _is_loopback(server_host) and _is_loopback(urlparse(main_public).hostname or ""):
        pytest.exit(
            f"OPENBROWSE_LIVE_URL points at {server_host} but the fixture site URL "
            f"{main_public} is loopback — the server's browser could never reach it. "
            "Set LIVE_FIXTURE_PUBLIC_URL (and LIVE_FIXTURE_PUBLIC_URL2) to an address "
            "the server can reach."
        )

    frames = _Server(build_app(frame_base=""), FRAMES_PORT)
    main = _Server(build_app(frame_base=frames_public), FIXTURE_PORT)
    frames.start()
    main.start()
    try:
        for public in (main_public, frames_public):
            try:
                resp = httpx.get(f"{public}/health", timeout=5.0)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                pytest.exit(
                    f"fixture site preflight failed for {public}/health: {e}. "
                    "If the OpenBrowse server is remote, check this machine's firewall "
                    "allows inbound connections, or set LIVE_FIXTURE_PUBLIC_URL."
                )
        yield main_public, frames_public
    finally:
        main.stop()
        frames.stop()


@pytest.fixture(scope="session")
def fixture_url(fixture_urls) -> str:
    return fixture_urls[0]


@pytest.fixture(scope="session")
def client(live_env) -> LiveClient:
    c = LiveClient(BASE_URL, API_KEY)
    try:
        c.preflight()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            pytest.exit(
                "OPENBROWSE_LIVE_API_KEY was rejected (401). Aborting before the "
                "per-IP auth throttle locks the whole run out."
            )
        raise
    except httpx.HTTPError as e:
        pytest.exit(f"cannot reach OpenBrowse at {BASE_URL}: {e}")
    yield c
    c.close()


@pytest.fixture(scope="session")
def artifact_root() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(__file__).parent / "artifacts" / stamp


class _Ledger:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, float, int, str]] = []

    @property
    def total(self) -> float:
        return sum(r[2] for r in self.rows)

    def charge(self, scenario: str, model: str, trace) -> None:
        self.rows.append(
            (
                scenario,
                model,
                trace.total_cost_usd,
                int(trace.session.get("stepCount") or 0),
                str(trace.session.get("status")),
            )
        )
        if self.total >= BUDGET_USD:
            pytest.exit(
                f"live budget ceiling reached: ${self.total:.2f} of "
                f"${BUDGET_USD:.2f} (LIVE_BUDGET_USD). Stopping before spending more."
            )


@pytest.fixture(scope="session")
def ledger(request) -> _Ledger:
    ledger = _Ledger()
    request.config._live_ledger = ledger
    return ledger


@pytest.fixture
def run_scenario(client, ledger, artifact_root, model_effort, request):
    """Run one scenario on the parametrised model and account for its cost."""
    model, effort = model_effort

    def _run(name: str, task: str, **kwargs):
        # One CDP screenshot flake costs the run over a minute in watchdog
        # retries; the ceiling must survive one of those without masking a hang.
        timeout_s = kwargs.pop("timeout_s", 360.0)
        if model.startswith("claude"):
            timeout_s *= 2
        trace = client.run_scenario(
            name=name,
            task=task,
            model=model,
            reasoning_effort=effort,
            artifact_dir=artifact_root,
            timeout_s=timeout_s,
            **kwargs,
        )
        ledger.charge(name, f"{model}:{effort}", trace)
        return trace

    return _run


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    ledger = getattr(config, "_live_ledger", None)
    if ledger is None or not ledger.rows:
        return
    tr = terminalreporter
    tr.section("live suite cost")
    for scenario, model, cost, steps, status in ledger.rows:
        tr.line(f"{scenario:28s} {model:24s} ${cost:7.4f} {steps:3d} steps {status}")
    tr.line(f"{'TOTAL':28s} {'':24s} ${ledger.total:7.4f} (ceiling ${BUDGET_USD:.2f})")
