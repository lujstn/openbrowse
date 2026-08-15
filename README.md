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
| **OpenBrowse** | claude-sonnet-5 | 10 | 3m 42s | 225k | **$0.45** | 16/16 |
| **OpenBrowse** | gpt-5.6-terra | 6 | 2m 30s | 129k | **$0.23** | 16/16 |
| **OpenBrowse** | claude-opus-5 | | | | | benchmark coming |

Both OpenBrowse rows matched the reference output field for field, and were produced *concurrently* on one Raspberry Pi. The concurrent-session limit is per device and yours to configure during setup.

## Recommended models

| Alias | Resolves to | Use it for |
|---|---|---|
| `bu` / `bu-latest` | claude-sonnet-5 | The default: reference-quality extraction |
| `bu-mini` | gpt-5.6-terra | Fastest and cheapest, benchmark-clean |
| `bu-max` | gpt-5.6-sol | Flagship OpenAI reasoning tier |
| `bu-ultra` | claude-opus-5 | Hardest tasks |

Any `claude-*` or `gpt-*` model id also works directly. `gpt-5.6-luna` is deliberately unsupported: in our testing it narrates answers instead of driving the browser.

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
