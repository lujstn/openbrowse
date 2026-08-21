# Live tool-coverage suite

Real-browser, real-LLM integration tests: one short, cheap scenario per agent tool
(or small tool cluster), asserting the agent used the target tool **correctly** —
no workarounds, no doom loops, no silent tool errors. Deliberately **not** part of
CI: every run spends real money. `pytest -q` never touches it (`addopts = -m 'not
live'` in pyproject.toml).

## Prerequisites

- A running OpenBrowse server (needs Linux/Xvfb — a Raspberry Pi, VPS, or a local
  Linux box; the suite only talks to it over HTTP, so macOS is fine for the tests
  themselves).
- The server should run with `CLOUD_MAX_COST_FACTOR=1` so per-scenario cost
  ceilings mean what they say.
- Provider keys on the server for the models you run (`OPENAI_API_KEY` for the
  default terra lane, `ANTHROPIC_API_KEY` for the sonnet smoke lane).
- The fixture site is served **by the test process** on ports 8621/8622 (two ports
  so iframe pages are genuinely cross-origin) and must be reachable from the
  server's browser. Same machine: works out of the box. Server elsewhere on the
  LAN: check this machine's firewall allows inbound connections on those ports.
  Server in Docker: set the public URLs to something the container can reach
  (e.g. `http://host.docker.internal:8621`).

## Environment

| Variable | Meaning |
| --- | --- |
| `OPENBROWSE_LIVE_URL` | Base URL of the running server, e.g. `http://localhost:8420`. Unset → the whole suite skips. |
| `OPENBROWSE_LIVE_API_KEY` | The server's API key. Checked once up front; a bad key aborts before the auth throttle locks the run out. |
| `LIVE_MODELS` | Comma list of `model:effort` for the base matrix. Default `gpt-5.6-terra:none` (the cheapest configuration). |
| `LIVE_SONNET` | `0` disables the extra `claude-sonnet-5:high` run of scenarios marked `sonnet_smoke`. On by default. |
| `LIVE_BUDGET_USD` | Hard ceiling for the whole run, default `2.00`. Crossing it aborts the session. |
| `LIVE_CAPTCHA` | `1` enables the paid captcha scenario. Set it only when the **server** has `CAPSOLVER_API_KEY` — the harness cannot see the server's env. |
| `LIVE_FIXTURE_PORT` / `LIVE_FIXTURE_PORT2` | Fixture site ports, default 8621/8622. |
| `LIVE_FIXTURE_PUBLIC_URL` / `LIVE_FIXTURE_PUBLIC_URL2` | How the *server's browser* reaches the fixture site. Defaults to this machine's LAN address; override for Docker/NAT setups. |

## Running

```bash
# cheapest single scenario first — proves the whole pipeline
OPENBROWSE_LIVE_URL=http://localhost:8420 OPENBROWSE_LIVE_API_KEY=... \
  pytest -m live tests/live -k http_fetch

# full matrix on the cheap model, plus the sonnet smoke lane
pytest -m live tests/live

# just the sonnet smoke scenarios
pytest -m "live and sonnet_smoke" tests/live

# one family
pytest -m live tests/live/test_schema.py
```

Scenarios run serially (the server defaults to `MAX_CONCURRENT_SESSIONS=1`). A
cost table prints at the end of the run.

## Reading a failure

Every scenario saves its full session export (session + complete message log) to
`tests/live/artifacts/<timestamp>/<scenario>-<model>.json`; the assertion message
names the file. The trace summary in the failure output shows which tools ran
(`used`), which errored, the judge intervention count, and `max_repeat_run` — the
longest streak of identical calls (same action, same arguments). A streak of 3+
fails the scenario even when the run eventually succeeded: that is the doom-loop
class that motivated this suite (a `find_elements` call repeated nine times with
identical arguments because the output guard hid its result).

## What "pass" means

- `isTaskSuccessful` with no `failureKind` (a budget-exceeded salvage does not
  count as a pass) and a clean completion summary.
- The target tool actually executed (requested-but-aborted actions do not count),
  with the workaround paths explicitly asserted absent where relevant.
- The built-in reviewer never had to intervene (`judge_rounds == 0`).
- Extracted values match the fixture site's ground truth exactly
  (`tests/live/fixture_site/__init__.py` — pages are rendered from the same
  constants the assertions use).
- Public-site scenarios (`test_live_sites.py`, the captcha demo) are deliberately
  tolerant: they prove the tool runs cleanly rather than pinning third-party copy.

Flakiness is signal here, not noise: agent runs are nondeterministic, so a
scenario that fails intermittently on the loop detector is worth investigating,
not retrying into silence.
