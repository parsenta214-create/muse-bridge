# Muse Bridge — Operation Notifications (操作中 / 操作終了) Spec (Draft)

- Status: Draft (design approved by Saardeim 2026-11-06; implementation not started; freeze needs Saardeim's approval)
- Source: Saardeim's proposal 2026-11-06 17:29 +07; analysis by subagent 2026-11-06 17:31–17:36 +07
- Version target: v2.1.0 / code 34 (minor feature bump; separate from the v2.0.0 P0 freeze, which is still pending and whose /launch_app background failure is NOT fixed by this feature)
- Local file: `workspace/computer-use-preview/SPEC-OPERATION-NOTIFY.md`

## 0. Goal

When the VM side operates the phone screen, the Bridge app posts a smartphone notification: "操作中" (with the current action named) while operating, "操作終了" when done. The notification comes from the Bridge app (`dev.muse.bridge`) via its own `NotificationManager` — never from the Muse app.

## 1. Locked decisions

| # | Decision |
|---|---|
| D1 | Begin is **automatic, server-side**. The server posts/updates "操作中" when it starts executing a screen-changing operation. The VM sends **no** explicit begin. |
| D2 | End is two-layered: primary = explicit `POST /operation_end` from the VM; fallback = server auto-end after 10 s of no operation activity **and** no operation in flight. |
| D3 | **Dedicated notification channel** `muse-bridge-operation`, not a reuse of the update channel (`muse-bridge-updates`). Separate IDs: `3` = "操作中" (ongoing), `4` = "操作終了" (one-shot). |
| D4 | "操作中" is `setOngoing(true)` + `setOnlyAlertOnce(true)`; "操作終了" is a separate post (alerts once), `setAutoCancel(true)`, **stays in the shade until swiped** (same ID reused, so at most one ever sits there). |
| D5 | The notification is an **observer, never a gate**: if posting fails (permission revoked), the operation still runs; the failure is logged only. |
| D6 | Notification content carries **no user data**: no typed text, no URIs, no coordinates, no clipboard. Only fixed action labels (and the app label for `/launch_app`). |
| D7 | **Red line:** the notification's tap opens only the Bridge app's own `MainActivity`. It must never be used as a vehicle to launch other apps (that would be a backdoor around the background-activity-start restriction). |

## 2. Trigger set (screen-changing operations)

The hook fires per-branch in `handle()`, after the operation is accepted for execution (after `requireService` passes and after request validation, immediately before the service call). A central hook at the top of `handle()` is rejected: it would fire before validation/a11y checks, so a rejected request (503 a11y-off, 400 bad input, 409 stale) would falsely show "操作中".

| Endpoint | Label (content text; title is always "操作中") |
|---|---|
| `/click` | `タップ中` |
| `/swipe` | `スワイプ中` |
| `/type_text` | `文字を入力中` (never the typed text) |
| `/press_key` | `キー操作中（BACK）` — key name appended (fixed enum, safe) |
| `/click_node` | `タップ中` |
| `/type_node` | `文字を入力中` (never the typed text) |
| `/scroll_node` | `スクロール中` |
| `/node_action` | `操作を実行中（click）` — action name appended (fixed enum, safe) |
| `/batch` | `複数の操作を実行中` (do not summarise the 20 actions) |
| `/launch_app` | `アプリを起動中：K PLUS` — app label via `PackageManager.getApplicationLabel()`, fallback to the package name |
| `/open` | `リンクを開いています` (never the URI — URIs can carry tokens) |

End title is `操作終了`, end text is `Muse の操作が終わりました`.

Not triggers: `/state`, `/capture_screen`, `/events` (reads); `/notify` (does not change the screen); `/install_apk` (long async job); POST `/clipboard` (device state, not screen). `/notification_action` "open" is an open question (§6).

## 3. Java changes — `android-app/src/dev/muse/bridge/HttpBridgeServer.java` (only file that changes)

### 3.1 Constants (next to the existing `UPDATE_CHANNEL_ID` / `UPDATE_NOTIFICATION_ID`)

```java
private static final String OPERATION_CHANNEL_ID = "muse-bridge-operation";
private static final int OPERATION_NOTIFICATION_ID = 3;
private static final int OPERATION_END_NOTIFICATION_ID = 4;
private static final long OPERATION_AUTO_END_TIMEOUT_MS = 10_000;
```

### 3.2 Tracker state (fields on `HttpBridgeServer`, same pattern as the existing install-job `volatile` state)

- `private final Object operationLock = new Object();`
- `private boolean operationActive = false;`
- `private long operationLastActivityMs = 0;` (elapsedRealtime base, like the request deadline)
- `private int operationInFlight = 0;` (screen operations currently executing)
- `private ScheduledFuture<?> operationAutoEndFuture = null;`
- `private ScheduledExecutorService operationScheduler;` (single daemon thread, created in the constructor, shut down in `stop()`)

All tracker state is guarded by `operationLock`. `NotificationManager.notify()/cancel()` are thread-safe binder calls and may be invoked from worker threads or the scheduler thread.

### 3.3 New private methods (placed next to `postUpdateNotification`)

- `noteOperation(String label)` — called at the start of an accepted screen operation. Under lock: if not active, set active, cancel any stale "操作終了" (ID 4); update label + `lastActivityMs`; post/update ID 3 ("操作中", `setOngoing(true)`, `setOnlyAlertOnce(true)`, small icon `android.R.drawable.ic_dialog_info`, contentIntent → `MainActivity` via `PendingIntent.getActivity` with request code `3`, `FLAG_UPDATE_CURRENT | FLAG_IMMUTABLE`); increment `operationInFlight`; (re)schedule the auto-end task. If notifications are disabled (`!nm.areNotificationsEnabled()`), skip the post but still maintain the tracker state, and record one throttled `BridgeLog.logThrottled("op-notify-disabled", ...)` entry.
- `operationFinished()` — called from `handle()`'s existing `finally` block. Under lock: decrement `operationInFlight` (floor 0). Track with a local `boolean opNoted = false;` initialised before the `try`, set to `true` by the `noteOperation()` call sites.
- `endOperation()` → `boolean wasActive` — explicit end (used by `/operation_end`). Under lock: if not active, return `false` and post nothing. Otherwise set inactive, cancel the pending auto-end future, cancel ID 3, post ID 4 ("操作終了", `setAutoCancel(true)`, same contentIntent), return `true`.
- `scheduleAutoEndLocked()` — cancels any pending future, schedules a task at `lastActivityMs + OPERATION_AUTO_END_TIMEOUT_MS`. The task, when it fires, re-acquires the lock and ends the session **only if** `operationActive && operationInFlight == 0 && now - lastActivityMs >= OPERATION_AUTO_END_TIMEOUT_MS`; otherwise it reschedules (debounce that keeps a long `/batch` from auto-ending mid-run).
- `postOperationRunning(String label)` / `postOperationEnded()` — the two `Notification.Builder` constructions (channel creation inside each, same idempotent `createNotificationChannel` pattern as `postUpdateNotification`).
- `cancelOperationNotifications()` — cancels IDs 3 and 4; called from `stop()` and from `start()` (a restart must clear notifications left in the shade by a previous process — notifications survive process death).

### 3.4 Lifecycle — `start()` / `stop()`

- Constructor: create the single-thread daemon `ScheduledExecutorService` (thread name `muse-bridge-op-timer`).
- `start()`: after listeners are bound, cancel IDs 3 and 4 (clear stale notifications from a previous process).
- `stop()`: cancel the pending auto-end future, `shutdownNow()` the scheduler, set `operationActive = false` under lock, cancel ID 3 **silently** (do not post "操作終了" on shutdown — the bridge is going away, not ending an operation cleanly).

### 3.5 What does NOT change in Java

- `BridgeService.java` — the foreground notification (ID 1, channel `muse-bridge`) is untouched. The operation notification is a second, separate notification; do not merge them (the FGS notification must stay stable and show the bridge address).
- `MainActivity.java` — no change. (`POST_NOTIFICATIONS` is already requested on first launch; the deferred P1 BANKING_SAFE UI bug in `refresh()` is untouched.)
- `AndroidManifest.xml` — no change. `POST_NOTIFICATIONS` is already declared; no new permission is needed.
- `strings.xml` — no change required. Follow the existing notification-code convention (`postUpdateNotification` hardcodes its channel name in Java); keep the two titles and the per-action labels as constants in `HttpBridgeServer.java`.
- `BridgeAccessibilityService.java`, `BridgeNotificationListener.java`, `BridgeLog.java`, `BridgeTileService.java`, `BootReceiver.java` — no change.
- The class-level endpoint Javadoc at the top of `HttpBridgeServer.java` gains one line for `/operation_end`.
- `/health`, `currentMode()`, `buildModules()`, `buildCapabilities()`, `overallStatus()`, `unavailableJson()`, `launchApp()`, `openUri()` — untouched.

## 4. New endpoint: `POST /operation_end`

| Aspect | Specification |
|---|---|
| Path / method | `POST /operation_end` |
| Auth | Same `X-Bridge-Token` check every endpoint gets (before the body is read). |
| a11y required? | **No.** It must work in MINIMAL — the whole point is sessions that include a11y-independent operations (`/launch_app`, `/open`). Place the branch where it cannot hit `requireService`. |
| Request body | None required. Accept an empty body or `{}`. If a non-empty body is present it must be valid JSON (the shared `JSONException` catch already turns bad JSON into 400); ignore any fields. |
| Response (200) | `{"ok": true, "was_active": true}` — `was_active` is `false` when no session was active (idempotent end; posts nothing in that case). |
| Error cases | `401` wrong/missing token (same as all endpoints). `400` malformed JSON body. No 503: ending never requires a11y. No 422: there is nothing to validate. |
| Retry | **Never retried** by the client (POST, not `/batch`). If the end signal is lost to a transport error, the server-side auto-end is the fallback — that is the design's purpose. |
| Server behaviour | Calls `endOperation()`: cancels ID 3, posts ID 4 ("操作終了"), cancels the pending auto-end future. |
| Logging | `BridgeLog.log("operation_end", was_active ? "ended" : "idle")` — no label, no user data. |

No `GET /operation_status` in v1 (not requested; the VM already knows when it operates).

## 5. Python client changes — `android_bridge.py` (+ `bridge_protocol.py`)

### 5.1 New method on `AndroidAdapter`

```python
def operation_end(self) -> dict:
    """End the current operation session (POST /operation_end)."""
```

- Sends `POST /operation_end` with body `{}` via `_app_post` (exactly-once, never retried — same as every non-batch POST).
- **Not gated by `actions_enabled`** — same rationale as `notify()`: it only dismisses a notification, it changes nothing on screen.
- Returns the server dict `{"ok": true, "was_active": bool, "transport": "app"}`.

### 5.2 New context manager on `AndroidAdapter`

```python
@contextlib.contextmanager
def operation_session(self):
    """Wrap a whole task's screen operations: guarantees the explicit
    end signal is sent in a finally, even if the task raises.
    Sends nothing on entry — begin is automatic, server-side."""
    try:
        yield
    finally:
        try:
            self.operation_end()
        except Exception:
            pass  # best-effort; the server auto-end is the fallback
```

- Entry sends **nothing** (D1). The `finally` swallows end-signal failures deliberately: a failed end must never mask the task's own exception, and the server auto-end covers it.
- Usage: callers wrap a **whole task** (a sequence of operations), not individual actions.

### 5.3 Confirm: no explicit begin from the client — and no per-action wrapping

- **Begin stays automatic, server-side.** (1) the server knows exactly which requests execute — a VM-sent begin would fire even for requests the server then rejects (503 a11y-off, 409 stale, 400 bad input); (2) a VM crash between begin and end cannot strand the session, because the server owns the state and its auto-end cleans up; (3) any current or future client gets the behaviour for free.
- **Do NOT wrap individual action methods** (`click`, `swipe`, `launch_app`, …) in `operation_session()`. That would send an end after every single tap and defeat the session concept.
- **Do NOT auto-wrap** `wait_for_text` / `click_text` sequences either, unless a later review decides multi-step helpers should own a session.
- No change to `_app_request`, `_app_request_once`, retry policy, `AccessibilityUnavailableError`, `check_app()` / `get_health_detail()`, or the CLI `main()` (no new CLI subcommand for v1).

### 5.4 `bridge_protocol.py`

- Add a trivial builder `build_operation_end() -> dict` returning `{}` (validated empty body), keeping the Step-3 convention that every POST body goes through a protocol builder.

## 6. Edge cases

1. **Single quick tap (flash).** A tap + immediate end flips 操作中 → 操作終了 within well under a second; the "操作中" state may be barely perceptible, but the "操作終了" record remains in the shade, so the user still sees that an operation happened. **Acceptable for v1.** Do **not** add a grace delay before first posting. (Tuning knob if the user reports the flash as noise.)
2. **Overlapping sessions / concurrent clients.** One global session, no session IDs, no per-client tracking (single-user reality). A second operation arriving while a session is active just refreshes the label and the last-activity timestamp. An explicit end ends the session even if another operation is in flight (the VM is authoritative on session boundaries); a later operation re-begins a fresh session.
3. **Server restart mid-operation.** `stop()` cancels ID 3 silently and shuts down the scheduler; `start()` cancels IDs 3 and 4 to clear stale notifications left by a previous process. A restart can never leave a stuck "操作中" in the shade.
4. **a11y off vs on.** The hook lives in `handle()`, independent of `requireService`, so the feature works identically in MINIMAL, BANKING_SAFE, and FULL_ACCESS. In MINIMAL, `/launch_app` and `/open` still trigger "操作中" (the operation was attempted) even though most other screen endpoints answer 503 — and rejected 503s do **not** trigger it, because the hook sits after `requireService` passes.
5. **"操作終了" disposition.** **Stays in the shade until swiped** (`setAutoCancel(true)`, fixed ID 4 reused so repeated ends replace each other — at most one ever sits there). The user explicitly wants to see that the operation ended; auto-dismissing after N seconds risks the signal being missed.
6. **Notification permission revoked.** `POST_NOTIFICATIONS` is already declared and requested on first launch. If revoked later, posting fails silently: the operation still runs, the tracker state is still maintained, one throttled `BridgeLog` entry notes it. The operation is never failed because a notification could not post (D5). The VM can already see this coming via `/health` → `capabilities.notification_post` (P0-3, A4).
7. **Self-observation via the notification listener.** `BridgeNotificationListener.onNotificationPosted` records **all** notifications, including the bridge's own (package `dev.muse.bridge`). So the "操作中"/"操作終了" events will appear in `GET /notifications`. Expected and arguably useful, but it has one concrete side effect: `phone_watcher.py` counts `notifications_count` / `latest_notification` as "changed", so the bridge's own operation-end will look like a new notification to the watcher. Recommend the watcher filter out `package == "dev.muse.bridge"` events (one-line follow-up in the watcher, **not** part of this feature). The listener stores title/text — our labels contain no sensitive data by design (D6), so no PII enters the stream.
8. **Lock-screen visibility.** Notification content is visible on the lock screen by default. The labels are fixed action words plus, for `/launch_app`, the app label (e.g. `K PLUS`). The app label is acceptable: the launched app itself becomes visible on screen anyway. Never typed text, URIs, coordinates, or clipboard content (D6).
9. **Rapid operation sequences.** Every `noteOperation()` re-posts ID 3 with the new label; `setOnlyAlertOnce(true)` keeps updates silent — no buzz storm during a 20-tap session. The first post of a session alerts once (channel `IMPORTANCE_DEFAULT` — see §8).
10. **Long `/batch`.** A 20-action batch can occupy a worker for up to the 45 s server deadline. The in-flight counter + rescheduling auto-end task keeps "操作中" up for the whole batch; the session ends 10 s after the batch's last activity, or on explicit end.
11. **`/notify` itself does not trigger "操作中".** Posting an update notification does not change the screen; it is not in the trigger set. Same for `/install_apk` (long async job), POST `/clipboard` (device state, not screen).

## 7. Test plan

### 7.1 Python mock tests — `test_android_app_client.py` (new section, after the P0 sections)

The mock server already falls through to generic `200 {"ok": true, "echo": payload}` for unknown POST paths, so add one explicit branch for `/operation_end` returning `{"ok": true, "was_active": false}` (stateless mock; `was_active` is always false — the test pins the field's presence, not server state).

| Test | What it pins |
|---|---|
| `test_operation_end_posts` | `adapter.operation_end()` sends `("POST", "/operation_end", {})` and returns `ok: True` with `was_active` present. |
| `test_operation_end_not_gated` | Works with `actions_enabled=False` (notification-only, like `notify`). |
| `test_operation_end_not_retried` | `fail_next[("POST", "/operation_end")] = (503, 1)` → exactly 1 request in `MockAppHandler.requests`. |
| `test_operation_end_with_a11y_off` | `accessibility = False` → still 200 (must not require a11y). |
| `test_operation_session_sends_end_on_exit` | `with adapter.operation_session(): adapter.click(1, 2)` → `/operation_end` appears in the request log after the click. |
| `test_operation_session_sends_end_on_exception` | Body raises → `/operation_end` is still sent (finally). |
| `test_operation_session_swallows_end_failure` | `fail_next` 503 on `/operation_end` → exiting the context manager does not raise. |
| `test_plain_actions_do_not_send_operation_end` | A bare `click`/`swipe`/`launch_app` leaves no `/operation_end` in the request log — pins "no per-action auto-end". |

`test_bridge_protocol.py`: one test that `build_operation_end()` returns `{}`.

### 7.2 Java-side verification (no Java unit-test harness exists in this repo)

The Java logic (tracker, timer, notification posting) **cannot** be covered by the Python mock tests — the mock only proves the client sends the right requests. Java correctness must be established by (a) code review, (b) a GPT review round in the same form as CONS-20261106-022 (GitHub + Notion links), and (c) the live checks below. Say this explicitly in the spec; do not pretend the mock tests cover the Java side.

### 7.3 Live verification on the phone (after installing the new APK)

1. New channel appears: after the first operation, Settings → Notifications → Muse Bridge shows the "Muse Bridge の操作" channel.
2. a11y ON: VM tap → "操作中 / タップ中" appears; VM sends `/operation_end` → "操作終了" appears and "操作中" is gone.
3. Auto-end: VM tap, no end sent → "操作終了" appears by itself after ~10 s.
4. a11y OFF (MINIMAL): `/launch_app` attempt → "操作中 / アプリを起動中" appears (verifies the MINIMAL path; the launch itself still won't open the app — the known P0 failure — but the notification must still show).
5. Quick tap: single tap + immediate end → "操作終了" visible in the shade; note whether the "操作中" flash was perceptible.
6. Long batch: 20-action batch → "操作中" stays up for the whole batch (no mid-batch auto-end), then ends.
7. Restart: start an operation, stop the bridge via the QS tile → no stuck "操作中" remains in the shade.
8. Permission revoked: revoke `POST_NOTIFICATIONS` → the operation still succeeds, no notification appears, `BridgeLog` notes it (check `/logs`).
9. Shade accumulation: run three sessions → only one "操作終了" remains (ID reuse).
10. `/notifications` shows the bridge's own operation notifications (document, don't be surprised).
11. Tap the "操作中" notification → opens the Bridge app (`MainActivity`) and nothing else.
12. Lock screen: no sensitive content in the notification text.

## 8. Interaction with P0 and the deferred P1 items

- **P0 design: no conflict.** The hook is independent of a11y (works in MINIMAL per A1/A5); `/health`, mode derivation, `overallStatus`, and the structured 503 path are untouched. `notification_post` in `buildCapabilities` (A4) already reports whether posting is possible.
- **Deferred P1 items — untouched, no silent changes:** BANKING_SAFE UI bug; UI vs Health a11y-state mismatch; `tailscale_ips` staleness (A7); `install_apk` capability always true; `parse_health` cross-field validation.
- **The open P0 failure (`/launch_app` returns `ok: true` while Android silently blocks the background launch — confirmed live 2026-11-06 17:24–17:26): this feature does NOT fix it.** The notification shows the launch was *attempted*; it must never be presented as proof the app opened. The false-positive fix remains separate work.
- **`phone_watcher.py` interaction:** the watcher treats any new notification as "changed". The bridge's own operation notifications will register as new notifications. Recommend a one-line follow-up in the watcher (ignore `package == "dev.muse.bridge"` events) — flagged here, not part of this feature.
- **Optional `/health` extension (not required for v1):** an `operation_active: true/false` field would let the VM/watcher see whether an operation is in progress. Recommend deferring; the notification itself is the user-visible signal, and the VM already knows when it operates.

## 9. Risks (Android constraints)

- **Notification permission.** `POST_NOTIFICATIONS` is declared and requested on first launch; if the user revokes it, operation notifications silently don't appear. Mitigated by D5 (fail open, log only) and by the existing `capabilities.notification_post` signal in `/health`.
- **Foreground-service rules.** None triggered. Posting a notification from a worker thread via `NotificationManager` needs no service type; the existing FGS notification (ID 1) is untouched. No new service, no new permission.
- **Background-activity limits — the red line (D7).** The operation notification must never carry a `PendingIntent` that launches any app other than the Bridge app's own `MainActivity`. A notification `PendingIntent` *can* start activities from the background — that is exactly the restriction `/launch_app` ran into — so using it to smuggle an app launch would be a bypass of an OS security constraint, which the v2.0 principles forbid. Tap → `MainActivity` only. Also note the Android 12+ notification-trampoline restriction: a direct `PendingIntent.getActivity` to `MainActivity` is the normal, allowed pattern; do not route the tap through a broadcast receiver or service.
- **Channel importance.** Recommend `IMPORTANCE_DEFAULT` for the operation channel: the user asked for visibility, and the first "操作中" post of a session should alert once. Updates are silenced by `setOnlyAlertOnce(true)`; the "操作終了" post alerts once. If the user finds it noisy, the channel importance is user-configurable in system Settings per channel — no code change needed. (Alternative, if the user prefers silent: `IMPORTANCE_LOW`, banner only.)
- **Notification spam / shade clutter.** Bounded by design: one ongoing ID 3, one ID 4, both reused. No per-operation notification IDs.
- **Doze / battery.** No impact: posting a notification needs no exemption; the scheduler is one daemon thread.
- **Threading.** The only new concurrency is the single-thread `ScheduledExecutorService` plus `synchronized` tracker state. No `Handler`/`Looper` dependency, no interaction with the a11y main-thread timeouts. The scheduler is shut down in `stop()`; a leaked scheduler thread across restarts is prevented by creating it in the constructor and shutting it down in `stop()`.
- **Stale notifications across restarts.** Covered by cancelling IDs 3 and 4 in both `start()` and `stop()`.

## 10. Open questions (pending Saardeim's decision)

1. **`/notification_action` "open"** also changes the foreground app, but it is outside the approved trigger list. Recommend including it (label `通知を開いています`) — confirm or explicitly exclude.
2. **`operation_active` in `/health`?** Recommend deferring (§8).
3. **Channel importance** `DEFAULT` (alerts once per session) vs `LOW` (silent banner)? Recommend `DEFAULT`.
4. **Grace delay for sub-second operations?** Recommend none for v1 (§6.1).

## 11. Rollout (process, not code)

1. **Spec doc:** this file + a Notion Draft page (same Documents DB pattern as the P0 spec), rather than extending the P0 spec — keeps the P0 freeze track clean. Record the adoption decision in the README as the next numbered decision (88), anchored on the end of decision 87's body per the `AGENTS.md` lesson.
2. **Version:** minor feature bump — `v2.1.0` / code `34` in `android-app/build.sh` (the P0 freeze is still pending and `/launch_app` is still broken; do not fold this into a v2.0.0 freeze).
3. **Review:** GPT review round in the same form as CONS-20261106-022 (GitHub commit + Notion spec linked together, read-only access), then Saardeim's freeze approval (freeze approval is Saardeim only). Full suite must pass (221 + new tests).
4. **Handoff to the phone:** APK via chat attachment → CX File Explorer → installer, update install (never uninstall; token is preserved). Then the §7.3 live checklist.

## 12. Files touched (complete list)

| File | Change |
|---|---|
| `android-app/src/dev/muse/bridge/HttpBridgeServer.java` | Constants, tracker fields/methods, 11 hook calls, `/operation_end` branch, `start()`/`stop()` cleanup, endpoint Javadoc line |
| `android-app/build.sh` | Version bump to `2.1.0` / code `34` (at implementation time) |
| `android_bridge.py` | `operation_end()` method + `operation_session()` context manager |
| `bridge_protocol.py` | `build_operation_end()` trivial builder |
| `test_android_app_client.py` | Mock `/operation_end` branch + 8 new tests (§7.1) |
| `test_bridge_protocol.py` | 1 test for `build_operation_end()` |
| `SPEC-OPERATION-NOTIFY.md` (new) + Notion Draft page (new) | Spec for this feature |
| `README.md` (computer-use-preview) | Next numbered decision (88) |

**Not touched:** `BridgeService.java`, `MainActivity.java`, `AndroidManifest.xml`, `strings.xml`, `BridgeAccessibilityService.java`, `BridgeNotificationListener.java`, `BridgeLog.java`, `BridgeTileService.java`, `BootReceiver.java`, `phone_watcher.py` (one-line filter flagged as a follow-up), the P0 spec, and all five deferred P1 items.
