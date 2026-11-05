# Muse Bridge

Operate an Android phone screen from a VM — no ADB, no USB, no wireless
debugging. Built to drive a Galaxy A55 5G over Tailscale, but the app works
on any Android 11+ device (minSdk 30).

Two halves:

- **`android-app/`** — a minimal Android app (pure Java, zero dependencies).
  An `AccessibilityService` does screenshots (`takeScreenshot`, API 30+),
  taps/swipes (`dispatchGesture`), text entry (`ACTION_SET_TEXT`), and
  global keys, and serves them over a small raw-socket HTTP server on the
  phone (Tailscale IPv4 + 127.0.0.1, port 8080). Every request needs the
  runtime token in the `X-Bridge-Token` header.
- **`android_bridge.py`** — the VM-side Python client (stdlib only):
  read-only by default; actions need `--allow-actions` plus an explicit
  user-approved action.

## What it can do (v1.6)

- Screenshots (half-scale JPEG by default; full PNG on request).
- Tap, swipe, type text, HOME / BACK / RECENTS / POWER.
- `/state`: the live node tree — every visible node with screen bounds,
  focus and input state, plus the focused node and keyboard state — so the
  operator decides the next action without a screenshot.
- `/click_node` / `/type_node`: act on a node by id, with a `tree_hash`
  staleness check (409 when the screen moved on).
- `/batch`: several actions in one request, with per-action ids and
  `expect_change` steps; re-sending a `batch_id` returns the cached result
  without re-executing (idempotency).
- `/events`: the recorded accessibility-event history (window changes,
  clicks, focus, text changes, scrolls, selections), coalesced and
  capped at 200; password text is always `"[REDACTED]"`. `/state` also
  carries the latest `event_seq`, so a client can tell from `/state`
  alone whether anything happened since its last poll.
- `POST /notify`: the app posts an update notification (tap opens
  Downloads) when a new APK is pushed.

## Setup

1. Build the app: `cd android-app && ./build.sh` — manual aapt2 / javac /
   d8 pipeline, no Gradle (the build VM has ~400 MiB free RAM).
   Output: `Muse_Bridge_v1.6.apk` (debug-signed, sideloading only).
2. Copy the APK to the phone and install it (allow "install unknown apps"
   for the app used to open the APK). Update installs keep the token.
3. Open **Muse Bridge**, enable its accessibility service, tap
   **Start bridge**. Note the bridge address and the token shown in the
   app UI.
4. On the VM, set `ANDROID_PHONE_IP` (your phone's Tailscale IP) and
   `ANDROID_APP_TOKEN` (from the app UI; when stored, only in a `.env`
   file, never in chat, logs, or source — see `.env.example`). The
   bridge URL defaults to `http://<ANDROID_PHONE_IP>:8080`, or set
   `ANDROID_APP_URL` directly. `push_apk.sh` pushes a new APK
   to the phone over Termux SSH and notifies the bridge app.

## Use

```bash
python3 android_bridge.py check --transport app
python3 android_bridge.py state --transport app
python3 android_bridge.py events --transport app --since 0 --limit 100
python3 android_bridge.py tap --transport app --allow-actions --x 540 --y 1200
python3 android_bridge.py batch --transport app --allow-actions \
  --batch-id b-001 --tree-hash <hash> \
  --actions '[{"action_id":"a1","type":"click_node","node_id":"n60"}]'
```

The Termux/rish and ADB transports from the earlier phases are still in
`android_bridge.py` (`--transport termux-rish|adb`), but the app transport
is the current route: wireless debugging and USB are unavailable on the
target phone, so Shizuku/ADB cannot start.

## Tests

```bash
python3 -m pytest -q
```

No phone needed: the app-transport tests run against a local mock HTTP
server that checks the token header and answers the same endpoints.

## Notes

- Screenshots are on-demand stills, not a live stream. Latency floor is
  ~0.75 s per request over the tunnel + Tailscale round trip; the design
  reduces round trips (node tree, batches, events) instead of chasing
  latency.
- `takeScreenshot` returns nothing for OS-protected secure windows
  (banking apps, DRM video).
- The `~/.muse-bridge-token` / `.env` token is scoped to the private
  Tailscale network. Rotate it by regenerating it in the app.
- `termux-open` silently fails to open the installer when Termux has
  been backgrounded a while (Android blocks the background activity
  start); bring Termux to the foreground first, or install from a chat
  attachment.
