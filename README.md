<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/openbrowse-dark.gif">
    <img src=".github/assets/openbrowse-light.gif" alt="OpenBrowse" width="432">
  </picture>
</p>

# OpenBrowse

**The open-source Browser Use Cloud alternative.** Self-host AI browser agents on a Raspberry Pi or any VPS, drive them through the same v3 REST API the `browser-use-sdk` already speaks, and watch every run live in a real browser. Built on top of the [Browser Use](https://github.com/browser-use/browser-use) SDK. It's cheaper, faster, and more reliable than BU Cloud.

[openbrowse.co](https://openbrowse.co)

<div align="left">
  <a href="https://buildin.london"><img src="https://buildin.london/badge.svg" alt="Built in London" style="width: 200px;"></a>
</div>

---

## Benchmarks

Given the same real-world extraction task (a careers page with 14 records behind an embedded, cross-origin board, requiring full schema output):

| Runtime | Model | Reasoning | Time | Tokens | LLM cost | Records |
| --- | --- | --- | ---: | ---: | ---: | --- |
| BU Cloud | claude-sonnet-5 | high | 2m 36s | 859k | $0.78 | 14/14<sup>1</sup> |
| **OpenBrowse** | **gpt-5.6-terra** | **none** | **1m 47s** | **202k** | **$0.24** | **14/14** |
| **OpenBrowse** | **claude-sonnet-5** | **high** | 4m 02s | **242k** | **$0.40** | **14/14** |

<sub><i><sup>1</sup> Extracted successfully, though some returned fields (e.g. job seniority) were hallucinated when not shown on the page.</i></sub>

## Why OpenBrowse over BU Cloud?

| | BU Cloud | OpenBrowse |
| --- | --- | --- |
| Hosting | Managed, per-task pricing | Your hardware, you pay only LLM tokens |
| How it works | Code-first: the agent scripts its way through pages | Visual-first: the agent opens real tabs you can watch live, like a human working |
| Bulk page reads | One page at a time | `read_pages` opens whole listings in parallel tab waves, one step |
| Structured output | Schema-validated | Schema-validated, plus a live answer store with a completeness gate the agent must pass before finishing |
| Anti-hallucination | Often fills fields the page never shows | On-screen data first, enriched from the page's own structured data (JSON-LD, APIs), never guessed: values without evidence are refused at the store boundary |
| Profiles | Cloud profiles | Import your BU Cloud profiles (cookies and localStorage) with one command |
| Live view | Replay | Real-time VNC of the actual browser, a step feed with the model's reasoning, and an IDE-style code tab that streams the agent's sandbox scripts live as they're written |
| API | v3 REST | The same v3 REST surface: point `browser-use-sdk` at your box and change nothing but `baseUrl` and `apiKey` |

### ⚡ See it in action

Here's a snippet of our benchmark run, with `claude-opus-5` taking agentic actions across parallel tabs while its code streams into a live IDE.

<https://github.com/user-attachments/assets/c1330d77-67b6-4a7d-bd43-7cdfa230b9d1>

## Model providers

### Recommended models

1. **For most use cases**, `gpt-5.6-terra { "reasoningEffort": "none" }`, `gpt-5.6-sol { "reasoningEffort": "none" }` and `claude-sonnet-5 { "reasoningEffort": "high" }` all strike a great balance of reliability, accuracy, and cost.

2. **For intense workflows**, use `claude-opus-5 { "reasoningEffort": "medium" }` or `gpt-5.6-sol { "reasoningEffort": "none" }` — both are great options, but watch out for token burn.

3. **On a budget?** Use `gpt-5.6-luna { "reasoningEffort": "max" }` with a tightly focused prompt. It might take a while, and it's more prone to hallucinations (especially with broad prompts), but the actual extractions are still great quality.

### Comparisons

The same real-world extraction task (a careers page with 14 records behind an embedded, cross-origin board, full schema output) run against BU Cloud and against OpenBrowse on a Raspberry Pi 5 (16GB) without concurrency, ordered cheapest to most expensive:

| Runtime | Model | Reasoning | Steps | Time | Tokens | LLM cost | Records |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| OpenBrowse | **gpt-5.6-luna** | **max** | 36 | 17m 03s | 1.08M | **$0.22** | 14/14 |
| OpenBrowse | **gpt-5.6-terra** | **none** | 11 | **1m 47s** | **202k** | **$0.24** | 14/14 |
| OpenBrowse | **claude-sonnet-5** | **high** | 10 | 4m 02s | **242k** | **$0.40** | 14/14 |
| OpenBrowse | **gpt-5.6-sol** | **none** | **8** | **2m 03s** | **136k** | **$0.41** | 14/14 |
| OpenBrowse | **claude-sonnet-5** | **none** | **9** | 5m 18s | **237k** | **$0.51** | 14/14 |
| OpenBrowse | **gpt-5.6-terra** | **high** | 17 | 5m 05s | **434k** | **$0.66** | 14/14 |
| BU Cloud | claude-sonnet-5 | high | 10 | 2m 36s | 859k | $0.78 | 14/14<sup>1</sup> |
| OpenBrowse | gpt-5.6-sol | medium | 16 | 5m 24s | **339k** | $1.12 | 14/14 |
| OpenBrowse | claude-opus-5 | medium | 15 | 3m 56s | **398k** | $1.32 | 14/14 |
| OpenBrowse | claude-opus-5 | none | 17 | 4m 53s | **480k** | $1.62 | 14/14 |

<sub><i><sup>1</sup> Extracted successfully, though some returned fields (e.g. job seniority) were hallucinated when not shown on the page.</i></sub>

OpenAI and Anthropic models are generally at their best at the opposite ends of the reasoning dial. For example, OpenAI's GPT-5.6-Terra performs better with less reasoning, spending less time planning ahead and more time reacting to the page in front of it, while Anthropic's 5-series Claude models lean towards rabbit holes and need reasoning time to refocus on the goal.

### Common questions

<details>

<summary>What's the difference between "thinking" and "reasoning"?</summary>

> OpenBrowse separates two kinds of reasoning. **Browser thinking** is our way of describing how the platform works in "steps" (the 👁️ see / 🛝 plan / ➡️ next / 💭 thinking cards in the live feed), so it can't be disabled.
>
>**Model reasoning** is different: it's the Chain-of-Thought reasoning provided by LLM providers (e.g. Anthropic's extended thinking, OpenAI's reasoning effort), and can be controlled per session by changing `reasoningEffort` in the API. Values are validated per model at runtime.

</details>

<details>

<summary>See all supported models</summary>


> - OpenAI: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`
> - Anthropic: `claude-mythos-5`, `claude-fable-5`, `claude-opus-5`, `claude-sonnet-5`, `claude-opus-4.8`, `claude-opus-4.8[1m]`, `claude-opus-4.7`, `claude-opus-4.7[1m]`, `claude-opus-4.6`, `claude-opus-4.6[1m]`, `claude-sonnet-4.6`, `claude-sonnet-4.6[1m]`
> - Google: ⚠️ Coming soon

</details>

## Quick start

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install openbrowse
openbrowse start
```

To run from source instead, clone the repo and use uv directly:

```bash
git clone git@github.com:lujstn/openbrowse.git
cd openbrowse
uv sync
uv run openbrowse serve
```

Then, open `http://localhost:8420` in a browser. You'll be guided through everything you need to run OpenBrowse.

[View the installation guide](https://openbrowse.co/docs/installation) for more details.

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
  publisher = {Zenodo},
  doi = {10.5281/zenodo.21986248},
  url = {https://github.com/lujstn/openbrowse}
}
```

[![DOI](https://zenodo.org/badge/1210478161.svg)](https://doi.org/10.5281/zenodo.21986248)

<br><br>

<table align="center">
  <tr>
    <th colspan="2">built with <з by @lujstn</th>
  </tr>
  <tr>
    <td><img src=".github/assets/IMG_8874.jpg" alt="@lujstn" width="400"></td>
    <td valign="middle">
      <a href="https://x.com/intent/user?screen_name=lujstn"><img src="https://img.shields.io/twitter/follow/lujstn?style=social" alt="Twitter"></a>
      <br>
      <a href="https://www.instagram.com/lujstn/"><img src="https://img.shields.io/badge/Instagram-Follow-E4405F?style=social&logo=instagram" alt="Instagram"></a>
      <br>
      <a href="https://www.tiktok.com/@lujstn"><img src="https://img.shields.io/badge/TikTok-000000?style=flat&logo=tiktok&logoColor=white" alt="TikTok"></a>
      <br>
      <a href="https://lujstn.com"><img src="https://img.shields.io/badge/%F0%9F%94%97_lujstn.com-1a1a1a" alt="lujstn.com"></a>
    </td>
  </tr>
</table>
