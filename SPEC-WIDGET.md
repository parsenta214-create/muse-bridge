# Muse Bridge — Home-Screen Widget Spec (Draft)

- Status: DRAFT (not frozen)
- Version target: v2.6.4 / code 43 (fix round, CONS-037 P1/P2; original widget v2.6.0 / code 39)
- SHA-256: (filled at Notion build time)

## 1. Purpose

A home-screen widget for the Muse Bridge app (dev.muse.bridge) so Saardeim can see the bridge state and start/stop the server without opening the app — the same one-tap convenience as the Quick Settings tile (BridgeTileService, v1.7), on the home screen.

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
  - `onReceive`: handles the toggle action (start/stop service, W6 fallback), the open-app action, and `ACTION_A11Y_STATE_CHANGED` → `updateAllWidgets`.
  - Toggle PendingIntent: broadcast back to the provider (explicit intent, `FLAG_UPDATE_CURRENT | FLAG_IMMUTABLE`).
- `android-app/res/layout/widget_bridge.xml` — RemoteViews layout (status / mode / a11y TextViews + Start-Stop + Open Buttons).
- `android-app/res/xml/bridge_widget_info.xml` — `appwidget-provider`: `minWidth`/`minHeight` for 2x1, `updatePeriodMillis="0"`, `resizeMode="horizontal|vertical"`, `widgetCategory="home_screen"`, `initialLayout="@layout/widget_bridge"`.

### Modified files

- `AndroidManifest.xml` — new `<receiver android:name=".BridgeWidgetProvider">` with `APPWIDGET_UPDATE` + `ACTION_A11Y_STATE_CHANGED` intent-filter and `appwidget-provider` metadata. Exported true (the system must be able to send APPWIDGET_UPDATE).
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

## 8. Fix round — CONS-037 P1/P2 (v2.6.4)

- Status: implemented 2026-11-08, re-review pending (CONS-049). The fix round resumes work stopped by the 2026-11-07 02:47 stop order, on Saardeim's decision 2026-11-08 to take the widget next. The receiver and provider changes below were drafted before the stop (in the computer-use-preview tree) and adopted as-is onto the v2.6.3 code; MainActivity there was stale (pre-UI-improvement) and was NOT carried over.
- P1 (CONS-037, confirmed again in CONS-048): the exported `BridgeWidgetProvider` handled the toggle action, so any app could toggle BridgeService with an explicit broadcast. Fix: the toggle PendingIntent now targets a separate non-exported receiver, `BridgeWidgetActionReceiver` (exported=false, no intent-filter — only explicit intents from our own app, same UID, reach it). The exported provider handles only display + system updates + the a11y-state redraw; it never handles a control action. Do NOT "fix" this by setting the provider exported=false — the system must be able to send APPWIDGET_UPDATE to it.
- P2 (CONS-037, confirmed again in CONS-048): a server-start failure was invisible — the widget (and MainActivity) showed merely "stopped". Fix: the action receiver, on the start path only, checks 2 s after the start request whether the service is running; if it still is not, it opens MainActivity so the user sees the failure and can tap Start there. The 2 s delay is deliberate: a cold start can be slow, and opening the app on a slow-but-succeeding start would be a false fallback. A bind failure is decided synchronously in BridgeService.onCreate, so a service still not running at 2 s almost certainly failed. Residual risk (honest note): on a very slow phone the service could still be starting at 2 s and the app opens unnecessarily — annoying, not harmful (the app then shows the true state). Live verification must watch for this.
- New files: `BridgeWidgetActionReceiver.java` (toggle handling + 2 s failure check + W6 sync fallback, same as the QS tile).
- Modified files: `BridgeWidgetProvider.java` (toggle handling and `toggleService` removed; toggle PendingIntent retargeted to the receiver), `AndroidManifest.xml` (new non-exported receiver entry).
- Version: v2.6.4 (code 43).
- Tests: the widget guard `test_widget_provider_handles_toggle_and_a11y` was replaced — the provider must handle a11y only, the receiver must be non-exported with no intent-filter, the receiver must handle the toggle and the 2 s failure check (start path only), and the receiver must never touch the token.
- Follow-up (not in this round, CONS-048 idea): distinguish STOPPED / RUNNING / FAILED as three states in BridgeService + MainActivity, so a start failure is visible without opening the app. Needs a spec decision first.

## 9. 採用記録（2026-11-08）

- 採用決定：2026-11-08 00:51 +07、サイガマンが v2.6.4 の採用を決定。
- 根拠：GPT 再レビュー CONS-20261107-049 の判定は条件付き採用（P0 なし、新規 P1 なし）。P1・P2 の修正を確認済み。
- 実機確認（サイガマン、2026-11-08 00:49–00:51 +07）：APK v2.6.4/code 43 をインストール（ヘルスチェックで確認）。ウィジェットから停止 → サービス停止を確認（ヘルスチェックで down、アプリ 2.6.4 のまま、a11y オン）。ウィジェットから開始 → サービス復旧を確認（ヘルスチェックで up、4層とも正常）。停止・開始のいずれでもアプリが勝手に開いたという報告はなし。
- 採用範囲：v2.6.4（code 43）、コミット 13f31f4。テスト 266 passed + 18 subtests passed。
- 未決のまま：P2-02（起動要求の識別子導入）は将来の改善ラウンドへ延期。CONS-048 の 3 状態表示（STOPPED / RUNNING / FAILED）も引き続き未決。
