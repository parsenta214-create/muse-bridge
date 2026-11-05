# Muse Bridge — Android bridge app (Phase 4, app transport)

A minimal Android app that lets the VM drive the Galaxy A55 5G screen
without ADB, USB, or wireless debugging. It uses an AccessibilityService
for screenshots (`takeScreenshot`, API 30+), taps/swipes
(`dispatchGesture`), text entry (`ACTION_SET_TEXT`), and global keys, and
serves them over a small HTTP server on the phone.

- Package: `dev.muse.bridge`, minSdk 30, targetSdk 34, pure Java, zero
  external dependencies (the HTTP server is a raw `ServerSocket`).
- The server binds to the phone's Tailscale IPv4 address (100.x) when
  present, plus 127.0.0.1, on port 8080.
- Auth: a token generated at runtime on first launch, shown in the app
  UI, stored in the app's own SharedPreferences. Every request needs it
  as the `X-Bridge-Token` header. No credential is in the source, and
  the token is never logged on either side.
- The bridge runs as a foreground service with a notification while
  started, and stops cleanly from the app. The VM side stays read-only
  by default (the existing action gate in `android_bridge.py` applies).

## Build (on the VM)

```bash
cd ~/workspace/computer-use-preview/android-app
./build.sh
# -> Muse_Bridge_v1.8.apk (debug-signed, sideloading only)
```

The build uses the manual aapt2 / javac / d8 pipeline — no Gradle,
because the VM has ~400 MiB free RAM and Gradle's daemon does not fit.
It needs:

- JDK 17 at `~/workspace/tools/jdk17` (Temurin, downloaded with curl)
- Android SDK at `~/workspace/tools/android-sdk` (see "SDK install" below)

Override with `ANDROID_HOME` / `JAVA_HOME` env vars. The build prints
`aapt2 dump badging` output and the APK size on success.

### SDK install (no sdkmanager — it cannot tunnel through this VM's proxy)

Java's `HttpURLConnection` fails in this VM with
`NoSuchElementException` in `doTunneling0` even though curl and raw
sockets CONNECT fine through the same egress proxy (a Java raw socket
to localhost even gets the runtime's "Other TCP connections is turned
off" reply). So the SDK packages were fetched as plain zips with curl
from the official repository and unpacked by hand:

```bash
export ANDROID_HOME=~/workspace/tools/android-sdk
# repository2-1.xml lists the packages; the zips used were:
#   build-tools_r34-linux.zip      -> $ANDROID_HOME/build-tools/34.0.0
#   platform-34-ext12_r01.zip      -> $ANDROID_HOME/platforms/android-34
#   platform-tools_r37.0.2-linux.zip -> $ANDROID_HOME/platform-tools
```

Sizes were checked against the repository (61,224,257 / 63,559,957 /
9,541,228 bytes). `aapt2`, `d8`, `apksigner`, `zipalign`,
`platforms/android-34/android.jar`, and `platform-tools/adb` are the
parts this project uses. Note: installing platform-tools also puts an
`adb` binary on the VM; the Phase 4 ADB transport is still untested
until the phone's wireless debugging is on.

## Install on the phone

1. Copy `Muse_Bridge_v1.8.apk` to the phone and install it (allow
   "install unknown apps" for the app you use to open the APK).
2. Open the **Muse Bridge** app.
3. Tap **Open accessibility settings**, find **Muse Bridge**, and turn
   it on. Confirm the dialog. (Android requires this by hand; no app
   can enable it for you.)
4. Back in the app, tap **Start bridge**. The status line should say
   "Bridge: running" and "Accessibility service: enabled".
5. Note the **bridge address** (e.g. `http://<phone-tailscale-ip>:8080`) and the
   **bridge token** shown in the app. The token is the only password;
   do not share it. Tap **Regenerate token** if it ever leaks.
6. Tell Muse the address and the token (via the secure entry, never in
   plain chat) so the VM client can connect. The VM client reads them
   from the `ANDROID_APP_URL` and `ANDROID_APP_TOKEN` environment
   variables at run time; they are never written to a file.
7. When done, tap **Stop bridge** in the app (and turn the
   accessibility service off if you no longer want it).

## Endpoints (all need `X-Bridge-Token`)

Coordinates are screen **pixels** (the VM client converts the model's
0–999 coordinates using the width/height from `/state`).

- `GET /health` → `{"ok":true,"service":"muse-bridge","version":1,"accessibility":true,"app_version":"1.8","app_version_code":9}`
- `GET /capture_screen` → PNG bytes
- `GET /state` → `{"width":1080,"height":2340,"package":"...","texts":["..."],"app_version":"1.8","app_version_code":9}`
- `POST /click` `{"x":500,"y":800}`
- `POST /swipe` `{"x1":..,"y1":..,"x2":..,"y2":..,"duration_ms":300}`
- `POST /type_text` `{"text":"hello"}` — sets text on the currently
  focused editable field; returns 422 when there is no focused
  editable field (tap the field first).
- `POST /press_key` `{"key":"HOME"|"BACK"|"RECENTS"|"POWER"}`

## Known limits

- `takeScreenshot` returns nothing for windows the OS marks secure
  (e.g. banking apps, DRM video); the endpoint then returns 500.
- `type_text` needs a focused editable field; it cannot type into a
  field that was never tapped.
- The server is plain HTTP on the private Tailscale network only. Do
  not forward the port to the internet; the token is the only auth.
- The VM side is verified against the real phone since v1.1 (screenshots,
  taps, swipes, typing, node actions, batches, events). See the main
  `README.md` frozen decisions for the verification log.
