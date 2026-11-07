# Muse Bridge v2.0 — P0 Startup Spec (Draft)

- Status: FROZEN (v2.0 P0 frozen by the System Development Lead, 2026-11-06 22:08 +07; README decision 93). Do not edit directly — make a new version for changes.
- Source: GPT consultation CONS-20261106-021 (P0-1/2/3); CONS-20261106-026 (P0-4); Muse verification notes in those logs
- Decision: README decision 87 (P0); README decision 90 (P0-4); README decision 93 (P0 freeze)
- Version: v2.2.0 / code 35 (live-verification build; GitHub 3e1099a7)

## 0. Principles (adopted)

- The management plane (HTTP, auth, logs, notifications, health) is independent of the operation plane (AccessibilityService screen control).
- MINIMAL is the standard startup state. FULL ACCESS is an explicit user switch.
- BANKING SAFE MODE is a named operational constraint on MINIMAL, not a separate feature set.
- A module failing must not take down the others. HTTP stays up if a11y or SSH fails.
- Startup success is redefined: success = available functions running normally AND unavailable functions reported with reasons. "All functions up" is NOT required.
- OS security constraints are never bypassed (no silent a11y enable, no Play Protect bypass, no silent install).

## 1. P0-1 — HTTP / Accessibility dependency separation

### Goal
KBank blocking the AccessibilityService must not stop the bridge. The HTTP server must work fully without a11y, and a11y-dependent calls must fail with a structured, machine-readable reason.

### Changes

1. `/launch_app` must NOT require the AccessibilityService.
   - Current: `HttpBridgeServer` calls `requireService(svc)` before `launchApp()` (P1-05, CONS-20261106-014, still open).
   - Fix: remove the `requireService` call on the `/launch_app` path. `launchApp()` uses `getLaunchIntentForPackage` + `startActivity` from the server context and does not need a11y.
   - Acceptance: `launch_app("com.twitter.android")` is processed with a11y OFF (live test on the phone). The RESULT semantics are defined by P0-4 (§4) — "returns ok" alone is not the acceptance; see P0-4 for the truthful `launched` / `unconfirmed` reporting.

2. Structured "unavailable" response for a11y-dependent endpoints.
   - When a11y is OFF, endpoints that need it return HTTP 503 with a JSON body:
     `{"ok": false, "status": "unavailable", "reason": "ACCESSIBILITY_DISABLED", "required_mode": "FULL_ACCESS", "detail": "<human-readable short text>"}`
   - Endpoints that need a11y (must return this when a11y is off): `/state`, `/capture_screen`, `/click`, `/swipe`, `/type_text`, `/press_key`, `/click_node`, `/type_node`, `/scroll_node`, `/node_action`, `/batch`, `/events`, `/notify` (only if it needs the service — verify; if it does not need a11y, it stays available).
   - Endpoints that must stay available without a11y (no change): `/health`, `/logs`, `/notifications`, `/notification_action` (listener-based, verify), `/open`, `/install_apk`, `/install_status`, `/clipboard` (GET/POST), `/launch_app` (after fix 1).
   - The Python client must surface the structured reason (not just a generic 503). `AndroidAdapter` should raise/return the `reason` and `required_mode` so callers can distinguish "a11y off" from "server down" from "overloaded".
   - Acceptance: with a11y OFF, each a11y-dependent endpoint returns the structured body (live test); with a11y ON, behaviour is unchanged (full suite passes).

3. No other behaviour change. No new endpoints in P0-1. No token/auth change.

### Tests
- Mock tests: /launch_app without a11y is processed (result semantics per P0-4 §4 — `unconfirmed` / VERIFICATION_UNAVAILABLE, not a bare ok); each a11y-dependent endpoint returns the structured 503 when a11y is off; available endpoints still return ok without a11y.
- Live test (after user enables the new APK and toggles a11y off): /launch_app processed (P0-4 result); /state returns the structured unavailable body; /health still ok.

## 2. P0-2 — MINIMAL as the standard startup state

### Goal
Make MINIMAL the formal default, visible to the user and to the VM, and define BANKING SAFE MODE as the banking-safe operating state.

### Changes

1. Startup default is MINIMAL.
   - The bridge server starts on START BRIDGE / QS tile / BootReceiver regardless of the a11y state. This is already the de-facto behaviour; P0-2 makes it the documented spec and adds the mode indicator below. No change to the boot path is required unless the review finds one.
   - The app MainActivity shows the current mode prominently: `Mode: MINIMAL` (or `FULL ACCESS`, or `BANKING SAFE`).
   - `/health` returns the current `mode` field (see P0-3 for the full object).

2. BANKING SAFE MODE (named operational constraint on MINIMAL).
   - Definition: MINIMAL plus the rules — (a) AccessibilityService is NOT used; (b) screen-operation APIs answer unavailable (P0-1); (c) banking apps take priority; (d) the bridge must NOT attempt to re-enable the AccessibilityService, and must NOT prompt the user to enable it while in this mode.
   - How it is entered: the user selects it (app UI toggle or a setting). It is NOT auto-detected (there is no Android API to detect a banking app blocking a11y — verified as a lead in CONS-20261106-021).
   - How it is left: the user switches back to MINIMAL or FULL ACCESS (turning a11y on again is a manual Settings action by the user).
   - The mode is persisted (SharedPreferences) so it survives an app restart. It is NOT a secret; the token is never stored in the mode/state file.
   - Acceptance: mode is visible in the app UI and in /health; entering BANKING SAFE does not attempt to enable a11y; leaving it and enabling a11y restores FULL ACCESS.

3. Mode transitions (spec).
   - Android boot → MINIMAL (server starts; a11y off unless the user enabled it before — a11y state itself is owned by Android Settings, not by the bridge).
   - MINIMAL → FULL ACCESS: user enables the AccessibilityService in Settings (manual). The bridge detects it and reports mode FULL ACCESS.
   - FULL ACCESS → MINIMAL / BANKING SAFE: user disables the AccessibilityService in Settings (manual). The bridge detects it and reports the new mode. The bridge never disables a11y itself.
   - BANKING SAFE → MINIMAL: user switches the mode back (app UI). No a11y change is made by the switch itself.

### Tests
- Mock/unit: mode persistence round-trips; /health mode field matches the persisted mode; BANKING SAFE does not call any a11y-enable path (code review + test).
- Live test: after install, app starts in MINIMAL; mode shown in UI and /health; user toggles a11y on → mode FULL ACCESS; user toggles a11y off → mode MINIMAL/BANKING SAFE as selected.

## 3. P0-3 — Per-module Health API

### Goal
Make the bridge's health machine-readable per module, so the VM can tell WHAT is wrong (not just "down"), and report the current Tailscale IP so the VM stops hardcoding it.

### Changes

1. Extend `/health` (backwards-compatible: keep all existing fields) with:
   - `mode`: `"MINIMAL"` | `"FULL_ACCESS"` | `"BANKING_SAFE"` (the current mode; see P0-2).
   - `overall_status`: `"HEALTHY"` | `"DEGRADED"` | `"CRITICAL"` | `"RECOVERING"`.
     - HEALTHY: all required modules for the current mode are up.
     - DEGRADED: a non-required module is down (e.g. a11y off in MINIMAL is NOT degraded; SSH down is DEGRADED, not CRITICAL).
     - CRITICAL: HTTP server or auth is broken, or the Core is stopping.
     - RECOVERING: a restart/reconnect is in progress.
     - Rule: a11y OFF is NEVER CRITICAL. In MINIMAL/BANKING_SAFE it is the normal state.
   - `modules`: an object with per-module status, e.g.
     `{"http": "HEALTHY", "tailscale": "HEALTHY", "accessibility": "DISABLED", "notification_access": "GRANTED", "termux_ssh": "UNKNOWN"}`
     - `termux_ssh` is reported as `"UNKNOWN"` (or `"UNAVAILABLE"`) from the bridge side, because the bridge cannot reliably check the Termux sshd itself; the VM checks SSH separately. Do NOT claim SSH status the bridge cannot verify.
   - `capabilities`: booleans for what the VM can actually do right now, e.g.
     `{"screenshot": false, "tap": false, "swipe": false, "text_input": false, "notification": true, "http_api": true, "launch_app": true, "install_apk": true}`
   - `tailscale_ips`: the phone's current Tailscale IPv4 address(es) the server is listening on (e.g. `["100.85.138.44", "100.111.74.42"]`). The bridge already binds these; return them so the VM can re-resolve.
   - Keep existing fields: `ok`, `service`, `version`, `app_version`, `app_version_code`, `accessibility`, `notification_listener`.

2. Python client: parse the extended /health. `check_app()` (or a new `get_health_detail()`) returns the mode, overall_status, modules, capabilities, and tailscale_ips. The existing `check_app()` ok/detail contract is unchanged for the health-check cron.

3. VM side (client only, no phone change): when a request to the stored phone IP fails, the client may re-resolve by trying the other known Tailscale IP(s) from the last good /health `tailscale_ips` (do NOT hardcode a single IP forever). Full MagicDNS adoption is OUT of P0 (unverified; see open items).

### Tests
- Mock tests: /health returns the extended object with the new fields; mode/overall_status/capabilities are consistent (e.g. a11y off + MINIMAL → overall HEALTHY, screenshot false); tailscale_ips is a list of strings.
- Live test: /health on the phone returns the extended object; values match the phone's actual state (a11y off → mode MINIMAL, screenshot false; notification_access granted).

## 4. P0-4 — Background Activity Launch / Launch Result Semantics

### Goal
Stop the /launch_app false positive (CONS-026, GPT consultation 2026-11-06; the System Development Lead adopted P0-4 on 2026-11-06). Change /launch_app's meaning from "launched the app" to "requested the launch and report the result". The bridge must honestly report Android's constraints to the VM instead of claiming success it cannot verify. This completes (does not retract) the P0-1 separation: the endpoint stays a11y-independent, but its result is now truthful.

**Scope exclusion (System Development Lead decision 2026-11-06 19:51 +07): banking apps (e.g. KBank / K PLUS) and other apps where heavy security is expected are EXCLUDED from the /launch_app target scope from the start.** They are not launch targets at all — not "best-effort", not "unconfirmed", but out of scope. The launch feature targets normal apps. (The KBank-a11y conflict remains the motivation for the HTTP/a11y separation in P0-1 — KBank blocks the AccessibilityService, so the bridge must work without a11y — but KBank itself is not a launch target.)

**Enforcement (System Development Lead decision 2026-11-06 20:16 +07, GPT P1 #4 in CONS-20261106-027): the exclusion is ENFORCED by the API with an explicit deny list, not just an operational declaration.** Rationale: the bridge accepts remote launch requests, so a caller could pass a banking package by mistake; the API must refuse it. The deny list is an explicit, hard-coded set of package names in `HttpBridgeServer.java` (a constant, no auto-detection — banking apps cannot be reliably auto-detected). Adding a package is a code change + test. The check runs BEFORE any launch attempt (before `getLaunchIntentForPackage`) and BEFORE the operation notification hook (a refused launch posts no "操作中" notification). A denied package returns HTTP 403 `{"ok": false, "error": "launch denied: <package> is on the deny list"}` (old error format, no `status` field — a policy refusal, not a launch outcome; it is NOT the `unavailable` state). The Python client surfaces the 403 as an error and never retries it. Initial list (verified 2026-11-06, Thai banks):
   - `com.kasikorn.retail.mbanking.wap` — K PLUS (KBank)
   - `com.bbl.mobilebanking` — Bualuang mBanking (Bangkok Bank)
   - `com.scb.phone` — SCB EASY
   - `com.krungsri.kma` — KMA (Krungsri Mobile App)
   (Krungthai NEXT and other banks are NOT on the initial list — their package names are unconfirmed; add only when verified.)

### Background (confirmed live)
- `launchApp()` calls `getLaunchIntentForPackage` + `startActivity` (FLAG_ACTIVITY_NEW_TASK) from the server context. On Android 10+, the background-activity-start (BAL) restriction can silently block the start without throwing, so `startActivity()` returning normally does NOT prove the app opened. Live 2026-11-06 17:24–17:26 (a11y OFF): X and KBank both returned `{"ok": true}` while the screen did not switch.
- AccessibilityService is NOT a BAL exception (Android's official exception list does not include it as an independent exception; GPT lead, high confidence). Do NOT assume a11y ON bypasses the restriction. A direct `startActivity()` from the a11y service is not guaranteed to succeed either.
- There is no unconditional background launch for a normal app. SYSTEM_ALERT_WINDOW, Device Owner, and ADB are out (not adopted).

### Changes

1. **Separate request, issue, and verification.** `/launch_app` handles three distinct events:
   - Request received: the HTTP request was accepted (auth + package validation).
   - Launch requested: `startActivity()` was issued (no exception).
   - Foreground confirmed: the target app was observed in the foreground after the request.
   - `startActivity()` not throwing is NEVER the success condition.

2. **Result states (vocabulary).** The response carries `status` plus `foreground_confirmed`:
   - `launched`: the target app was observed in the foreground after the request (`foreground_confirmed: true`).
   - `unconfirmed`: the launch was requested but foreground display could not be confirmed (`foreground_confirmed: false`, or `null` when verification was unavailable). This is NOT a failure and NOT a claim of being blocked — the app may not be in front for many reasons (BAL restriction, slow init, crash, lock screen, another app coming forward).
   - `unavailable`: the current mode cannot execute the launch request. Reserved in P0-4; /launch_app returns it only if a mode forbids launching (no such mode exists in P0-4 — the endpoint is available in both MINIMAL and FULL_ACCESS). Defined for completeness and future modes.
   - `failed`: a definite failure (package not installed, no launcher activity — HTTP 422 as today; other definite errors as they arise).
   - The response splits `accepted` (request received) from `ok` (launch confirmed). `ok` is true ONLY when `status` is `launched`.

3. **Foreground verification (P0 scope).**
   - FULL_ACCESS (a11y ON): verify via the AccessibilityService. `BridgeAccessibilityService` exposes the current foreground package (via `getRootInActiveWindow()` package name). After `startActivity()`, poll the foreground package at ~500 ms, ~1,000 ms, up to ~2,000–3,000 ms. If it equals the target package → `launched` (`verification: "accessibility"`). If it never matches within the window → `unconfirmed` with `reason: "FOREGROUND_NOT_CONFIRMED"` (`verification: "accessibility"`).
   - MINIMAL (a11y OFF): verification is unavailable (UsageStats is P1, not P0). After `startActivity()`, return `unconfirmed` with `reason: "VERIFICATION_UNAVAILABLE"`, `foreground_confirmed: null`, `verification: "none"`. Do NOT claim the launch succeeded or failed.
   - Verification only counts foreground observations AFTER the `startActivity()` call (record the time just before issuing it; never use a pre-request foreground event as success). If the target app is already in the foreground when checked after the request, that satisfies `launched` (the requested end state is achieved) — note it in the log.
   - Verification runs on the HTTP worker thread (NOT the service main thread — never block a11y callbacks). Bounded to ~3 s max; the whole /launch_app HTTP round trip stays within ~3–4 s. A slow/bank app must not be failed on a short timeout alone — use the full window before reporting `unconfirmed`.
   - No double-launch confusion: each request is independent; the verification is tied to that request's timestamp + package.

4. **Response format (P0-4).** All /launch_app responses carry the new fields (backward-compatible superset — `ok` and `error` keep their existing meanings where they apply):
   - Launched: `{"ok": true, "status": "launched", "accepted": true, "package": "<pkg>", "launch_requested": true, "foreground_confirmed": true, "verification": "accessibility"}` (HTTP 200).
   - Unconfirmed (verified, not confirmed): `{"ok": false, "status": "unconfirmed", "reason": "FOREGROUND_NOT_CONFIRMED", "accepted": true, "package": "<pkg>", "launch_requested": true, "foreground_confirmed": false, "verification": "accessibility"}` (HTTP 200 — the request was processed; `ok: false` reflects the launch outcome, not an HTTP error).
   - Unconfirmed (verification unavailable): `{"ok": false, "status": "unconfirmed", "reason": "VERIFICATION_UNAVAILABLE", "accepted": true, "package": "<pkg>", "launch_requested": true, "foreground_confirmed": null, "verification": "none"}` (HTTP 200).
   - Failed (package): HTTP 422 `{"ok": false, "error": "<message>"}` (unchanged).
   - Failed (launch refused with an exception): HTTP 503 `{"ok": false, "error": "launch blocked by the system"}` (unchanged; only when `startActivity()` actually throws).
   - The Python client must parse `status` / `foreground_confirmed` / `verification` and surface them (not just `ok`). Callers must evaluate `status` and `foreground_confirmed`, not `ok` alone. `AndroidAdapter.launch_app()` returns the parsed result dict.
   - The operation notification ("操作中 / アプリを起動中：<label>") is unchanged (it shows the attempt, never proof the app opened — see the v2.1 operation-notify spec).

5. **Capabilities (P0-3 amendment).** The existing `launch_app: true` field is kept for backward compatibility and keeps its A5 meaning ("endpoint available"). Add two fields so the VM does not misread it:
   - `launch_app_guaranteed`: `false` (always false in P0-4 — launch success is never guaranteed).
   - `foreground_verification`: `true` when a11y is ON (the a11y service can verify), `false` when a11y is OFF (until UsageStats is added in P1).
   - The Python `parse_health` validation accepts the new fields when present (old-server compat preserved).

6. **Acceptance criteria change (replaces the P0-1 / §6 KBank guarantee).** The old criterion "with a11y OFF, the bridge HTTP works and KBank launches" is removed — it is not achievable under Android's BAL restriction and must not be promised. The new criteria are:
   - With a11y OFF, the HTTP server works normally and /launch_app processes the request.
   - When a launch is confirmed, it is reported as `launched` with `foreground_confirmed: true`.
   - When a launch cannot be confirmed, it is reported as `unconfirmed` (never as success).
   - A block by Android's restriction is never reported as success.
   - The meaning of the result is consistent across a11y ON and OFF (only the verification method differs).
   - The VM client surfaces `status` / `foreground_confirmed` (not just `ok`).

### Explicitly NOT in P0-4 (P1)
- UsageStatsManager verification (PACKAGE_USAGE_STATS / Usage Access permission; user grants in Settings). Optional P1 verification module for MINIMAL.
- FULL_ACCESS launcher operation (GLOBAL_ACTION_HOME → find the app icon → tap → verify foreground). Optional P1 additional path; not a BAL bypass, just user-like screen operation. Reliability depends on launcher type, app drawer, folders, lock screen.
- Any new permission acquisition flow, any SYSTEM_ALERT_WINDOW use, any Device Owner / ADB path.

### Tests
- Mock tests: /launch_app with a11y ON + foreground match → `launched`; a11y ON + no match within the window → `unconfirmed` / FOREGROUND_NOT_CONFIRMED; a11y OFF → `unconfirmed` / VERIFICATION_UNAVAILABLE; package not installed → 422 (unchanged); startActivity throws → 503 (unchanged); **deny-listed package (e.g. KBank) → HTTP 403 with the deny-list error, no launch attempted, no operation notification posted**; non-denied package → processed normally. /health carries `launch_app_guaranteed: false` and `foreground_verification` matching the a11y state. Python client parses the new fields.
- Live test (after install, a11y ON): launch X → `launched` (foreground confirmed). Live test (a11y OFF): launch X → `unconfirmed` / VERIFICATION_UNAVAILABLE (and the screen does not switch — the known BAL behaviour — but the response no longer claims success). Banking apps (e.g. KBank) are OUT OF SCOPE as launch targets (the System Development Lead, 2026-11-06 19:51) and are not tested as launch targets; **the deny list is verified by requesting KBank and confirming the 403 refusal (no app opens)**.

## 5. Out of P0 (explicitly NOT in this spec)

- P1: VM-side watchdog formalisation, Tailscale IP dynamic re-resolution in the client (beyond returning the IPs), post-update self-diagnosis.
- P1: UsageStatsManager foreground verification (PACKAGE_USAGE_STATS / Usage Access permission; optional verification module for MINIMAL).
- P1: FULL_ACCESS launcher operation (GLOBAL_ACTION_HOME → find the app icon → tap → verify foreground) as an additional launch path.
- P2: Termux SSH independent management, auto-recovery backoff in the bridge.
- P3: separate watchdog process.
- MagicDNS adoption (unverified; needs a live name-resolution check before adopting).
- Any change to the token, auth, or the tunnel proxy.
- Any bypass of Play Protect, the install confirmation dialog, or Android's a11y-enable requirement.
- Any SYSTEM_ALERT_WINDOW use, Device Owner, or ADB path for launching apps.

## 6. Acceptance (P0 as a whole)

- All P0 items (P0-1, P0-2, P0-3, P0-4) implemented, mock-tested, and live-verified on the phone (v2.0.0 APK or the agreed version).
- Full test suite passes (current baseline 197; target = 197 + new P0 tests).
- README decision(s) recorded; Notion draft pages updated to Frozen after the System Development Lead's freeze approval; GitHub push with the new version.
- The launch-reporting criteria (P0-4) are verified: with a11y OFF, /launch_app processes the request and reports `unconfirmed` / VERIFICATION_UNAVAILABLE (never success); with a11y ON, a confirmed launch reports `launched` with `foreground_confirmed: true`. The old criterion "with a11y OFF, KBank launches" is REMOVED (not achievable under Android's BAL restriction — see P0-4). Banking apps (e.g. KBank / K PLUS) and other heavy-security apps are OUT OF SCOPE as launch targets (the System Development Lead, 2026-11-06 19:51); they are not tested as launch targets. The live launch test uses a normal app (e.g. X).

## 7. Open items / risks

- MagicDNS: unverified. Do not adopt until a live check proves name resolution works through the tunnel proxy.
- `/notify` and `/notification_action` a11y-dependence: verified in code — neither needs a11y (both stay available). `/events` DOES need a11y (reads the service's event buffer).
- `termux_ssh` status from the bridge: the bridge cannot verify sshd; report UNKNOWN and let the VM check SSH directly.
- Android 12+ foreground-service and Android 14 service-type constraints (GPT lead): verify against the manifest during implementation; do not assume.
- The bridge server restart loop (09:32–15:35 downtime) is addressed by P1 (post-update self-diagnosis) and P2 (backoff), not by P0. P0 does not by itself prevent a future long downtime; it makes the state visible (Health API) and the KBank conflict structural.

## 8. Implementation decisions (made during P0 implementation, 2026-11-06)

- **"error" field kept in the structured 503 body.** The spec body has no `error` key, but `bridge_protocol.parse_error_body` requires `ok` + `error`. The body is a superset of the spec (adds `error`), zero cost, keeps the helper working.
- **A1 (mode store):** persisted mode string `KEY_MODE` ("MINIMAL"/"BANKING_SAFE") + derivation `a11yOn ? FULL_ACCESS : persisted`. FULL_ACCESS is never persisted.
- **A2 (RECOVERING):** emitted only when `ServerState == STARTING` (brief bind window).
- **A3 (notification_access → DEGRADED):** NOT_GRANTED and GRANTED_NOT_CONNECTED degrade overall_status to DEGRADED (the VM watcher depends on notifications).
- **A4 (notification capability):** `notification` = listener READ path (granted + connected); separate `notification_post` key for the /notify path (POST_NOTIFICATIONS permission).
- **A5 (launch_app/install_apk semantics):** "endpoint available" (true whenever the server is up), not "operation will succeed" (launch can still be blocked at runtime).
- **A6 (sticky BANKING_SAFE):** the preference persists; if the user enables a11y while BANKING_SAFE is persisted, effective mode becomes FULL_ACCESS and BANKING_SAFE re-appears when a11y goes off.
- **A7 (tailscale_ips staleness):** bound-at-start list; goes stale if the IP changes after start (no re-bind; re-bind is P1).
- **A8 (modules vocabulary):** locked as in the P0-3 analysis (http HEALTHY/RECOVERING; tailscale HEALTHY/UNAVAILABLE; accessibility ENABLED/DISABLED; notification_access GRANTED/GRANTED_NOT_CONNECTED/NOT_GRANTED; termux_ssh UNKNOWN always).
- **A9 (CRITICAL is VM-side only):** the server never emits CRITICAL in a 200 body (broken HTTP returns nothing; broken auth returns 401 before the handler). The VM decides CRITICAL/down from no response / 401 / malformed body.
- **A10 (http module during STARTING):** "RECOVERING", matching overall_status.
- **P0-2 UI:** MainActivity shows the effective mode in the status text and a "Banking safe" toggle button (MINIMAL ↔ BANKING_SAFE). The button toggles the persisted preference; the bridge never enables a11y itself.

## 9. Implementation status (2026-11-06)

- P0-1, P0-2, P0-3 all implemented and mock-tested. Full suite: **221 tests pass** (was 197 before P0). Java builds OK (APK v2.0.0 built, not yet installed on the phone).
- Live verification on the phone is still pending (needs the v2.0.0 APK installed and a11y toggled off/on).
- GPT review received (CONS-20261106-022): **conditionally failed**. Design judged sound, but /launch_app needs live verification (Android background-activity-start restriction may block it), and P1 fixes are recommended (BANKING_SAFE UI contradiction — a real bug in the refresh() prompt; UI vs Health a11y-state mismatch; tailscale_ips staleness; install_apk capability always true; parse_health cross-field validation). Freeze is blocked pending the System Development Lead's decision on the fix path.
- **Live verification 2026-11-06 17:24–17:26 +07 (v2.0.0 installed, a11y OFF, MINIMAL): /health extended object OK; /state structured 503 OK. /launch_app returned ok=true for X (com.twitter.android) and for KBank (com.kasikorn.retail.mbanking.wap — the correct package; the first guess com.kasikornbank.kplus returned 422 "package not installed or has no launcher"). BUT the System Development Lead confirmed the phone screen did NOT switch — neither app came to the foreground. This confirms GPT's P0 warning: Android's background-activity-start restriction silently blocks the launch while startActivity() returns without throwing, so ok=true is a false positive. The v2.0 core goal (launch apps, esp. KBank, without a11y from the background) does NOT work as implemented. Fix path: P0-4 (§4), adopted by the System Development Lead, 2026-11-06 19:50 +07 (CONS-20261106-026).**
- **P0-4 (2026-11-06 19:50 +07): spec written (this file §4, plus §5/§6/§9 updates). Implementation done (commit 778895c5, 235 tests). GPT review CONS-20261106-027 (2026-11-06 20:14): 条件付き合格 — P1 fixes required. P1 fixes + launch deny list done (commit b93b2918, 245 tests, APK v2.1.0/code 34 SHA e0f4de9eacb19c499f834b0570eda249d9ee179348022f485726b6bfbbf60ff9). GPT re-review CONS-20261106-028 (2026-11-06 20:36, GPT-5.2): 条件付き合格 — code freeze PERMITTED for b93b291, no new P1. Known P2 improvements (recorded, NOT fixed before freeze): P2-01 move the verification baseline timestamp to just before startActivity() (currently recorded before the pre-read, so the window can be up to ~500 ms shorter); P2-02 require accepted and launch_requested to be True in parse_launch_app() (only their types are checked today); P2-03 Java-side concurrent-launch/disconnect not directly tested (future Instrumentation test). Version bumped to v2.2.0/code 35 (README decision 92, GitHub 3e1099a7) for proper version management. Live verification COMPLETE 2026-11-06 22:08 +07 (LIVE-VERIFY-P0-4.md): FULL_ACCESS X launch → launched (screen switched); MINIMAL X launch → unconfirmed / VERIFICATION_UNAVAILABLE (screen did NOT switch, user confirmed); KBank deny → 403 (app did not open). P0 FROZEN 2026-11-06 22:08 +07 (README decision 93).**
