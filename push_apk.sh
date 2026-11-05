#!/bin/bash
# Push the Muse Bridge APK straight to the phone's Download folder over
# the existing Termux SSH channel (same ProxyCommand as android_bridge.py).
# The user then taps the file once to install (Android requires a manual
# install tap for sideloaded APKs — there is no silent auto-update).
# Usage: ./push_apk.sh [path/to/Muse_Bridge_vX.Y.apk]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# The bridge token (for the notify step) comes from .env, same as the
# other tools. Never print it.
if [ -f "$HERE/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$HERE/.env"
    set +a
fi
APK="${1:-$(ls -t "$HERE"/android-app/Muse_Bridge_v*.apk 2>/dev/null | head -1)}"
if [ -z "$APK" ] || [ ! -f "$APK" ]; then
    echo "no APK found: pass the path, e.g. ./push_apk.sh android-app/Muse_Bridge_v1.2.apk" >&2
    exit 1
fi
NAME="$(basename "$APK")"
LOCAL_SHA="$(sha256sum "$APK" | cut -d' ' -f1)"

PROXY_CMD="python3 $HERE/android_bridge.py _proxy %h %p"
# Note: scp takes -P (capital) for the port; ssh takes -p.
# No hardcoded phone address: ANDROID_PHONE_IP / ANDROID_TERMUX_USER come
# from .env (sourced above); removed 2026-11-06 when the code went public.
SSH_PORT="${ANDROID_TERMUX_SSH_PORT:-8022}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new
          -o "ProxyCommand=$PROXY_CMD" -p "$SSH_PORT" -i "$HOME/.ssh/id_ed25519")
SCP_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new
          -o "ProxyCommand=$PROXY_CMD" -P "$SSH_PORT" -i "$HOME/.ssh/id_ed25519")
if [ -z "${ANDROID_PHONE_IP:-}" ] || [ -z "${ANDROID_TERMUX_USER:-}" ]; then
    echo "ANDROID_PHONE_IP and ANDROID_TERMUX_USER are not set; put them in .env (see .env.example)" >&2
    exit 1
fi
REMOTE="${ANDROID_TERMUX_USER}@${ANDROID_PHONE_IP}"

echo "== pushing $NAME ($(stat -c%s "$APK") bytes) to the phone =="
if ! scp "${SCP_OPTS[@]}" "$APK" "$REMOTE:storage/downloads/$NAME" 2>/tmp/push_apk_scp.err; then
    # storage/downloads needs termux-setup-storage; fall back to Termux home.
    echo "storage/downloads failed; trying Termux home instead" >&2
    cat /tmp/push_apk_scp.err >&2 || true
    scp "${SCP_OPTS[@]}" "$APK" "$REMOTE:$NAME"
    DEST="~/$NAME"
else
    DEST="~/storage/downloads/$NAME"
fi

echo "== verifying on the phone =="
REMOTE_SHA="$(ssh "${SSH_OPTS[@]}" "$REMOTE" "sha256sum $DEST" | cut -d' ' -f1)"
if [ "$REMOTE_SHA" != "$LOCAL_SHA" ]; then
    echo "SHA MISMATCH: local $LOCAL_SHA phone $REMOTE_SHA" >&2
    exit 1
fi
echo "OK: $DEST sha256:$LOCAL_SHA"
echo "On the phone, tap the file to install (update). No uninstall needed."
if [ "$DEST" = "~/$NAME" ]; then
    # Termux-home fallback: open the installer directly (the file is
    # not in Downloads, so a notification pointing at Downloads
    # would mislead). Non-fatal if Termux:API is missing.
    ssh "${SSH_OPTS[@]}" "$REMOTE" "termux-open ~/$NAME" \
        && echo "installer opened on the phone" \
        || echo "note: termux-open failed — install from the chat attachment instead"
    NOTIFY_TEXT="$NAME がTermuxのホームに届いたよ。インストール画面を開いたから、そこで「インストール」をタップしてね。"
else
    NOTIFY_TEXT="$NAME がダウンロードフォルダに届いたよ。タップしてインストールしてね。"
fi
echo "== notifying the bridge app (v1.3+) =="
python3 "$HERE/android_bridge.py" notify --transport app \
    --title "Muse Bridge の更新" \
    --text "$NOTIFY_TEXT" \
    || echo "note: notify failed (the installed app is older than v1.3, or notifications are not permitted) — install from Downloads manually"
