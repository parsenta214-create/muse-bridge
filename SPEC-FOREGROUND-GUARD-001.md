# SPEC-FOREGROUND-GUARD-001 — Foreground verification and operation context for non-node operations (C-04)

- **Version**: v2.1 FROZEN (2026-11-12, Decision 104)
- **Status**: FROZEN — Decision 104 (2026-11-12, System Development Lead decision). Kohaku Tier 1 ADOPT (CONS-20261112-139, P0=0/P1=0/P2=0); Mitsuki final review ADOPTABLE 9.6/10 (P0=0/P1=0/P2=3, all implementation clarifications, no SPEC text change required).
- **Parent**: v2 DRAFT (CONS-20261112-138, CONDITIONAL ADOPT, P0=0/P1=0/P2=2). This v2.1 addresses the two remaining P2 findings (main-thread serialization, snapshot consistency) and clarifies acceptance criteria C-04-7, C-04-10, C-04-14, C-04-16. No architectural change from v2.
- **Addresses**: CONS-131 condition C-04 (A4: target integrity — never send operations to a stale screen / wrong app).
- **Related**: Decision 48 (A-3 adopted model), Decision 49 (A-4 batch idempotency), Decision 66 (DPAD keys, the A4 concrete example), Decision 87 (MINIMAL / BANKING_SAFE operating model), Decision 89 (operation notification), Decision 93 (startup modes), SPEC-BATCH-PERSIST-001 (C-01), SPEC-AUTH-001 (C-02).
- **Scope**: Android app only (`HttpBridgeServer.java`, `BridgeAccessibilityService.java`). VM-side Python client: include the operation context in every non-node operation request, and never auto-retry on 409 (including after a timeout or a lost response). No protocol version change (stays v1).

## 1. Problem

Node operations (`/click_node`, `/type_node`, `/scroll_node`, `/node_action`) check the `tree_hash` before acting (409 on a stale tree). Non-node operations have no precondition at all:

- `/click` reads coordinates and calls `tap(x, y)`. No foreground check.
- `/swipe` reads coordinates and calls `swipe(...)`. No foreground check.
- `/type_text` reads text and calls `typeText(text)`. No foreground check (only a 422 if no field is focused).
- `/press_key` validates the key and calls `pressKey(key)`. No foreground check.

So if the user switches apps (or the screen changes) between the VM reading `/state` and the VM sending a tap, the tap goes to the wrong app. The concrete example is decision 66: a retry went to the Muse app because the user had switched to the chat to send a message, instead of to X where the operation was intended. A tap, a swipe, or typed text landing in the wrong app can send a message, confirm a dialog, or change a setting the user did not intend (CONS-131 A4, P1).

A notification ("notify before operation") does not prevent this. It tells the user an operation is about to happen, but it does not verify the target. Notification is visibility only, not a technical guard.

This SPEC adds a precondition to every non-node operation: the server verifies the foreground package and the operation context before executing, and refuses the operation (409) if the target does not match. If the target cannot be confirmed, the operation is refused (fail-closed).

## 2. Design

### 2.1 Operation context

Every non-node operation request carries an `operation_context` object. The VM includes it in the request body (for `/click`, `/swipe`, `/type_text`, `/press_key`, `/launch_app`, `/open`).

| Field | Required | Meaning |
|---|---|---|
| `expected_package` | Yes | The Android package the VM expects to be in the foreground (for example `com.twitter.android`). Taken from the last `/state` read. |
| `session_id` | Yes | The operation session ID. Ties the operation to the current operation session (see §2.4). The server issues it; the client never selects it. |
| `tree_hash` | No | The last known tree hash, if the VM has one (from the last `/state` read). If provided, it is checked. |
| `generation` | No | A server-owned snapshot revision (see below). If the VM has one (from the last `/state` read), it includes it, and the server checks it. |

**Generation semantics (P2-01):** `generation` is a server-owned snapshot revision. It is the `snapshot_id` (the monotonic revision incremented per snapshot) from `/state`. Its scope is the AccessibilityService's snapshot stream: it increments on every new snapshot (every tree walk that produces a new revision). It is **not** scoped to the operation session. A foreground app change increments it (because a new snapshot is taken). An accessibility-tree refresh increments it. A notification overlay does **not** increment it unless it produces a new snapshot (an overlay that does not change the accessibility tree does not increment the revision). It is a **snapshot generation**, not an operation generation.

Comparison rules: the server compares the supplied `generation` to the current snapshot revision. If the supplied generation is older than the current revision, the screen has changed since the VM read it, and the operation is refused with 409 `generation_mismatch`. If the supplied generation is newer than the current revision (which should not happen in correct operation), it is also refused with 409 `generation_mismatch`, because it cannot be compared reliably. A supplied generation that is not a number, or that is negative, is rejected with 400. **Generation must never be used to bypass a failed foreground check.** If the foreground package does not match, the operation is refused regardless of the generation value. The generation is an additional staleness signal, not a substitute for the foreground check.

**Snapshot consistency (P2-02).** The `tree_hash` and the `generation` must refer to the **same authoritative snapshot**. The server reads the current snapshot as one consistent unit (the snapshot object contains both the tree hash and the generation, read atomically). The validation compares the supplied `tree_hash` and `generation` against that same snapshot — never a tree hash from one snapshot and a generation from another. If a new snapshot is produced between the validation read and the dispatch, the admission is invalidated: the dispatch re-checks that the snapshot has not changed since validation, and if it has, the operation is refused (409 `generation_mismatch`, because the validated context no longer holds). If a consistent snapshot cannot be obtained at all (for example the snapshot is being replaced and no stable snapshot is available), the operation is refused with 503 `snapshot_unavailable`. A snapshot update must not interleave between the final snapshot validation and dispatch without invalidating admission. A mixed state (a tree hash from one snapshot and a generation from another) is never used silently.

**Limitation, stated explicitly.** Generation detects accessibility-tree changes, but it cannot detect every visual change that leaves the accessibility tree unchanged (for example a video playing, an animation, or a canvas redraw). The guard is a strong target-integrity mechanism, not a guarantee that every pixel remains unchanged. This SPEC does not claim that generation detects visual changes that do not alter the accessibility snapshot.

The `expected_package` and `session_id` are required. A request without them is rejected with 400 (missing required field). The `tree_hash` and `generation` are optional; if the VM has them, it includes them, and the server checks them. If the VM does not have them (for example for a `/press_key` that does not depend on the screen content), it omits them and the server checks only the package and the session.

### 2.2 Foreground verification and the serialized execution admission boundary (P0-01)

Checking the target before execution is not enough. If the server validates the foreground and then queues the operation for later dispatch, the target can change between the validation and the dispatch (the user switches apps, an asynchronous callback fires, or another accepted operation changes the context). The validation must be connected to the dispatch by a serialized boundary, so the decision to execute is preserved all the way to the side effect.

**The serialized execution admission boundary.** All guarded operations (`/click`, `/swipe`, `/type_text`, `/press_key`, and `/batch`) pass through one serialized admission boundary. The boundary guarantees:

**Main-thread serialization (P2-01).** The admission boundary is executed **on the service main thread**, not on the HTTP worker thread. This is the concrete concurrency contract, and it is what prevents a lock cycle. The HTTP worker does **not** hold any lock while waiting for the main thread. Instead, the HTTP worker posts the operation (with its operation context) to the main thread via `runOnMainThread` (as in the A-3 adopted model, decision 48, with the 20-second timeout → 503), and waits for the result without holding a lock. The main thread, when it runs the posted task, performs the foreground check, the snapshot validation, and the dispatch, all in the same serialized main-thread task. Because the main thread processes its tasks serially (it is the single execution context, decision 48), no two guarded operations can interleave. There is no cross-thread lock cycle, because the HTTP worker never holds a lock that the main-thread task needs, and the main-thread task never waits for a lock held by the HTTP worker. The admission boundary is the main-thread task itself: the final foreground result and the operation dispatch belong to the same serialized admission decision. The implementation must not release the admission boundary while waiting for main-thread dispatch — the boundary is only complete when the main-thread task finishes (either by dispatching the operation or by refusing it). The HTTP worker's 20-second timeout (A-3) applies: if the main thread does not complete the task within 20 seconds, the HTTP worker returns 503 and the operation is not dispatched (the main-thread task, if it later runs, must check that the request is still valid — the session has not ended and the admission has not been invalidated — before dispatching; see §2.4).

1. The foreground and the operation context are validated (see below).
2. The operation is admitted only while that context remains valid.
3. No competing Bridge operation can change the execution context between validation and dispatch (the lock serializes all guarded operations, so a second operation cannot interleave between the first operation's validation and its dispatch).
4. The side effect is dispatched through the same serialized execution path (the operation is dispatched on the main thread while the admission lock is held, or the dispatch is handed to the main thread with the admission lock held until the dispatch is confirmed as started — the lock is not released between validation and dispatch).
5. A stopped/restarted service or an invalidated operation session cannot execute already-queued work (see §2.4: when a session is invalidated or the service stops, queued operations from that session are cancelled or rejected, and the admission boundary refuses them).
6. A failed final validation returns without dispatching the operation. Nothing is executed.

**Validation (inside the boundary).** While holding the admission lock, the server:

1. Calls `getForegroundPackage()` (via the exception-safe wrapper `foregroundPackageSafe()`), which uses `getRootInActiveWindow()` on the main thread and returns the foreground package name.
2. Compares the result to `operation_context.expected_package`. If they differ, returns **409** `foreground_mismatch` (with `expected_package` and `current_package`). The operation is not executed.
3. If `tree_hash` was provided and does not match the current tree hash, returns **409** `tree_hash_mismatch` (stale screen). The operation is not executed.
4. If `generation` was provided and does not match the current snapshot revision (see §2.1 for the comparison rules), returns **409** `generation_mismatch` (stale screen). The operation is not executed.
5. If the `session_id` does not match the current active operation session (or there is no active session), returns **409** `session_mismatch` (stale session). The operation is not executed.

All checks happen inside the admission boundary, before any side effect. A 409 means nothing was executed. The guarantee covers the whole path: a rejected request has no side effect, including no notification hook that could indirectly initiate execution, no queued main-thread work, and no batch admission.

**Realistic guarantee (what this SPEC does and does not claim).** Android cannot prevent the human user from physically changing the foreground app at an arbitrary instant. This SPEC does **not** claim an absolute operating-system lock on the foreground. What it guarantees is: (a) Bridge-side races are prevented (no competing Bridge operation can change the execution context between validation and dispatch, because of the serialized boundary), and (b) the best available foreground revalidation is performed at the actual execution boundary (the foreground is re-checked immediately before dispatch, inside the boundary, not just at HTTP request time). If the user changes the foreground app in the tiny window between the final revalidation and the physical tap, that is a human action outside the Bridge's control, and the SPEC does not claim to prevent it. The SPEC prevents Bridge-side races and requires revalidation at the execution boundary; it does not claim to lock the foreground against the user.

**Machine-readable error identifiers (preserved).** The 409/503/400 responses use distinct machine-readable error identifiers, so the VM can distinguish stale context from unavailable capability: `foreground_mismatch`, `foreground_unverifiable`, `tree_hash_mismatch`, `generation_mismatch`, `session_mismatch`. The 409 response is appropriate for a confirmed mismatch. The 503 response is appropriate when foreground verification is unavailable. The 400 response is appropriate for missing required context.

### 2.3 Fail-closed by operation class (P1-01)

The fail-closed rule is defined by operation class, reconciled with the frozen MINIMAL / BANKING_SAFE operating model (README decisions 87 and 93). MINIMAL is the standard startup mode. BANKING SAFE is a mode without AccessibilityService. The model separates management-plane availability from screen-control availability.

| Operation class | Foreground verification | When verification is unavailable |
|---|---|---|
| **Management operations** (`/health`, `/state`, `/events`, `/logs`, `/notifications` GET, `/clipboard` GET, `/install_status`) | Not required. These do not change the screen and do not need a confirmed foreground. | Not applicable. They work in MINIMAL and BANKING SAFE modes. |
| **Ordinary screen operations** (`/click`, `/swipe`, `/type_text`, `/press_key`, `/click_node`, `/type_node`, `/scroll_node`, `/node_action`, `/batch`, `/notification_action`) | Required. The foreground package must be confirmed (§2.2). | **Fail-closed.** If the foreground cannot be determined (accessibility is OFF, `getForegroundPackage()` returns null, the root window is inaccessible, or the AccessibilityService is disconnected), the operation is refused with **503** `foreground_unverifiable`. Nothing is executed. The user must re-enable accessibility to do screen operations. This is consistent with the Bridge already requiring accessibility for node operations. |
| **Foreground-changing operations** (`/launch_app`, `/open`) | Post-launch verification (see §2.5). A pre-execution foreground check does not apply (the launch itself changes the foreground). | The launch attempt and the foreground confirmation are distinct results. An unconfirmed launch is not treated as a verified operation (see §2.5). |

Rules:
- For ordinary screen operations, an inaccessible or unknown foreground means no execution and a 503 `foreground_unverifiable` response. "Cannot verify" is never treated as "verified". There is no fallback that executes the operation when the target is unknown.
- No fallback to UsageStats or another approximate source is introduced, and no approximate source is called "verified", unless that source is independently established as sufficient for the target-integrity guarantee. (UsageStats verification is a P1 item from the A11Y-OFF work and is not established here.)
- The distinction between 503 (service capability unavailable — the foreground cannot be verified) and 409 (context mismatch — the foreground was verified and does not match) remains explicit. A 503 means "we could not check"; a 409 means "we checked and it does not match". The VM treats them differently: a 503 means the capability is unavailable (do not retry the same operation; the user must re-enable accessibility), while a 409 means the context is stale (re-read `/state` and re-evaluate, §2.6).

### 2.4 Operation session state machine (P1-02)

An operation session groups the operations the VM performs in one continuous run. The session is the unit that ties an operation to the user's current intent. The session lifecycle is a single authoritative state machine, owned by the server.

**States:** `NONE` (no active session) → `ACTIVE` (a session is running) → `ENDED` (the session ended, by idle timeout or by `/operation_end`) → `NONE` (a new session may start). Expiry is monotonic: once a session has ended, it cannot become active again. An ended session's ID is never reused.

**Creation (serialized).** Session creation is serialized under the session lock. Only one active session may exist per Bridge execution context. When a request arrives and no session is active, the server creates a session and issues its ID. If two requests arrive simultaneously when no session exists, the session lock serializes them: the first creates the session and gets its ID; the second sees the active session and uses that session's ID (it does not create a second session). Concurrent session-start requests have deterministic outcomes (one session is created, and both requests use it). The server issues the ID once. **The server never accepts a client-selected session ID.** A request that supplies a `session_id` that the server did not issue is rejected with 409 `session_mismatch`.

**Idle timeout.** A session ends after 10 seconds of idle time (as in the existing operation-session rule). The idle timer is refreshed by a **successful** operation (an operation that was admitted and dispatched). A **rejected** request (409, 503, or 400) does **not** refresh the timer. An in-flight operation (admitted but not yet completed) keeps the session alive until it completes (the timer is not allowed to expire mid-operation; the session ends only after the in-flight operation completes and the idle period then elapses). Validation alone (a check that does not dispatch) does not refresh the timer.

**Invalidation.** `/operation_end` invalidates the active session immediately. The session moves to `ENDED` and its ID becomes invalid. Any operation that arrives with that ID after invalidation returns 409 `session_mismatch` and is not executed. **Queued but not yet dispatched operations belonging to the invalidated session are cancelled or rejected.** They are never executed after the session ends. A stop/restart of the BridgeService or the AccessibilityService invalidates the active session and all outstanding operations from it. After a restart, a new session must be created (the old session ID is invalid).

**Stale sessions.** A stale or superseded session always returns 409 without execution. There is no grace period and no automatic migration of an old session's operations to a new session. If the VM has operations planned under an old session and the session ends, the VM must start a new session (by sending a new operation, which creates a new session) and re-establish the operation context from a fresh `/state` read.

**Session ID.** The session ID is a random string (UUID v4), generated by the server when the session starts. The VM reads it from the session-start response (the first operation's response includes the `session_id`, or the VM reads it from a session-start endpoint). The VM includes that `session_id` in every operation in the session.

### 2.5 `/launch_app` and `/open` (foreground-changing operations, P1-03)

`/launch_app` and `/open` change the foreground app, so a pre-execution foreground check does not apply to them (there is no "expected" foreground to check against before the launch — the launch itself is what changes it). They retain a separate launch pathway, but with a complete safety contract:

- **Valid active session before launch.** The `session_id` is validated before any launch side effect, not merely eventually checked. If the session is not active (it ended, was invalidated, or was never created), the launch is refused with 409 `session_mismatch` and nothing is launched.
- **Explicit launch target package.** The launch target package is derived from the request (the `package` field for `/launch_app`, the URL/app for `/open`) and validated. The target must be a well-formed package name (or a valid URL that resolves to a package for `/open`). A malformed or missing target is rejected with 400. The launch target is explicit; it is never inferred from the current foreground.
- **Launch-attempt notification and result semantics.** The launch attempt is notified (as today, the operation notification is posted). The result distinguishes five outcomes, which must not be conflated: (1) launch request accepted, (2) launch attempted, (3) target package confirmed in the foreground, (4) target package not confirmed, (5) verification unavailable. The response reports which outcome occurred.
- **Post-launch verification against the exact requested package.** The existing `verifyLaunchForeground` polls for about 3 seconds after the launch and confirms the expected package is in the foreground. The verification is against the **exact requested package**, not merely "some package became foreground". A launch to package A must never be reported as successfully verified merely because package B became foreground. If the target package is not confirmed in the foreground after the poll, the result is "target package not confirmed" (not "verified").
- **Session/lifecycle validity on async completion.** The launch completion is asynchronous (the poll runs after the launch). When the completion callback fires, it checks that the session is still valid (the session that the launch belonged to is still active). If the session has ended or been replaced, the completion must not mark the obsolete session as successful, and must not trigger a subsequent operation under the old context. A delayed launch completion cannot reactivate or authorize an invalidated session.
- **No automatic continuation.** A successful launch response does not automatically continue to another operation. The VM must re-read `/state` and establish a new operation context before proceeding (the launch changed the foreground, so the previous context is stale).
- **Unconfirmed launch stays unconfirmed.** If the launch target is not confirmed, the launch remains unconfirmed. The VM must re-read `/state` and establish a new operation context before proceeding. It must not assume the launch succeeded.

They do **not** require `expected_package` in the request (the expected package is the app being launched, which is already in the request as the launch target). They do require `session_id`.

### 2.6 No auto-retry on 409 (VM client rule)

If the server returns 409 (foreground mismatch, stale tree, stale session, or any other 409), the VM **MUST NOT** automatically re-send the same operation.

- The VM must re-read `/state`, re-evaluate whether the operation is still valid (is the target app still the one the user intends? is the screen still the one the operation was planned for?), and only then — with a fresh operation context (a new `expected_package`, a new `tree_hash`, the current `session_id`) — issue a new operation if it is still appropriate.
- An automatic retry of the same operation (same coordinates, same text, same key) without re-reading the state is prohibited. It would re-send a stale operation to a screen that has already changed, which is the exact failure this SPEC prevents.
- **Additional prohibitions (beyond a plain 409 retry):** the VM must not retry after a timeout merely because the VM did not receive a response (a lost response is not proof the operation did not run — the operation may have executed and the response was lost). The VM must not treat a lost response as proof that the operation did not run. The VM must not reuse the same stale operation context after obtaining a new session (a new session requires a fresh context from a fresh `/state` read). The VM must not automatically regenerate coordinates or reuse the previous action based solely on the new screen snapshot. **A new operation requires a new decision, not merely a new HTTP request.** The VM re-reads the state, re-evaluates the user's intent, and decides afresh whether the operation is still wanted.
- This rule also applies to the batch endpoint (`/batch`): if a batch returns 409, the VM does not re-send the same batch_id. It re-reads the state and, if the work is still needed, issues a new batch with a new batch_id (see SPEC-BATCH-PERSIST-001 §2.6 and §2.9). **Note on 409 semantics:** the batch persistence SPEC's 409 (a payload-hash conflict — the same batch_id with a different payload hash) is not the same thing as a foreground mismatch. The VM must not automatically retry either case, but the recovery procedure must be appropriate to the specific error. For a payload-hash conflict, the VM must not re-send the same batch_id with a different payload; it must issue a new batch_id if the work is still needed. For a foreground mismatch, the VM must re-read `/state` and re-establish the operation context before issuing a new batch.

### 2.7 Notification is visibility only, with explicit ordering and failure independence (P2-02)

Operation notifications provide user visibility only. They are not an execution-safety guarantee and do not replace foreground-package verification, operation-context/session validation, or fail-closed behavior.

This principle is stated here because it is easy to mistake the notification ("the user was told an operation was about to happen") for a safety check ("the operation was verified to go to the right target"). The notification tells the user; the verification in §2.2 is what actually prevents a mistargeted operation. Both exist, and they serve different purposes. A notification without verification does not make an operation safe, and verification without notification does not keep the user informed.

**Ordering and failure independence:**
- **Notification success is not evidence of correct targeting.** A notification being posted does not mean the operation went to the right target. The targeting is established only by the foreground verification in §2.2.
- **Notification failure is not evidence of incorrect targeting.** If the notification fails to post (for example the notification permission is disabled, or the notification channel is unavailable), that does not mean the operation was mistargeted. The operation's correctness is established by the verification, not by the notification.
- **Notification permission being disabled does not disable the foreground guard.** The foreground verification in §2.2 runs regardless of whether notification permission is granted. (This preserves the established behaviour from decision 101: screen operations may continue when notification permission is disabled. The notification is a visibility mechanism; its absence does not weaken the guard.)
- **Notification hooks must not execute the screen operation themselves.** The notification is a post-hoc observation (it reports that an operation happened or is about to happen). It must not be the mechanism that triggers the operation. The operation is dispatched only through the serialized admission boundary in §2.2.
- **Any notification-related asynchronous completion must not authorize an otherwise rejected operation.** If a notification completes (or fails) asynchronously after an operation was rejected, that completion must not cause the rejected operation to execute, and must not mark the rejected operation as authorized.
- **Ordering:** the notification is posted as a visibility event around the operation (before or after the operation, as in the existing notification design, decisions 88–89). The notification's timing relative to the validation does not affect the validation: the validation in §2.2 is the authority, and the notification is an independent observation. The established notification behaviour is preserved unless an independently approved change is made.

### 2.8 What does NOT change

- Node operations (`/click_node`, `/type_node`, `/scroll_node`, `/node_action`) are unchanged. They already check `tree_hash` (409 on stale). They do not need the new operation context (their target is a node in a verified tree, not a coordinate on a possibly-changed screen). (A future revision may add the session check to node operations as well; it is out of scope for this SPEC.)
- `/type_text` still returns 422 when no editable field is focused. The foreground check (§2.2) runs before the focus check, so a mistargeted `/type_text` returns 409 (wrong app), not 422 (no field focused).
- `/press_key` still validates the key first (unknown keys are 400). The foreground check runs after key validation and before the key is sent.
- The `/batch` endpoint's own idempotency and persistence are governed by SPEC-BATCH-PERSIST-001. This SPEC adds the foreground/session precondition to the batch as a whole (the batch's `operation_context` is checked once, before the batch executes).
- `protocol_version` stays 1. No new protocol version.
- The notification rule ("notify before operation") is unchanged. It remains user visibility only (§2.7).

## 3. Acceptance criteria

- [ ] C-04-1: a `/click` with an `expected_package` that does not match the foreground returns 409 and nothing is tapped.
- [ ] C-04-2: a `/swipe` with a mismatched `expected_package` returns 409 and nothing is swiped.
- [ ] C-04-3: a `/type_text` with a mismatched `expected_package` returns 409 and no text is typed.
- [ ] C-04-4: a `/press_key` with a mismatched `expected_package` returns 409 and no key is sent.
- [ ] C-04-5: a `/click` with a stale `tree_hash` (provided and mismatched) returns 409 and nothing is tapped.
- [ ] C-04-6: a `/click` with a `session_id` from a previous (ended) session returns 409 and nothing is tapped.
- [ ] C-04-7: a `/click` with a matching `expected_package` and a current `session_id` succeeds (the tap is executed **through the serialized admission boundary** — the test must demonstrate that the tap succeeded after the final admission validation on the main thread, not merely that the foreground package matched before the request was dispatched).
- [ ] C-04-8: a `/click` when accessibility is OFF returns 503 `foreground_unverifiable` and nothing is tapped (fail-closed).
- [ ] C-04-9: a `/click` without `expected_package` or without `session_id` returns 400 (missing required field).
- [ ] C-04-10: a `/launch_app` with a valid `session_id` launches the app and the post-launch verification confirms the foreground. The test must distinguish a **confirmed** launch (the target package is in the foreground) from a merely **attempted** launch (the launch was requested but the target was not confirmed), and must verify that an **unavailable** verification result is not reported as a successful launch confirmation.
- [ ] C-04-11: the VM does not auto-retry after a 409 (verified by code read and by a live 409 test where the VM re-reads `/state` before any new operation).
- [ ] C-04-12: a `/batch` with a mismatched `expected_package` returns 409 and no action in the batch is executed.
- [ ] C-04-13: the operation notification is still sent (user visibility) and the foreground verification still runs (the notification does not replace the verification).
- [ ] C-04-14: a foreground change or a competing operation cannot cause a request that passed initial validation to execute under a different Bridge execution context (execution serialization, §2.2). The test must involve main-thread access, lock contention, and concurrent Bridge operations (for example two guarded operations submitted simultaneously, and a foreground change attempted while an operation is in flight). A status-code-only test is insufficient.
- [ ] C-04-15: when a session ends or the service stops, queued operations from that session cannot execute (queued-operation invalidation, §2.4).
- [ ] C-04-16: two simultaneous requests attempting to start a session cannot create two simultaneously valid operation sessions (concurrent session creation, §2.4). The test must verify that the session-start response contains the **same authoritative server-issued session ID for both concurrent requests**, and that neither request can supply or replace that ID.
- [ ] C-04-17: a launch to package A is never reported as successfully verified merely because package B became foreground (launch target confirmation, §2.5).
- [ ] C-04-18: a delayed launch completion cannot reactivate or authorize an invalidated session (launch completion after session invalidation, §2.5).
- [ ] C-04-19: a null foreground result, an exception, an inaccessible root window, and an accessibility disconnection all result in refusal of ordinary screen operations (foreground verification unavailable, §2.3).
- [ ] C-04-20: disabling notification permission does not bypass or weaken foreground verification (notification independence, §2.7).
- [ ] C-04-21: a timeout or a lost HTTP response does not cause an automatic resend of an operation whose execution outcome is unknown (ambiguous operation outcome, §2.6).
- [ ] C-04-22: a rejected foreground/session precondition prevents every batch action from being dispatched (batch failure atomicity at admission, §2.2 and §2.6).

## 4. Failure behaviour (summary)

- Foreground mismatch: 409 `foreground_mismatch` (with `expected_package` and `current_package`), nothing executed.
- Stale tree / stale generation: 409 (`tree_hash_mismatch` / `generation_mismatch`), nothing executed.
- Stale session: 409 `session_mismatch`, nothing executed.
- Foreground unverifiable (accessibility off, or package cannot be determined): 503 `foreground_unverifiable`, nothing executed (fail-closed).
- Missing `expected_package` or `session_id`: 400, nothing executed.
- All 409/503/400 responses mean the operation did not run. The VM must re-read `/state` and re-evaluate before issuing any new operation; it must not auto-retry the same operation.

## 5. Verification (tests)

Kohaku (CONS-131, CONS-137) requires, at minimum, C-04-1 through C-04-22 (§3). The tests must verify that a 409 actually leaves the phone unchanged (by reading `/state` / taking a screenshot after the 409, not just by checking the status code). A test that only checks the HTTP status code without confirming no side effect does not verify this SPEC.

The minimum live test set (on the real phone, with a safe tap target):
- Switch the foreground app (for example from X to the Muse app), then send a `/click` with `expected_package` set to X. Verify 409 and that nothing was tapped in the Muse app.
- Send a `/click` with a stale `tree_hash`. Verify 409.
- End the operation session (wait 10 seconds idle, or call `/operation_end`), then send a `/click` with the old `session_id`. Verify 409.
- Turn accessibility OFF, then send a `/click`. Verify 503 `foreground_unverifiable` and that nothing was tapped. Turn accessibility back ON.
- Send a `/click` with a matching `expected_package` and a current `session_id`. Verify the tap is executed.
- Send a `/click` with a mismatched package, then verify the VM does not auto-retry (the VM re-reads `/state` before any new operation).
- Use a safe tap target (for example the bottom gesture bar at (540, 2320), which does nothing) for the success-path test, so a mistaken tap cannot trigger an unintended action. Do not test with a tap that could send a message, confirm a dialog, or change a setting.

## 6. Notes

- This SPEC is implemented during the Kotlin migration (A-plan, System Development Lead decision 2026-11-12). The implementation language is Kotlin. The `HttpBridgeServer` is converted last (Stage 3); the foreground verification is implemented as part of that conversion. The VM client operation-context and no-auto-retry rules (§2.1, §2.6) are implemented in the VM-side Python client at the same time.
- The notification rule ("notify before operation") is user visibility only and is not a substitute for this SPEC. See decision 102 and §2.7 for the role separation.
- Related: decision 48 (A-3 adopted model — the single execution context; this SPEC adds a precondition on top of it, it does not change the execution model), decision 66 (the A4 concrete example — a retry went to the Muse app instead of X), decision 102 (correction record), SPEC-BATCH-PERSIST-001 (C-01 — the batch's own persistence; this SPEC adds the foreground/session precondition to the batch as a whole), SPEC-AUTH-001 (C-02 — authorization tiers; this SPEC adds the target-integrity precondition, which is a separate concern from authorization).
- Changes from the groundwork: the operation context fields are now fixed (§2.1, with `expected_package` and `session_id` required and `tree_hash`/`generation` optional), the fail-closed rule for an unverifiable foreground is explicit (§2.3), the session model is defined (§2.4), `/launch_app` and `/open` are separated as foreground-changing operations with post-launch verification (§2.5), the no-auto-retry rule is explicit (§2.6), and the notification role separation is stated verbatim as Kohaku recommended (§2.7).
- Changes from v1 (for the re-review): the serialized validation-to-dispatch execution boundary with its concurrency guarantees (P0-01, §2.2), the fail-closed rule defined by operation class and reconciled with MINIMAL/BANKING_SAFE (P1-01, §2.3), the single authoritative session state machine with serialized creation, monotonic expiry, invalidation, and queued-operation cancellation (P1-02, §2.4), the complete /launch_app and /open async lifecycle and post-launch verification contract (P1-03, §2.5), the generation scope, increment rules, and comparison semantics (P2-01, §2.1), the notification ordering and failure independence (P2-02, §2.7), the additional no-auto-retry prohibitions (timeout, lost response, stale context reuse, coordinate regeneration) and the batch 409 semantics reconciliation (§2.6), and acceptance criteria C-04-14..22 added (§3).
- Changes from v2 (for the final review): the main-thread serialization contract — the admission boundary is executed on the service main thread via runOnMainThread, the HTTP worker holds no lock while waiting, and the final foreground result and the dispatch belong to the same serialized main-thread task (P2-01, §2.2); the snapshot consistency rule — tree_hash and generation must refer to the same authoritative snapshot, a snapshot update between validation and dispatch invalidates admission, and an unavailable/inconsistent snapshot is refused with 503 (P2-02, §2.1); the explicit limitation that generation cannot detect visual changes that leave the accessibility tree unchanged (§2.1); and the test clarifications to acceptance criteria C-04-7, C-04-10, C-04-14, C-04-16 (§3). No architectural change from v2.
