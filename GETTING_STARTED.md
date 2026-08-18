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

## 3. Clone and Set Up Python Environment

```bash
cd ~
git clone git@github.com:lujstn/openbrowse.git
cd openbrowse

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
```

---

## 4. Configure Environment

The easiest way: start the server once (next section) and open it in a browser. An unconfigured instance serves a one-time **setup screen** at `/setup` that generates your API bearer key, collects your provider keys, dashboard password and concurrency limit, and writes `.env` for you.

To configure by hand instead, create `.env` in the repo root with:

| Variable                  | Description                                                                    |
| ------------------------- | ------------------------------------------------------------------------------ |
| `API_KEY`                 | A secret bearer token used to authenticate API requests                        |
| `ANTHROPIC_API_KEY`       | Your Anthropic API key (`sk-ant-...`), for `claude-*` models                   |
| `OPENAI_API_KEY`          | _(Optional)_ Your OpenAI API key, for `gpt-*` models                           |
| `CAPSOLVER_API_KEY`       | _(Optional)_ Your [Capsolver](https://capsolver.com/) key for CAPTCHA solving. Without it a challenge simply blocks the session, and the feed says so. Billed per solve by Capsolver, typically well under a cent, and shown against the session |
| `DASHBOARD_PASSWORD`      | _(Optional)_ Dashboard password for user `admin`; defaults to the `API_KEY`    |
| `MAX_CONCURRENT_SESSIONS` | _(Optional)_ Concurrent sessions this device runs (default 1); budget ~2GB RAM and one CPU core per session |
| `CLOUD_MAX_COST_FACTOR`   | _(Optional)_ Scales an incoming API `maxCostUsd` to local cost, for callers whose budgets are priced for a hosted service. Greater than 0 and at most 1; `0.5` turns a `$6` cap into `$3`. Default `1.0` (unscaled) |
| `CAPTCHA_MAX_COST_USD`    | _(Optional)_ Ceiling on CAPTCHA spend per run. Default `1.0`; set `0` to remove the ceiling |

Generate a secure `API_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### What CAPTCHA solving covers

With `CAPSOLVER_API_KEY` set, the agent solves a challenge itself by calling its
`solve_captcha` action. The page is inspected and the right solver chosen, so the
agent never has to name the challenge type.

| Challenge | Spotted on the page | Solved | Notes |
| --- | :---: | :---: | --- |
| reCAPTCHA v2, including invisible and full-page verification walls | ✅ | ✅ | Proven end to end against live challenges |
| reCAPTCHA v2 Enterprise | ✅ | ✅ | |
| reCAPTCHA v3 | ✅ | ✅ | Score based; the page action is read off the page where it can be, and assumed otherwise |
| reCAPTCHA v3 Enterprise | ✅ | ✅ | |
| reCAPTCHA image grids, "select every bus" | ✅ | ✅ | Answered by the ordinary reCAPTCHA solve, which clears the grid for you; the click-the-grid path is unproven and not offered |
| Cloudflare Turnstile | ✅ | ✅ | |
| Geetest v3 and v4 | ✅ | ✅ | Single-use parameters are refreshed before a retry |
| MTCaptcha | ✅ | ✅ | |
| AWS WAF, token | ✅ | ✅ | Cleared by writing the token as a cookie and re-requesting the page |
| Image to text | ❌ | ✅ | Asked for by name, with the answer field's selector, since a bare image has no reliable marker |
| AWS WAF, image | ❌ | ❌ | Written but unproven, so it is named and refused rather than charged for |
| hCaptcha | ✅ | ❌ | Recognised and reported plainly: Capsolver publishes no hCaptcha task |
| DataDome | ✅ | ❌ | Recognised and reported plainly: Capsolver publishes no DataDome task |

Coverage follows Capsolver's published service list, and a test refuses any task
type that list does not offer, so this table cannot quietly drift from what the
service will actually accept. A challenge it cannot solve is still recognised and
named, costing nothing, rather than being missed or charged for.

Only reCAPTCHA v2 has been proven against live challenges so far. The rest are
implemented and covered by tests, and each will tell you plainly if it cannot
clear a challenge rather than reporting a success it did not achieve. A challenge
type marked as not solved creates no task, so it costs nothing to meet one.

A solved challenge is written straight into the page, so its checkbox does not
visibly tick. Success is judged only by the page moving on, never by the widget's
appearance, and a challenge that will not clear is reported as a failure rather
than dressed up as one. Each solve is billed by Capsolver, typically well under a
cent, is shown against the session, and stops at the `CAPTCHA_MAX_COST_USD`
ceiling. After two solves that do not clear the same host, further spending on
that host is refused for the rest of the session.

---

## 5. Verify the Setup

Start the server:

```bash
source .venv/bin/activate
python -m app.main
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

Create the unit file:

```bash
sudo nano /etc/systemd/system/browser-use.service
```

Paste the following (adjust `User` and `WorkingDirectory` if your username differs):

```ini
[Unit]
Description=OpenBrowse
After=network.target tailscaled.service
Wants=tailscaled.service

[Service]
Type=simple
User=<user>
WorkingDirectory=/home/<user>/openbrowse
EnvironmentFile=/home/<user>/openbrowse/.env
ExecStart=/home/<user>/openbrowse/.venv/bin/python -m app.main
ExecStartPost=+/usr/bin/tailscale funnel --bg 8420
ExecStopPost=-+/usr/bin/tailscale funnel --bg off
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target

```

The `ExecStartPost` line automatically enables Tailscale Funnel when the service starts, and `ExecStopPost` disables it on stop. To run **without** the funnel (local network only), remove those two lines.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable browser-use
sudo systemctl start browser-use
```

Check status:

```bash
sudo systemctl status browser-use
```

Verify the funnel is active:

```bash
tailscale funnel status
```

View live logs:

```bash
journalctl -u browser-use -f
```

---

## 8. Point Your App at OpenBrowse

Any client of the official `browser-use-sdk` works unchanged: pass your OpenBrowse base URL and API key when constructing the client.

```ts
import { BrowserUse } from "browser-use-sdk";

const client = new BrowserUse({
  baseUrl: "https://<your-host>/v3",
  apiKey: process.env.OPENBROWSE_API_KEY,
});
```

Everything else in your integration, such as retry logic, polling and profile ids, stays the same.

---

## 9. Create Browser Profiles

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
# on the Pi, from the repo root, under the venv
.venv/bin/python -m scripts.import_profiles personal_profile.storage_state.json \
  --profile-id <cloud-profile-id> --name "Personal Profile"
```

A bundle (a JSON list, or `{"profiles": [...]}`) carries an id per entry, so a single command imports many:

```bash
.venv/bin/python -m scripts.import_profiles bundle.json
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

## 10. Troubleshooting

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
journalctl -u browser-use -n 50
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

**Check:** The server defaults to a maximum of 3 concurrent sessions (`max_concurrent_sessions = 3` in `app/config.py`). Each Chromium instance uses ~400–600 MB. On a 16GB Pi this is comfortable, but if you've reduced the default or are running other services:

```bash
# Check current memory usage
free -h

# Check OOM kills
dmesg | grep -i oom
```

To reduce concurrency, edit `max_concurrent_sessions` in `app/config.py` directly:

```python
max_concurrent_sessions: int = 1  # default is 3
```

Then restart the service. Or increase swap:

```bash
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile  # set CONF_SWAPSIZE=4096
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```
