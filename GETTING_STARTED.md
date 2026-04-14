# Getting Started — Browser Use Raspberry Pi

A self-hosted replacement for Browser Use Cloud, running on a Raspberry Pi 5. Exposes a v3-compatible REST API that the `browser-use-sdk` TypeScript client can call without modification — just swap the `baseUrl` and `apiKey`.

---

## 1. Prerequisites

- Raspberry Pi 5 with 16GB RAM running Debian Trixie (64-bit)
- SSH access to the Pi
- An [Anthropic API key](https://console.anthropic.com/)
- [Tailscale](https://tailscale.com/) installed and authenticated on the Pi

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
git clone https://github.com/lujstn/browser-use-raspberrypi.git
cd browser-use-raspberrypi

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
```

---

## 4. Configure Environment

Copy the example file and fill in your credentials:

```bash
cp .env.example .env
```

Open `.env` and set the following variables:

| Variable            | Description                                                                   |
| ------------------- | ----------------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY` | Your Anthropic API key (`sk-ant-...`)                                         |
| `API_KEY`           | A secret bearer token used to authenticate API requests                       |
| `CAPSOLVER_API_KEY` | _(Optional)_ Your [Capsolver](https://capsolver.com/) key for CAPTCHA solving |

Generate a secure `API_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Your `.env` should look like:

```
ANTHROPIC_API_KEY=sk-ant-...
API_KEY=<your-generated-token>

# Optional
CAPSOLVER_API_KEY=CAP-...
```

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
# {"status":"ok","active_sessions":0}
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
https://llmpi.tail12345.ts.net
```

Find your exact hostname:

```bash
tailscale status
```

Test the public endpoint:

```bash
curl https://llmpi.tail12345.ts.net/health
# {"status":"ok","active_sessions":0}
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
Description=Browser Use Raspberry Pi
After=network.target tailscaled.service
Wants=tailscaled.service

[Service]
Type=simple
User=lucas
WorkingDirectory=/home/lucas/Developer/browser-use-raspberrypi
EnvironmentFile=/home/lucas/Developer/browser-use-raspberrypi/.env
ExecStart=/home/lucas/Developer/browser-use-raspberrypi/.venv/bin/python -m app.main
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

## 8. Point buildinlondon to the Pi

### Environment variables

In your `buildinlondon` repo, add to `.env.local`:

```
PI_BROWSER_USE_URL=https://llmpi.tail12345.ts.net
PI_BROWSER_USE_API_KEY=<your-API_KEY-from-the-pi>
```

### Update pool.ts

In `src/scripts/test-browser-use/lib/pool.ts`, the `BrowserUse` client is initialised lazily:

```ts
get client(): BrowserUse {
  if (!this._client) {
    this._client = new BrowserUse();
  }
  return this._client;
}
```

Change this to pass your Pi's base URL and API key:

```ts
get client(): BrowserUse {
  if (!this._client) {
    this._client = new BrowserUse({
      baseUrl: process.env.PI_BROWSER_USE_URL,
      apiKey: process.env.PI_BROWSER_USE_API_KEY,
    });
  }
  return this._client;
}
```

Everything else in pool.ts — retry logic, stall detection, profile IDs — stays the same.

---

## 9. Create Browser Profiles

Profiles persist browser cookies across sessions, so the agent stays logged in to sites.

### Create a profile

```bash
curl -X POST https://llmpi.tail12345.ts.net/v3/profiles \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-profile"}'
```

Response includes the profile `id` — use this as `profileId` when creating sessions.

### List profiles

```bash
curl https://llmpi.tail12345.ts.net/v3/profiles \
  -H "Authorization: Bearer <API_KEY>"
```

### Import cookies from cloud Browser Use

If you have existing cookies in a cloud Browser Use profile, export the storage state as a JSON file in [Playwright storage state format](https://playwright.dev/docs/api/class-browsercontext#browser-context-storage-state) and copy it to the Pi:

```bash
scp cookies.json lucas@<pi-ip>:~/browser-use-raspberrypi/data/profiles/<profile-id>.json
```

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
