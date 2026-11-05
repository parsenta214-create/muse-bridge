#!/bin/bash
# Run a command on the phone's Termux over the existing SSH channel
# (same tunnel-proxy route as push_apk.sh). Streams the remote
# output and exits with the remote command's exit code (124 on
# timeout, 255 when SSH itself fails).
# Usage: ./termux_exec.sh [--timeout SECONDS] <command...>
#   ./termux_exec.sh uptime
#   ./termux_exec.sh --timeout 300 'ls -la ~/storage/downloads'
# Requires: sshd running on the phone (Termux:Boot + the boot
# script in termux/boot/ keeps it up across reboots), and
# ANDROID_PHONE_IP / ANDROID_TERMUX_USER in .env.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$HERE/.env"
    set +a
fi
if [ -z "${ANDROID_PHONE_IP:-}" ] || [ -z "${ANDROID_TERMUX_USER:-}" ]; then
    echo "ANDROID_PHONE_IP and ANDROID_TERMUX_USER are not set; put them in .env (see .env.example)" >&2
    exit 1
fi

TIMEOUT=120
if [ "${1:-}" = "--timeout" ]; then
    TIMEOUT="${2:-120}"
    shift 2
fi
if [ "$#" -eq 0 ]; then
    echo "usage: $0 [--timeout SECONDS] <command...>" >&2
    exit 1
fi

PROXY_CMD="python3 $HERE/android_bridge.py _proxy %h %p"
SSH_PORT="${ANDROID_TERMUX_SSH_PORT:-8022}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new
          -o "ProxyCommand=$PROXY_CMD" -p "$SSH_PORT" -i "$HOME/.ssh/id_ed25519")
REMOTE="${ANDROID_TERMUX_USER}@${ANDROID_PHONE_IP}"

# timeout -k: TERM first, KILL 5 s later if the remote command
# ignores it. ssh's own exit code is the remote command's.
exec timeout -k 5 "$TIMEOUT" ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"
