# Getting Started with OpenBrowse

OpenBrowse is a self-hosted replacement for Browser Use Cloud, built for a Raspberry Pi 5 but happy on any Linux VPS or server. Exposes a v3-compatible REST API that the `browser-use-sdk` TypeScript client can call without modification — just swap the `baseUrl` and `apiKey`.

---

## 1. Prerequisites

- A Linux machine: built and benchmarked on a Raspberry Pi 5 (16GB, Debian 64-bit), but any Debian/Ubuntu VPS or home server works
- SSH access to the machine
- An [Anthropic API key](https://console.anthropic.com/) and/or an [OpenAI API key](https://platform.openai.com/api-keys)
- _(Optional)_ [Tailscale](https://tailscale.com/) installed and authenticated, for private access or public exposure

---

## 2. Install System Dependencies

```bash
sudo apt update && sudo apt upgrade -y

# Virtual display + VNC + noVNC
sudo apt install -y xvfb x11vnc novnc websockify

# Python
sudo apt install -y python3-venv python3-pip

# Chromium system deps (required for CloakBrowser / Playwright)
sudo apt install -y \
  libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 libxdamage1 \
  libxrandr2 libgbm1 libpango-1.0-0 libasound2 libxshmfence1 libgtk-3-0
```

---

## 3. Install OpenBrowse

Install [uv](https://docs.astral.sh/uv/) once, then install OpenBrowse as a tool. This gives you the `openbrowse` command on your PATH and clean, one-command upgrades later:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install openbrowse
```

To hack on the source instead, clone the repo and let uv manage the environment; every `openbrowse` command below then becomes `uv run openbrowse` from the repo root:

```bash
cd ~
git clone git@github.com:lujstn/openbrowse.git
cd openbrowse
uv sync
```

---

## 4. Configure Environment

The easiest way: start the server once (next section) and open it in a browser. An unconfigured instance serves a one-time **setup screen** at `/setup` that generates your API bearer key, collects your provider keys, dashboard password and concurrency limit, and writes `.env` for you.

To configure by hand instead, create `.env` in OpenBrowse's home directory — `~/.openbrowse/` for a tool install, the repo root for a source checkout (`OPENBROWSE_HOME` overrides both) — with:

| Variable                  | Description                                                                    |
| ------------------------- | ------------------------------------------------------------------------------ |
| `API_KEY`                 | A secret bearer token used to authenticate API requests                        |
| `ANTHROPIC_API_KEY`       | Your Anthropic API key (`sk-ant-...`), for `claude-*` models                   |
| `OPENAI_API_KEY`          | _(Optional)_ Your OpenAI API key, for `gpt-*` models                           |
| `CAPSOLVER_API_KEY`       | _(Optional)_ Your [Capsolver](https://capsolver.com/) key for CAPTCHA solving. Without it a challenge simply blocks the session, and the feed says so. Billed per solve by Capsolver, typically well under a cent, and shown against the session |
| `DASHBOARD_PASSWORD`      | _(Optional)_ Dashboard password for user `admin`; defaults to the `API_KEY`    |
| `MAX_CONCURRENT_SESSIONS` | _(Optional)_ Concurrent sessions this device runs (default 1). The `/setup` screen detects your hardware and recommends a value; budget ~2GB RAM and one CPU core per session when setting it by hand |
| `CLOUD_MAX_COST_FACTOR`   | _(Optional)_ Scales an incoming API `maxCostUsd` to local cost, for callers whose budgets are priced for a hosted service. Greater than 0 and at most 1; `0.5` turns a `$6` cap into `$3`. Default `1.0` (unscaled) |
| `KEEP_ALIVE_IDLE_TIMEOUT` | _(Optional)_ Seconds a keep-alive session waits, browser and history still open, for its next follow-up before closing itself. Default `600`; `0` waits indefinitely. A parked session is also closed early if a newly started session needs its display slot |
| `CAPTCHA_MAX_COST_USD`    | _(Optional)_ Ceiling on CAPTCHA spend for a single task. Default `0.03`, which buys about ten solves at Capsolver's most expensive tier; a keep-alive session gets that allowance again for each follow-up, and each task's solving counts against the session's `maxCostUsd` for that task. Neither is a fixed total for a whole conversation, since both refresh on every follow-up. Set `0` to remove the ceiling |
| `CHROME_LIGHT_FLAGS`      | _(Optional)_ `1` launches each browser with a lighter profile suited to small or GPU-less hosts; default `0`. The `/setup` screen pre-selects it on hardware where it helps; see "The lighter browser profile" below |

Generate a secure `API_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### What CAPTCHA solving covers

With `CAPSOLVER_API_KEY` set, the agent solves a challenge itself by calling its `solve_captcha` action. The page is inspected and the right solver chosen, so the agent never has to name the challenge type.

| Challenge | Spotted on the page | Solved | Notes |
| --- | :---: | :---: | --- |
| reCAPTCHA v2, including invisible and full-page verification walls | ✅ | ✅ | Proven end to end against live challenges |
| reCAPTCHA v2 Enterprise | ✅ | ✅ | Proven end to end against live challenges |
| reCAPTCHA v3 | ✅ | ✅ | Score based; proven end to end against live challenges |
| reCAPTCHA v3 Enterprise | ✅ | ✅ | Proven end to end against live challenges |
| reCAPTCHA image grids, "select every bus" | ✅ | ✅ | Answered by the ordinary reCAPTCHA solve, which clears the grid for you; proven end to end against live challenges |
| Cloudflare Turnstile | ✅ | ✅ | Proven end to end against live challenges |
| Geetest v3 and v4 | ✅ | ✅ | Proven end to end against live challenges, including fresh v3 challenge registration and provider callbacks |
| MTCaptcha | ✅ | ✅ | Proven end to end against a live challenge using runtime configuration and verified-callback support |
| AWS WAF, token | ✅ | ✅ | Cleared by writing the token as a cookie and re-requesting the page |
| Image to text | ❌ | ✅ | Asked for by name, with the answer field's selector, since a bare image has no reliable marker |
| hCaptcha | ✅ | ❌ | Recognised and reported plainly: Capsolver publishes no hCaptcha task |
| DataDome | ✅ | ❌ | Recognised and reported plainly: Capsolver publishes no DataDome task |
| AWS WAF, image | ❌ | ❌ | Not yet implemented |

Coverage follows Capsolver's published service list, and a test refuses any task type that list does not offer, so this table cannot quietly drift from what the service will actually accept. A challenge it cannot solve is still recognised and named, costing nothing, rather than being missed or charged for.

The live acceptance suite proves reCAPTCHA v2 (checkbox, explicit and invisible), reCAPTCHA v3 with multiple page actions, Cloudflare Turnstile, MTCaptcha and Geetest v3 and v4. Other solved types are implemented and covered by local tests, and each will tell you plainly if it cannot clear a challenge rather than reporting a success it did not achieve. A challenge type marked as not solved creates no task, so it costs nothing to meet one.

A solved challenge is written straight into the page, so its checkbox does not visibly tick. Success is judged only by the page moving on, never by the widget's appearance, and a challenge that will not clear is reported as a failure rather than dressed up as one. Each solve is billed by Capsolver, typically well under a cent, is shown against the session, and stops at the `CAPTCHA_MAX_COST_USD` ceiling, which defaults to about ten solves for a single task; a keep-alive session gets that allowance again for each follow-up, and each task's solving counts against the session's cost budget for that task. Neither bounds a whole conversation: a session that takes ten follow-ups may spend the allowance ten times. After two solves that do not clear the same host, further spending on that host is refused for the rest of the session.

### The lighter browser profile

`CHROME_LIGHT_FLAGS=1` starts every browser session with a slimmer Chromium configuration: no GPU process (under a virtual display there is no GPU acceleration to lose), at most four renderer processes shared between tabs, a 256MB JavaScript heap per renderer, Chromium's own low-end device mode, and no background update traffic.

On small machines this is usually a straight win. Each session starts with a lower memory floor, and page-load bursts contend less with everything else, which matters most when more than one session runs at once. On a Raspberry Pi 5 it measured faster and lighter than the default in both solo and concurrent runs, with identical output quality.

The trade-off is the JavaScript heap cap: an unusually heavy web application could run slower against it, or in rare cases fail. If one specific site misbehaves with the profile on, turn it off and restart; that is the first thing to try.

The `/setup` screen pre-selects the profile on hardware where it earns its keep (a Raspberry Pi, four cores or fewer, or 8GB of memory or less) and leaves it unticked elsewhere; either way the choice is yours. Change it any time from the dashboard under **Settings**, where the Capacity card notes whether it is enabled or recommended for the machine: set `CHROME_LIGHT_FLAGS` to `1` (or clear it to turn the profile off) and save, which restarts the server.

---

## 5. Verify the Setup

Start the server in the foreground for a first look (the next sections make it a proper service):

```bash
openbrowse serve
```

Expected output:

```
INFO:     Initializing database...
INFO:     Server ready on 0.0.0.0:8420
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8420
```

Open the admin dashboard in a browser on your local network:

```
http://<pi-ip>:8420/
```

Confirm the API is responding:

```bash
curl http://<pi-ip>:8420/health
# {"status":"ok"}
```

---

## 6. Set Up Tailscale Funnel

Tailscale Funnel exposes the server to the internet over HTTPS with a stable hostname.

```bash
sudo tailscale funnel --bg 8420
```

The `--bg` flag runs the funnel as a background daemon managed by Tailscale — it survives reboots without a separate systemd unit.

Your URL will be in the format:

```
https://your-pi.tail0a1b2c.ts.net
```

Find your exact hostname:

```bash
tailscale status
```

Test the public endpoint:

```bash
curl https://your-pi.tail0a1b2c.ts.net/health
# {"status":"ok"}
```

To disable the funnel later:

```bash
sudo tailscale funnel --bg off
```

---

## 7. Run as a Systemd Service

One command does it:

```bash
openbrowse start
```

This registers OpenBrowse as a systemd service (reusing an existing `openbrowse.service` unit if one is already there), starts it immediately, and enables it **so it starts automatically on every boot from now on**. It says so when it succeeds. Manage it afterwards with:

```bash
openbrowse status
openbrowse restart
openbrowse stop             # stops now; still starts on the next boot
openbrowse stop --disable   # stops now and stays off at boot
```

### Manual unit (optional — needed for the Tailscale Funnel hooks)

The generated unit is deliberately minimal. If you want the funnel to follow the service up and down, or want to see exactly what runs, write the unit yourself instead:

```bash
sudo nano /etc/systemd/system/openbrowse.service
```

Paste the following (adjust `User` and the paths if your username differs):

```ini
[Unit]
Description=OpenBrowse
After=network.target tailscaled.service
Wants=tailscaled.service

[Service]
Type=simple
User=<user>
WorkingDirectory=/home/<user>
EnvironmentFile=-/home/<user>/.openbrowse/.env
ExecStart=/home/<user>/.local/bin/openbrowse serve
ExecStartPost=+/usr/bin/tailscale funnel --bg 8420
ExecStopPost=-+/usr/bin/tailscale funnel --bg off
Restart=on-failure
RestartSec=5
CPUWeight=300
MemoryHigh=12G
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target

```

The `ExecStartPost` line automatically enables Tailscale Funnel when the service starts, and `ExecStopPost` disables it on stop. To run **without** the funnel (local network only), remove those two lines.

`CPUWeight=300` only matters when the CPU is oversubscribed: it tells the kernel to favour browser sessions over background services (a media server's transcodes, for example) during contention, and does nothing on an idle box. `MemoryHigh=12G` throttles the service before it can push the host into out-of-memory territory. Both lines are safe to remove on a dedicated host.

You do not need to add these by hand: run the bundled tuning script once and it writes a systemd override sized from how much of the machine you want OpenBrowse to use, enables PSI on a Raspberry Pi, and lets the dashboard's Settings page apply future tuning changes with a button:

```bash
openbrowse tune --share most   # all | most | shared
```

**Optional, recommended for concurrent sessions — enable CPU pressure metrics (PSI).** `host_tune.sh` above does this for you on a Raspberry Pi; the manual steps follow for other setups. The server prefers the kernel's pressure stall information over load average when judging whether the host is struggling; PSI measures time tasks actually spent waiting for CPU, so a busy-but-healthy box is not misread as overloaded. Raspberry Pi OS compiles PSI in but ships it disabled. To enable it, append `psi=1` to the single line in `/boot/firmware/cmdline.txt` and reboot:

```bash
sudo sed -i '1 s/$/ psi=1/' /boot/firmware/cmdline.txt
sudo reboot
```

Verify with `cat /proc/pressure/cpu` — if the file exists, PSI is live. Without it the server falls back to load-average heuristics, which still work but over-count a session's own rendering as pressure.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable openbrowse
sudo systemctl start openbrowse
```

Check status:

```bash
sudo systemctl status openbrowse
```

Verify the funnel is active:

```bash
tailscale funnel status
```

View live logs:

```bash
journalctl -u openbrowse -f
```

---

## 8. Updating

The server checks PyPI for new releases in the background (every 6 hours by default; set `UPDATE_CHECK_HOURS` in `.env` to change it, `0` to disable). When one exists, the dashboard shows an **Update available** badge in the navigation bar; the Settings page then offers a one-click **Install and restart** button, which upgrades whichever way this copy was installed (uv tool, pip, or a git checkout) and restarts the service.

From a shell, the same is available as:

```bash
openbrowse check-update
openbrowse update
```

After a shell update, restart the service (`openbrowse restart`) — the dashboard button does this for you.

---

## 9. Point Your App at OpenBrowse

Any client of the official `browser-use-sdk` works unchanged: pass your OpenBrowse base URL and API key when constructing the client.

```ts
import { BrowserUse } from "browser-use-sdk";

const client = new BrowserUse({
  baseUrl: "https://<your-host>/v3",
  apiKey: process.env.OPENBROWSE_API_KEY,
});
```

Everything else in your integration, such as retry logic, polling and profile ids, stays the same.

### How `maxCostUsd` is spent

`maxCostUsd` is a ceiling on the whole session, not on one task, exactly as it is in the v3 contract: `totalCostUsd` reports what the session has spent altogether, and the run stops as soon as that reaches the cap.

That matters once a session is kept alive for follow-ups. If every follow-up drew from the same fixed pot, a conversation would slowly strangle itself, so each new dispatch tops the pot back up by the allowance the session was created with. Send a task to a session created with `maxCostUsd: 3` that has already spent $2.40, and it runs with a $5.40 ceiling: no single turn can run away, and the conversation is never cut short.

Be clear about what that does and does not promise. The bound is per task: nothing limits how many follow-ups a session takes, so a conversation of ten may spend the allowance ten times. That is the same trade the v3 contract makes, and it is the one worth making, because the alternative is a session that quietly stops working part-way through. If you need a fixed total, spend it as one task, or start a fresh session per task and let each carry its own budget.

Naming `maxCostUsd` on the follow-up itself overrides that for one dispatch, as an absolute ceiling on the session rather than an amount to add. A session created without a budget stays unbudgeted.

---

## 10. Create Browser Profiles

Profiles persist browser cookies across sessions, so the agent stays logged in to sites.

### Create a profile

```bash
curl -X POST https://your-pi.tail0a1b2c.ts.net/v3/profiles \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-profile"}'
```

Response includes the profile `id` — use this as `profileId` when creating sessions.

### List profiles

```bash
curl https://your-pi.tail0a1b2c.ts.net/v3/profiles \
  -H "Authorization: Bearer <API_KEY>"
```

### Import cookies from cloud Browser Use

Cloud Browser Use profiles are cookie/localStorage jars. Export a profile's storage state as a JSON file in [Playwright storage state format](https://playwright.dev/docs/api/class-browsercontext#browser-context-storage-state), then import it so the local profile id matches the cloud id.

**Recommended — the import CLI.** It creates the profile if it does not exist, normalises the cookies, and backs up any existing jar to `.import-bak`:

```bash
# on the Pi, from a source checkout of the repo
uv run python -m scripts.import_profiles personal_profile.storage_state.json \
  --profile-id <cloud-profile-id> --name "Personal Profile"
```

A bundle (a JSON list, or `{"profiles": [...]}`) carries an id per entry, so a single command imports many:

```bash
uv run python -m scripts.import_profiles bundle.json
```

**Or the API** (this is what the in-app importer calls):

```bash
curl -X PUT https://your-pi.tail0a1b2c.ts.net/v3/profiles/<profile-id>/storage-state \
  -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  --data @personal_profile.storage_state.json
```

Verify: `GET /v3/profiles/<id>` (or the dashboard **Profiles** page) lists the imported `cookieDomains`. Then run a session with that `profileId`.

The storage state file uses the format:

```json
{
  "cookies": [
    {
      "name": "session",
      "value": "...",
      "domain": "example.com",
      "path": "/",
      "expires": 1999999999,
      "httpOnly": true,
      "secure": true,
      "sameSite": "Lax"
    }
  ],
  "origins": []
}
```

`origins` carries each origin's `localStorage` (and `sessionStorage`), which browser-use restores on load. Cookies acquired or refreshed during a session are persisted back to the same file when the session ends, with localStorage preserved. These files are live session credentials — treat them as secrets and never commit them (`data/` is git-ignored).

---

## 11. Troubleshooting

### CloakBrowser won't start

**Symptom:** Sessions fail immediately with a Chromium launch error.

**Check:** Confirm all Chromium system dependencies are installed:

```bash
playwright install-deps
```

Then verify cloakbrowser resolves an executable:

```bash
python3 -c "import cloakbrowser; print(cloakbrowser.ensure_binary())"
```

If the path doesn't exist, reinstall:

```bash
pip install --force-reinstall cloakbrowser
```

---

### noVNC shows a blank screen

**Symptom:** The live browser view in the dashboard loads but shows nothing.

**Check:** Confirm xvfb, x11vnc, and websockify are installed and on `$PATH`:

```bash
which Xvfb x11vnc websockify
```

If any are missing, install them:

```bash
sudo apt install -y xvfb x11vnc novnc websockify
```

Also check that the display processes started cleanly in the server logs:

```bash
journalctl -u openbrowse -n 50
```

---

### Tailscale Funnel not working

**Symptom:** The public HTTPS URL returns a connection error or 502.

**Check 1:** Confirm Tailscale Funnel is active:

```bash
tailscale funnel status
```

If the output is empty, re-run:

```bash
sudo tailscale funnel 8420
```

**Check 2:** Confirm the server is listening on port 8420:

```bash
ss -tlnp | grep 8420
```

**Check 3:** Funnel requires HTTPS — make sure you're not using `http://` in the URL.

---

### Out of memory

**Symptom:** Sessions are killed mid-task, the Pi becomes unresponsive, or the OOM killer fires (visible in `dmesg`).

**Check:** The server defaults to a maximum of 1 concurrent session (`MAX_CONCURRENT_SESSIONS` in `.env`). Each Chromium instance uses ~400–600 MB. On a 16GB Pi a few sessions are comfortable memory-wise, but if you've raised the cap or are running other services:

```bash
# Check current memory usage
free -h

# Check OOM kills
dmesg | grep -i oom
```

To change concurrency, use the Capacity card on the dashboard's Settings page (it knows this machine's limits), or set `MAX_CONCURRENT_SESSIONS` in `.env`:

```bash
MAX_CONCURRENT_SESSIONS=1
```

Then restart the service. Or increase swap:

```bash
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile  # set CONF_SWAPSIZE=4096
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```
