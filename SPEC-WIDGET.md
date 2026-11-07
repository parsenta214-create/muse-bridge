# Muse Bridge — Home-Screen Widget Spec (Draft)

- Status: DRAFT (not frozen)
- Version target: v2.6.0 / code 39 (new feature = minor bump, on top of the P2 code at commit 104e1bb2)
- SHA-256: (filled at Notion build time)

## 1. Purpose

A home-screen widget for the Muse Bridge app (dev.muse.bridge) so the System Development Lead can see the bridge state and start/stop the server without opening the app — the same one-tap convenience as the Quick Settings tile (BridgeTileService, v1.7), on the home screen.

## 2. What the widget shows

- Server state: running / stopped (from `BridgeService.isRunning()`).
- Effective mode: MINIMAL / FULL_ACCESS / BANKING_SAFE (same derivation as `/health` and MainActivity: `a11yOn ? FULL_ACCESS : persisted mode`).
- Accessibility service state: enabled / not enabled (from `BridgeAccessibilityService.getInstance() != null`).
- Last-updated time (when the widget last refreshed).

The widget shows NO IP address, NO token, NO notification content. (Standing rule: mask private info in user-facing text; the Tailscale IP is already in the foreground-service notification for the user who needs it.)

## 3. Locked decisions

- **W1 — Display fields.** Server state, effective mode, a11y state, last-updated time only. No IP, no token, no notification content.
- **W2 — Buttons.** Two buttons: Start/Stop toggle and Open app. Tapping the widget body also opens MainActivity.
  - Start/Stop toggles the foreground `BridgeService` ONLY. It does NOT enable the AccessibilityService (same rule as the QS tile and the app itself — Android requires a11y to be enabled by hand in Settings).
  - Open app opens MainActivity (where the user can enable a11y, toggle the mode, and see the full status).
- **W3 — Updates are event-driven; no periodic polling.** `updatePeriodMillis="0"`. Refresh triggers:
  - Widget added / `onUpdate` / `onEnabled`.
  - `BridgeService` start/stop (`onCreate` / `onDestroy` push an update).
  - Accessibility state change (`ACTION_A11Y_STATE_CHANGED` broadcast, already sent by BridgeAccessibilityService on connect/destroy).
  - Mode change (MainActivity pushes an update after the mode toggle).
  - After a toggle tap: refresh now (best effort) and again after a short delay (~600 ms, same as the QS tile), because the service flips its flag asynchronously in onCreate/onDestroy.
- **W4 — Widget never enables a11y itself.** A11y off is shown as a state only.
- **W5 — Widget never touches the HTTP API or the token.** It talks to the service via Intents only (same UID). No token string in widget code.
- **W6 — Start fallback.** Starting a foreground service from a widget tap is a user interaction, so `startForegroundService` is normally allowed. On `ForegroundServiceStartNotAllowedException` (or any failure), fall back to opening MainActivity so the user can tap Start there (same as the QS tile).
- **W7 — Size.** Resizable, minimum 2x1 cells (about 180dp x 110dp). Layout: status line, mode line, a11y line, two buttons. Compact; no scrolling.

## 4. Implementation plan

### New files

- `android-app/src/dev/muse/bridge/BridgeWidgetProvider.java` — `AppWidgetProvider`:
  - `updateAllWidgets(Context)` static helper: reads `BridgeService.isRunning()`, a11y via `BridgeAccessibilityService.getInstance()`, mode via MainActivity's derivation (persisted `KEY_MODE` + a11y), builds `RemoteViews` from `R.layout.widget_bridge`, calls `AppWidgetManager.updateAppWidget` for every widget id.
  - `onUpdate` / `onEnabled` → `updateAllWidgets`.
  - `onReceive`: handles `ACTION_A11Y_STATE_CHANGED` → `updateAllWidgets` only (the toggle action lives in `BridgeWidgetActionReceiver` — §8, CONS-037 P1).
  - Toggle PendingIntent: broadcast to `BridgeWidgetActionReceiver` (explicit intent, `FLAG_UPDATE_CURRENT | FLAG_IMMUTABLE`).
- `android-app/src/dev/muse/bridge/BridgeWidgetActionReceiver.java` — non-exported `BroadcastReceiver` (§8, CONS-037 P1):
  - Handles `ACTION_WIDGET_TOGGLE`: start/stop the service (W6 sync fallback to MainActivity), refresh now + after ~600 ms, and the async start-failure check (2 s delay, start path only — §8, CONS-037 P2).
- `android-app/res/layout/widget_bridge.xml` — RemoteViews layout (status / mode / a11y TextViews + Start-Stop + Open Buttons).
- `android-app/res/xml/bridge_widget_info.xml` — `appwidget-provider`: `minWidth`/`minHeight` for 2x1, `updatePeriodMillis="0"`, `resizeMode="horizontal|vertical"`, `widgetCategory="home_screen"`, `initialLayout="@layout/widget_bridge"`.

### Modified files

- `AndroidManifest.xml` — new `<receiver android:name=".BridgeWidgetProvider">` with `APPWIDGET_UPDATE` + `ACTION_A11Y_STATE_CHANGED` intent-filter and `appwidget-provider` metadata. Exported true (the system must be able to send APPWIDGET_UPDATE). Plus `<receiver android:name=".BridgeWidgetActionReceiver" android:exported="false" />` with no intent-filter (§8, CONS-037 P1).
- `BridgeService.java` — push `BridgeWidgetProvider.updateAllWidgets(this)` in `onCreate` (after `running` is set) and `onDestroy`.
- `MainActivity.java` — push `BridgeWidgetProvider.updateAllWidgets(this)` after the mode toggle.

### Build

- The existing `build.sh` (aapt2 compile/link over `res/`) picks up the new layout/xml resources automatically. No build-script change expected. The aapt2 link step validates the widget XML — that is the resource test.

## 5. Tests

The Python suite cannot run Java widget code (no Java test harness in this project — same limitation as P1-01/P1-06 and P2-03). Source-level guard tests in `test_android_app_client.py` (same honest pattern):

- Manifest declares the widget receiver with `APPWIDGET_UPDATE` and the `appwidget-provider` metadata.
- `bridge_widget_info.xml` exists with `updatePeriodMillis="0"`.
- `widget_bridge.xml` exists with the required view ids (status, mode, a11y, start/stop button, open button).
- Widget code never references the token (`getOrCreateToken` / `currentToken` absent from `BridgeWidgetProvider.java`).
- `updateAllWidgets` is called from `BridgeService.onCreate`/`onDestroy` and from the mode-toggle path.
- Docstrings state the honest limit: these guards prove structure in the source; actual widget behaviour needs live-phone testing.

Full suite must pass; APK rebuilt (v2.6.0/code 39).

## 6. Live verification (user, on the phone)

1. Install the v2.6.0 APK (update install — token preserved).
2. Long-press home screen → Widgets → Muse Bridge → place the widget.
3. Widget shows: stopped + MINIMAL (or current mode) + a11y state. Tap Start → server runs, widget shows running. Tap Stop → stopped.
4. Toggle a11y in Settings → widget's a11y line updates.
5. Toggle the mode in the app → widget's mode line updates.
6. Remove the widget from the home screen.

## 7. Known limitations

- The widget cannot enable the AccessibilityService itself (Android restriction, W4).
- No periodic refresh: if the process dies without an event, the widget can go stale until the next trigger (W3). The manual toggle tap refreshes it.
- The widget shows the phone-local state only; it cannot show the VM-side health-check view (proxy/tailscale/SSH layers) — that stays on the dashboard.
- Adding the widget to the home screen is the user's own action (long-press → Widgets); Muse cannot place it.

## 8. Fix round (CONS-037) — P1 + P2, before freeze

GPT review CONS-20261106-037 (2026-11-07): 条件付き不合格 — one P1 blocker + one P2. W1–W7 decisions unchanged; only the control path moved and the W6 fallback extended.

### P1 — external app could toggle the service

- Problem: `BridgeWidgetProvider` was `exported=true` and handled `ACTION_WIDGET_TOGGLE` in `onReceive`, so ANY app could send an explicit broadcast with that action and toggle `BridgeService`.
- Fix: the toggle path moved to a new non-exported receiver `BridgeWidgetActionReceiver` (`exported=false`, no intent-filter). Widget buttons' PendingIntents target it, not the provider. The exported provider now handles only `APPWIDGET_UPDATE` (via `super.onReceive`) and `ACTION_A11Y_STATE_CHANGED` (redraw only — an external spoof there can only force a redraw, no info leak).
- Guard tests: manifest declares the action receiver `exported=false`; provider source has no toggle handling; `buildViews` targets the action receiver.

### P2 — W6 async start failure not detected

- Problem: only sync exceptions from `startForegroundService()` were caught. If the call succeeded but `BridgeService.server.start()` returned false (bind failure), `isRunning()` stayed false and no fallback to MainActivity happened.
- Fix: after a start attempt, a delayed check opens MainActivity if the service is still not running. The check delay is 2 s — deliberately longer than the 600 ms refresh settle delay, so a slow cold start does not trigger a false fallback. The check runs only on the start path, never after a stop tap (the service is correctly not running then).
- Residual risk (honest): on a very slow phone the service could still be starting at 2 s, and the app would open unnecessarily — annoying, not harmful (the app then shows the true state, running once the service starts). Live verification (§6) must watch for this.
- Guard test: the delayed `isRunning()` check + MainActivity fallback exists in the receiver's toggle path (source-level only).
