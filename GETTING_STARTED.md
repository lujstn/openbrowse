# Getting started with OpenBrowse

The full documentation lives at **<https://openbrowse.co/docs>**. Every page there
is also available as plain Markdown by adding `.md` to its address, so
<https://openbrowse.co/docs/installation.md> works if you would rather read it in a
terminal.

This file is the short version. It exists so that someone who has just cloned the
repository can get a browser agent running without leaving the checkout.

## What you need

A Debian or Ubuntu machine with SSH access, and an API key from Anthropic or
OpenAI, or both. It was built and benchmarked on a Raspberry Pi 5 with 16GB of
RAM. Budget roughly 2GB of RAM per concurrent session: Chromium itself is 400 to
600MB, and the rest covers the pages it loads, the virtual display and the Python
process.

## Install

The agent draws into a virtual X display and streams it over VNC, so those
packages are required before anything will run:

```bash
sudo apt update
sudo apt install -y xvfb x11vnc novnc websockify \
  libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 libxdamage1 \
  libxrandr2 libgbm1 libpango-1.0-0 libasound2 libxshmfence1 libgtk-3-0
```

Then install OpenBrowse with either [uv](https://docs.astral.sh/uv/) or
[pipx](https://pipx.pypa.io/), whichever you already use for Python apps:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

uv tool install openbrowse
openbrowse start
```

```bash
pipx install openbrowse
openbrowse start
```

Neither is preferred. OpenBrowse works out which one installed it and upgrades
itself with that same tool, so the installer that owns the app stays the one
that changes it.

`openbrowse start` registers OpenBrowse as a systemd service, so it is running
now and starts again on every boot. `openbrowse stop --disable` undoes that, and
`openbrowse status` and `openbrowse restart` manage it in between. On a machine
without systemd it runs in the foreground instead.

To work on OpenBrowse rather than only run it, clone the repository and run it
from the checkout:

```bash
git clone git@github.com:lujstn/openbrowse.git
cd openbrowse && uv sync
uv run openbrowse serve
```

Open `http://<your-host>:8420`. A fresh install serves a one-time setup screen at
`/setup` that generates your API bearer key, collects your provider keys, sets a
dashboard password, reads the machine to recommend a concurrency limit, and
writes `.env` for you. Restart the server afterwards so the new configuration is
picked up.

Your first session downloads the stealth Chromium build, so expect it to take
several minutes longer than every run after it. To get that out of the way before
you start, from a checkout:

```bash
uv run python -c "import cloakbrowser; print(cloakbrowser.ensure_binary())"
```

## Tune the host

Two things the application cannot do for itself: tell systemd that OpenBrowse
should win a contended CPU, and turn on the kernel's pressure stall information,
which a Raspberry Pi ships with compiled out. One idempotent command does both,
plus the sudoers entry the dashboard's tuning button needs:

```bash
openbrowse tune --share most --dry-run   # show the plan
openbrowse tune --share most             # apply it
```

The script needs root, so `openbrowse tune` asks for your password. Run it again
after upgrading: the sudoers grant it writes names the script by its full path,
which moves when the package does, and the dashboard's tuning buttons depend on
that grant.

## Updating

The server checks PyPI for new releases in the background and shows an **Update
available** badge in the dashboard when one exists. Installing it is one click on
the Settings page, and the server restarts itself afterwards. From a shell:

```bash
openbrowse check-update
openbrowse update
```

`openbrowse update` runs whichever upgrade the install method calls for: `uv tool
upgrade` for a uv tool, `pipx upgrade` for a pipx app, `pip install --upgrade`
for a plain venv, or a `git pull` for a checkout.

Set `UPDATE_CHECK_HOURS` in your `.env` to change how often it looks, or `0` to
switch the background check off.

## Then read

| Topic | Page |
| --- | --- |
| Sizing it for your machine, and the full environment variable list | <https://openbrowse.co/docs/installation> |
| Writing tasks, which changes results more than model choice does | <https://openbrowse.co/docs/tasks> |
| Structured output and the validated answer store | <https://openbrowse.co/docs/structured-output> |
| What a run costs, capping it, and how the cap behaves on a conversation | <https://openbrowse.co/docs/cost> |
| Which CAPTCHA types are solved, which are only recognised, and what solving costs | <https://openbrowse.co/docs/captchas> |
| Reaching it from outside your network with Tailscale | <https://openbrowse.co/docs/exposing> |
| Importing your Browser Use Cloud profiles | <https://openbrowse.co/docs/profiles> |
| When something goes wrong | <https://openbrowse.co/docs/troubleshooting> |
| The v3 REST API | <https://openbrowse.co/docs/api> |
