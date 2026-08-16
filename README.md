# OpenBrowse

**The open-source Browser Use Cloud alternative.** Self-host AI browser agents on a Raspberry Pi or any VPS, drive them through the same v3 REST API the `browser-use-sdk` already speaks, and watch every run live in a real browser. Built on top of the [Browser Use](https://github.com/browser-use/browser-use) SDK. Cheaper than the cloud, and on our benchmark, faster and better.

[openbrowse.co](https://openbrowse.co)

## Why OpenBrowse over BU Cloud?

| | BU Cloud | OpenBrowse |
|---|---|---|
| Hosting | Managed, per-task pricing | Your hardware, you pay only LLM tokens |
| How it works | Code-first: the agent scripts its way through pages | Visual-first: the agent opens real tabs you can watch live, like a human working |
| Bulk page reads | One page at a time | `read_pages` opens whole listings in parallel tab waves, one step |
| Structured output | Schema-validated | Schema-validated, plus a live answer store with a completeness gate the agent must pass before finishing |
| Anti-fabrication | Prompt rules | Enforced: enum values with no on-page evidence are refused at the store boundary |
| Profiles | Cloud profiles | Import your BU Cloud profiles (cookies and localStorage) with one command |
| Live view | Replay | Real-time VNC of the actual browser, plus a step feed with the model's thinking |
| API | v3 REST | The same v3 REST surface: point `browser-use-sdk` at your box and change nothing but `baseUrl` and `apiKey` |

## Benchmark

The same real-world extraction task (a careers page with 16 records behind an embedded, cross-origin board, full schema output) run against BU Cloud and against OpenBrowse on a Raspberry Pi 5:

| Runtime | Model | Steps | Time | Tokens | LLM cost | Records |
|---|---|---|---:|---:|---:|---|
| BU Cloud | claude-sonnet-5 | 18 | 4m 30s | 1.4M | $0.86 | 16/16 |
| **OpenBrowse** | claude-sonnet-5 | **10** | **3m 42s** | **225k** | **$0.45** | 16/16 |
| **OpenBrowse** | gpt-5.6-terra | **6** | **2m 30s** | **129k** | **$0.23** | 16/16 |
| **OpenBrowse** | claude-opus-5 | | | | | benchmark coming |

Both OpenBrowse rows matched the reference output field for field, and were produced *concurrently* on one Raspberry Pi. The concurrent-session limit is per device and yours to configure during setup. All benchmarks were run on 14 August 2026; the exact task and output schema are in [benchmark.json](benchmark.json) if you want to repeat them.

## Supported models

### Recommended models

| Model | Aliases | Description |
|---|---|---|
| claude-sonnet-5 | `bu`, `bu-latest` | The default: reference-quality extraction |
| gpt-5.6-terra | `bu-mini` | Fastest and cheapest; benchmark-clean |
| gpt-5.6-sol | `bu-max` | Flagship OpenAI reasoning tier |
| claude-opus-5 | `bu-ultra` | Hardest tasks |

### Other supported models

| Model | Benchmark observations |
|---|---|
| `claude-fable-5` | Not benchmarked. The most capable model available, and the most expensive at $10/$50 per million tokens. Model thinking cannot be disabled, so there is no `off` — see the thinking table below. Requires an organisation on 30-day data retention. |
| `claude-mythos-5` | Not benchmarked. Identical to Fable 5 in capability, pricing and behaviour, including the always-on thinking. Only reachable by organisations in Project Glasswing; every other API key is rejected. |
| `claude-opus-4.8`, `claude-opus-4.8[1m]` | Not benchmarked |
| `claude-opus-4.7`, `claude-opus-4.7[1m]` | Not benchmarked |
| `claude-opus-4.6`, `claude-opus-4.6[1m]` | Not benchmarked |
| `claude-sonnet-4.6`, `claude-sonnet-4.6[1m]` | Not benchmarked |
| `gpt-5.6-luna` | ⚠️ **Accessible, but we strongly advise against use.** Often narrates answers instead of driving the browser and invents nonexistent "limits" to avoid completing tasks. [Whilst this is the model BU Cloud recommends](https://docs.browser-use.com/cloud/agent/models), it was repeatedly unable to complete our benchmark across multiple runs. |

### Model thinking

OpenBrowse separates two kinds of reasoning. **Browser thinking** is the platform's own step reasoning — the 👁️ see / 🛝 plan / ➡️ next / 💭 thinking cards in the live feed — and is always on for every model. **Model thinking** is the provider-side reasoning feature (Anthropic extended thinking, OpenAI reasoning effort), controlled per session by `modelThinkingEffort` (the API also accepts the older `thinkingEffort` name). Values are validated per model at runtime; the valid set, and what an unset value means, differ by model:

| Model | Valid efforts | Default (when unset) | Can be disabled? |
|---|---|---|---|
| claude-sonnet-5, claude-opus-5 | low, medium, high, xhigh, max | **high** — these models think unless told not to | Yes (`off`) |
| claude-fable-5, claude-mythos-5 | low, medium, high, xhigh, max | high | **No** — the API rejects a disabled config, so `off` is refused with an error |
| claude-opus-4.8 | low, medium, high, xhigh, max | none (no thinking) | Yes |
| claude-opus-4.7, claude-opus-4.6, claude-sonnet-4.6 | low, medium, high | none (no thinking) | Yes |
| gpt-5.6-terra, gpt-5.6-sol, gpt-5.6-luna | low, medium, high, xhigh | a provider-managed depth below `low` (the API rejects `max`) | Yes |

`off` genuinely disables thinking wherever the provider allows it, and the dashboard always preselects each model's real default — e.g. "High (Default)" for Sonnet 5, "None (Default)" for Opus 4.8 — so what a run will do is on screen before it starts. When a model returns its reasoning (Anthropic adaptive thinking with an explicit effort), the summarised reasoning appears live in the session activity bar and as a 🧠 card on each step.

## Quick start

```bash
git clone git@github.com:lujstn/openbrowse.git
cd openbrowse
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8420
```

Open `http://<your-host>:8420` in a browser. A fresh install serves a one-time **setup screen** that generates your API bearer key, takes your Anthropic / OpenAI / CapSolver keys, sets your dashboard password and concurrency limit, and writes `.env` for you.

Then from any `browser-use-sdk` client:

```ts
import { BrowserUse } from "browser-use-sdk";

const client = new BrowserUse({
  apiKey: process.env.OPENBROWSE_API_KEY,
  baseUrl: "http://<your-host>:8420/v3",
});

const task = await client.tasks.create({
  task: "Find every product on this page and return the structured list.",
  model: "bu-latest",
  outputSchema: mySchema,
});
```

Full installation (Raspberry Pi system packages, Xvfb + VNC live view, systemd service): see [GETTING_STARTED.md](GETTING_STARTED.md).

## Exposing it to the web

OpenBrowse was built and benchmarked on a Raspberry Pi 5 (16GB), but it is plain Python + Chromium: any Linux VPS or home server works. To reach it from outside that box without opening ports, put it behind [Tailscale](https://tailscale.com/):

```bash
# private access from your own devices
tailscale up

# or expose the API publicly over TLS with Tailscale Funnel
sudo tailscale funnel --bg 8420
```

## Features

- **v3-compatible REST API**: sessions, structured output schemas, cost caps, live URLs.
- **Visual, tab-based browsing**: parallel foreground tab waves for bulk reads; a code tab shows when the agent runs a script; everything visible over VNC.
- **Schema answer store**: every write validated live against your JSON Schema, coverage tracked per field, a completeness gate before `done`, and mark-absent semantics for data a site genuinely does not publish.
- **Grounding guards**: shell-read detection with automatic in-frame retry, evidence-checked enum writes, honest failure over invented data.
- **Profile import**: bring BU Cloud profiles (cookies + localStorage) via CLI or the dashboard.
- **Dashboard**: live session feed with model thinking, per-step costs, JSON export (full / steps / output-only), profile management.
- **CAPTCHA solving**: optional CapSolver integration.
- **Multi-provider**: Anthropic and OpenAI models behind one alias set, with per-provider repair layers for each family's failure modes.

## Licence

MIT
