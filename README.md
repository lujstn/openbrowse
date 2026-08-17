<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/openbrowse-dark.gif">
    <img src=".github/assets/openbrowse-light.gif" alt="OpenBrowse" width="432">
  </picture>
</p>

# OpenBrowse

**The open-source Browser Use Cloud alternative.** Self-host AI browser agents on a Raspberry Pi or any VPS, drive them through the same v3 REST API the `browser-use-sdk` already speaks, and watch every run live in a real browser. Built on top of the [Browser Use](https://github.com/browser-use/browser-use) SDK. It's cheaper, faster, and more reliable than BU Cloud.

[openbrowse.co](https://openbrowse.co)

## Benchmarks

Given the same real-world extraction task (a careers page with 16 records behind an embedded, cross-origin board, requiring full schema output):

| Runtime | Model | Reasoning | Steps | Time | Tokens | LLM cost | Records |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| BU Cloud | claude-sonnet-5 | high | 18 | 4m 30s | 1.4M | $0.86 | 16/16 |
| **OpenBrowse** | **claude-sonnet-5** | **high** | **10** | **3m 42s** | **225k** | **$0.45** | **16/16** |
| **OpenBrowse** | **gpt-5.6-terra** | **none** | **6** | **2m 30s** | **129k** | **$0.23** | **16/16** |

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

## Model providers

### Recommended models

1. **For most use cases**, `gpt-5.6-terra { "reasoningEffort": "none" }` and `claude-sonnet-5 { "reasoningEffort": "high" }` both strike a great balance of reliablity, accuracy, and cost.

2. **For intense workflows**, using either `claude-opus-5` or `gpt-5.6-sol` with `{ "reasoningEffort": "none" }` are great options, but watch out for token burn.

3. **On a budget?** Use `gpt-5.6-luna { "reasoningEffort": "max" }` with a tightly focused prompt. It might take a while, and it's more prone to hallucinations (especially with broad prompts), but the actual extractions are still great quality.

### Comparisons

The same real-world extraction task (a careers page with 16 records behind an embedded, cross-origin board, full schema output) run against BU Cloud and against OpenBrowse on a Raspberry Pi 5 (16GB) without concurrency, ordered best to worst:

| Runtime | Model | Reasoning | Steps | Time | Tokens | LLM cost | Records |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| BU Cloud | claude-sonnet-5 | high | 18 | 4m 30s | 1.4M | $0.86 | 16/16 |
| OpenBrowse | claude-sonnet-5 | high | **10** | **3m 42s** | **225k** | **$0.45** | 16/16 |
| OpenBrowse | claude-sonnet-5 | none | TBD | TBD | TBD | TBD | TBD |
| OpenBrowse | gpt-5.6-terra | high | TBD | TBD | TBD | TBD | TBD |
| OpenBrowse | gpt-5.6-terra | none | **6** | **2m 30s** | **129k** | **$0.23** | 16/16 |
| OpenBrowse | gpt-5.6-luna | max | TBD | TBD | TBD | TBD | TBD |
| OpenBrowse | claude-opus-5 | high | TBD | TBD | TBD | TBD | TBD |
| OpenBrowse | claude-opus-5 | none | TBD | TBD | TBD | TBD | TBD |

OpenAI and Anthropic models are generally at their best at the opposite ends of the reasoning dial. For example, OpenAI's GPT-5.6-Terra performs better with less reasoning, spending less time planning ahead and more time reacting to the page in front of it, while Anthropic's 5-series Claude models lean towards rabbit holes and need reasoning time to refocus on the goal.

### What is "thinking" in OpenBrowse?

OpenBrowse separates two kinds of reasoning. **Browser thinking** is our way of describing how the platform works in "steps" (the 👁️ see / 🛝 plan / ➡️ next / 💭 thinking cards in the live feed), so it can't be disabled.

**Model reasoning** is different: it's the Chain-of-Thought reasoning provided by LLM providers (e.g. Anthropic's extended thinking, OpenAI's reasoning effort), and can be controlled per session by changing `reasoningEffort` in the API. Values are validated per model at runtime. Models will have different default reasoning levels depending on their provider, so it's a good idea to set this value explicitly.

### All supported models

- OpenAI: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`
- Anthropic: `claude-mythos-5`, `claude-fable-5`, `claude-opus-5`, `claude-sonnet-5`, `claude-opus-4.8`, `claude-opus-4.8[1m]`, `claude-opus-4.7`, `claude-opus-4.7[1m]`, `claude-opus-4.6`, `claude-opus-4.6[1m]`, `claude-sonnet-4.6`, `claude-sonnet-4.6[1m]`
- Google: ⚠️ Coming soon

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

OpenBrowse runs on plain Python + Chromium and can be easily port forwarded. To reach it from outside that box without opening ports, put it behind [Tailscale](https://tailscale.com/):

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
- **Grounding guards**: shell-read detection with automatic in-frame retry, evidence-checked enum writes, URL fields validated as absolute http(s) links at the store boundary, honest failure over invented data.
- **Profile import**: bring BU Cloud profiles (cookies + localStorage) via CLI or the dashboard.
- **Dashboard**: live session feed with model reasoning, per-step costs, JSON export (full / steps / output-only), profile management.
- **CAPTCHA solving**: optional CapSolver integration.
- **Multi-provider**: Anthropic and OpenAI models behind one API, with per-provider repair layers for each family's failure modes.

## Citation

This project is licensed under the MIT License. If you use OpenBrowse as part of your research or project, please cite:

```bibtex
@software{openbrowse2026,
  author = {Johnston Kurilov, Lucas},
  title = {{OpenBrowse}: Self-hosted AI browser agents},
  year = {2026},
  publisher = {GitHub},
  url = {https://github.com/lujstn/openbrowse}
}
```

<br>

---
<br>

<p align="center" style="font-size:12px; font-weight: 600; margin-bottom: 1.3rem;" >built with <з by @lujstn</p>

<div align="center">
  <div style="display: flex; max-width: 600px; flex-direction: row; align-items: center; justify-content: space-between;">
    <img src=".github/assets/IMG_8874.jpg" alt="@lujstn" width="400">
    <div>

  [![Twitter](https://img.shields.io/twitter/follow/lujstn?style=social)](https://x.com/intent/user?screen_name=lujstn)

  [![Instagram](https://img.shields.io/badge/Instagram-Follow-E4405F?style=social&logo=instagram)](https://www.instagram.com/lujstn/)

  [![TikTok](https://img.shields.io/badge/TikTok-000000?style=flat&logo=tiktok&logoColor=white)](https://www.tiktok.com/@lujstn)

  [![GitHub](https://img.shields.io/github/followers/lujstn?style=social)](https://github.com/lujstn)
    </div>
  </div>
</div>

<br>

<div align="center">

  [![Built in London](https://buildin.london/badge.svg)](https://buildin.london)

</div>
