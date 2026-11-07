# Muse Bridge — CONS-014 P1 Spec (P1-01/02/03/04/06/07)

- Status: FROZEN (v2.5.0 CONS-014 P1 code frozen by the System Development Lead 2026-11-07 00:37 +07; README decision 97). Do not edit directly — make a new version for changes.
- Source: GPT consultation CONS-20261106-014 (v1.9.13 review, the P1 list); CONS-20261106-032 (P1 implementation review, conditional fail); CONS-20261106-033 (fix-round re-review, conditional fail); CONS-20261106-034 (fix-round-2 re-review, conditional pass — code freeze approved, live Logcat verification remaining).
- Decision: README decision 97 (CONS-014 P1 freeze).
- Version: v2.5.0 / code 38 (GitHub 51325cc7). This build also contains TARMUX_KICKASS steps 1+2 (/health termux_sshd_local, POST /repair_termux), which are NOT frozen by this decision — only the CONS-014 P1 items below are frozen.
- Base spec: SPEC-V2-STARTUP-P0.md (FROZEN v2.2.0, README decision 93) and SPEC-P1.md (FROZEN v2.3.0, README decision 96). This spec is a separate document; it does NOT edit the frozen P0 or P1 specs.

## 1. What CONS-014 P1 changes (6 items; P1-05 already fixed by v2.0 P0-1)

### P1-01 — collectNodes recycle leak (BridgeAccessibilityService.java collectNodes)
On the two early returns (`depth > MAX_DEPTH`, `out.size() >= MAX_NODES`) the passed-in node was not recycled — it was obtained by the parent via getChild() and never reached the live map. Fixed: both early returns now recycle() the node before returning. The root cannot reach these paths (depth 0, empty out); the walk-level finally in currentStateInternal is unchanged. No Python-observable surface, so no behaviour test — a source-level regression guard test verifies the recycle() calls exist in both branches.

### P1-02 — bind check too broad (HttpBridgeServer.java bindAddresses)
The BIND check treated every "100." address as Tailscale (`ip.startsWith("100.")`), but 100.64.0.0/10 is also CGNAT. Fixed: the bind check now uses MainActivity.isTailscaleIp(ip) (100.64.0.0/10: first octet 100, second 64–127), matching the P1 findTailscaleIps() narrowing. The stale comment in MainActivity.java that said the bind check was "NOT changed here" was updated. No Python-observable surface, so no behaviour test.
- Known limitation: 100.64.0.0/10 is RFC 6598 shared address space, not Tailscale-exclusive. Interface-name identification is a future improvement (noted in CONS-032/033/034, not a freeze blocker).

### P1-03 — log sanitize / PII (BridgeLog.java + HttpBridgeServer.java)
BridgeLog.sanitize() only replaced control characters, so IPs, exception messages, package names, and URLs reached the logs. Fixed:
- BridgeLog.sanitize() now masks IPv4 to the first two octets (`100.111.74.42` → `100.111.*.*`) and 32+ hex runs to `[token]`.
- Call-site audit in HttpBridgeServer.java: `install_result` failed → status code only; `bind-failed` → exception class only; `accept-failed` → exception class only; all six launch logs → no package name (the VM answer still names it); `install_download_start` → no URL; `install_committed` → no file name; `install_failed` → exception class only.
- Raw android.util.Log calls fixed in two fix rounds (CONS-032/033): `listening on` → port only (no IP); `cannot bind` → exception class only; `accept failed` → exception class only.
- The request-line log (line 1716 → 1745) no longer logs the raw request path: a KNOWN_PATHS set (exactly the 24 endpoints from the handler's if/else chain) + a loggablePath() helper return the endpoint name for known paths and "UNKNOWN" otherwise. The raw request target never reaches Logcat. On the early-return path before parsing, path is "?", which logs as "UNKNOWN".
- Deliberately NOT changed: installDetail/installStatus API state (operator-facing, token-auth, not the log file), `listen` (IP masked centrally), `batch` (status + counts only), fixed-string lines. BridgeNotificationListener logs only event types (posted/removed/opened/replied) — no titles/bodies.
- Known limitation: a true end-to-end Logcat observation of the sanitized request line was NOT done (requires a live phone + Logcat capture). The user froze on the source-level verification (guard test + dex + code review + GPT code review). See §4.

### P1-04 — /scroll_node direction required (HttpBridgeServer.java /scroll_node handler)
A missing direction used to default to "forward". Fixed: `!j.has("direction")` → HTTP 400 `"missing direction"`; the value validation (`forward`/`backward`) is unchanged. 3 new tests (missing → 400, bad value → 400, valid → ok).

### P1-06 — accept-loop residue (HttpBridgeServer.java)
An old accept thread could see the new start()'s STARTING state and stay alive after stop→restart. Fixed: start() captures `final long acceptGeneration = serverLifecycleGeneration` right after the bump and passes it to acceptLoop(); the loop condition now requires `acceptGeneration == serverLifecycleGeneration && !ss.isClosed()`, so a stale thread that wakes after stop/restart exits instead of spinning on its closed socket; the catch block only logs when the generation still matches. This reuses the v2.1 serverLifecycleGeneration mechanism; operation-notification behaviour untouched. No Python-observable surface, so no behaviour test — a source-level regression guard test verifies the generation check exists in the loop condition.

### P1-07 — /node_action arg validation (BridgeAccessibilityService.java nodeActionArgs + android_bridge.py)
- Server: nodeActionArgs rewritten — allowedNodeActionArgs() rejects unknown keys for all 32 actions; requireIntArg() rejects non-integers like 1.8 (integral doubles like 2.0 accepted) and out-of-int-range values; ranges added — set_progress 0.0–1.0, scroll_to_position row ≥ 0, set_selection start/end ≥ 0, press_and_hold duration_ms 1..60000. Non-object args → HTTP 400 `"args must be an object"` (before the notification hook).
- Client mirror: android_bridge.py node_action — NODE_ACTION_ARG_KEYS, _require_int_arg, _validate_node_action_args, non-dict args rejected; all raise ValueError before any request. The 32-bit range check now matches the Java server exactly at both edges (`value < -(2**31) or value > 2**31 - 1`; -2**31 accepted, -(2**31)-1 rejected).
- 6 new tests (fractional rejected, integral float accepted + normalised to int on the wire, range checks, unknown key, non-object args, raw-post 400s) + the large-int edge test.

## 2. Tests

- Full Python suite: **286 passed, 18 subtests** (272 baseline + 10 implementation + 3 fix round 1 + 1 fix round 2).
- New tests (14, all in test_android_app_client.py): test_app_logs_never_carry_full_ips_or_packages (P1-03); test_app_scroll_node_missing_direction_is_400, test_app_scroll_node_bad_direction_raw_is_400, test_app_scroll_node_with_direction_raw (P1-04); test_app_node_action_fractional_arg_rejected, test_app_node_action_integral_float_accepted, test_app_node_action_range_checks, test_app_node_action_unknown_arg_rejected, test_app_node_action_non_object_args_rejected, test_app_node_action_raw_bad_args_are_400 (P1-07); test_app_node_action_large_int_arg_rejected (P1-07 edge); test_p1_01_collect_nodes_early_returns_recycle, test_p1_06_accept_loop_checks_generation (source-level guards); test_p1_03_request_log_never_carries_arbitrary_path (P1-03, source-level guard).
- The P1-01/P1-06 guards and the P1-03 request-log guard are source-level (they verify the mechanism exists in the source), not behaviour tests — Java-internal behaviour has no Python-observable surface. Their limits are documented in the test docstrings.
- The log test verifies the mock's client-visible contract only; Java sanitize behaviour and actual Logcat output are verified by code audit, dex inspection, and GPT code review — not by the Python test.

## 3. Reviews (GPT, GitHub + Notion linked)

- CONS-20261106-032 (2026-11-07 00:11–00:14): **条件付き不合格**. P1-01 pass, P1-02 conditional pass, P1-03 FAIL (three raw Logcat calls at lines 421/424/607 bypassed BridgeLog.sanitize), P1-04 pass, P1-06 pass, P1-07 conditional pass (Python int 32-bit range check missing for int values). Muse verified both claims directly in the code before the fix round.
- CONS-20261106-033 (2026-11-07 00:23–00:26): **条件付き不合格**. The 425/432/619 fixes confirmed OK. Remaining: line 1716 dynamic path in Logcat (`Log.i(TAG, method + " " + path + " -> " + status)`). P1-07 boundary (-2**31) noted as an improvement. Muse verified the line-1716 claim directly (path is the raw request target with only the query string stripped).
- CONS-20261106-034 (2026-11-07 00:33–00:36): **条件付き合格 — code freeze approved**. P1-03 (KNOWN_PATHS + loggablePath) pass, P1-07 (boundary) pass, no frozen-spec contract violation, no further code changes required. Remaining condition: live-phone Logcat verification (install APK, clear Logcat, send an unknown path, confirm the log reads `POST UNKNOWN -> 404`). GPT did not re-run the tests or independently verify the APK build.
- The user froze on 2026-11-07 00:37 +07 ("このまま凍結する"), accepting the source-level verification in place of the live Logcat check.

## 4. Known limitations (recorded, NOT fixed before freeze)

- Live Logcat verification NOT done: the sanitized request line (`POST UNKNOWN -> 404`) was never observed in a live phone Logcat capture. Source-level guard + dex + code review + GPT code review stand in its place. If the bridge is ever updated with a Logcat-reading endpoint, or ADB access becomes available, this check should be run.
- P1-02 CGNAT caveat: 100.64.0.0/10 is RFC 6598 shared space; interface-name identification is a future improvement.
- P1-01/P1-06 have no behaviour tests (Java-internal, no Python-observable surface); source-level guards only.
- The log test cannot verify Java sanitize behaviour or actual Logcat output (mock-only).
- CONS-014 P1-05 was already fixed by v2.0 P0-1 and is not part of this freeze.
- CONS-028 P2-01/02/03, CONS-022 P2 (ServiceUnavailable detail), and the CONS-031 tailscale_ips [""] empty-string note are still open (step 3, P2 wrap-up).
