#!/usr/bin/env bash

set -euo pipefail

SHARE="most"
SERVICE="openbrowse.service"
DRY_RUN=0
SERVICE_USER="${SUDO_USER:-$(id -un)}"

# @nonobvious(means): the HT_* variables exist so tests can exercise every
# branch against temp files without root; unset, the real paths apply.
SYSTEMD_DIR="${HT_SYSTEMD_DIR:-/etc/systemd/system}"
CMDLINE="${HT_CMDLINE:-/boot/firmware/cmdline.txt}"
SUDOERS_DIR="${HT_SUDOERS_DIR:-/etc/sudoers.d}"
MEMINFO="${HT_MEMINFO:-/proc/meminfo}"
PSI_CPU="${HT_PSI_CPU:-/proc/pressure/cpu}"
DEVICE_TREE_MODEL="${HT_DEVICE_TREE_MODEL:-/proc/device-tree/model}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --share) SHARE="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "usage: sudo bash $0 [--share all|most|shared] [--service NAME] [--dry-run]" >&2; exit 2 ;;
  esac
done

# @nonobvious(mirrors): percentages must stay in sync with SHARE_PRESETS in
# openbrowse/hostinfo.py — the dashboard shows recommendations computed from those.
case "$SHARE" in
  all) SHARE_PCT=90 ;;
  most) SHARE_PCT=70 ;;
  shared) SHARE_PCT=40 ;;
  *) echo "unknown share preset: $SHARE (use all|most|shared)" >&2; exit 2 ;;
esac

run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "would: $*"
  else
    echo "doing: $*"
    "$@"
  fi
}

MEM_TOTAL_KB=$(awk '/^MemTotal:/ {print $2}' "$MEMINFO" 2>/dev/null || echo 0)
if [[ "$MEM_TOTAL_KB" -gt 0 ]]; then
  MEMORY_HIGH_M=$(( MEM_TOTAL_KB * SHARE_PCT / 100 / 1024 ))
else
  MEMORY_HIGH_M=0
fi

OVERRIDE_DIR="$SYSTEMD_DIR/$SERVICE.d"
OVERRIDE_FILE="$OVERRIDE_DIR/50-capacity.conf"
OVERRIDE_CONTENT="[Service]
CPUWeight=300
$( [[ $MEMORY_HIGH_M -gt 0 ]] && echo "MemoryHigh=${MEMORY_HIGH_M}M" )"

if [[ -f "$OVERRIDE_FILE" ]] && [[ "$(cat "$OVERRIDE_FILE")" == "$OVERRIDE_CONTENT" ]]; then
  echo "ok: resource limits already set for --share $SHARE ($OVERRIDE_FILE)"
else
  run mkdir -p "$OVERRIDE_DIR"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "would: write $OVERRIDE_FILE (CPUWeight=300, MemoryHigh=${MEMORY_HIGH_M}M)"
  else
    printf '%s\n' "$OVERRIDE_CONTENT" > "$OVERRIDE_FILE"
    echo "doing: wrote $OVERRIDE_FILE"
  fi
  if command -v systemctl >/dev/null 2>&1; then
    run systemctl daemon-reload
    echo "note: limits apply from the next service restart"
  fi
fi

if [[ -e "$PSI_CPU" ]]; then
  echo "ok: PSI already available"
elif [[ -e "$DEVICE_TREE_MODEL" ]] && grep -qi "raspberry pi" "$DEVICE_TREE_MODEL" 2>/dev/null; then
  if [[ -f "$CMDLINE" ]]; then
    if grep -qw "psi=1" "$CMDLINE"; then
      echo "ok: psi=1 already in $CMDLINE (reboot pending if PSI is absent)"
    else
      if [[ $DRY_RUN -eq 1 ]]; then
        echo "would: append psi=1 to $CMDLINE"
      else
        awk 'NR==1 {print $0 " psi=1"; next} {print}' "$CMDLINE" > "$CMDLINE.tmp"
        mv "$CMDLINE.tmp" "$CMDLINE"
        echo "doing: appended psi=1 to $CMDLINE"
      fi
      echo "note: PSI activates after a reboot"
    fi
  else
    echo "skip: $CMDLINE not found; enable PSI manually for this boot setup"
  fi
else
  echo "skip: PSI not available and this is not a Raspberry Pi"
fi

SUDOERS_FILE="$SUDOERS_DIR/openbrowse-hosttune"
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SYSTEMCTL_PATH="$(command -v systemctl || echo /usr/bin/systemctl)"
# @nonobvious(must-hold): the dashboard restarts the service with `sudo -n
# systemctl restart`, which without this line always fails and falls back to
# exiting so systemd respawns the process.
SUDOERS_LINE="$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/bash $SCRIPT_PATH *, $SYSTEMCTL_PATH restart $SERVICE"
if [[ -f "$SUDOERS_FILE" ]] && grep -qF "$SUDOERS_LINE" "$SUDOERS_FILE"; then
  echo "ok: sudoers entry already present ($SUDOERS_FILE)"
elif [[ $DRY_RUN -eq 1 ]]; then
  echo "would: write $SUDOERS_FILE allowing $SERVICE_USER to re-run this script and restart $SERVICE"
else
  printf '%s\n' "$SUDOERS_LINE" > "$SUDOERS_FILE"
  chmod 0440 "$SUDOERS_FILE"
  if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$SUDOERS_FILE" >/dev/null; then
    rm -f "$SUDOERS_FILE"
    echo "error: sudoers entry failed validation and was removed" >&2
    exit 1
  fi
  echo "doing: wrote $SUDOERS_FILE (dashboard tuning buttons now work without a password)"
fi

echo "done"
