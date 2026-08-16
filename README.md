# OpenBrowse

**The open-source Browser Use Cloud alternative.** Self-host AI browser agents on a Raspberry Pi or any VPS, drive them through the same v3 REST API the `browser-use-sdk` already speaks, and watch every run live in a real browser. Built on top of the [Browser Use](https://github.com/browser-use/browser-use) SDK. Cheaper than the cloud, and on our benchmark, faster and better.

[openbrowse.co](https://openbrowse.co)

## Why OpenBrowse over BU Cloud?

| | BU Cloud | OpenBrowse |
| --- | --- | --- |
| Hosting | Managed, per-task pricing | Your hardware, you pay only LLM tokens |
| How it works | Code-first: the agent scripts its way through pages | Visual-first: the agent opens real tabs you can watch live, like a human working |
| Bulk page reads | One page at a time | `read_pages` opens whole listings in parallel tab waves, one step |
| Structured output | Schema-validated | Schema-validated, plus a live answer store with a completeness gate the agent must pass before finishing |
| Anti-fabrication | Prompt rules | Enforced: enum values with no on-page evidence are refused at the store boundary |
| Profiles | Cloud profiles | Import your BU Cloud profiles (cookies and localStorage) with one command |
| Live view | Replay | Real-time VNC of the actual browser, a step feed with the model's reasoning, and an IDE-style code tab that streams the agent's sandbox scripts live as they're written |
| API | v3 REST | The same v3 REST surface: point `browser-use-sdk` at your box and change nothing but `baseUrl` and `apiKey` |

## Models

### Recommended models

1. **For most use cases**, `gpt-5.6-terra { "reasoningEffort": "none" }` and `claude-sonnet-5 { "reasoningEffort": "high" }` both strike a great balance of reliablity, accuracy, and cost.

2. **For intense workflows**, using either `claude-opus-5` or `gpt-5.6-sol` with `{ "reasoningEffort": "none" }` are great options, but watch out for token burn.

3. **On a budget?** Use `gpt-5.6-luna { "reasoningEffort": "max" }` with a tightly focused prompt. It might take a while, and it's more prone to hallucinations (especially with broad prompts), but the actual extractions are still great quality.

### Benchmarks and observations

The same real-world extraction task (a careers page with 16 records behind an embedded, cross-origin board, full schema output) run against BU Cloud and against OpenBrowse on a Raspberry Pi 5, ordered best to worst:

| Runtime | Model | Reasoning | Steps | Time | Tokens | LLM cost | Records |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| BU Cloud | claude-sonnet-5 | high | 18 | 4m 30s | 1.4M | $0.86 | 16/16 |
| OpenBrowse | gpt-5.6-terra | none | **6** | **2m 30s** | **129k** | **$0.23** | 16/16 |
| OpenBrowse | claude-sonnet-5 | high | **10** | **3m 42s** | **225k** | **$0.45** | 16/16 |
| OpenBrowse | claude-sonnet-5 | none | **15** | 10m 04s | **382k** | **$0.69** | 16/16 |
| OpenBrowse | gpt-5.6-terra | high | **17** | 6m 10s | **364k** | **$0.71** | 16/16 |
| OpenBrowse | claude-opus-5 | none | 21 | 5m 27s | **584k** | $1.82 | 16/16 |
| OpenBrowse | gpt-5.6-luna | xhigh | 43 | 17m 35s | **935k** | **$0.23** | 16/16, one wrong URL |
| OpenBrowse | claude-opus-5 | high | 37 | DNF | 1.06M | $3.00 | DNF, stopped at the $3 cost cap |

OpenAI and Anthropic models are generally at their best at the opposite ends of the reasoning dial. For example, OpenAI's GPT-5.6-Terra performs better with less reasoning, spending less time planning ahead and more time reacting to the page in front of it, while Anthropic's 5-series Claude models lean towards rabbit holes and need reasoning time to refocus on the goal.

### What is "thinking" in OpenBrowse?

OpenBrowse separates two kinds of reasoning. **Browser thinking** is our way of describing how the platform works in "steps" (the 👁️ see / 🛝 plan / ➡️ next / 💭 thinking cards in the live feed), so it can't be disabled.

**Model reasoning** is different: it's the Chain-of-Thought reasoning provided by LLM providers (e.g. Anthropic's extended thinking, OpenAI's reasoning effort), and can be controlled per session by changing `reasoningEffort` in the API. Values are validated per model at runtime. Models will have different default reasoning levels depending on their provider, so it's a good idea to set this value explicitly.

### All supported models

| Model | Description |
| --- | --- |
| `claude-opus-5` | Flagship Anthropic reasoning tier |
| `claude-sonnet-5` | Our default for reliable, high-quality extraction |
| `gpt-5.6-sol` | Flagship OpenAI reasoning tier |
| `gpt-5.6-terra` | Fastest and cheapest, great all-rounder |
| `gpt-5.6-luna` | Cheap but slow; only completes reliably with reasoning at `xhigh` or above, and even then took ~10x longer than Terra on our benchmark. Use only when time doesn't matter. |
| `claude-mythos-5` | Not tested. Only reachable by organisations in Project Glasswing. |
| `claude-fable-5` | Not tested. The most capable model available, and model reasoning cannot be disabled. |
| `claude-opus-4.8`, `claude-opus-4.8[1m]` | Not tested. |
| `claude-opus-4.7`, `claude-opus-4.7[1m]` | Not tested. |
| `claude-opus-4.6`, `claude-opus-4.6[1m]` | Not tested. |
| `claude-sonnet-4.6`, `claude-sonnet-4.6[1m]` | Not tested. |

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
  model: "claude-sonnet-5",
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
- **Dashboard**: live session feed with model reasoning, per-step costs, JSON export (full / steps / output-only), profile management.
- **CAPTCHA solving**: optional CapSolver integration.
- **Multi-provider**: Anthropic and OpenAI models behind one API, with per-provider repair layers for each family's failure modes.

## Licence

MIT
