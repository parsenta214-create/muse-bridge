// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-14
// Stage 3 batch 4 (final): HttpBridgeServer.java (3,797 lines) -> Kotlin, with
// C-04 (SPEC-FOREGROUND-GUARD-001 v2.1) woven in: operation_context on the
// guarded endpoints, the server-issued session_id, and the serialized
// admission boundary (BridgeAccessibilityService.runGuardedOperation).
package dev.muse.bridge

import android.app.DownloadManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ActivityNotFoundException
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.LinkAddress
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.InputStream
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.nio.charset.StandardCharsets
import java.util.Collections
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.Semaphore
import java.util.concurrent.ThreadFactory
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import java.util.UUID

/**
 * Minimal raw-socket HTTP server for the bridge. No external dependencies.
 *
 * Binds to the Tailscale IPv4 address when present, plus 127.0.0.1.
 * Every request needs the runtime token in the X-Bridge-Token header.
 * Only the method, a known endpoint name (anything else logs as
 * "UNKNOWN"), and status are logged — never the token or body.
 *
 * Endpoints (coordinates are screen pixels):
 *   GET  /health          -> {"ok":true, ...} (v1.8: also
 *                         "app_version"/"app_version_code")
 *   GET  /capture_screen  -> PNG bytes (default) or, with
 *   ?scale=0.5&format=jpeg&quality=80, a half-scale JPEG (v1.2).
 *   GET  /state           -> {"width":..,"height":..,"package":"..","texts":[..],
 *                         "revision":..,"nodes":[..],"focused":..,
 *                         "ime_visible":..,"truncated":..,"event_seq":..,
 *                         "app_version":..,"app_version_code":..}
 *                         (v1.4: node tree; v1.6: event_seq;
 *                         v1.8: installed app version)
 *   GET  /events?since=..&limit=.. -> {"events":[..],"latest_seq":..,
 *                         "dropped":..,"tree_hash":..,"revision":..} (v1.6)
 *   GET  /notifications?package=..&since=..&limit=.. -> {"events":[..],
 *                         "latest_seq":..,"dropped":..} (v1.9.7; 503 when
 *                         the listener is not granted / not connected)
 *   POST /click           {"x":..,"y":..,"operation_context":{..}}
 *                         (C-04: guarded — 409 on foreground/tree/
 *                         generation/session mismatch, 503 when the
 *                         foreground cannot be verified)
 *   POST /click_node      {"node_id":"..","tree_hash":..} (v1.5)
 *   POST /type_node       {"node_id":"..","tree_hash":..,"text":".."} (v1.5)
 *   POST /batch           {"batch_id":"..","tree_hash":..,"actions":[..],
 *                         "operation_context":{..}} (v1.5; C-04: the
 *                         context is checked once, before the batch
 *                         executes)
 *   POST /swipe           {"x1":..,"y1":..,"x2":..,"y2":..,"duration_ms":..,
 *                         "operation_context":{..}} (C-04: guarded)
 *   POST /type_text       {"text":"..","operation_context":{..}}
 *                         (C-04: guarded)
 *   POST /press_key       {"key":"HOME"|"BACK"|"RECENTS"|"POWER"|
 *                         "DPAD_UP"|"DPAD_DOWN"|"DPAD_LEFT"|"DPAD_RIGHT"|
 *                         "DPAD_CENTER","operation_context":{..}}
 *                         (v1.9.9: DPAD keys; C-04: guarded)
 *   POST /scroll_node     {"node_id":"..","tree_hash":..,
 *                         "direction":"forward"|"backward"} (v1.9.10)
 *   POST /node_action     {"node_id":"..","tree_hash":..,"action":"..",
 *                         "args":{..}} (v1.9.11: any one-shot node action)
 *   POST /launch_app      {"package":"com.example.app",
 *                         "operation_context":{"session_id":".."}} (v1.9.12;
 *                         C-04: session validated before launch, target
 *                         confirmed by post-launch verification)
 *   POST /open            {"uri":"https://example.com",
 *                         "operation_context":{"session_id":".."}} (v1.9.15;
 *                         C-04: same launch contract as /launch_app)
 *   POST /install_apk     {"url":"https://...apk"} or {"path":"/...apk"} (v1.9.16)
 *   GET  /install_status  (v1.9.16)
 *   POST /repair_termux   {"action":"sshd"} (v2.5.0, TARMUX_KICKASS step 2:
 *                         fixed repair only — termux-wake-lock then sshd
 *                         via Termux RUN_COMMAND, then a local re-check)
 *   GET  /clipboard       (v1.9.17)
 *   POST /clipboard       {"text":"..."} (v1.9.17)
 *   POST /notification_action {"key":"...","action":"open"|"reply","text":"...",
 *                         "operation_context":{..}} (v1.9.18; C-04: guarded)
 *   POST /notify          {"title":"..","text":".."} -> posts a notification
 *                         (v1.3; tap opens Downloads so the user can tap
 *                         the pushed APK to install it)
 *   POST /operation_end   {} -> {"ok":true,"was_active":..} (v2.1: ends
 *                         the current operation session; posts
 *                         "操作終了" when a session was active)
 *
 * C-04 operation_context (SPEC-FOREGROUND-GUARD-001 §2.1):
 *   {"expected_package":"..","session_id":"..",
 *    "tree_hash":..,"generation":..} — expected_package is required
 *   on the guarded endpoints (400 when missing); session_id is the
 *   server-issued UUID (omitted on the first operation of a session
 *   — the server creates the session and returns its id in the
 *   response; a supplied id that is not the active one is 409
 *   session_mismatch); tree_hash and generation are optional and,
 *   when supplied, are checked against the same snapshot.
 */
open class HttpBridgeServer(
    private val port: Int,
    private val tokens: TokenProvider,
    ctx: Context
) {

    private val context: Context = ctx.applicationContext

    companion object {
        private const val TAG = "MuseBridge"
        const val PORT = 8080
        private const val UPDATE_CHANNEL_ID = "muse-bridge-updates"
        private const val UPDATE_NOTIFICATION_ID = 2

        // v2.1: operation notifications ("操作中" / "操作終了"). The
        // bridge posts them while the VM operates the phone screen, so
        // the user can see at a glance that Muse is operating the
        // phone and when it finished. Dedicated channel (not the
        // update channel); ID 3 is the ongoing "操作中", ID 4 the
        // one-shot "操作終了". See SPEC-OPERATION-NOTIFY.md.
        private const val OPERATION_CHANNEL_ID = "muse-bridge-operation"
        private const val OPERATION_NOTIFICATION_ID = 3
        private const val OPERATION_END_NOTIFICATION_ID = 4
        private const val OPERATION_AUTO_END_TIMEOUT_MS = 10_000L
        // SPEC-OVERRIDE-001 §3.3: notification throttle window.
        private const val OPERATION_NOTIFY_THROTTLE_MS = 500L

        // v2.1 fix (CONS-023 P2, D6): the one-shot node actions the
        // service accepts. Canonical list is
        // BridgeAccessibilityService.actionIdForName(); this copy is
        // checked before the /node_action notification hook, so an
        // unknown action name can never reach a notification label.
        // Case-sensitive, like the service's switch. Fail-closed: if
        // the service gains a new action, the server 400s it until
        // this list is updated.
        private val KNOWN_NODE_ACTIONS = setOf(
            "click", "long_click", "focus", "clear_focus",
            "accessibility_focus", "clear_accessibility_focus",
            "select", "clear_selection", "set_selection",
            "copy", "cut", "paste", "expand", "collapse", "dismiss",
            "show_on_screen", "context_click", "ime_enter",
            "scroll_forward", "scroll_backward",
            "scroll_up", "scroll_down", "scroll_left", "scroll_right",
            "page_up", "page_down", "page_left", "page_right",
            "press_and_hold", "set_progress", "scroll_to_position",
            "set_text")

        // P1-03 fix round 2 (CONS-033): the request path logged in the
        // finally block must never be the raw request target — any
        // arbitrary path (a typo, a probe, a future path-parameter
        // endpoint) would land verbatim in Logcat. Only these known
        // endpoints (the handler's if/else chain, exactly) are logged
        // by name; anything else logs as "UNKNOWN". Fail-closed: if
        // the handler gains a new endpoint, it logs as UNKNOWN until
        // this list is updated — and the source-level guard in
        // test_android_app_client.py fails the suite until it is.
        private val KNOWN_PATHS = setOf(
            "/health", "/capture_screen", "/state", "/logs",
            "/events", "/notifications", "/notification_action",
            "/click_node", "/type_node", "/scroll_node",
            "/node_action", "/launch_app", "/open", "/clipboard",
            "/install_status", "/install_apk", "/repair_termux",
            "/batch", "/batch/confirm", "/batch/restore",
            "/batch/clear", "/batch/recovery_complete",
            "/batch/durability_verify",
            "/click", "/swipe", "/type_text",
            "/press_key", "/notify", "/operation_end")

        /** Loggable form of a request path: the path itself when it is
         * a known endpoint, "UNKNOWN" otherwise. */
        fun loggablePath(path: String): String {
            return if (KNOWN_PATHS.contains(path)) path else "UNKNOWN"
        }

        // A-1 (v1.9): input-validation limits and per-phase deadlines.
        // These are initial design values (Codex Phase A); tune them from
        // measured load, not from first principles.
        private const val MAX_HEADER_BYTES = 8 * 1024
        private const val MAX_BODY_BYTES = 256 * 1024
        private const val HEADER_TIMEOUT_MS = 5000
        private const val BODY_TIMEOUT_MS = 10000
        private const val TOTAL_TIMEOUT_MS = 45000L

        // A-2 (v1.9): executor separation. Accept threads never run HTTP
        // handling; the worker pool is bounded so a flood cannot queue
        // forever; at most MAX_CONNECTIONS sockets are held at once.
        // These are the Codex Phase A initial values (decision 47) —
        // tune them from measured load, not from first principles.
        private const val HTTP_WORKERS = 4
        private const val HTTP_QUEUE = 8
        private const val MAX_CONNECTIONS = 16

        private const val CLIENT_FIRST_MAX = 64

        const val INSTALL_RESULT_ACTION = "dev.muse.bridge.INSTALL_RESULT"

        /** P0-4 (v2.0): poll offsets (ms after the launch request)
         * for the foreground check. ~500 ms first (a fast launch),
         * then ~1 s, ~2 s, and a final ~3 s check; the whole
         * verification is bounded to ~3 s so the HTTP round trip
         * stays within ~3–4 s. A slow app must not be failed on a
         * short timeout alone — the full window is used before
         * reporting `unconfirmed`. */
        private val LAUNCH_VERIFY_OFFSETS_MS = longArrayOf(500, 1000, 2000, 3000)
        private const val LAUNCH_VERIFY_MAX_MS = 3000L

        /** P0-4 fix (CONS-027 P1 #1): the pre-request foreground
         * read (wasForeground) is informational only — it must not
         * let a main-thread stall delay the launch itself. It waits
         * at most this long; a timeout just means "not observed". */
        private const val LAUNCH_PRE_READ_TIMEOUT_MS = 500L

        /** P0-4 deny list (System Development Lead decision 2026-11-06 20:16,
         * README decision 91, GPT P1 #4 in CONS-20261106-027):
         * banking apps and other apps where heavy security is
         * expected are NOT launch targets at all. An explicit,
         * hard-coded set of package names — no auto-detection
         * (banking apps cannot be reliably auto-detected). Adding
         * a package is a code change + test. Checked in the
         * /launch_app handler BEFORE any launch attempt and BEFORE
         * the operation notification hook, so a refused launch
         * posts no "操作中" notification. Krungthai NEXT is
         * deliberately NOT on the list — its package name is
         * unconfirmed. Initial list verified 2026-11-06. */
        private val LAUNCH_DENY_LIST = setOf(
            "com.kasikorn.retail.mbanking.wap", // K PLUS (KBank)
            "com.bbl.mobilebanking", // Bualuang mBanking
            "com.scb.phone", // SCB EASY
            "com.krungsri.kma") // KMA (Krungsri)
    }

    interface TokenProvider {
        fun currentToken(): String
    }

    /** Server lifecycle (A-2). start() only reports RUNNING when at
     * least one listener socket is bound; a bind failure leaves
     * FAILED instead of pretending to run. SPEC-LOG-001 BIND-02
     * adds DEGRADED: the server is listening on loopback only (no
     * Tailscale bind), which must NOT be reported as normal. */
    enum class ServerState { NEW, STARTING, RUNNING, DEGRADED, FAILED, STOPPING, STOPPED }

    /** SPEC-LOG-001 LOG-03: why the server stopped. The first
     * determined reason wins (AtomicReference + compareAndSet);
     * the STOPPED record itself is guarded separately by
     * stopCompleted, because determining the reason and recording
     * STOPPED once are two different problems (P1-C). */
    enum class StopReason { MANUAL, SERVICE_DESTROY, WIDGET, RESTART, ERROR, UNKNOWN }

    private val stopReason = AtomicReference<StopReason?>(null)
    private val stopCompleted = AtomicBoolean(false)
    /** Set by stop() before the state moves to STOPPING; checked
     * before each bind. CONS-063 P0-02: the lifecycle lock is the
     * synchronized start()/stop() monitor — stop() cannot run
     * while start() holds it, so a confirmed stop can never
     * interleave with a bind. The flag is the explicit pre-bind
     * confirmation required by SPEC §3.6 step 8. */
    @Volatile
    private var stopRequested = false

    /** SPEC-LOG-001 LOG-05: remote IPs already logged as
     * client-first this run (insertion order, FIFO at 64).
     * Guarded by its own monitor; reset on every start(). */
    private val clientFirstSeen = LinkedHashSet<String>()

    private val connectionSlots = Semaphore(MAX_CONNECTIONS)
    private var workers: ThreadPoolExecutor? = null
    private val listeners = ArrayList<ServerSocket>()
    private val acceptThreads = ArrayList<Thread>()
    @Volatile
    private var state = ServerState.NEW

    @Volatile
    private var installStatus = "idle"
    @Volatile
    private var installDetail = ""
    @Volatile
    private var installRunning = false

    // Decision 95 (v2.5.0, TARMUX_KICKASS step 2): cooldown for
    // POST /repair_termux. A repair that restarts sshd must not
    // be spammed — one attempt per 60 s at most, so a 5-minute
    // health loop (or a retrying client) cannot kill sshd over
    // and over while the real fault is elsewhere.
    @Volatile
    private var lastRepairAttemptMs = 0L
    private val installReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context?, intent: Intent) {
            val st = intent.getIntExtra(PackageInstaller.EXTRA_STATUS,
                PackageInstaller.STATUS_FAILURE)
            val msg = intent.getStringExtra(
                PackageInstaller.EXTRA_STATUS_MESSAGE)
            // Decision 79 (v1.9.20): also read the legacy
            // INSTALL_FAILED_* code — the status alone came back
            // as -1 with no message on the first Termux install,
            // which says nothing about the cause. A negative
            // legacy code is the real reason (e.g. -25 version
            // downgrade, -7 update incompatible / signature).
            // EXTRA_LEGACY_STATUS is a hidden API, so use its
            // literal key ("android.content.pm.extra.LEGACY_STATUS").
            val legacy = intent.getIntExtra(
                "android.content.pm.extra.LEGACY_STATUS", 0)
            val pkg = intent.getStringExtra(
                PackageInstaller.EXTRA_PACKAGE_NAME)
            val detail = "status=" + st + " legacy=" + legacy +
                (if (pkg == null) "" else " pkg=" + pkg) +
                (if (msg == null) "" else " " + msg)
            if (st == PackageInstaller.STATUS_SUCCESS) {
                installStatus = "installed"
                installDetail = detail
                BridgeLog.log("install_result", "success")
                installRunning = false
            } else if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                // Decision 80 (v1.9.21): -1 is NOT a failure.
                // STATUS_PENDING_USER_ACTION (-1) means the
                // install needs the user's tap, and the
                // confirmation activity arrives in EXTRA_INTENT
                // — the caller MUST start it, or no dialog ever
                // appears. (1.9.16–1.9.20 treated every
                // non-success status as "failed", so the Termux
                // installs stalled silently at this step with
                // "failed status=-1".) Launch the confirmation
                // and keep the job running: the final result
                // broadcast arrives after the user acts.
                // Decision 81 (v1.9.22): the key is
                // Intent.EXTRA_INTENT ("android.intent.extra.
                // INTENT") — a public constant. 1.9.21 guessed
                // the literal "android.content.pm.extra.INTENT",
                // which does not exist, so the lookup returned
                // null and the dialog still never appeared.
                val confirm = intent.getParcelableExtra(Intent.EXTRA_INTENT) as Intent?
                if (confirm != null) {
                    confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    ctx?.startActivity(confirm)
                    installStatus = "pending_user_action"
                    installDetail = "waiting for the install confirmation"
                    BridgeLog.log("install_result",
                        "pending user action; confirmation shown")
                } else {
                    installStatus = "failed"
                    installDetail = detail + " (no confirmation intent)"
                    // P1-03 (CONS-014): the log carries the status
                    // code only — `detail` holds the package name
                    // and the installer's message. (installDetail
                    // itself keeps the detail: it is the
                    // /install_status API answer to the operator,
                    // behind token auth, not the log file.)
                    BridgeLog.log("install_result", "failed status=" + st)
                    installRunning = false
                }
            } else {
                installStatus = "failed"
                installDetail = detail
                BridgeLog.log("install_result", "failed status=" + st)
                installRunning = false
            }
        }
    }

    // v2.1: operation-session tracker (SPEC-OPERATION-NOTIFY.md).
    // One global session: the server posts "操作中" when a
    // screen-changing operation starts, and "操作終了" when the
    // VM sends POST /operation_end or 10 s pass with no operation
    // activity and none in flight. All fields are guarded by
    // operationLock; NotificationManager calls are thread-safe
    // binder calls and may run on worker or scheduler threads.
    private val operationLock = Any()
    private var operationActive = false
    private var operationLastActivityMs = 0L
    private var operationInFlight = 0
    private var operationAutoEndFuture: ScheduledFuture<*>? = null
    private var operationScheduler: ScheduledExecutorService? = null
    // v2.1 fix (CONS-023 P1): session generation.
    // operationInFlight counts only operations of the current
    // generation. A request that started in an older session and
    // finishes after an explicit end (or after a new session
    // began) is stale and must not touch the new session's
    // counter — operationFinished() ignores it by generation.
    private var operationGeneration = 0L
    // SPEC-OVERRIDE-001 §3.3: notification throttle + deferred
    // post. All guarded by operationLock. pendingToken identifies
    // one deferred-post reservation; -1 means "no reservation".
    // pendingGeneration is the session generation the pending
    // label belongs to; -1 means "no reservation".
    private var operationLastNotifyMs = 0L
    private var hasPostedInSession = false
    private var pendingLabel: String? = null
    private var pendingGeneration = -1L
    private var pendingPostFuture: ScheduledFuture<*>? = null
    private var pendingToken = -1L
    private var pendingTokenCounter = 0L
    // v2.1 fix (CONS-024 P1-NEW): server-lifecycle generation.
    // Distinct from the operation-session generation above: the
    // session generation changes when a session ends, the
    // lifecycle generation changes when the server starts a new
    // lifecycle (stop -> start). A request accepted in an old
    // lifecycle must not start (or end) a session in the new
    // one, even if its worker outlives the stop and reaches
    // noteOperation()/endOperation() after the restart. The
    // generation is captured at accept time and carried through
    // handle(); noteOperation()/endOperation() refuse a
    // mismatched generation. Volatile: bumped in synchronized
    // start(), read in the accept thread (no lock) and under
    // operationLock in noteOperation()/endOperation().
    @Volatile
    private var serverLifecycleGeneration = 0L
    // C-04 (SPEC-FOREGROUND-GUARD-001 §2.4): the server-issued
    // session id (UUID v4) of the active operation session, or
    // null when no session is active. Guarded by operationLock
    // like every other session field; issued in noteOperation()
    // when a session starts, cleared in endOperationLocked()
    // and in stop(). A new session always gets a fresh UUID —
    // an ended session's id is never reused.
    private var operationSessionId: String? = null

    /** Auto-end task: ends the session only when nothing is in
     * flight and the last activity is at least the timeout old;
     * otherwise reschedules (this debounce keeps a long /batch
     * from auto-ending mid-run). Runs on the scheduler thread. */
    private val operationAutoEndTask = Runnable {
        synchronized(operationLock) {
            if (!operationActive) {
                return@Runnable
            }
            val now = SystemClock.elapsedRealtime()
            if (operationInFlight == 0 &&
                now - operationLastActivityMs >= OPERATION_AUTO_END_TIMEOUT_MS
            ) {
                endOperationLocked()
            } else {
                scheduleAutoEndLocked()
            }
        }
    }

    init {
        // v2.1: the operation-notification timer (single daemon
        // thread). Recreated in start() if a previous stop() shut
        // it down, mirroring the worker pool.
        ensureOperationScheduler()
        // SPEC-DRIVE-001: first-init check before BridgeLog.init
        // creates the run-meta file (see MainActivity.onCreate).
        LogActivity.ensureDriveInit(this.context)
        // v1.9.3: the debug log reads the previous run's tail and
        // opens the file once, when the server is created.
        BridgeLog.init(this.context)
        // Decision 78 (v1.9.19): on Android 13+ (API 33+), a
        // dynamically-registered receiver MUST declare whether
        // it is exported — the 2-arg registerReceiver throws
        // SecurityException for apps targeting API 33+ (this
        // crashed 1.9.16–1.9.18 at startup: the constructor
        // runs on service start). The install result goes only
        // to our own PackageInstaller commit, so NOT_EXPORTED.
        // The 3-arg overload does not exist below API 33, so
        // older devices keep the 2-arg call.
        if (Build.VERSION.SDK_INT >= 33) {
            ctx.registerReceiver(installReceiver,
                IntentFilter(INSTALL_RESULT_ACTION),
                Context.RECEIVER_NOT_EXPORTED)
        } else {
            ctx.registerReceiver(installReceiver,
                IntentFilter(INSTALL_RESULT_ACTION))
        }
    }

    /** Binds the listeners and starts one dedicated accept thread per
     * listener. Returns true when at least one socket is bound
     * (RUNNING, or DEGRADED on loopback only); false on total bind
     * failure. A second start after STOPPED creates a fresh worker
     * pool. SPEC-LOG-001: the bind targets come from
     * selectBindTargets() (VPN network selection + DEGRADED), and
     * the listen / bind-summary lines carry interface names. */
    @Synchronized
    fun start(): Boolean {
        if (state == ServerState.RUNNING || state == ServerState.STARTING ||
            state == ServerState.DEGRADED
        ) {
            return state == ServerState.RUNNING || state == ServerState.DEGRADED
        }
        // v2.1 fix (CONS-024 P1-NEW): a new lifecycle. Bump
        // before state becomes STARTING, so any request accepted
        // by this lifecycle's accept threads carries the new
        // generation, and any worker accepted in an old lifecycle
        // is refused by the generation check even if it reaches
        // noteOperation() during the STARTING window.
        serverLifecycleGeneration++
        state = ServerState.STARTING
        stopRequested = false
        stopReason.set(null)
        stopCompleted.set(false)
        synchronized(clientFirstSeen) {
            clientFirstSeen.clear()
        }
        // v2.1 fix (CONS-023 P2): clear operation notifications
        // left in the shade by a previous process BEFORE the
        // bind attempts — a total bind failure must not leave a
        // stale "操作中" behind. (The old position, after
        // RUNNING, is removed: it would also cancel a
        // legitimate "操作中" posted by a request accepted
        // during the STARTING window.)
        cancelOperationNotifications()
        // v2.1: a second start after STOPPED needs a fresh timer
        // (stop() shut the old one down), like the worker pool.
        ensureOperationScheduler()
        workers = ThreadPoolExecutor(HTTP_WORKERS, HTTP_WORKERS,
            0L, TimeUnit.MILLISECONDS,
            ArrayBlockingQueue<Runnable>(HTTP_QUEUE),
            ThreadPoolExecutor.AbortPolicy())
        // SPEC-LOG-001 LOG-04 (P1-D): accept notification callbacks
        // again (stop() turned them off).
        BridgeNotificationListener.setAccepting(true)
        // SPEC-LOG-001 P1-C: report a STOPPED record that failed
        // to reach the log file in a previous run, if any.
        reportPreviousStoppedRecordFailure()
        val bind = selectBindTargets()
        // SPEC-LOG-001 LOG-02: the enumerated interfaces, one line.
        BridgeLog.log("ifaces", bind.ifaceSummary)
        // Pre-bind cancel check (SPEC §3.6 step 8). With the
        // synchronized start()/stop() monitor this cannot fire —
        // stop() cannot run while start() holds the monitor — but
        // the check is the explicit confirmation the SPEC asks for.
        if (stopRequested) {
            state = ServerState.STOPPING
            workers?.shutdownNow()
            workers = null
            state = ServerState.STOPPED
            recordStopped(StopReason.UNKNOWN)
            return false
        }
        // P1-06 (CONS-014): each accept thread is bound to the
        // lifecycle generation current at its creation. An old
        // thread that wakes after a stop/restart sees the
        // mismatch and exits instead of spinning on its closed
        // socket while the new lifecycle is STARTING/RUNNING.
        val acceptGeneration = serverLifecycleGeneration
        var boundCount = 0
        var failedCount = 0
        var boundTailscale = false
        var boundLoopback = false
        for (t in bind.targets) {
            try {
                val ss = ServerSocket()
                ss.reuseAddress = true
                ss.bind(InetSocketAddress(t.addr, port))
                listeners.add(ss)
                val sock = ss
                val th = Thread(
                    { acceptLoop(sock, acceptGeneration) },
                    "muse-bridge-accept-" + t.addr.hostAddress)
                acceptThreads.add(th)
                th.start()
                // P1-03 fix round (CONS-032): never log the full IP
                // to Logcat — the port alone identifies the
                // listener here; the masked address is already in
                // BridgeLog ("listen").
                Log.i(TAG, "listening on port " + port)
                // SPEC-LOG-001 LOG-02: interface name + masked IP.
                BridgeLog.log("listen",
                    (t.ifName ?: "unknown") + " " +
                        BridgeLog.maskIp(t.addr.hostAddress) + ":" + port)
                boundCount++
                if (t.addr.isLoopbackAddress) {
                    boundLoopback = true
                } else {
                    boundTailscale = true
                }
            } catch (e: Exception) {
                // P1-03 fix round (CONS-032): exception class only —
                // the message may carry user data, and the full IP
                // must not reach Logcat. (The adjacent BridgeLog
                // call was already sanitized; the raw Log.w was not.)
                Log.w(TAG, "cannot bind: " + e.javaClass.simpleName)
                // P1-03 (CONS-014): log the exception class only —
                // the message may carry user data. The IP is
                // masked to two octets by BridgeLog.sanitize().
                BridgeLog.log("bind-failed",
                    BridgeLog.maskIp(t.addr.hostAddress) + ": " +
                        e.javaClass.simpleName)
                failedCount++
            }
        }
        // SPEC-LOG-001 LOG-02: the bind summary, after the loop so
        // bound/failed are actual counts. Loopback is included in
        // bound (its interface is listed in the detail).
        BridgeLog.log("bind-summary", "bound=" + boundCount +
            " excluded=" + bind.excluded + " failed=" + failedCount +
            (if (boundTailscale) " (tailscale bound)" else "") +
            (if (boundLoopback) " (loopback bound)" else ""))
        if (listeners.isEmpty()) {
            state = ServerState.FAILED
            workers?.shutdownNow()
            workers = null
            Log.e(TAG, "no listener bound; server FAILED")
            BridgeLog.log("server", "FAILED: no listener bound")
            return false
        }
        if (!boundTailscale) {
            // Loopback only: DEGRADED, not RUNNING (BIND-02). The
            // reason is the selector's, or the bind failure's.
            state = ServerState.DEGRADED
            val reason = bind.degradedReason ?: "tailscale-bind-failed"
            BridgeLog.log("server", "DEGRADED: " + reason)
            return true
        }
        state = ServerState.RUNNING
        BridgeLog.log("server", "RUNNING")
        return true
    }

    /** Stop with an unknown reason (kept for existing callers). */
    @Synchronized
    fun stop() {
        stop(StopReason.UNKNOWN)
    }

    /** SPEC-LOG-001 LOG-03/LOG-04 (P1-C, P1-D): stop with a reason.
     * Order: (1) stop accepting new notifications, (2) wait for
     * in-flight notification processing (5 s cap), (3) flush the
     * suppressed count, (4) stop the server, (5) record STOPPED
     * exactly once, (6) the log file is appended per line, so
     * there is no separate save step. The first-determined reason
     * wins; a second stop() is a no-op (state is STOPPED). */
    @Synchronized
    fun stop(reason: StopReason?) {
        if (state == ServerState.STOPPED || state == ServerState.NEW) {
            return
        }
        stopReason.compareAndSet(null,
            reason ?: StopReason.UNKNOWN)
        stopRequested = true
        state = ServerState.STOPPING
        // (1) Stop accepting new notifications.
        BridgeNotificationListener.setAccepting(false)
        // (2) Wait for in-flight notification processing (5 s cap,
        // monotonic clock). On timeout, record the incomplete items
        // and continue — the stop must never hang. A callback that
        // started before the stop cannot write to the log after
        // the flush: the listener re-checks the accepting flag
        // right before each log call.
        val quiesced = BridgeNotificationListener.awaitQuiesce(5000)
        if (!quiesced) {
            BridgeLog.log("notification", "stop continued with " +
                BridgeNotificationListener.inFlightCount() + " in-flight")
        }
        // (3) Flush the suppressed count.
        BridgeLog.flushThrottled("notification", "notification")
        // (4) Stop the server.
        closeListeners()
        acceptThreads.clear()
        workers?.let {
            it.shutdownNow()
            workers = null
        }
        // v2.1: stop the operation timer and clear the ongoing
        // notification silently — the bridge is going away, not
        // ending an operation cleanly, so no "操作終了" is posted.
        synchronized(operationLock) {
            operationAutoEndFuture?.let {
                it.cancel(false)
                operationAutoEndFuture = null
            }
            operationActive = false
            // v2.1 fix (CONS-024 P1-NEW): invalidate the session
            // — an in-flight request's finally must become stale
            // (generation mismatch) and the counter must not leak
            // into the next lifecycle. GPT's stop/restart trace:
            // A gen 1 inFlight 1 -> stop (inFlight 0, gen 2) ->
            // start -> B gen 3 inFlight 1 -> A finishes, stale,
            // ignored -> B finishes, inFlight 0.
            operationInFlight = 0
            operationGeneration++
            // C-04: the session id dies with the session — a
            // restarted server never accepts the old id.
            operationSessionId = null
            // SPEC-OVERRIDE-001: a pending deferred reservation
            // must not survive the stop — a stale non-null
            // pendingPostFuture would block the next session's
            // first deferred reservation.
            invalidatePendingPostLocked()
        }
        operationScheduler?.let {
            it.shutdownNow()
            operationScheduler = null
        }
        cancelOperationNotifications()
        state = ServerState.STOPPED
        // (5) Record STOPPED exactly once.
        recordStopped(reason)
    }

    private fun closeListeners() {
        for (ss in listeners) {
            try {
                ss.close()
            } catch (ignored: Exception) {
            }
        }
        listeners.clear()
    }

    /** Record STOPPED exactly once (P1-C). The AtomicBoolean guards
     * the record; the synchronized stop() + the STOPPED state check
     * are the primary guards. On a log-file write failure, persist
     * a flag so the next start() can report it. */
    private fun recordStopped(fallback: StopReason?): Boolean {
        if (!stopCompleted.compareAndSet(false, true)) {
            return false
        }
        var r = stopReason.get()
        if (r == null) {
            r = fallback ?: StopReason.UNKNOWN
        }
        val written = BridgeLog.log("server", "STOPPED: " + r.name)
        if (!written) {
            try {
                context.getSharedPreferences(MainActivity.PREFS, Context.MODE_PRIVATE)
                    .edit().putBoolean("prev_stopped_record_failed", true).apply()
            } catch (ignored: Exception) {
            }
        }
        return written
    }

    /** Report a STOPPED record that failed to reach the log file in
     * a previous run, if any (P1-C). Called once per start(). */
    private fun reportPreviousStoppedRecordFailure() {
        try {
            val prefs = context.getSharedPreferences(MainActivity.PREFS, Context.MODE_PRIVATE)
            if (prefs.getBoolean("prev_stopped_record_failed", false)) {
                prefs.edit().remove("prev_stopped_record_failed").apply()
                BridgeLog.log("server", "previous STOPPED record failed")
            }
        } catch (ignored: Exception) {
        }
    }

    fun isRunning(): Boolean {
        return state == ServerState.RUNNING || state == ServerState.DEGRADED
    }

    /** One bind target: an address plus the interface carrying it
     * (for the LOG-02 listen line). */
    private class BindTarget(val addr: InetAddress, val ifName: String?)

    /** Result of the bind-target selection (SPEC-LOG-001 §3.6/§3.7). */
    private class BindResult {
        /** Addresses to bind (Tailscale + loopback, or loopback only). */
        val targets = ArrayList<BindTarget>()
        /** DEGRADED reason when no Tailscale bind is selected; null otherwise. */
        var degradedReason: String? = null
        /** Non-loopback IPv4 addresses found but not bound (e.g. CGNAT). */
        var excluded = 0
        /** "name:masked-ip, ..." for the LOG-02 ifaces line. */
        var ifaceSummary = ""
    }

    /**
     * SPEC-LOG-001 BIND-01: select the bind targets from the VPN
     * network state — never from the 100.64/10 range alone (that
     * range is Tailscale AND carrier CGNAT, so it cannot tell them
     * apart; the 2026-11-08 outage bound the CGNAT address). The
     * Tailscale address comes from the ConnectivityManager VPN
     * discovery below; 127.0.0.1 is always bound. The interface
     * name is never used as the selector (a Tailscale TUN may be
     * "tun0", not "tailscale0").
     */
    private fun selectBindTargets(): BindResult {
        val result = BindResult()
        // Enumerate all interfaces (NetworkInterface is not
        // deprecated) for the ifaces line and the excluded count.
        val ifaceParts = ArrayList<String>()
        val nonLoopbackV4 = ArrayList<InetAddress>()
        try {
            for (ni in Collections.list(NetworkInterface.getNetworkInterfaces())) {
                for (addr in Collections.list(ni.inetAddresses)) {
                    if (addr !is Inet4Address) {
                        continue
                    }
                    val ip = addr.hostAddress
                    ifaceParts.add(ni.name + ":" + BridgeLog.maskIp(ip))
                    if (!addr.isLoopbackAddress) {
                        nonLoopbackV4.add(addr)
                    }
                }
            }
        } catch (ignored: Exception) {
        }
        result.ifaceSummary = joinComma(ifaceParts)
        // VPN discovery (§3.6 steps 1–7) + the pure decision.
        val vpns = discoverVpnNetworks()
        val decision = VpnBindSelector.select(vpns)
        if (decision.isBind()) {
            try {
                val tailscale = InetAddress.getByName(decision.bindIp)
                result.targets.add(BindTarget(tailscale, decision.bindInterface))
            } catch (e: Exception) {
                // The selector returned an address that does not
                // resolve (should not happen): fall back to DEGRADED.
                result.degradedReason = "tailscale-interface-unavailable"
            }
        } else {
            result.degradedReason = decision.degradedReason
        }
        try {
            result.targets.add(BindTarget(
                InetAddress.getByName("127.0.0.1"), "lo"))
        } catch (ignored: Exception) {
        }
        // Excluded: non-loopback IPv4 addresses not in the targets.
        for (addr in nonLoopbackV4) {
            var inTargets = false
            for (t in result.targets) {
                if (t.addr == addr) {
                    inTargets = true
                    break
                }
            }
            if (!inTargets) {
                result.excluded++
            }
        }
        return result
    }

    private fun joinComma(parts: List<String>): String {
        val out = StringBuilder()
        for (i in parts.indices) {
            if (i > 0) {
                out.append(", ")
            }
            out.append(parts[i])
        }
        return out.toString()
    }

    /**
     * SPEC-LOG-001 §3.6 steps 1–5: discover the VPN networks with
     * a NetworkCallback and snapshot them. getAllNetworks() is
     * deprecated since API 31, so the callback is the route.
     * The NetworkRequest MUST remove NET_CAPABILITY_NOT_VPN —
     * the builder requires it by default, so adding TRANSPORT_VPN
     * alone would return no VPN network (CONS-061 P0-01).
     *
     * Rules (CONS-062/063): per-Network state; no dependence on
     * the arrival order of onAvailable / onLinkPropertiesChanged;
     * onLost removes the Network from the collection and marks it
     * expired, so a delayed LinkPropertiesChanged cannot
     * re-register it; the snapshot and the close are under the
     * same lock; the 2000 ms wait is a cap measured on the
     * monotonic clock, not a retrieval guarantee; unregister runs
     * in finally, even on exception or interruption. An
     * InterruptedException interrupts safely: no bind, unregister,
     * restore the interrupt status. The full 2000 ms is always
     * waited: a second VPN's onAvailable may arrive late, and
     * fail-closed needs the complete count.
     */
    private fun discoverVpnNetworks(): List<VpnBindSelector.VpnNetwork> {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            ?: return ArrayList()
        val lock = Any()
        val collected = LinkedHashMap<Network, LinkProperties>()
        val available = HashSet<Network>()
        val expired = HashSet<Network>()
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_VPN)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
            .build()
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                synchronized(lock) {
                    if (!expired.contains(network)) {
                        available.add(network)
                    }
                }
            }

            override fun onLinkPropertiesChanged(network: Network, lp: LinkProperties) {
                synchronized(lock) {
                    if (expired.contains(network)) {
                        return
                    }
                    available.add(network)
                    collected[network] = lp
                }
            }

            override fun onLost(network: Network) {
                synchronized(lock) {
                    available.remove(network)
                    collected.remove(network)
                    expired.add(network)
                }
            }
        }
        var registered = false
        try {
            cm.registerNetworkCallback(request, cb)
            registered = true
        } catch (e: Exception) {
            // Registration failed: no VPN info — DEGRADED, not a crash.
            return ArrayList()
        }
        try {
            val deadline = SystemClock.elapsedRealtime() + 2000
            synchronized(lock) {
                while (true) {
                    val remaining = deadline - SystemClock.elapsedRealtime()
                    if (remaining <= 0) {
                        break
                    }
                    try {
                        (lock as Object).wait(remaining)
                    } catch (e: InterruptedException) {
                        Thread.currentThread().interrupt()
                        break
                    }
                }
                // Snapshot under the same lock (atomicity of close
                // and snapshot). Networks available without
                // LinkProperties count as a VPN with no IPv4.
                val out = ArrayList<VpnBindSelector.VpnNetwork>()
                val all = HashSet(available)
                all.addAll(collected.keys)
                for (net in all) {
                    val lp = collected[net]
                    val v4 = ArrayList<String>()
                    var ifName: String? = null
                    if (lp != null) {
                        ifName = lp.interfaceName
                        for (la in lp.linkAddresses) {
                            val ia = la.address
                            if (ia is Inet4Address) {
                                v4.add(ia.hostAddress)
                            }
                        }
                    }
                    out.add(VpnBindSelector.VpnNetwork(ifName, v4))
                }
                return out
            }
        } finally {
            if (registered) {
                try {
                    cm.unregisterNetworkCallback(cb)
                } catch (ignored: Exception) {
                }
            }
        }
    }

    private fun acceptLoop(ss: ServerSocket, acceptGeneration: Long) {
        // A-2: this thread only accepts. HTTP handling runs on the
        // bounded worker pool; the connection slot is released when
        // the worker finishes (or immediately when we shed the load).
        // A-5 fix: accept threads are started while the server is
        // still STARTING, so the loop must run in STARTING too — the
        // old "RUNNING only" condition made the thread exit at once
        // (the socket stayed bound but deaf, and start() still
        // reported running). It now runs until STOPPING/STOPPED.
        // P1-06 (CONS-014): the loop is also bound to the
        // lifecycle generation captured at thread creation. A
        // stop() closes this socket, so a blocked accept() throws;
        // if a new start() has already set the state back to
        // STARTING/RUNNING by the time the old thread wakes, the
        // generation mismatch (and the closed socket) ends the
        // loop instead of letting the old thread spin on the
        // closed socket and log "accept failed" forever.
        val pool = workers
        while ((state == ServerState.RUNNING || state == ServerState.STARTING ||
            state == ServerState.DEGRADED) &&
            acceptGeneration == serverLifecycleGeneration &&
            !ss.isClosed
        ) {
            try {
                val conn = ss.accept()
                // SPEC-LOG-001 LOG-05: first connection from a new
                // remote IP, once per run. Accept time, not HTTP
                // success; the port is never logged.
                logClientFirst(conn)
                // v2.1 fix (CONS-024 P1-NEW): capture the server
                // lifecycle generation at accept time. A worker
                // that outlives its lifecycle (accepted before
                // stop(), handled after restart) carries the old
                // generation and is refused by noteOperation() /
                // endOperation() — capturing at the top of
                // handle() would be too late, handle() itself
                // runs after the restart.
                val lifecycleGeneration = serverLifecycleGeneration
                if (pool == null || pool.isShutdown) {
                    try {
                        conn.close()
                    } catch (ignored: Exception) {
                    }
                    continue
                }
                if (!connectionSlots.tryAcquire()) {
                    // At the connection cap: shed without reading the
                    // request — reading is what we protect the
                    // workers from.
                    try {
                        conn.close()
                    } catch (ignored: Exception) {
                    }
                    Log.w(TAG, "connection cap reached; closed a connection")
                    BridgeLog.logThrottled("conn-cap", "conn-cap",
                        "closed a connection at cap")
                    continue
                }
                try {
                    pool.execute {
                        try {
                            val st = handle(conn, lifecycleGeneration)
                            if (st >= 400) {
                                BridgeLog.logThrottled("http-$st",
                                    "http-$st", "")
                            }
                        } finally {
                            connectionSlots.release()
                        }
                    }
                } catch (e: RejectedExecutionException) {
                    // Worker queue full: answer 503 without reading the
                    // request (reading it would defeat the shedding)
                    // and release the slot.
                    connectionSlots.release()
                    BridgeLog.logThrottled("overload-503", "overload-503",
                        "worker queue full")
                    sendOverload(conn)
                }
            } catch (e: Exception) {
                // P1-06 (CONS-014): an accept failure from a stale
                // thread (its socket was closed by stop() and a new
                // lifecycle already started) is expected, not an
                // error — log it only when this thread still
                // belongs to the current lifecycle.
                if (acceptGeneration == serverLifecycleGeneration &&
                    (state == ServerState.RUNNING ||
                        state == ServerState.STARTING ||
                        state == ServerState.DEGRADED)
                ) {
                    // P1-03 fix round (CONS-032): exception class
                    // only — the message may carry user data. (The
                    // adjacent BridgeLog call was already sanitized;
                    // the raw Log.w was not.)
                    Log.w(TAG, "accept failed: " + e.javaClass.simpleName)
                    BridgeLog.logThrottled("accept-failed", "accept-failed",
                        e.javaClass.simpleName)
                }
            }
        }
    }

    /** SPEC-LOG-001 LOG-05: log the first connection from a remote
     * IP, once per run. Dedup uses the real IP in memory (a masked
     * key would conflate 100.111.10.10 and 100.111.20.20); only the
     * masked form reaches the log. IPv4-mapped IPv6 is normalized
     * to IPv4 first. FIFO eviction at 64 entries (pure insertion
     * order — a re-connection does NOT refresh the order); an
     * evicted IP re-logs on reconnect. Thread-safe for concurrent
     * accepts. */
    private fun logClientFirst(conn: Socket) {
        try {
            val remote = conn.inetAddress ?: return
            var ip = remote.hostAddress ?: return
            if (ip.startsWith("::ffff:")) {
                ip = ip.substring(7)
            }
            synchronized(clientFirstSeen) {
                if (clientFirstSeen.contains(ip)) {
                    return
                }
                if (clientFirstSeen.size >= CLIENT_FIRST_MAX) {
                    val it = clientFirstSeen.iterator()
                    if (it.hasNext()) {
                        it.next()
                        it.remove()
                    }
                }
                clientFirstSeen.add(ip)
            }
            BridgeLog.log("client-first", BridgeLog.maskIp(ip))
        } catch (ignored: Exception) {
        }
    }

    /** 503 for an overloaded server, sent without reading the request:
     * the point of shedding load is to not spend work on it. */
    private fun sendOverload(conn: Socket) {
        try {
            val body = errorJson("server busy")
            val bytes = body.toString().toByteArray(StandardCharsets.UTF_8)
            val head = "HTTP/1.1 503 Service Unavailable\r\n" +
                "Content-Type: application/json\r\n" +
                "Content-Length: " + bytes.size + "\r\n" +
                "Connection: close\r\n\r\n"
            val out = conn.getOutputStream()
            out.write(head.toByteArray(StandardCharsets.US_ASCII))
            out.write(bytes)
            out.flush()
        } catch (ignored: Exception) {
        } finally {
            try {
                conn.close()
            } catch (ignored: Exception) {
            }
        }
    }

    private fun handle(conn: Socket, lifecycleGeneration: Long): Int {
        var method = "?"
        var path = "?"
        var status = 500
        // A-1 (v1.9): per-request monotonic deadline; each phase gets
        // the smaller of its own timeout and the remaining total time.
        val deadline = SystemClock.elapsedRealtime() + TOTAL_TIMEOUT_MS
        // v2.1: the generation noteOperation() returned when
        // this request started a screen operation (-1 when it
        // noted none); the finally block passes it to
        // operationFinished() so a stale request cannot touch
        // another session's counter (CONS-023 P1).
        // v2.1 fix (CONS-024 P1-NEW): lifecycleGeneration is the
        // server lifecycle captured at accept time; noteOperation()
        // and endOperation() refuse a mismatch with the current
        // lifecycle (a request that outlived a stop/restart).
        var opGeneration = -1L
        try {
            val `in`: InputStream = conn.getInputStream()
            val out: OutputStream = conn.getOutputStream()

            // Phase 1: headers, capped at MAX_HEADER_BYTES (over -> 431),
            // deadline HEADER_TIMEOUT_MS (timeout -> 408).
            setPhaseTimeout(conn, HEADER_TIMEOUT_MS, deadline)
            val head = readHead(`in`)
            if (head == null || head.isEmpty()) {
                return status
            }
            val lines = head.split("\r\n").dropLastWhile { it.isEmpty() }
            // Java's String.split drops trailing empty strings, Kotlin's
            // does not. readHead returns the head ending in "\r\n\r\n",
            // so without dropLastWhile the trailing "" reaches the header
            // loop below and every request 400s with "bad header line"
            // (found by the v2.9.25 smoke test, 2026-11-14). Same idiom
            // as BridgeLog.kt / VpnBindSelector.kt.
            val req = lines[0].split(" ")
            if (req.size != 3 ||
                !(req[0] == "GET" || req[0] == "POST") ||
                !req[1].startsWith("/") ||
                !(req[2] == "HTTP/1.0" || req[2] == "HTTP/1.1")
            ) {
                status = 400
                sendJson(out, status, errorJson("bad request line"))
                return status
            }
            method = req[0]
            val target = req[1]
            val q = target.indexOf('?')
            path = if (q >= 0) target.substring(0, q) else target
            val query = if (q >= 0) target.substring(q + 1) else ""
            var token: String? = null
            var contentLength: Long? = null
            var sawTransferEncoding = false
            for (i in 1 until lines.size) {
                val line = lines[i]
                val colon = line.indexOf(':')
                if (colon <= 0) {
                    status = 400
                    sendJson(out, status, errorJson("bad header line"))
                    return status
                }
                val name = line.substring(0, colon).trim()
                val value = line.substring(colon + 1).trim()
                if ("X-Bridge-Token".equals(name, ignoreCase = true)) {
                    if (token != null) {
                        status = 400
                        sendJson(out, status, errorJson("duplicate X-Bridge-Token"))
                        return status
                    }
                    token = value
                } else if ("Content-Length".equals(name, ignoreCase = true)) {
                    if (contentLength != null) {
                        status = 400
                        sendJson(out, status, errorJson("duplicate Content-Length"))
                        return status
                    }
                    try {
                        contentLength = value.toLong()
                    } catch (e: NumberFormatException) {
                        status = 400
                        sendJson(out, status, errorJson("bad Content-Length"))
                        return status
                    }
                } else if ("Transfer-Encoding".equals(name, ignoreCase = true)) {
                    sawTransferEncoding = true
                }
            }
            if (sawTransferEncoding) {
                status = 400
                sendJson(out, status, errorJson("Transfer-Encoding not supported"))
                return status
            }

            // Phase 2: token check BEFORE the body is read, so an
            // unauthenticated peer cannot make us consume a body.
            val expected = tokens.currentToken()
            if (expected != token) {
                status = 401
                sendJson(out, status, errorJson("unauthorized"))
                return status
            }

            // SPEC-PING-001 (CONS-052 PING-04/PING-05): remember
            // the client IP (the Muse client's Tailscale address)
            // as the ping target — only for authenticated requests,
            // and only when it is a real peer address (not
            // loopback, link-local, or multicast). Never fail a
            // request for this.
            try {
                val remote = conn.inetAddress
                if (remote != null && !remote.isLoopbackAddress &&
                    !remote.isLinkLocalAddress &&
                    !remote.isMulticastAddress
                ) {
                    PingProber.setLastClientIp(remote.hostAddress)
                }
            } catch (ignored: Exception) {
            }

            // Phase 3: Content-Length validation.
            val cl = contentLength ?: 0L
            if (cl < 0) {
                status = 400
                sendJson(out, status, errorJson("negative Content-Length"))
                return status
            }
            if (cl > MAX_BODY_BYTES) {
                status = 413
                sendJson(out, status, errorJson("body too large"))
                return status
            }

            // Phase 4: body, capped at MAX_BODY_BYTES, deadline
            // BODY_TIMEOUT_MS (timeout -> 408, short body -> 400).
            var body = ByteArray(0)
            if (cl > 0) {
                setPhaseTimeout(conn, BODY_TIMEOUT_MS, deadline)
                body = `in`.readNBytes(cl.toInt())
                if (body.size != cl.toInt()) {
                    status = 400
                    sendJson(out, status, errorJson("short body"))
                    return status
                }
            }

            val svc = BridgeAccessibilityService.getInstance()
            if ("/health" == path) {
                // P0-3 (v2.0): the extended health body. All 7
                // legacy fields are kept byte-identical; the 5 new
                // fields (mode, overall_status, modules,
                // capabilities, tailscale_ips) are appended.
                val a11yOn = svc != null
                val listenerGranted =
                    BridgeNotificationListener.isAccessGranted(context)
                val listenerConnected =
                    BridgeNotificationListener.getInstance() != null
                val mode = currentMode(a11yOn)
                // P1-3 (CONS-022): tailscale_ips is the CURRENT
                // Tailscale IPv4s, re-fetched on each /health
                // call (MainActivity.findTailscaleIps). The
                // bound-at-start list (boundTailscaleIps) goes
                // stale if Tailscale reassigns the IP after
                // start; it is reported separately as
                // tailscale_ips_bound so the VM can see a
                // mismatch (bound != current) and know the
                // server needs a re-bind. Re-bind itself stays
                // P1/future — this fix only makes the staleness
                // visible instead of reporting a stale IP as
                // current.
                val tailscaleIps = MainActivity.findTailscaleIps()
                val boundIps = boundTailscaleIps()
                val modules = buildModules(a11yOn, listenerGranted,
                    listenerConnected, tailscaleIps, boundIps)
                val caps = buildCapabilities(a11yOn, listenerGranted,
                    listenerConnected)
                val overall = overallStatus(modules)
                status = 200
                sendJson(out, status, JSONObject()
                    .put("ok", true)
                    .put("service", "muse-bridge")
                    .put("version", 1)
                    .put("app_version", AppVersion.name(context))
                    .put("app_version_code", AppVersion.code(context))
                    .put("accessibility", a11yOn)
                    .put("notification_listener", listenerGranted)
                    .put("mode", mode)
                    .put("overall_status", overall)
                    .put("modules", modules)
                    .put("capabilities", caps)
                    .put("tailscale_ips", JSONArray(tailscaleIps))
                    .put("tailscale_ips_bound", JSONArray(boundIps))
                    .put("termux_sshd_local", checkTermuxSshdLocal()))
            } else if ("/capture_screen" == path) {
                if (svc == null) {
                    status = 503
                    sendJson(out, status, unavailableJson(
                        "accessibility service is not enabled; " +
                            "/capture_screen needs FULL_ACCESS"))
                } else {
                    // v1.2: ?scale=0.5&format=jpeg&quality=80 for a light
                    // routine shot; defaults keep the old full-res PNG.
                    val scale = parseFloat(queryParam(query, "scale"), 1.0f)
                    val format = queryParam(query, "format")
                    val quality = parseInt(queryParam(query, "quality"), 80)
                    val jpeg = "jpeg".equals(format, ignoreCase = true) ||
                        "jpg".equals(format, ignoreCase = true)
                    val t0 = SystemClock.elapsedRealtime()
                    val shot = svc.takeScreenshot(scale, format, quality)
                    val captureMs = SystemClock.elapsedRealtime() - t0
                    status = 200
                    sendBytes(out, status, if (jpeg) "image/jpeg" else "image/png", shot,
                        "X-Capture-Ms: " + captureMs + "\r\n")
                }
            } else if ("/state" == path) {
                if (svc == null) {
                    status = 503
                    sendJson(out, status, unavailableJson(
                        "accessibility service is not enabled; " +
                            "/state needs FULL_ACCESS"))
                } else {
                    val st = svc.currentState()
                    val texts = JSONArray()
                    for (t in st.texts) {
                        texts.put(t)
                    }
                    val nodes = JSONArray()
                    for (n in st.nodes) {
                        val b = JSONObject()
                            .put("left", n.left).put("top", n.top)
                            .put("right", n.right).put("bottom", n.bottom)
                        val actions = JSONArray()
                        for (a in n.actions) {
                            actions.put(a)
                        }
                        nodes.put(JSONObject()
                            .put("id", n.id)
                            .put("parent", n.parentId ?: JSONObject.NULL)
                            .put("class", n.className)
                            .put("text", n.text)
                            .put("desc", n.contentDescription)
                            .put("bounds", b)
                            .put("clickable", n.clickable)
                            .put("focusable", n.focusable)
                            .put("focused", n.focused)
                            .put("editable", n.editable)
                            .put("scrollable", n.scrollable)
                            .put("enabled", n.enabled)
                            .put("selected", n.selected)
                            .put("password", n.password)
                            .put("input_type", n.inputType)
                            .put("resource_id", n.resourceId)
                            .put("actions", actions))
                    }
                    status = 200
                    // SPEC-PING-001: null (JSONObject.NULL) when no
                    // probe has run yet.
                    val ping = pingToClientJson()
                    sendJson(out, status, JSONObject()
                        .put("ok", true)
                        .put("app_version", AppVersion.name(context))
                        .put("app_version_code", AppVersion.code(context))
                        .put("width", st.width)
                        .put("height", st.height)
                        .put("package", st.packageName)
                        .put("texts", texts)
                        .put("revision", st.revision)
                        .put("tree_hash", st.treeHash)
                        .put("snapshot_id", st.snapshotId)
                        .put("snapshot_valid", st.snapshotValid)
                        .put("snapshot_stable", st.snapshotStable)
                        .put("nodes", nodes)
                        .put("focused", st.focusedId ?: JSONObject.NULL)
                        .put("ime_visible", st.imeVisible)
                        .put("event_seq", st.eventSeq)
                        .put("device", deviceState())
                        .put("ping_to_client",
                            ping ?: JSONObject.NULL)
                        .put("truncated", st.truncated))
                }
            } else if ("/logs" == path) {
                // v1.9.3: debug log, pull-based and token-authenticated
                // like every endpoint. ?limit= (default 100, max 200)
                // takes the newest lines of the current run; the
                // previous run's tail rides along separately.
                var limit = parseInt(queryParam(query, "limit"), 100)
                if (limit < 1) {
                    limit = 1
                }
                if (limit > 200) {
                    limit = 200
                }
                val all = BridgeLog.snapshot()
                val arr = JSONArray()
                val start = Math.max(0, all.size - limit)
                for (i in start until all.size) {
                    arr.put(all[i])
                }
                val prev = JSONArray()
                for (l in BridgeLog.previousSnapshot()) {
                    prev.put(l)
                }
                status = 200
                sendJson(out, status, JSONObject()
                    .put("ok", true)
                    .put("app_version", AppVersion.name(context))
                    .put("app_version_code", AppVersion.code(context))
                    .put("lines", arr)
                    .put("previous_lines", prev))
                return status

            } else if ("/events" == path) {
                // Phase 3 (v1.6): the recorded event history after
                // `since` (default 0 = everything buffered), oldest
                // first, capped at `limit` (default 100, max 200).
                // dropped=true means the buffer evicted events the
                // caller never saw — refetch /state instead.
                if (svc == null) {
                    status = 503
                    sendJson(out, status, unavailableJson(
                        "accessibility service is not enabled; " +
                            "/events needs FULL_ACCESS"))
                } else {
                    val since = parseLong(queryParam(query, "since"), 0)
                    var limit = parseInt(queryParam(query, "limit"), 100)
                    if (limit < 1) {
                        limit = 1
                    }
                    if (limit > 200) {
                        limit = 200
                    }
                    val r = svc.getEvents(since, limit)
                    val events = JSONArray()
                    for (e in r.events) {
                        val b = JSONObject()
                            .put("left", e.left).put("top", e.top)
                            .put("right", e.right).put("bottom", e.bottom)
                        events.put(JSONObject()
                            .put("seq", e.seq)
                            .put("t", e.t)
                            .put("type", e.type)
                            .put("package", e.packageName)
                            .put("class", e.className)
                            .put("text", e.text)
                            .put("desc", e.desc)
                            .put("bounds", b)
                            .put("coalesced", e.coalesced))
                    }
                    status = 200
                    sendJson(out, status, JSONObject()
                        .put("ok", true)
                        .put("events", events)
                        .put("latest_seq", r.latestSeq)
                        .put("dropped", r.dropped)
                        .put("tree_hash", r.treeHash)
                        .put("revision", r.revision))
                }
            } else if ("/notifications" == path) {
                // Decision 63 (v1.9.7): the notification event stream.
                // Needs neither the accessibility service nor the
                // bridge token beyond the check every endpoint gets.
                // 503 when the listener is not granted, and a
                // different 503 when granted but not connected.
                if (!BridgeNotificationListener.isAccessGranted(context)) {
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "notification_listener_access_not_granted"))
                } else if (BridgeNotificationListener.getInstance() == null) {
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "notification_listener_not_connected"))
                } else {
                    val since = parseLong(queryParam(query, "since"), 0)
                    var limit = parseInt(queryParam(query, "limit"), 100)
                    if (limit < 1) {
                        limit = 1
                    }
                    if (limit > 200) {
                        limit = 200
                    }
                    var pkg = queryParam(query, "package")
                    if (pkg != null && pkg.isEmpty()) {
                        pkg = null
                    }
                    val nr = BridgeNotificationListener.getEvents(since, limit, pkg)
                    val notifs = JSONArray()
                    for (e in nr.events) {
                        val o = JSONObject()
                            .put("seq", e.seq)
                            .put("type", e.type)
                            .put("key", e.key)
                            .put("package", e.packageName)
                            .put("id", e.id)
                            .put("tag", e.tag ?: JSONObject.NULL)
                            .put("title", e.title)
                            .put("text", e.text)
                            .put("t", e.t)
                            .put("ongoing", e.ongoing)
                            .put("group_summary", e.groupSummary)
                        if ("removed" == e.type) {
                            o.put("removed_t", e.removedT)
                        }
                        notifs.put(o)
                    }
                    status = 200
                    sendJson(out, status, JSONObject()
                        .put("ok", true)
                        .put("events", notifs)
                        .put("latest_seq", nr.latestSeq)
                        .put("dropped", nr.dropped))
                }
            } else if ("/notification_action" == path && "POST" == method) {
                // Decision 77 (v1.9.18): act on a live notification.
                // {"key": "...", "action": "open"} fires its
                // contentIntent; {"action": "reply", "text": "..."}
                // sends an inline reply through its RemoteInput.
                // The lookup is live (no stored actions), so a
                // gone key is 422. 503 when the listener is not
                // granted or not connected (same as /notifications).
                // Reply SENDS a message: the caller must have the
                // user's approval of the exact text first.
                // C-04: an ordinary screen operation — the guard
                // applies (the notification's package is the
                // expected package the VM supplies). "open" notes
                // the operation (it changes the foreground).
                // CONS-167 P1-01: "reply" notes it too — a reply is
                // a guarded operation validated against the session,
                // so it reserves the session the same way "open"
                // does (the old "no hook for reply" behaviour left
                // a validated reply with no reservation at all).
                if (!BridgeNotificationListener.isAccessGranted(context)) {
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "notification_listener_access_not_granted"))
                } else if (BridgeNotificationListener.getInstance() == null) {
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "notification_listener_not_connected"))
                } else {
                    val j = JSONObject(String(body, StandardCharsets.UTF_8))
                    val ctx = parseOperationContext(j)
                    requireService(svc)
                    requireSessionCurrent(ctx.sessionId)
                    val key = j.getString("key")
                    val action = j.optString("action", "")
                    var err: String?
                    try {
                        if ("open" == action) {
                            err = svc!!.runGuardedOperation(
                                ctx.expectedPackage, ctx.treeHash, ctx.generation,
                                { isSessionCurrent(ctx.sessionId) }) {
                                // v2.1: opening a notification changes
                                // the foreground app — show "操作中".
                                opGeneration = noteOperation(lifecycleGeneration, "通知を開いています", ctx.sessionId)
                                BridgeNotificationListener.openNotification(key)
                            }
                        } else if ("reply" == action) {
                            val text = j.getString("text")
                            err = svc!!.runGuardedOperation(
                                ctx.expectedPackage, ctx.treeHash, ctx.generation,
                                { isSessionCurrent(ctx.sessionId) }) {
                                // CONS-167 P1-01: the reply branch had no
                                // noteOperation — unlike "open" it started
                                // no session, so a reply validated against
                                // session A could execute after A ended
                                // with no reservation at all. Reserve the
                                // session the same way "open" does.
                                opGeneration = noteOperation(lifecycleGeneration, "通知に返信しています", ctx.sessionId)
                                BridgeNotificationListener.replyNotification(key, text)
                            }
                        } else {
                            throw org.json.JSONException("unknown action: " + action)
                        }
                        status = if (err == null) 200 else 422
                        sendJson(out, status, JSONObject().put("ok", err == null)
                            .put("error", err ?: JSONObject.NULL)
                            .put("session_id", currentSessionId() ?: JSONObject.NULL))
                    } catch (e: IllegalArgumentException) {
                        status = 422
                        sendJson(out, status, JSONObject().put("ok", false)
                            .put("error", e.message))
                    }
                }
            } else if ("/click_node" == path && "POST" == method) {
                // Phase 2: click by node id (tree_hash from the /state
                // the client used). 409 when the screen moved on.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                requireService(svc)
                val nodeId = j.getString("node_id")
                val nodeTreeHash = j.getInt("tree_hash")
                // v2.1: the operation is accepted — show "操作中".
                opGeneration = noteOperation(lifecycleGeneration, "タップ中")
                try {
                    val o = svc!!.clickNode(nodeId, nodeTreeHash)
                    status = if (o.ok) 200 else 422
                    sendJson(out, status, JSONObject().put("ok", o.ok)
                        .put("snapshot_id", o.snapshotId)
                        .put("error", if (o.ok) JSONObject.NULL else "node not clickable"))
                } catch (e: BridgeAccessibilityService.StaleRevisionException) {
                    status = 409
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BridgeAccessibilityService.NodeNotFoundException) {
                    status = 422
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                }
            } else if ("/type_node" == path && "POST" == method) {
                // Phase 2: set text on an editable node by id.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                requireService(svc)
                val nodeId = j.getString("node_id")
                val nodeTreeHash = j.getInt("tree_hash")
                val nodeText = j.getString("text")
                // v2.1: the operation is accepted — show "操作中".
                // The typed text never goes into the notification.
                opGeneration = noteOperation(lifecycleGeneration, "文字を入力中")
                try {
                    val o = svc!!.typeNode(nodeId, nodeTreeHash, nodeText)
                    status = if (o.ok) 200 else 422
                    sendJson(out, status, JSONObject().put("ok", o.ok)
                        .put("snapshot_id", o.snapshotId)
                        .put("error", if (o.ok) JSONObject.NULL else "node not editable"))
                } catch (e: BridgeAccessibilityService.StaleRevisionException) {
                    status = 409
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BridgeAccessibilityService.NodeNotFoundException) {
                    status = 422
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                }
            } else if ("/scroll_node" == path && "POST" == method) {
                // Decision 67 (v1.9.10): scroll a node (or its first
                // scrollable ancestor). 409 when the screen moved on.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                requireService(svc)
                // P1-04 (CONS-014): direction is REQUIRED — a
                // missing direction used to default to "forward"
                // and scroll without being asked. 400 when absent;
                // the value itself is still validated below.
                if (!j.has("direction")) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "missing direction"))
                } else {
                    val direction = j.optString("direction", "forward")
                    if (!"forward".equals(direction, ignoreCase = true) &&
                        !"backward".equals(direction, ignoreCase = true)
                    ) {
                        status = 400
                        sendJson(out, status, JSONObject().put("ok", false)
                            .put("error", "unknown direction: " + direction))
                    } else {
                        val nodeId = j.getString("node_id")
                        val nodeTreeHash = j.getInt("tree_hash")
                        // v2.1: the operation is accepted — show "操作中".
                        opGeneration = noteOperation(lifecycleGeneration, "スクロール中")
                        try {
                            val o = svc!!.scrollNode(nodeId, nodeTreeHash, direction)
                            status = if (o.ok) 200 else 422
                            sendJson(out, status, JSONObject().put("ok", o.ok)
                                .put("snapshot_id", o.snapshotId)
                                .put("error", if (o.ok) JSONObject.NULL else "node not scrollable"))
                        } catch (e: BridgeAccessibilityService.StaleRevisionException) {
                            status = 409
                            sendJson(out, status, JSONObject().put("ok", false)
                                .put("error", e.message))
                        } catch (e: BridgeAccessibilityService.NodeNotFoundException) {
                            status = 422
                            sendJson(out, status, JSONObject().put("ok", false)
                                .put("error", e.message))
                        }
                    }
                }
            } else if ("/node_action" == path && "POST" == method) {
                // Decision 68 (v1.9.11): any one-shot node action by
                // name, with optional args. 409 when the screen moved on.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                requireService(svc)
                // P1-07 (CONS-014): args must be an object when
                // present. optJSONObject() silently turns a
                // non-object value (number, string, array) into
                // null, which the old code then treated as "no
                // args" — reject it explicitly with 400 instead.
                if (j.has("args") && j.opt("args") !is JSONObject) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "args must be an object"))
                    return status
                }
                val args = j.optJSONObject("args")
                val nodeId = j.getString("node_id")
                val nodeTreeHash = j.getInt("tree_hash")
                val actionName = j.getString("action")
                // v2.1 fix (CONS-023 P2, D6): reject unknown
                // actions before the hook — the notification
                // label must never contain an unvalidated value.
                if (!KNOWN_NODE_ACTIONS.contains(actionName)) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "unknown action: " + actionName))
                    return status
                }
                // v2.1: the operation is accepted — show "操作中"
                // with the action name (fixed enum, safe).
                opGeneration = noteOperation(lifecycleGeneration, "操作を実行中（" + actionName + "）")
                try {
                    val o = svc!!.nodeAction(nodeId, nodeTreeHash, actionName, args)
                    status = if (o.ok) 200 else 422
                    sendJson(out, status, JSONObject().put("ok", o.ok)
                        .put("snapshot_id", o.snapshotId)
                        .put("error", if (o.ok) JSONObject.NULL else "action not supported"))
                } catch (e: IllegalArgumentException) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BridgeAccessibilityService.StaleRevisionException) {
                    status = 409
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BridgeAccessibilityService.NodeNotFoundException) {
                    status = 422
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                }
            } else if ("/launch_app" == path && "POST" == method) {
                // Decision 69 (v1.9.12): launch an app by package
                // name. 422 when the package is not installed or has
                // no launcher; 503 when Android refuses the start
                // (startActivity throws). P0-4 (v2.0): the 200
                // answer is a structured launch report — `launched`
                // only when the target was actually observed in the
                // foreground, `unconfirmed` otherwise. startActivity()
                // not throwing is NEVER the success condition (the
                // background-activity-start restriction can swallow
                // the start silently).
                // P0-1 (v2.0): deliberately no requireService() —
                // launching needs only startActivity from this
                // server's own context, so it works even when the
                // accessibility service is off (P1-05 fixed).
                // C-04 (§2.5): a launch is a foreground-changing
                // operation — no expected_package check (the launch
                // itself changes the foreground), but the session IS
                // validated before any launch side effect (409
                // session_mismatch), and the post-launch verification
                // confirms the EXACT requested package.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                val launchSessionId = parseLaunchSession(j)
                requireSessionCurrent(launchSessionId)
                val pkg = j.getString("package")
                // P0-4 deny list (System Development Lead decision 2026-11-06
                // 20:16, README decision 91): refuse BEFORE any
                // launch attempt and BEFORE the operation
                // notification hook — a refused launch posts no
                // "操作中" notification and never reaches
                // getLaunchIntentForPackage. HTTP 403, old error
                // format (a policy refusal, not a launch outcome).
                if (LAUNCH_DENY_LIST.contains(pkg)) {
                    // P1-03 (CONS-014): the package name is not
                    // logged — the 403 answer to the VM already
                    // names it. The log records the denial only.
                    BridgeLog.log("launch_denied", "")
                    status = 403
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "launch denied: " + pkg + " is on the deny list"))
                    return status
                }
                // v2.1: the operation is accepted — show "操作中"
                // with the app label (falls back to the package
                // name when no label exists).
                opGeneration = noteOperation(lifecycleGeneration, "アプリを起動中：" + operationAppLabel(pkg), launchSessionId)
                try {
                    // P0-4: note whether the target is already in
                    // front — the verification below only counts
                    // observations after this request. The
                    // pre-request read is bounded (500 ms): it is
                    // informational only and must not delay the
                    // launch on a main-thread stall.
                    // P2-01 (CONS-028, fix round after CONS-035):
                    // the verification baseline is captured inside
                    // launchApp(), just before startActivity() —
                    // after the Intent fetch as well as after the
                    // pre-foreground read — so neither the up-to-500 ms
                    // read nor the PackageManager lookup eats into
                    // the ~3 s post-launch window. launchApp()
                    // returns the baseline, or -1 when the start
                    // was refused.
                    val wasForeground = svc != null &&
                        pkg == foregroundPackageSafe(svc,
                            LAUNCH_PRE_READ_TIMEOUT_MS)
                    if (wasForeground) {
                        // P1-03 (CONS-014): no package name in the
                        // log — the launch answer to the VM names it.
                        BridgeLog.log("launch_already_foreground", "")
                    }
                    val requestTimeMs = launchApp(pkg)
                    if (requestTimeMs < 0) {
                        status = 503
                        sendJson(out, status, JSONObject().put("ok", false)
                            .put("error", "launch blocked by the system"))
                    } else {
                        status = 200
                        sendJson(out, status, verifyLaunchForeground(
                            pkg, svc, requestTimeMs, wasForeground)
                            .put("session_id", currentSessionId() ?: JSONObject.NULL))
                    }
                } catch (e: IllegalArgumentException) {
                    status = 422
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                }
            } else if ("/open" == path && "POST" == method) {
                // Decision 74 (v1.9.15): open a URI with ACTION_VIEW
                // (web URL or an app's deep link). Deliberately no
                // requireService() — opening needs only startActivity
                // from this server's own context, so it works even
                // when the accessibility service is off (unlike
                // /launch_app today — see P1-05). 422 when no app
                // handles the URI; 503 when Android refuses the
                // start.
                // F1 (M6 FG-01): /open now runs the same §2.5 launch
                // contract as /launch_app — the URI is resolved to its
                // target package and validated, the launch is verified
                // against the exact package, and the response carries
                // the 5-outcome shape (launchResultJson) + session_id.
                // A URI that resolves to no app is 422 (as before).
                // The target is resolved BEFORE noteOperation, so a
                // refused open does not refresh the idle timer
                // (SPEC L100; the /open half of M6 FG-02).
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                val openSessionId = parseLaunchSession(j)
                requireSessionCurrent(openSessionId)
                val uri = j.getString("uri")
                val targetPkg = resolveOpenTargetPackage(uri)
                if (targetPkg == null) {
                    status = 422
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "no app can open this link")
                        .put("session_id", currentSessionId() ?: JSONObject.NULL))
                    return status
                }
                // F1 (CONS-180 P1-01): /open applies the launch deny
                // list, as /launch_app does — a deep link into a
                // banking app must not bypass it. Checked after the
                // target is resolved and before noteOperation/dispatch,
                // so a refused open posts no notification and never
                // refreshes the idle timer. HTTP 403, old error format
                // (a policy refusal, not a launch outcome).
                if (LAUNCH_DENY_LIST.contains(targetPkg)) {
                    // P1-03 (CONS-014): the package name is not
                    // logged — the 403 answer to the VM already
                    // names it. The log records the denial only.
                    BridgeLog.log("launch_denied", "")
                    status = 403
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "launch denied: " + targetPkg + " is on the deny list"))
                    return status
                }
                // v2.1: the operation is accepted — show "操作中".
                // The URI itself never goes into the notification
                // (it can carry tokens).
                opGeneration = noteOperation(lifecycleGeneration, "リンクを開いています", openSessionId)
                try {
                    // P0-4: note whether the target is already in
                    // front — the verification below only counts
                    // observations after this request.
                    val wasForeground = svc != null &&
                        targetPkg == foregroundPackageSafe(svc,
                            LAUNCH_PRE_READ_TIMEOUT_MS)
                    val requestTimeMs = SystemClock.elapsedRealtime()
                    // CONS-181 P1-01: openUri() returns false when
                    // the start fails — the old 422 path ("could not
                    // open uri"), not a 200 verification. A failed
                    // launch is a different state from "launch
                    // succeeded but foreground unverified". (openUri()
                    // today throws instead of returning false, so
                    // this is defensive; the catch blocks below cover
                    // the live failure paths.)
                    if (!openUri(uri)) {
                        status = 422
                        sendJson(out, status, JSONObject().put("ok", false)
                            .put("error", "could not open uri")
                            .put("session_id", currentSessionId() ?: JSONObject.NULL))
                        return status
                    }
                    status = 200
                    sendJson(out, status, verifyLaunchForeground(
                        targetPkg, svc, requestTimeMs, wasForeground)
                        .put("session_id", currentSessionId() ?: JSONObject.NULL))
                } catch (e: IllegalArgumentException) {
                    status = 422
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: ActivityNotFoundException) {
                    status = 422
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "no app can open this link"))
                } catch (e: SecurityException) {
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "open blocked by the system"))
                }
            } else if ("/clipboard" == path && "GET" == method) {
                // Decision 76 (v1.9.17): read the clipboard. Runs
                // on this server's own context, no accessibility
                // service needed. text is null when the clipboard
                // is empty — or when Android 10+ blocks the read
                // while the app runs in the background (the live
                // test settles which one it is). Capped at 64 KiB
                // with truncated=true. The content is never written
                // to BridgeLog (it may hold a password).
                val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
                var clip: ClipData? = null
                try {
                    clip = if (cm == null) null else cm.primaryClip
                } catch (e: SecurityException) {
                    // Background read blocked on this device — the
                    // same outcome as an empty clipboard (null).
                    clip = null
                }
                var cs: CharSequence? = null
                if (clip != null && clip.itemCount > 0) {
                    cs = clip.getItemAt(0).coerceToText(context)
                }
                var text = cs?.toString()
                var truncated = false
                if (text != null && text.length > 65536) {
                    text = text.substring(0, 65536)
                    truncated = true
                }
                status = 200
                sendJson(out, status, JSONObject()
                    .put("ok", true)
                    .put("text", text ?: JSONObject.NULL)
                    .put("truncated", truncated))
            } else if ("/clipboard" == path && "POST" == method) {
                // Decision 76 (v1.9.17): write the clipboard.
                // Empty text clears it. Writing works from the
                // background (only reading is restricted).
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                val text = j.getString("text")
                val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
                cm?.setPrimaryClip(ClipData.newPlainText("muse-bridge", text))
                status = 200
                sendJson(out, status, JSONObject().put("ok", true))
            } else if ("/install_status" == path && "GET" == method) {
                // Decision 75 (v1.9.16): the install-apk job state
                // (idle / downloading / installing / committed /
                // installed / failed). Read-only.
                status = 200
                sendJson(out, status, JSONObject()
                    .put("ok", true)
                    .put("status", installStatus)
                    .put("detail", installDetail))
            } else if ("/install_apk" == path && "POST" == method) {
                // Decision 75 (v1.9.16): install an APK. {"url":
                // "https://...apk"} downloads it on the phone
                // (the bridge itself fetches it, so installing
                // Termux does not depend on Termux); {"path":
                // "/...apk"} installs a file already on the
                // phone. Exactly one of the two. The job runs on
                // its own thread — a ~114 MB download takes
                // minutes on mobile data — and returns "started"
                // at once; the PackageInstaller then shows the
                // system confirmation dialog and the USER taps
                // Install (a normal app cannot install silently:
                // INSTALL_PACKAGES is signature-level). 400 on a
                // bad request, 409 while a job is running, 422
                // when a given path does not exist.
                // Decision 80 (v1.9.21): the dialog appears because
                // the install receiver launches the confirmation
                // activity from EXTRA_INTENT on
                // STATUS_PENDING_USER_ACTION (-1) — that status is
                // NOT a failure, and 1.9.16–1.9.20 misread it as
                // one, so no dialog ever appeared. Decision 81
                // (v1.9.22): the key is Intent.EXTRA_INTENT —
                // 1.9.21 guessed the wrong literal key, so the
                // lookup returned null and the dialog still never
                // appeared.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                val url = j.optString("url", null)
                val apkPath = j.optString("path", null)
                if ((url == null) == (apkPath == null)) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "give exactly one of url, path"))
                } else if (url != null && !url.startsWith("https://")) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "url must be https"))
                } else if (apkPath != null && !File(apkPath).isFile) {
                    status = 422
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "apk file not found: " + apkPath))
                } else if (installRunning) {
                    status = 409
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "install already in progress"))
                } else {
                    val fUrl = url
                    val fPath = apkPath
                    Thread({ runInstall(fUrl, fPath) }, "install-apk").start()
                    status = 200
                    sendJson(out, status, JSONObject().put("ok", true)
                        .put("status", "started"))
                }
            } else if ("/repair_termux" == path && "POST" == method) {
                // Decision 95 (v2.5.0, TARMUX_KICKASS step 2):
                // repair Termux's sshd. {"action":"sshd"} is the
                // only accepted action (absent means "sshd");
                // anything else is 400. No path, command or
                // arguments are taken from the request — the two
                // commands are hardcoded in repairTermuxSshd().
                // 403 when the RUN_COMMAND permission is not
                // granted yet; 409 inside the 60 s cooldown.
                // Always 200 for a completed attempt: "ok" is
                // true exactly when sshd answers locally after
                // the attempt (see repairTermuxSshd()).
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                val action = j.optString("action", "sshd")
                if ("sshd" != action) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "unknown action: " + action))
                } else if (!canRunTermuxCommands()) {
                    status = 403
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "RUN_COMMAND permission not granted — " +
                            "open Muse Bridge and allow it, or grant it " +
                            "in Settings > Apps > Muse Bridge > Permissions"))
                } else if (System.currentTimeMillis() - lastRepairAttemptMs < 60000) {
                    status = 409
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "repair attempted recently, wait 60s"))
                } else {
                    lastRepairAttemptMs = System.currentTimeMillis()
                    status = 200
                    sendJson(out, status, repairTermuxSshd())
                }
            } else if ("/batch" == path && "POST" == method) {
                // Phase 2 (A-4): several actions in one request
                // (batch_id, protocol_version, tree_hash, actions[]).
                // Re-sent batch_id + same payload returns the cached
                // result without re-executing (idempotency); same id
                // with different content is 409, an expired id is 410.
                // C-04 (§2.2, §2.8): the batch's operation_context
                // is checked once, before the batch executes — the
                // foreground/session precondition for the batch as a
                // whole. A guard refusal means no action in the
                // batch is executed (C-04-12/22).
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                requireService(svc)
                try {
                    rejectUnknownFields(j, "batch_id", "protocol_version",
                        "tree_hash", "actions", "supersedes", "operation_context")
                    val batchCtx = parseOperationContext(j)
                    val protocolVersion = j.optInt("protocol_version", 1)
                    if (protocolVersion != 1) {
                        throw org.json.JSONException(
                            "unsupported protocol_version: " + protocolVersion)
                    }
                    val actions = ArrayList<BridgeAccessibilityService.BatchAction>()
                    val arr = j.getJSONArray("actions")
                    // A-5: a batch is capped at 20 actions (Codex Phase A).
                    // F1 (M7-07): the old "20 x 2 s" rationale was wrong
                    // — expect_change always breaks on its first step,
                    // so 20 x 2 s can never occur. A batch self-limits
                    // to 15 s of main-thread work (BATCH_BUDGET_MS)
                    // inside the 20 s A-3 timeout, which is inside the
                    // 45 s request deadline.
                    if (arr.length() > 20) {
                        throw org.json.JSONException(
                            "batch has too many actions (max 20): " + arr.length())
                    }
                    for (i in 0 until arr.length()) {
                        val a = arr.getJSONObject(i)
                        rejectUnknownFields(a, "action_id", "type", "node_id",
                            "x", "y", "x1", "y1", "x2", "y2", "duration_ms",
                            "text", "key", "wait_ms", "expect_change")
                        val ba = BridgeAccessibilityService.BatchAction()
                        ba.actionId = a.optString("action_id", "a" + i)
                        ba.type = a.getString("type")
                        ba.nodeId = a.optString("node_id", "")
                        ba.x = a.optInt("x", 0)
                        ba.y = a.optInt("y", 0)
                        ba.x1 = a.optInt("x1", 0)
                        ba.y1 = a.optInt("y1", 0)
                        ba.x2 = a.optInt("x2", 0)
                        ba.y2 = a.optInt("y2", 0)
                        ba.durationMs = a.optLong("duration_ms", 300)
                        ba.text = a.optString("text", "")
                        ba.key = a.optString("key", "")
                        ba.waitMs = a.optLong("wait_ms", 0)
                        ba.expectChange = a.optBoolean("expect_change", false)
                        actions.add(ba)
                    }
                    val batchId = j.getString("batch_id")
                    val batchTreeHash = j.getInt("tree_hash")
                    // C-01 (SPEC-BATCH-PERSIST-001 §2.6): an optional
                    // supersedes names the UNKNOWN record this batch
                    // replaces. Absent, null, or empty means none.
                    var supersedes = j.optString("supersedes", null)
                    if (supersedes != null && supersedes.isEmpty()) {
                        supersedes = null
                    }
                    // C-04: worker-side session pre-check — a rejected
                    // batch must not refresh the session timer (§2.4).
                    // CONS-167 P1-02: noteOperation runs only after the
                    // final in-task guard passes, via the
                    // onFinalGuardPassed callback — a guard-refused
                    // batch (worker guard, admission, or final guard)
                    // posts no "操作中" and starts no session, and a
                    // replayed / unknown batch (which executes nothing)
                    // posts none either.
                    // CONS-168 P1-01: the generation MUST reach
                    // handle()'s finally even when runBatch throws
                    // after the hook ran (e.g. ServiceTimeoutException
                    // from the 20 s main-thread timeout — the hook
                    // runs at the start of runBatchInternal, the
                    // actions can stall long after it). Copying it
                    // only after a normal return leaked
                    // operationInFlight: handle()'s finally saw -1,
                    // never called operationFinished, and the 10 s
                    // auto-end timer was defeated until an explicit
                    // /operation_end or stop(). So the copy lives in
                    // a finally, and the hook is disarmed there too:
                    // a main-thread task still queued when the worker
                    // timed out must not note an operation after
                    // handle() has already exited (its generation
                    // would never be finished). The disarm and the
                    // copy share batchHookLock, so the callback's
                    // check-and-note and the worker's disarm-and-copy
                    // are mutually exclusive. Lock order: service
                    // monitor -> batchHookLock -> operationLock; no
                    // path takes them in reverse.
                    requireSessionCurrent(batchCtx.sessionId)
                    val batchOpGeneration = AtomicLong(-1L)
                    val batchHookLock = Any()
                    var batchHookArmed = true
                    val br = try {
                        svc!!.runBatch(
                            batchId, protocolVersion, batchTreeHash, actions, supersedes,
                            guardExpectedPackage = batchCtx.expectedPackage,
                            guardTreeHash = batchCtx.treeHash ?: batchTreeHash,
                            guardGeneration = batchCtx.generation,
                            guardSessionValid = { isSessionCurrent(batchCtx.sessionId) },
                            onFinalGuardPassed = {
                                synchronized(batchHookLock) {
                                    if (batchHookArmed) {
                                        batchOpGeneration.set(noteOperation(
                                            lifecycleGeneration, "複数の操作を実行中",
                                            batchCtx.sessionId))
                                    }
                                }
                            })
                    } finally {
                        synchronized(batchHookLock) {
                            batchHookArmed = false
                            opGeneration = batchOpGeneration.get()
                        }
                    }
                    // C-01 (SPEC §2.10): an UNKNOWN response carries
                    // no results array — only the batch_id, the
                    // status, and a message. The batch is NOT
                    // re-executed; the client must not re-send this
                    // batch_id and must report to the operator.
                    if ("unknown" == br.status) {
                        status = 200
                        sendJson(out, status, JSONObject()
                            .put("ok", true)
                            .put("batch_id", br.batchId)
                            .put("status", "unknown")
                            .put("replayed", false)
                            .put("session_id", currentSessionId() ?: JSONObject.NULL)
                            .put("message", "batch outcome is unknown — do not re-send this batch_id; check the phone and confirm manually"))
                        return status
                    }
                    val results = JSONArray()
                    for (r in br.results) {
                        results.put(JSONObject()
                            .put("action_id", r.actionId)
                            .put("status", r.status)
                            .put("error", if (r.error.isEmpty()) JSONObject.NULL else r.error)
                            .put("hash_before", r.hashBefore)
                            .put("hash_after", r.hashAfter))
                    }
                    val completed = JSONArray()
                    for (id in br.completedActionIds) {
                        completed.put(id)
                    }
                    status = 200
                    sendJson(out, status, JSONObject()
                        .put("ok", true)
                        .put("batch_id", br.batchId)
                        .put("snapshot_id", br.snapshotId)
                        .put("status", br.status)
                        .put("payload_hash", br.payloadHash)
                        .put("replayed", br.replayed)
                        .put("session_id", currentSessionId() ?: JSONObject.NULL)
                        .put("results", results)
                        .put("completed_action_ids", completed)
                        .put("failed_action_id", if (br.failedActionId.isEmpty()) JSONObject.NULL else br.failedActionId)
                        .put("stopped_at", br.stoppedAt)
                        .put("stop_reason", if (br.stopReason.isEmpty()) JSONObject.NULL else br.stopReason))
                } catch (e: BridgeAccessibilityService.BatchMismatchException) {
                    status = 409
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BridgeAccessibilityService.BatchGoneException) {
                    status = 410
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BridgeAccessibilityService.StaleRevisionException) {
                    status = 409
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BridgeAccessibilityService.NodeNotFoundException) {
                    status = 422
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BatchStoreException) {
                    // C-01 (SPEC §2.4, §2.5, §2.7): the Registry
                    // fail-closed errors. "error" carries the SPEC
                    // error id so the client can tell them apart.
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.errorId)
                        .put("message", e.message))
                } catch (e: BatchBadRequestException) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BatchNotFoundException) {
                    status = 404
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                }
            } else if ("/batch/confirm" == path && "POST" == method) {
                // C-01 (SPEC-BATCH-PERSIST-001 §2.6): record a manual
                // confirmation on an UNKNOWN record. A management
                // operation, not a screen operation — no "操作中"
                // notification. Behind the existing token auth (the
                // VM's authorized client) until SPEC-AUTH-001
                // defines the tier.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                requireService(svc)
                try {
                    rejectUnknownFields(j, "batch_id", "confirmed_by",
                        "confirmed_via", "outcome", "note")
                    val batchId = j.getString("batch_id")
                    val confirmedBy = j.getString("confirmed_by")
                    val confirmedVia = j.optString("confirmed_via", "vm")
                    val outcome = j.getString("outcome")
                    val note = j.optString("note", "")
                    if (confirmedBy.isEmpty()) {
                        throw BatchBadRequestException("confirmed_by must not be empty")
                    }
                    if (confirmedVia.isEmpty()) {
                        throw BatchBadRequestException("confirmed_via must not be empty")
                    }
                    val registry = requireBatchRegistry(svc)
                    val mc = registry.confirm(
                        batchId, confirmedBy, confirmedVia, outcome, note)
                    status = 200
                    sendJson(out, status, JSONObject()
                        .put("ok", true)
                        .put("batch_id", batchId)
                        .put("status", "unknown")
                        .put("manual_confirmation", JSONObject()
                            .put("confirmed_at", mc.confirmedAt)
                            .put("confirmed_by", mc.confirmedBy)
                            .put("confirmed_via", mc.confirmedVia)
                            .put("outcome", mc.outcome)
                            .put("note", mc.note)))
                } catch (e: BatchStoreException) {
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.errorId)
                        .put("message", e.message))
                } catch (e: BatchBadRequestException) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BatchNotFoundException) {
                    status = 404
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                }
            } else if ("/batch/restore" == path && "POST" == method) {
                // C-01 (SPEC §2.5): restore from the backup file.
                // Operator-explicit only — never automatic. Sets
                // recoveryPending, so /batch stays refused until the
                // operator completes verification.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                requireService(svc)
                try {
                    rejectUnknownFields(j, "note")
                    val note = j.optString("note", "")
                    val registry = requireBatchRegistry(svc)
                    val summary = registry.restoreFromBackup(note)
                    status = 200
                    sendJson(out, status, JSONObject()
                        .put("ok", true)
                        .put("restored_records", summary.restoredRecords)
                        .put("converted_to_unknown", summary.convertedToUnknown)
                        .put("recovery_pending", true))
                } catch (e: BatchStoreException) {
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.errorId)
                        .put("message", e.message))
                } catch (e: BatchBadRequestException) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BatchNotFoundException) {
                    status = 404
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                }
            } else if ("/batch/clear" == path && "POST" == method) {
                // C-01 (SPEC §2.5): deliberately clear the store.
                // One atomic transition to a gated-empty store; the
                // clear alone does NOT resume /batch (the recovery
                // gate stays on until verification completes).
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                requireService(svc)
                try {
                    rejectUnknownFields(j, "note")
                    val note = j.optString("note", "")
                    val registry = requireBatchRegistry(svc)
                    registry.clearStore(note)
                    status = 200
                    sendJson(out, status, JSONObject()
                        .put("ok", true)
                        .put("recovery_pending", true))
                } catch (e: BatchStoreException) {
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.errorId)
                        .put("message", e.message))
                } catch (e: BatchBadRequestException) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BatchNotFoundException) {
                    status = 404
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                }
            } else if ("/batch/recovery_complete" == path && "POST" == method) {
                // C-01 (SPEC §2.5): the operator has checked the
                // phone's execution history; the recovery gate lifts
                // and /batch resumes.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                requireService(svc)
                try {
                    rejectUnknownFields(j, "completed_by")
                    val completedBy = j.getString("completed_by")
                    if (completedBy.isEmpty()) {
                        throw BatchBadRequestException("completed_by must not be empty")
                    }
                    val registry = requireBatchRegistry(svc)
                    registry.completeRecovery(completedBy)
                    status = 200
                    sendJson(out, status, JSONObject()
                        .put("ok", true)
                        .put("recovery_pending", false))
                } catch (e: BatchStoreException) {
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.errorId)
                        .put("message", e.message))
                } catch (e: BatchBadRequestException) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BatchNotFoundException) {
                    status = 404
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                }
            } else if ("/batch/durability_verify" == path && "POST" == method) {
                // C-01 (SPEC §2.2, C-01-18): record that the
                // phone-side storage durability has been demonstrated
                // by the §5 real-device power-loss-equivalent test.
                // Only after this does /batch resume. The method
                // must name this build's storage method.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                requireService(svc)
                try {
                    rejectUnknownFields(j, "method", "note")
                    val storageMethod = j.getString("method")
                    val note = j.optString("note", "")
                    val registry = requireBatchRegistry(svc)
                    registry.recordDurabilityVerified(storageMethod, note)
                    status = 200
                    sendJson(out, status, JSONObject()
                        .put("ok", true)
                        .put("method", storageMethod)
                        .put("durability_verified", true))
                } catch (e: BatchStoreException) {
                    status = 503
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.errorId)
                        .put("message", e.message))
                } catch (e: BatchBadRequestException) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                } catch (e: BatchNotFoundException) {
                    status = 404
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", e.message))
                }
            } else if ("/click" == path && "POST" == method) {
                // C-04: guarded. The context is validated inside
                // runGuardedOperation on the service main thread,
                // together with the dispatch — the serialized
                // admission boundary (§2.2). noteOperation runs
                // inside the same task (after the guard passes), so
                // a rejected request posts no "操作中" and does not
                // refresh the session timer (§2.4).
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                val ctx = parseOperationContext(j)
                requireService(svc)
                requireSessionCurrent(ctx.sessionId)
                val x = j.getInt("x")
                val y = j.getInt("y")
                // SPEC-OVERRIDE-001 §2: coordinates are the
                // operation's own metadata (getInt() guarantees an
                // integer, not an in-screen range — out-of-range
                // values are posted as-is).
                val ok = svc!!.runGuardedOperation(
                    ctx.expectedPackage, ctx.treeHash, ctx.generation,
                    { isSessionCurrent(ctx.sessionId) }) {
                    opGeneration = noteOperation(lifecycleGeneration, "タップ " + x + "/" + y, ctx.sessionId)
                    svc!!.tap(x, y)
                }
                status = 200
                sendJson(out, status, JSONObject().put("ok", ok)
                    .put("session_id", currentSessionId() ?: JSONObject.NULL))
            } else if ("/swipe" == path && "POST" == method) {
                // C-04: guarded (same shape as /click).
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                val ctx = parseOperationContext(j)
                requireService(svc)
                requireSessionCurrent(ctx.sessionId)
                val x1 = j.getInt("x1")
                val y1 = j.getInt("y1")
                val x2 = j.getInt("x2")
                val y2 = j.getInt("y2")
                val durationMs = j.optLong("duration_ms", 300)
                // SPEC-OVERRIDE-001 §2: coordinates are the
                // operation's own metadata (see /click).
                val ok = svc!!.runGuardedOperation(
                    ctx.expectedPackage, ctx.treeHash, ctx.generation,
                    { isSessionCurrent(ctx.sessionId) }) {
                    opGeneration = noteOperation(lifecycleGeneration,
                        "スワイプ " + x1 + "/" + y1 + " → " + x2 + "/" + y2, ctx.sessionId)
                    svc!!.swipe(x1, y1, x2, y2, durationMs)
                }
                status = 200
                sendJson(out, status, JSONObject().put("ok", ok)
                    .put("session_id", currentSessionId() ?: JSONObject.NULL))
            } else if ("/type_text" == path && "POST" == method) {
                // C-04: guarded (same shape as /click).
                // The typed text never goes into the notification.
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                val ctx = parseOperationContext(j)
                requireService(svc)
                requireSessionCurrent(ctx.sessionId)
                val text = j.getString("text")
                val ok = svc!!.runGuardedOperation(
                    ctx.expectedPackage, ctx.treeHash, ctx.generation,
                    { isSessionCurrent(ctx.sessionId) }) {
                    opGeneration = noteOperation(lifecycleGeneration, "文字を入力中", ctx.sessionId)
                    svc!!.typeText(text)
                }
                status = if (ok) 200 else 422
                sendJson(out, status, JSONObject().put("ok", ok)
                    .put("error", if (ok) JSONObject.NULL
                        else "no focused editable field — tap the field first")
                    .put("session_id", currentSessionId() ?: JSONObject.NULL))
            } else if ("/press_key" == path && "POST" == method) {
                // C-04: guarded (same shape as /click).
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                val ctx = parseOperationContext(j)
                requireService(svc)
                requireSessionCurrent(ctx.sessionId)
                val key = j.getString("key")
                // v2.1 fix (CONS-023 P2, D6): reject unknown keys
                // before the hook — the notification label must
                // never contain an unvalidated value.
                if (!isKnownPressKey(key)) {
                    status = 400
                    sendJson(out, status, JSONObject().put("ok", false)
                        .put("error", "unknown key: " + key))
                    return status
                }
                // v2.1: the operation is accepted — show "操作中"
                // with the key name (fixed enum, safe).
                val ok = svc!!.runGuardedOperation(
                    ctx.expectedPackage, ctx.treeHash, ctx.generation,
                    { isSessionCurrent(ctx.sessionId) }) {
                    opGeneration = noteOperation(lifecycleGeneration, "キー操作中（" + key + "）", ctx.sessionId)
                    svc!!.pressKey(key)
                }
                status = if (ok) 200 else 422
                sendJson(out, status, JSONObject().put("ok", ok)
                    .put("error", if (ok) JSONObject.NULL else "unknown key")
                    .put("session_id", currentSessionId() ?: JSONObject.NULL))
            } else if ("/notify" == path && "POST" == method) {
                // v1.3: the VM posts this after pushing a new APK so the
                // phone tells the user an update is waiting. No polling,
                // no new server, no new dependency. Tapping the
                // notification opens Downloads; the user taps the APK
                // file once to install (Android requires that tap).
                val j = JSONObject(String(body, StandardCharsets.UTF_8))
                val title = j.optString("title", "Muse Bridge の更新")
                val text = j.optString("text", "")
                val posted = postUpdateNotification(title, text)
                status = if (posted) 200 else 422
                sendJson(out, status, JSONObject().put("ok", posted)
                    .put("error", if (posted) JSONObject.NULL
                        else "notifications not permitted — grant notification permission in the Muse Bridge app"))
            } else if ("/operation_end" == path && "POST" == method) {
                // v2.1: end the current operation session (the VM's
                // explicit end signal). No accessibility service
                // needed — it must work in MINIMAL. An empty body
                // or {} is accepted; a non-empty body must be
                // valid JSON (fields are ignored).
                if (body.isNotEmpty()) {
                    JSONObject(String(body, StandardCharsets.UTF_8))
                }
                val wasActive = endOperation(lifecycleGeneration)
                BridgeLog.log("operation_end", if (wasActive) "ended" else "idle")
                status = 200
                sendJson(out, status, JSONObject().put("ok", true)
                    .put("was_active", wasActive))
            } else {
                status = 404
                sendJson(out, status, JSONObject().put("ok", false).put("error", "not found"))
            }
        } catch (e: HeaderTooLarge) {
            status = 431
            try {
                sendJson(conn.getOutputStream(), status, errorJson("header too large"))
            } catch (ignored: Exception) {
            }
        } catch (e: java.net.SocketTimeoutException) {
            status = 408
            try {
                sendJson(conn.getOutputStream(), status, errorJson("request timeout"))
            } catch (ignored: Exception) {
            }
        } catch (e: org.json.JSONException) {
            // A-5 fix: carry the specific message (unknown field, too
            // many actions, unsupported version) instead of a fixed
            // "invalid JSON" — a genuine parse failure still has the
            // parser's own message in e.getMessage().
            status = 400
            try {
                val msg = e.message
                sendJson(conn.getOutputStream(), status, errorJson(
                    if (msg.isNullOrEmpty()) "invalid JSON" else msg))
            } catch (ignored: Exception) {
            }
        } catch (e: BridgeAccessibilityService.OperationGuardException) {
            // C-04: a guard refusal — nothing was executed. 409 for a
            // confirmed mismatch, 503 when verification was
            // unavailable. The error id is the machine-readable
            // contract; foreground_mismatch also names both packages.
            status = if (e.errorId == "foreground_unverifiable" ||
                e.errorId == "snapshot_unavailable"
            ) 503 else 409
            try {
                val guardBody = JSONObject().put("ok", false)
                    .put("error", e.errorId)
                    .put("message", e.message)
                if (e.expectedPackage != null) {
                    guardBody.put("expected_package", e.expectedPackage)
                }
                if (e.currentPackage != null) {
                    guardBody.put("current_package", e.currentPackage)
                }
                sendJson(conn.getOutputStream(), status, guardBody)
            } catch (ignored: Exception) {
            }
        } catch (e: ServiceUnavailable) {
            // P0-1 (v2.0): the structured "a11y off" body — one
            // edit covers all requireService endpoints.
            // CONS-022 P2: the detail names the endpoint, like
            // the three inline checks do — a caller can tell
            // WHICH endpoint was unavailable. loggablePath()
            // keeps the P1-03 invariant (a raw request target
            // never leaves the server); the catch can only fire
            // for known requireService endpoints, so the name
            // is always real here.
            status = 503
            try {
                sendJson(conn.getOutputStream(), status,
                    unavailableJson("accessibility service is not enabled; " +
                        loggablePath(path) + " needs FULL_ACCESS"))
            } catch (ignored: Exception) {
            }
        } catch (e: BridgeAccessibilityService.ServiceTimeoutException) {
            // F1 (M2-01, D1案A): a main-thread timeout is NOT proof of
            // non-execution — the main-thread task may still run and
            // dispatch after this response. The 503 keeps the status,
            // but the body follows the guard-refusal convention: the
            // machine-readable id in "error", the human message in
            // "message", and "dispatched": "unknown" so the client
            // can tell it apart from a guard refusal (where nothing
            // was dispatched). SPEC-FOREGROUND-GUARD-001 L186 ("all
            // 409/503/400 mean the operation did not run") is amended
            // by the F1 decision record to carve out timeout 503s.
            // The client reads body["error"] as error_id, and
            // "main_thread_timeout" is not in its
            // _BATCH_NOT_EXECUTED_ERROR_IDS, so a timeout 503 is
            // recorded outcome_unknown, never not-executed.
            status = 503
            try {
                sendJson(conn.getOutputStream(), status, JSONObject()
                    .put("ok", false)
                    .put("error", "main_thread_timeout")
                    .put("message", e.message
                        ?: "accessibility work timed out on the main thread")
                    .put("dispatched", "unknown"))
            } catch (ignored: Exception) {
            }
        } catch (e: Exception) {
            status = 500
            try {
                sendJson(conn.getOutputStream(), status, JSONObject()
                    .put("ok", false).put("error", e.message.toString()))
            } catch (ignored: Exception) {
            }
        } finally {
            // v2.1: the operation this request started (if any) has
            // finished — release its in-flight slot so the auto-end
            // timer can fire. Runs on every exit path, including
            // the early returns above. A generation of -1 means
            // this request noted no operation.
            if (opGeneration >= 0) {
                operationFinished(opGeneration)
            }
            // P1-03 fix round 2 (CONS-033): never log the raw request
            // target — loggablePath() returns the endpoint name for
            // known endpoints and "UNKNOWN" for anything else, so an
            // arbitrary path can never land in Logcat.
            Log.i(TAG, method + " " + loggablePath(path) + " -> " + status)
            try {
                conn.close()
            } catch (ignored: Exception) {
            }
        }
        return status
    }

    /**
     * Posts an update notification (v1.3). Returns false when
     * notifications are not permitted, so the caller can tell the
     * user to grant the permission instead of failing silently.
     */
    private fun postUpdateNotification(title: String, text: String): Boolean {
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
        if (nm == null || !nm.areNotificationsEnabled()) {
            return false
        }
        val channel = NotificationChannel(
            UPDATE_CHANNEL_ID, "Muse Bridge の更新",
            NotificationManager.IMPORTANCE_DEFAULT)
        channel.description = "新しいバージョンのAPKが届いたときのお知らせ。"
        nm.createNotificationChannel(channel)

        val downloads = Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)
        downloads.flags = Intent.FLAG_ACTIVITY_NEW_TASK
        val tap = PendingIntent.getActivity(
            context, 0, downloads,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(context, UPDATE_CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentIntent(tap)
            .setAutoCancel(true)
            .build()
        nm.notify(UPDATE_NOTIFICATION_ID, notification)
        return true
    }

    // ------------------------------------------------------------------
    // v2.1: operation notifications ("操作中" / "操作終了").
    // The server posts them while the VM operates the phone screen.
    // The notification is an observer, never a gate: when posting
    // fails (permission revoked) the operation still runs and the
    // miss is only logged. See SPEC-OPERATION-NOTIFY.md.
    // ------------------------------------------------------------------

    /** Creates the operation timer if there is none (constructor
     * and start(); a second start after STOPPED needs a fresh one,
     * mirroring the worker pool). */
    private fun ensureOperationScheduler() {
        if (operationScheduler == null || operationScheduler!!.isShutdown) {
            operationScheduler = Executors.newSingleThreadScheduledExecutor(
                ThreadFactory { r ->
                    val t = Thread(r, "muse-bridge-op-timer")
                    t.isDaemon = true
                    t
                })
        }
    }

    /** v2.1 fix (CONS-023 P2, D6): the keys the service accepts.
     * Canonical set is BridgeAccessibilityService.pressKey()
     * (case-insensitive chain). Checked before the /press_key
     * notification hook, so an unknown key can never reach a
     * notification label. Fail-closed like KNOWN_NODE_ACTIONS. */
    private fun isKnownPressKey(key: String): Boolean {
        return "HOME".equals(key, ignoreCase = true) ||
            "BACK".equals(key, ignoreCase = true) ||
            "RECENTS".equals(key, ignoreCase = true) ||
            "POWER".equals(key, ignoreCase = true) ||
            "DPAD_UP".equals(key, ignoreCase = true) ||
            "DPAD_DOWN".equals(key, ignoreCase = true) ||
            "DPAD_LEFT".equals(key, ignoreCase = true) ||
            "DPAD_RIGHT".equals(key, ignoreCase = true) ||
            "DPAD_CENTER".equals(key, ignoreCase = true)
    }

    // ------------------------------------------------------------------
    // C-04: operation session (SPEC-FOREGROUND-GUARD-001 §2.4).
    // The session id (UUID v4) rides on the existing operation
    // session: issued when noteOperation() starts a session,
    // cleared when the session ends (endOperationLocked() /
    // stop()). The VM reads it from operation responses and
    // includes it in every guarded request.
    // ------------------------------------------------------------------

    /** C-04: the operation context parsed from a request body. */
    private class OperationContext(
        val expectedPackage: String,
        val sessionId: String?,
        val treeHash: Int?,
        val generation: Int?
    )

    /**
     * C-04 (§2.1): parses the operation_context object.
     * expected_package is required (400 when missing or empty);
     * session_id is the server-issued UUID (absent means
     * bootstrap — the server creates or reuses the session and
     * returns its id in the response); tree_hash and generation
     * are optional and, when present, must be numbers
     * (generation must be >= 0, else 400). Unknown fields are
     * rejected. Throws JSONException → the handle() catch turns
     * it into a 400 with the message.
     */
    private fun parseOperationContext(j: JSONObject): OperationContext {
        if (!j.has("operation_context")) {
            throw org.json.JSONException("missing operation_context")
        }
        val ctx = j.getJSONObject("operation_context")
        rejectUnknownFields(ctx, "expected_package", "session_id", "tree_hash", "generation")
        if (!ctx.has("expected_package")) {
            throw org.json.JSONException("missing expected_package in operation_context")
        }
        val expectedPackage = ctx.getString("expected_package")
        if (expectedPackage.isEmpty()) {
            throw org.json.JSONException("expected_package must not be empty")
        }
        val sessionId = if (ctx.has("session_id") && !ctx.isNull("session_id")) ctx.getString("session_id") else null
        val treeHash = if (ctx.has("tree_hash") && !ctx.isNull("tree_hash")) ctx.getInt("tree_hash") else null
        var generation: Int? = null
        if (ctx.has("generation") && !ctx.isNull("generation")) {
            val g = ctx.getInt("generation")
            if (g < 0) {
                throw org.json.JSONException("generation must not be negative")
            }
            generation = g
        }
        return OperationContext(expectedPackage, sessionId, treeHash, generation)
    }

    /**
     * C-04 (§2.5): the session id for a foreground-changing
     * operation (/launch_app, /open). Only session_id is read —
     * expected_package does not apply (the launch itself changes
     * the foreground); the other context fields are accepted and
     * ignored when present. Absent means bootstrap; a supplied
     * id is validated by requireSessionCurrent before any side
     * effect.
     */
    private fun parseLaunchSession(j: JSONObject): String? {
        if (!j.has("operation_context")) return null
        val ctx = j.getJSONObject("operation_context")
        rejectUnknownFields(ctx, "expected_package", "session_id", "tree_hash", "generation")
        return if (ctx.has("session_id") && !ctx.isNull("session_id")) ctx.getString("session_id") else null
    }

    /** C-04 (§2.4): the id of the active operation session, or
     * null when no session is active. Guarded by operationLock. */
    private fun currentSessionId(): String? {
        synchronized(operationLock) {
            return operationSessionId
        }
    }

    /** C-04: true when the supplied id is the active session's —
     * or when none was supplied (bootstrap: any session state is
     * fine, noteOperation() will create or reuse the session). */
    private fun isSessionCurrent(suppliedId: String?): Boolean {
        if (suppliedId == null) return true
        synchronized(operationLock) {
            return operationSessionId == suppliedId
        }
    }

    /** C-04: worker-side session pre-check — fail fast with 409
     * session_mismatch before any side effect (no notification,
     * no timer refresh). A null id bootstraps and always passes
     * here; the in-task predicate re-checks at dispatch time. */
    private fun requireSessionCurrent(suppliedId: String?) {
        if (suppliedId != null && !isSessionCurrent(suppliedId)) {
            throw BridgeAccessibilityService.OperationGuardException("session_mismatch",
                "the operation session $suppliedId is not active")
        }
    }

    /** Records the start of an accepted screen operation. Posts or
     * updates the ongoing "操作中" notification, refreshes the
     * last-activity timestamp, and (re)schedules the auto-end
     * timer. Returns the generation this operation belongs to —
     * the caller records it for the finally block — or -1 when
     * the server is stopping/stopped and no session was started
     * (CONS-023 P1: a worker that already passed the accept loop
     * must not resurrect the notification after stop() cleared
     * it; STARTING is allowed, the server is coming up and the
     * scheduler already exists). A new session bumps the
     * generation first, so an in-flight request from the previous
     * session is recognised as stale when its finally runs. When
     * notifications are disabled the tracker state is still
     * maintained and the miss is logged once (throttled). The
     * lifecycleGeneration argument is the server lifecycle
     * captured when this request was accepted; a mismatch with
     * the current lifecycle means the request outlived a
     * stop/restart and is refused without touching any state
     * (CONS-024 P1-NEW) — this also covers the STARTING window,
     * where the state check alone would pass. CONS-167 P1-01:
     * expectedSessionId is the session id the caller already
     * validated (requireSessionCurrent on the worker, checkGuard
     * on the main thread), re-validated here in the same critical
     * section that starts or refreshes the session — validation
     * and operation start are atomic under operationLock, so the
     * 10 s auto-end timer (which can only end a session with
     * inFlight == 0) cannot slip in between validation and start
     * and silently move the operation into a fresh session. A null
     * id bootstraps (any session state is fine); a supplied id
     * that is no longer current throws session_mismatch and
     * nothing is dispatched. */
    private fun noteOperation(
        lifecycleGeneration: Long,
        label: String,
        expectedSessionId: String? = null
    ): Long {
        synchronized(operationLock) {
            // v2.1 fix (CONS-024 P1-NEW): a request accepted in
            // an old lifecycle must not start a session in the
            // new one — refuse before touching any tracker state.
            if (lifecycleGeneration != serverLifecycleGeneration) {
                return -1
            }
            if (state != ServerState.RUNNING && state != ServerState.STARTING &&
                state != ServerState.DEGRADED
            ) {
                return -1
            }
            // CONS-167 P1-01: validate-then-start, atomically. The
            // check runs before the session is created or reused,
            // under the same operationLock acquisition that bumps
            // operationInFlight below — once this returns, the
            // auto-end timer cannot end the session until the
            // operation finishes (it only ends a session with
            // inFlight == 0).
            if (expectedSessionId != null && expectedSessionId != operationSessionId) {
                throw BridgeAccessibilityService.OperationGuardException("session_mismatch",
                    "the operation session $expectedSessionId is not active")
            }
            if (!operationActive) {
                operationGeneration++
                operationActive = true
                // C-04: a new session gets a fresh server-issued
                // id (UUID v4). An ended session's id is never
                // reused — the next session issues a new one.
                operationSessionId = UUID.randomUUID().toString()
                // SPEC-OVERRIDE-001 §3.3 step 1: the first post
                // of a new session is never throttled.
                hasPostedInSession = false
                // A stale "操作終了" from the previous session must
                // not sit next to the new "操作中".
                val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
                nm?.cancel(OPERATION_END_NOTIFICATION_ID)
            }
            operationLastActivityMs = SystemClock.elapsedRealtime()
            operationInFlight++
            // SPEC-OVERRIDE-001 §2/§3.3: the content text carries
            // "Override - " plus the action label; /click and
            // /swipe build their coordinate labels at the call
            // sites. Throttle + deferred post live in
            // postOperationRunningThrottledLocked.
            if (!postOperationRunningThrottledLocked("Override - " + label)) {
                BridgeLog.logThrottled("op-notify-disabled", "op-notify-disabled",
                    "operation notifications disabled")
            }
            scheduleAutoEndLocked()
            return operationGeneration
        }
    }

    /** Releases one in-flight slot (called from handle()'s finally
     * with the generation noteOperation() returned). A generation
     * that no longer matches the current one belongs to an ended
     * session and is ignored (CONS-023 P1) — it must not touch
     * the new session's counter or its timer. When the last
     * operation of the current session finishes, the auto-end
     * timer is re-anchored at the finish time, so "操作終了"
     * appears ~10 s after the last finish instead of up to ~10 s
     * late (CONS-023 P1). */
    private fun operationFinished(generation: Long) {
        synchronized(operationLock) {
            if (generation != operationGeneration) {
                return
            }
            if (operationInFlight > 0) {
                operationInFlight--
            }
            if (operationInFlight == 0 && operationActive) {
                operationLastActivityMs = SystemClock.elapsedRealtime()
                scheduleAutoEndLocked()
            }
        }
    }

    /** Explicit end of the current operation session (POST
     * /operation_end). Returns true when a session was active
     * (and "操作終了" was posted), false when idle. */
    private fun endOperation(lifecycleGeneration: Long): Boolean {
        synchronized(operationLock) {
            // v2.1 fix (CONS-024 P1-NEW): same lifecycle rule as
            // noteOperation() — a stale /operation_end accepted
            // before stop() must not end a session that began
            // after the restart.
            if (lifecycleGeneration != serverLifecycleGeneration) {
                return false
            }
            return endOperationLocked()
        }
    }

    /** Lock-held half of endOperation(); also used by the auto-end
     * task. Caller must hold operationLock. */
    private fun endOperationLocked(): Boolean {
        if (!operationActive) {
            return false
        }
        operationActive = false
        // C-04: the session id dies with the session.
        operationSessionId = null
        // v2.1 fix (CONS-023 P1): in-flight requests of the ended
        // session are orphaned — their finally calls are stale
        // (generation mismatch) and ignored. Reset the counter
        // here so it cannot leak into the next session.
        operationInFlight = 0
        // SPEC-OVERRIDE-001 §3.3 step 5: discard any pending
        // label; no deferred post after the session ends.
        invalidatePendingPostLocked()
        operationAutoEndFuture?.let {
            it.cancel(false)
            operationAutoEndFuture = null
        }
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
        nm?.cancel(OPERATION_NOTIFICATION_ID)
        postOperationEnded()
        return true
    }

    /** (Re)schedules the auto-end task for lastActivity + timeout.
     * Caller must hold operationLock. A delay already in the past
     * (a long operation started more than the timeout ago) falls
     * back to one full timeout — the task re-checks the in-flight
     * count when it fires, so it cannot end a session mid-run. */
    private fun scheduleAutoEndLocked() {
        operationAutoEndFuture?.let {
            it.cancel(false)
            operationAutoEndFuture = null
        }
        val scheduler = operationScheduler
        if (scheduler == null || scheduler.isShutdown) {
            return
        }
        var delay = operationLastActivityMs + OPERATION_AUTO_END_TIMEOUT_MS -
            SystemClock.elapsedRealtime()
        if (delay < 0) {
            delay = OPERATION_AUTO_END_TIMEOUT_MS
        }
        try {
            operationAutoEndFuture = scheduler.schedule(
                operationAutoEndTask, delay, TimeUnit.MILLISECONDS)
        } catch (ignored: RejectedExecutionException) {
            // Scheduler shut down concurrently with stop(); the
            // session simply has no auto-end — stop() clears the
            // notification anyway.
        }
    }

    /** Posts or updates the ongoing "操作中" notification.
     * Returns false when notifications are not permitted. */
    private fun postOperationRunning(label: String): Boolean {
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
        if (nm == null || !nm.areNotificationsEnabled()) {
            return false
        }
        ensureOperationChannel(nm)
        val notification = Notification.Builder(context, OPERATION_CHANNEL_ID)
            .setContentTitle("操作中")
            .setContentText(label)
            .setSmallIcon(R.drawable.ic_override)
            .setContentIntent(operationTapIntent(OPERATION_NOTIFICATION_ID))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
        nm.notify(OPERATION_NOTIFICATION_ID, notification)
        return true
    }

    // SPEC-OVERRIDE-001 §3.3: throttled post of the ongoing
    // "操作中" notification. Caller must hold operationLock.
    // Returns true when the label was posted immediately or
    // deferred (it will be shown); false only when the immediate
    // post was attempted and notifications are disabled. A
    // throttled skip is not a failure: no false, no log.
    private fun postOperationRunningThrottledLocked(fullLabel: String): Boolean {
        val now = SystemClock.elapsedRealtime()
        if (!hasPostedInSession) {
            // Step 1: the first post of a session is never
            // throttled.
            hasPostedInSession = true
            operationLastNotifyMs = now
            return postOperationRunning(fullLabel)
        }
        if (now - operationLastNotifyMs >= OPERATION_NOTIFY_THROTTLE_MS) {
            // Step 2: immediate post; cancel any pending deferred
            // reservation and fully invalidate it (CONS-081 P1-01:
            // a stale pendingPostFuture would block the next
            // reservation).
            invalidatePendingPostLocked()
            operationLastNotifyMs = now
            return postOperationRunning(fullLabel)
        }
        // Step 3: inside the throttle window — hold the latest
        // label; schedule a deferred post (or update the label
        // only when one is already scheduled).
        pendingLabel = fullLabel
        pendingGeneration = operationGeneration
        if (pendingPostFuture == null) {
            pendingToken = ++pendingTokenCounter
            val capturedGeneration = operationGeneration
            val capturedToken = pendingToken
            var delay = operationLastNotifyMs + OPERATION_NOTIFY_THROTTLE_MS - now
            if (delay < 0) {
                delay = 0
            }
            try {
                val scheduler = operationScheduler
                if (scheduler == null) {
                    pendingPostFuture = null
                    pendingLabel = null
                    pendingToken = -1
                    pendingGeneration = -1
                } else {
                    pendingPostFuture = scheduler.schedule(
                        Runnable { runDeferredPost(capturedGeneration, capturedToken) },
                        delay, TimeUnit.MILLISECONDS)
                }
            } catch (ignored: RejectedExecutionException) {
                // Scheduler shut down concurrently with stop();
                // the label simply stays unposted — the operation
                // itself is unaffected.
                pendingPostFuture = null
                pendingLabel = null
                pendingToken = -1
                pendingGeneration = -1
            }
        }
        return true
    }

    // SPEC-OVERRIDE-001 §3.3 step 4: the deferred post. Runs on
    // the scheduler thread; re-checks session-active + generation
    // + token before posting. An old task must never touch a newer
    // reservation's state (CONS-080 P1-02, CONS-081 P1-01).
    private fun runDeferredPost(capturedGeneration: Long, capturedToken: Long) {
        synchronized(operationLock) {
            if (!operationActive ||
                capturedGeneration != operationGeneration ||
                capturedToken != pendingToken ||
                pendingLabel == null
            ) {
                // Stale or cancelled reservation: do nothing, and
                // touch none of the pending fields.
                return
            }
            val labelToPost = pendingLabel!!
            if (postOperationRunning(labelToPost)) {
                operationLastNotifyMs = SystemClock.elapsedRealtime()
            } else {
                BridgeLog.logThrottled("op-notify-disabled", "op-notify-disabled",
                    "operation notifications disabled")
            }
            pendingLabel = null
            pendingPostFuture = null
            pendingToken = -1
            pendingGeneration = -1
        }
    }

    // SPEC-OVERRIDE-001 §3.3: fully invalidate a pending deferred
    // reservation (cancel + clear all four fields). Caller must
    // hold operationLock.
    private fun invalidatePendingPostLocked() {
        pendingPostFuture?.let {
            it.cancel(false)
            pendingPostFuture = null
        }
        pendingLabel = null
        pendingToken = -1
        pendingGeneration = -1
    }

    /** Posts the one-shot "操作終了" notification. It stays in
     * the shade until swiped (setAutoCancel, no auto-dismiss
     * timer); the fixed ID is reused, so at most one ever sits
     * there. Returns false when notifications are not permitted. */
    private fun postOperationEnded(): Boolean {
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
        if (nm == null || !nm.areNotificationsEnabled()) {
            return false
        }
        ensureOperationChannel(nm)
        val notification = Notification.Builder(context, OPERATION_CHANNEL_ID)
            .setContentTitle("操作終了")
            .setContentText("Muse の操作が終わりました")
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(operationTapIntent(OPERATION_END_NOTIFICATION_ID))
            .setAutoCancel(true)
            .build()
        nm.notify(OPERATION_END_NOTIFICATION_ID, notification)
        return true
    }

    /** Idempotent channel creation, same pattern as
     * postUpdateNotification. IMPORTANCE_DEFAULT: the first
     * "操作中" post of a session alerts once (the user asked for
     * visibility); updates are silenced by setOnlyAlertOnce. */
    private fun ensureOperationChannel(nm: NotificationManager) {
        val channel = NotificationChannel(
            OPERATION_CHANNEL_ID, "Muse Bridge の操作",
            NotificationManager.IMPORTANCE_DEFAULT)
        channel.description = "Muse の操作中・操作終了のお知らせ。"
        nm.createNotificationChannel(channel)
    }

    /** Tap target for both operation notifications: the Bridge
     * app's own MainActivity, and nothing else. It must never
     * launch another app — a notification PendingIntent can start
     * activities from the background, so using it for anything
     * else would bypass the background-activity-start
     * restriction. */
    private fun operationTapIntent(requestCode: Int): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
        intent.flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        return PendingIntent.getActivity(context, requestCode, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }

    /** Cancels both operation notifications (start/stop cleanup;
     * notifications survive process death). */
    private fun cancelOperationNotifications() {
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
            ?: return
        nm.cancel(OPERATION_NOTIFICATION_ID)
        nm.cancel(OPERATION_END_NOTIFICATION_ID)
    }

    /** Display label for a package, for the /launch_app hook.
     * Falls back to the package name when the package is not
     * installed or has no label (never throws). */
    private fun operationAppLabel(pkg: String): String {
        try {
            val label = context.packageManager.getApplicationLabel(
                context.packageManager.getApplicationInfo(pkg, 0))
            if (label != null && label.isNotEmpty()) {
                return label.toString()
            }
        } catch (ignored: Exception) {
            // Not installed (launchApp reports 422) — fall through.
        }
        return pkg
    }

    /** SPEC-PING-001: the latest phone-to-client probe result for
     * /state (null when no probe has run yet). Never fails /state. */
    private fun pingToClientJson(): JSONObject? {
        try {
            val r = PingProber.getLastResult() ?: return null
            val o = JSONObject()
                .put("target", r.target)
                .put("method", r.method)
                .put("rtt_ms", r.rttMs)
                .put("sent", r.sent)
                .put("received", r.received)
                .put("at", r.measuredAtMs)
            if (r.error != null) {
                o.put("error", r.error)
            }
            return o
        } catch (e: Exception) {
            return null
        }
    }

    /**
     * Device condition (decision 62, v1.9.6): battery, network
     * transport, and interactivity, read on the worker thread (not
     * the a11y main thread). Best effort only — unreadable values
     * fall back to -1 / false / "unknown" and never fail /state.
     * battery_pct=-1 marks "unknown": charging=false alone means
     * "not charging" only when battery_pct >= 0.
     */
    private fun deviceState(): JSONObject {
        var batteryPct = -1
        var charging = false
        try {
            val filter = IntentFilter(Intent.ACTION_BATTERY_CHANGED)
            val battery = context.registerReceiver(null, filter)
            if (battery != null) {
                val level = battery.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
                val scale = battery.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
                if (level >= 0 && scale > 0) {
                    batteryPct = Math.round(level * 100f / scale)
                    if (batteryPct < 0) {
                        batteryPct = 0
                    }
                    if (batteryPct > 100) {
                        batteryPct = 100
                    }
                }
                val plugged = battery.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0)
                charging = plugged != 0
            }
        } catch (ignored: Exception) {
            // Best effort: leave the fallbacks.
        }
        val network = networkTransport()
        var interactive = false
        try {
            val pm = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
            if (pm != null) {
                interactive = pm.isInteractive
            }
        } catch (ignored: Exception) {
            // Best effort: leave the fallback.
        }
        try {
            return JSONObject()
                .put("battery_pct", batteryPct)
                .put("charging", charging)
                .put("network", network)
                .put("interactive", interactive)
        } catch (e: org.json.JSONException) {
            // Unreachable for these put() calls, but never fail /state.
            return JSONObject()
        }
    }

    /**
     * The default network's transport type (not internet
     * reachability). When the default network is a VPN, use its
     * underlying network's transport (wifi > mobile > other):
     * the phone's Tailscale VPN is the default network, so
     * without this the field would almost always read "other".
     */
    private fun networkTransport(): String {
        try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
                ?: return "unknown"
            val active = cm.activeNetwork ?: return "none"
            val caps = cm.getNetworkCapabilities(active) ?: return "unknown"
            return transportOf(caps, cm)
        } catch (ignored: Exception) {
            return "unknown"
        }
    }

    private fun transportOf(caps: NetworkCapabilities, cm: ConnectivityManager): String {
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
            return "wifi"
        }
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) {
            return "mobile"
        }
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
            // A VPN rides on an underlying network: scan the other
            // networks for it (getUnderlyingNetworks is a system API
            // and not callable here). Prefer wifi, then mobile.
            try {
                var best = "other"
                for (n in cm.allNetworks) {
                    val uc = cm.getNetworkCapabilities(n)
                    if (uc == null ||
                        uc.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
                    ) {
                        continue
                    }
                    if (uc.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                        return "wifi"
                    }
                    if (uc.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) {
                        best = "mobile"
                    }
                }
                return best
            } catch (ignored: Exception) {
                // Best effort: fall through to "other".
            }
        }
        return "other"
    }

    private fun requireService(svc: BridgeAccessibilityService?) {
        if (svc == null) {
            throw ServiceUnavailable("accessibility service not enabled")
        }
    }

    /** C-01 (SPEC-BATCH-PERSIST-001 §2.1): the BatchRegistry for
     * the batch management endpoints. The service exists (the
     * caller already required it), but the Registry is created in
     * onServiceConnected, so a request in the narrow window before
     * it exists gets a fail-closed 503 instead of a crash. */
    private fun requireBatchRegistry(svc: BridgeAccessibilityService?): BatchRegistry {
        // CONS-168 P2-02: no `svc!!` — the Elvis throws the same
        // fail-closed ServiceUnavailable the callers already rely on
        // (they call requireService(svc) first, but that returns Unit
        // and does not smart-cast the nullable parameter).
        val service = svc ?: throw ServiceUnavailable("accessibility service not enabled")
        val registry = service.getBatchRegistry()
        if (registry == null) {
            throw BatchStoreException("batch_store_unavailable",
                "batch registry is not ready — the accessibility service is still connecting")
        }
        return registry
    }

    /** P0-3 (v2.0): the current startup mode. a11y on wins
     * (FULL_ACCESS); a11y off falls back to the persisted
     * MINIMAL / BANKING_SAFE. */
    private fun currentMode(a11yOn: Boolean): String {
        if (a11yOn) return "FULL_ACCESS"
        return MainActivity.getPersistedMode(context)
    }

    /** P0-3 (v2.0): per-module status for /health. The
     * tailscaleIps argument is the CURRENT Tailscale IPv4s
     * (P1-3, CONS-022); boundTailscaleIps is the list the HTTP
     * server actually bound to at start. Tailscale is HEALTHY
     * only when a current IP is also a bound IP — the VM
     * connects on the current IP, so a current IP the server
     * is not listening on must not be reported HEALTHY.
     * CONS-029 P1-3 fix: no current IP -> UNAVAILABLE;
     * current IP(s) exist but NONE is in the bound list ->
     * DEGRADED (not a new STALE state, to avoid a new enum
     * value); at least one current IP is bound -> HEALTHY.
     * overallStatus() treats DEGRADED the same way. */
    private fun buildModules(
        a11yOn: Boolean, listenerGranted: Boolean,
        listenerConnected: Boolean,
        tailscaleIps: List<String>,
        boundTailscaleIps: List<String>
    ): JSONObject {
        // SPEC-LOG-001 BIND-02: a DEGRADED server (loopback only) is
        // not HEALTHY, even though it is listening.
        val http = if (state == ServerState.STARTING) "RECOVERING"
            else if (state == ServerState.DEGRADED) "DEGRADED" else "HEALTHY"
        val tailscale: String
        if (tailscaleIps.isEmpty()) {
            tailscale = "UNAVAILABLE"
        } else {
            var anyBound = false
            for (ip in tailscaleIps) {
                if (boundTailscaleIps.contains(ip)) {
                    anyBound = true
                    break
                }
            }
            tailscale = if (anyBound) "HEALTHY" else "DEGRADED"
        }
        val accessibility = if (a11yOn) "ENABLED" else "DISABLED"
        val notificationAccess: String
        if (!listenerGranted) {
            notificationAccess = "NOT_GRANTED"
        } else if (!listenerConnected) {
            notificationAccess = "GRANTED_NOT_CONNECTED"
        } else {
            notificationAccess = "GRANTED"
        }
        return JSONObject()
            .put("http", http)
            .put("tailscale", tailscale)
            .put("accessibility", accessibility)
            .put("notification_access", notificationAccess)
            .put("termux_ssh", "UNKNOWN")
    }

    /** P0-3 (v2.0): what the VM can do right now. `notification`
     * is the listener READ path (/notifications); `notification_post`
     * is the /notify path (POST_NOTIFICATIONS permission).
     * P0-4: `launch_app_guaranteed` is always false — a launch
     * is best-effort, never guaranteed. `foreground_verification`
     * is true when a11y is ON (the service can verify the
     * foreground package) and false when it is OFF (UsageStats
     * verification is P1).
     * P1-4 (CONS-022, split by CONS-029): `install_apk` is
     * always true — P0 spec A5 defines it as "endpoint
     * available", and the /install_apk endpoint exists
     * regardless of the user's install setting. The Android
     * permission state goes in the NEW `install_apk_permitted`
     * key (PackageManager.canRequestPackageInstalls(), i.e.
     * the user's "Install unknown apps" setting is on for this
     * app). NOTE: canRequestPackageInstalls() == true does NOT
     * guarantee an install succeeds — the confirmation dialog,
     * Play Protect, and package checks still apply; it only
     * means an install request may be started. */
    private fun buildCapabilities(
        a11yOn: Boolean, listenerGranted: Boolean,
        listenerConnected: Boolean
    ): JSONObject {
        val notificationRead = listenerGranted && listenerConnected
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
        val notificationPost = nm != null && nm.areNotificationsEnabled()
        val canInstallApk =
            context.packageManager.canRequestPackageInstalls()
        return JSONObject()
            .put("screenshot", a11yOn)
            .put("tap", a11yOn)
            .put("swipe", a11yOn)
            .put("text_input", a11yOn)
            .put("notification", notificationRead)
            .put("notification_post", notificationPost)
            .put("http_api", true)
            .put("launch_app", true)
            .put("launch_app_guaranteed", false)
            .put("foreground_verification", a11yOn)
            .put("install_apk", true)
            .put("install_apk_permitted", canInstallApk)
            .put("repair_termux", true)
            .put("repair_termux_permitted", canRunTermuxCommands())
    }

    /** P0-3 (v2.0): the overall status for /health. CRITICAL is
     * never emitted here — a broken HTTP server returns nothing
     * and broken auth returns 401 before this handler runs, so
     * CRITICAL is a VM-side verdict (see the P0 spec).
     * CONS-029 P1-3 fix: a tailscale DEGRADED (current IP
     * exists but the server is not bound to it) degrades the
     * overall status too. */
    private fun overallStatus(modules: JSONObject): String {
        if (state == ServerState.STARTING) return "RECOVERING"
        // SPEC-LOG-001 BIND-02: a DEGRADED server (loopback only)
        // degrades the overall status too.
        if (state == ServerState.DEGRADED) return "DEGRADED"
        if ("UNAVAILABLE" == modules.optString("tailscale") ||
            "DEGRADED" == modules.optString("tailscale") ||
            "NOT_GRANTED" == modules.optString("notification_access") ||
            "GRANTED_NOT_CONNECTED" == modules.optString("notification_access")
        ) {
            return "DEGRADED"
        }
        return "HEALTHY"
    }

    /**
     * TARMUX_KICKASS step 1 — local Termux sshd diagnosis.
     * "TARMUX_KICKASS" is the working name the System Development Lead gave this
     * work: TERMUX, kicked back to life. Step 1 does NOT kick
     * anything — it only checks, from inside the phone, whether
     * Termux's sshd is listening on 127.0.0.1:8022, by opening
     * a local socket and reading its SSH banner. That separates
     * the two causes of a VM-side termux_ssh failure: if this
     * is reachable, sshd is alive and the tunnel proxy / port
     * 8022 path is the culprit; if it is not, sshd itself is
     * down and a later RUN_COMMAND repair would have something
     * to fix. Never fails /health: any error is reported in
     * the returned object. Worst case ~2 s (1 s connect +
     * 1 s banner read); a refused connection or a live sshd
     * answers in milliseconds.
     */
    private fun checkTermuxSshdLocal(): JSONObject {
        val t0 = SystemClock.elapsedRealtime()
        var reachable = false
        var banner = ""
        var error = ""
        try {
            Socket().use { sock ->
                sock.connect(InetSocketAddress("127.0.0.1", 8022), 1000)
                sock.soTimeout = 1000
                val reader = BufferedReader(
                    InputStreamReader(sock.getInputStream(),
                        StandardCharsets.UTF_8))
                val line = reader.readLine()
                if (line != null && line.startsWith("SSH-")) {
                    reachable = true
                    banner = if (line.length > 80) line.substring(0, 80) else line
                } else {
                    error = "no SSH banner" + (if (line == null) " (EOF)" else "")
                    if (line != null && line.isNotEmpty()) {
                        banner = if (line.length > 80) line.substring(0, 80) else line
                    }
                }
            }
        } catch (e: Exception) {
            val msg = e.message
            error = e.javaClass.simpleName + (if (msg == null) "" else ": " + msg)
        }
        val ms = SystemClock.elapsedRealtime() - t0
        return JSONObject()
            .put("reachable", reachable)
            .put("banner", banner)
            .put("latency_ms", ms)
            .put("error", error)
    }

    /** Decision 95 (v2.5.0, TARMUX_KICKASS step 2): whether
     * this app may send RUN_COMMAND intents to Termux. The
     * permission is declared in the manifest and must be
     * granted by the user in person; MainActivity asks for it
     * on launch. */
    private fun canRunTermuxCommands(): Boolean {
        return context.checkSelfPermission("com.termux.permission.RUN_COMMAND") ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
    }

    /** Decision 95: send ONE fixed command to Termux via
     * RunCommandService, in the background (no Termux tab
     * opens, no screen change). Only the two call sites in
     * repairTermuxSshd() use this — the path and arguments
     * are hardcoded there, never taken from a request, so
     * the endpoint cannot be turned into arbitrary shell
     * execution. startForegroundService is used because the
     * caller is a foreground service; startService is the
     * fallback if that throws. */
    private fun sendTermuxRunCommand(path: String, args: Array<String>) {
        val intent = Intent("com.termux.RUN_COMMAND")
        intent.setClassName("com.termux", "com.termux.app.RunCommandService")
        intent.putExtra("com.termux.RUN_COMMAND_PATH", path)
        intent.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", args)
        intent.putExtra("com.termux.RUN_COMMAND_WORKDIR",
            "/data/data/com.termux/files/home")
        intent.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true)
        intent.putExtra("com.termux.RUN_COMMAND_SESSION_ACTION", "0")
        try {
            context.startForegroundService(intent)
        } catch (e: Exception) {
            context.startService(intent)
        }
    }

    /** Decision 95: repair Termux's sshd, fixed steps only.
     * 1. If sshd is already reachable locally, do nothing
     *    and report ALREADY_RUNNING.
     * 2. Otherwise send termux-wake-lock, then sshd, each as
     *    a background RUN_COMMAND intent.
     * 3. Wait 2 s, re-check locally, and report the result.
     * The result is honest by construction: "ok" is true
     * exactly when sshd answers locally after the attempt.
     * Returns a repair report object. */
    private fun repairTermuxSshd(): JSONObject {
        val before = checkTermuxSshdLocal()
        if (before.optBoolean("reachable", false)) {
            return JSONObject()
                .put("ok", true)
                .put("repaired", false)
                .put("reason", "ALREADY_RUNNING")
                .put("termux_sshd_local", before)
                .put("steps", JSONArray())
        }
        sendTermuxRunCommand(
            "/data/data/com.termux/files/usr/bin/termux-wake-lock",
            emptyArray())
        sendTermuxRunCommand(
            "/data/data/com.termux/files/usr/bin/sshd",
            emptyArray())
        try {
            Thread.sleep(2000)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        }
        val after = checkTermuxSshdLocal()
        val repaired = after.optBoolean("reachable", false)
        val steps = JSONArray()
        steps.put("termux-wake-lock")
        steps.put("sshd")
        return JSONObject()
            .put("ok", repaired)
            .put("repaired", repaired)
            .put("reason", if (repaired) "REPAIRED" else "STILL_DOWN")
            .put("termux_sshd_local", after)
            .put("steps", steps)
    }

    /** P0-3 (v2.0): the bound non-loopback addresses (the 100.*
     * Tailscale addresses from bindAddresses()). 127.0.0.1 is
     * excluded. Bound-at-start: the list goes stale if Tailscale
     * assigns a new IP afterwards (re-bind is P1). P1-3
     * (CONS-022): /health reports this list separately as
     * tailscale_ips_bound, while tailscale_ips carries the
     * CURRENT addresses — so the VM can see a mismatch
     * (bound != current) instead of mistaking a stale IP for
     * the current one. */
    @Synchronized
    fun boundTailscaleIps(): List<String> {
        val out = ArrayList<String>()
        for (ss in listeners) {
            val a = ss.inetAddress
            if (a != null && !a.isLoopbackAddress && a.hostAddress != null) {
                out.add(a.hostAddress)
            }
        }
        return out
    }

    /**
     * Opens a URI with ACTION_VIEW (decision 74, v1.9.15): a web
     * URL, or an app's deep link when the URI's scheme belongs
     * to an installed app. Runs on this server's own application
     * context, so it needs no accessibility service. Throws
     * IllegalArgumentException on an empty URI,
     * ActivityNotFoundException when no app handles the URI,
     * SecurityException when Android refuses the start.
     */
    /**
     * F1 (M6 FG-01): resolve a URI to the package that would handle
     * it (ACTION_VIEW, default handler), for the §2.5 launch
     * contract on /open. Returns null when no installed app handles
     * the URI. A URI that cannot be parsed throws
     * IllegalArgumentException (the caller's 422 path, as in
     * openUri). The manifest <queries> must declare VIEW (see
     * AndroidManifest.xml) or resolveActivity() returns null for
     * every handler under package-visibility rules.
     */
    private fun resolveOpenTargetPackage(uriString: String): String? {
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(uriString))
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val info = context.packageManager.resolveActivity(
            intent, PackageManager.MATCH_DEFAULT_ONLY) ?: return null
        return info.activityInfo?.packageName
    }

    private fun openUri(uriString: String?): Boolean {
        if (uriString == null || uriString.isEmpty()) {
            throw IllegalArgumentException("uri must not be empty")
        }
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(uriString))
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
        return true
    }

    /** P0-4: build the launch result body. `foregroundConfirmed`
     * is null when verification was unavailable (a11y off). */
    private fun launchResultJson(
        pkg: String, ok: Boolean,
        status: String, reason: String?, foregroundConfirmed: Boolean?,
        verification: String
    ): JSONObject {
        val j = JSONObject()
            .put("ok", ok)
            .put("status", status)
            .put("accepted", true)
            .put("package", pkg)
            .put("launch_requested", true)
            .put("foreground_confirmed", if (foregroundConfirmed == null)
                JSONObject.NULL else foregroundConfirmed)
            .put("verification", verification)
        if (reason != null) {
            j.put("reason", reason)
        }
        return j
    }

    /** P0-4: read the foreground package, never throwing. A
     * failure to read (service timeout, no active window) is a
     * missed observation, not a launch failure — the caller keeps
     * polling and only claims `launched` on a positive match.
     * P0-4 fix (CONS-027 P1 #1): `timeoutMs` bounds how long
     * this read may wait — the caller passes the remaining
     * verification budget, so one main-thread stall cannot
     * overrun the ~3 s window. */
    private fun foregroundPackageSafe(
        svc: BridgeAccessibilityService?,
        timeoutMs: Long
    ): String? {
        if (svc == null) {
            return null
        }
        try {
            return svc.getForegroundPackage(timeoutMs)
        } catch (e: Exception) {
            BridgeLog.log("launch_fg_read_failed",
                e.javaClass.simpleName)
            return null
        }
    }

    /**
     * P0-4 (v2.0): after startActivity() was issued without
     * throwing, check whether the target package actually came
     * to the foreground. a11y ON: poll the service's foreground
     * package (getRootInActiveWindow) up to ~3 s; a11y OFF:
     * verification is unavailable (UsageStats is P1), so the
     * answer is `unconfirmed` / VERIFICATION_UNAVAILABLE. Runs
     * on the HTTP worker thread — never the service main thread
     * — and only counts observations after requestTimeMs. A match
     * when the app was already in front (wasForeground) still
     * counts as launched: the requested end state is achieved.
     * startActivity() not throwing is NEVER the success condition.
     *
     * P0-4 fix (CONS-027 P1 #1 + P2 #5): the verification
     * window is measured with SystemClock.elapsedRealtime()
     * (monotonic — immune to wall-clock corrections), and the
     * remaining budget is propagated into every foreground
     * read, so a main-thread stall cannot overrun the window:
     * each read waits at most the time left, and the whole
     * /launch_app response stays within ~3–4 s. A read that
     * times out is a missed poll (unconfirmed), never a false
     * success. The poll offsets (500/1000/2000/3000 ms) and
     * the ~3 s window are unchanged.
     */
    private fun verifyLaunchForeground(
        pkg: String,
        svc: BridgeAccessibilityService?, requestTimeMs: Long,
        wasForeground: Boolean
    ): JSONObject {
        if (svc == null) {
            // P1-03 (CONS-014): no package name in the log.
            BridgeLog.log("launch_unverified", "")
            return launchResultJson(pkg, false, "unconfirmed",
                "VERIFICATION_UNAVAILABLE", null, "none")
        }
        val deadlineMs = requestTimeMs + LAUNCH_VERIFY_MAX_MS
        for (offset in LAUNCH_VERIFY_OFFSETS_MS) {
            val wait = requestTimeMs + offset -
                SystemClock.elapsedRealtime()
            if (wait > 0) {
                try {
                    Thread.sleep(wait)
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                    break
                }
            }
            // Remaining budget bounds this read: a stalled main
            // thread cannot push the response past the deadline.
            val remainingMs = deadlineMs -
                SystemClock.elapsedRealtime()
            if (remainingMs <= 0) {
                break
            }
            if (pkg == foregroundPackageSafe(svc, remainingMs)) {
                // P1-03 (CONS-014): no package name in the log —
                // only whether it was already in front.
                BridgeLog.log("launch_confirmed", if (wasForeground)
                    "already foreground" else "")
                return launchResultJson(pkg, true, "launched",
                    null, java.lang.Boolean.TRUE, "accessibility")
            }
            if (SystemClock.elapsedRealtime() - requestTimeMs >=
                LAUNCH_VERIFY_MAX_MS
            ) {
                break
            }
        }
        // P1-03 (CONS-014): no package name in the log.
        BridgeLog.log("launch_unconfirmed", "")
        return launchResultJson(pkg, false, "unconfirmed",
            "FOREGROUND_NOT_CONFIRMED", java.lang.Boolean.FALSE, "accessibility")
    }

    /**
     * Launches an app by package name (decision 69, v1.9.12;
     * moved from BridgeAccessibilityService in P0-1, v2.0).
     * Runs on this server's own application context, so it
     * needs no accessibility service — the same reason /open
     * works with a11y off. Returns the verification baseline
     * (elapsedRealtime — monotonic, so a wall-clock correction
     * must not move the window, which is a duration) captured
     * just before startActivity(), or -1 when Android refuses
     * the start (background-activity-start restriction);
     * throws IllegalArgumentException when the package is not
     * installed or has no launcher activity. P0-4: a
     * non-negative return means only that startActivity() was
     * ISSUED without throwing — it does NOT mean the app
     * opened (the BAL restriction can swallow the start
     * silently). The caller verifies the foreground
     * separately (verifyLaunchForeground) and reports the
     * truthful result. P2-01 fix round (CONS-035): the
     * baseline is captured here, after the Intent fetch, so
     * the PackageManager lookup no longer eats into the
     * ~3 s post-launch verification window.
     */
    private fun launchApp(pkg: String?): Long {
        if (pkg == null || pkg.isEmpty()) {
            throw IllegalArgumentException("package must not be empty")
        }
        val intent = context.packageManager.getLaunchIntentForPackage(pkg)
        if (intent == null) {
            throw IllegalArgumentException(
                "package not installed or has no launcher: " + pkg)
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        try {
            // P2-01 (CONS-028, fix round after CONS-035):
            // capture the baseline just before startActivity(),
            // after the Intent fetch above.
            val requestTimeMs = SystemClock.elapsedRealtime()
            context.startActivity(intent)
            return requestTimeMs
        } catch (e: ActivityNotFoundException) {
            // P1-03 (CONS-014): no package name in the log.
            BridgeLog.log("launch_blocked", "")
            return -1
        } catch (e: SecurityException) {
            // P1-03 (CONS-014): no package name in the log.
            BridgeLog.log("launch_blocked", "")
            return -1
        }
    }

    /**
     * Install-apk job (decision 75, v1.9.16). Runs on its own
     * thread: downloads the APK when a URL was given (into the
     * app's private cache, so no storage permission is needed),
     * then hands it to PackageInstaller and commits. The
     * commit shows the system confirmation dialog; the result
     * (installed / failed) arrives on installReceiver. The
     * downloaded file is deleted after the commit. P1-03
     * (CONS-014): the log carries only the byte count — never
     * the URL, the file name, or the failure message.
     */
    private fun runInstall(url: String?, apkPath: String?) {
        installRunning = true
        var apk: File? = null
        var downloaded = false
        try {
            if (url != null) {
                installStatus = "downloading"
                installDetail = url
                // P1-03 (CONS-014): never log the URL — it may
                // carry query parameters or tokens. The VM that
                // requested the install already knows it.
                BridgeLog.log("install_download_start", "")
                var name = url.substring(url.lastIndexOf('/') + 1)
                if (name.isEmpty() || name.contains("..")) {
                    name = "app.apk"
                }
                val dir = File(context.cacheDir, "install")
                dir.mkdirs()
                apk = File(dir, name)
                val conn =
                    URL(url).openConnection() as HttpURLConnection
                conn.connectTimeout = 15000
                conn.readTimeout = 30000
                conn.connect()
                if (conn.responseCode != HttpURLConnection.HTTP_OK) {
                    throw java.io.IOException(
                        "download HTTP " + conn.responseCode)
                }
                var total = 0L
                conn.inputStream.use { input ->
                    FileOutputStream(apk).use { out ->
                        val buf = ByteArray(8192)
                        var n: Int
                        while (input.read(buf).also { n = it } != -1) {
                            out.write(buf, 0, n)
                            total += n
                        }
                    }
                }
                downloaded = true
                BridgeLog.log("install_downloaded", total.toString() + " bytes")
            } else {
                apk = File(apkPath)
            }
            installStatus = "installing"
            installDetail = apk!!.name
            val installer =
                context.packageManager.packageInstaller
            val params =
                PackageInstaller.SessionParams(
                    PackageInstaller.SessionParams.MODE_FULL_INSTALL)
            val sessionId = installer.createSession(params)
            val session = installer.openSession(sessionId)
            session.openWrite("package", 0, -1).use { out ->
                FileInputStream(apk).use { input ->
                    val buf = ByteArray(8192)
                    var n: Int
                    while (input.read(buf).also { n = it } != -1) {
                        out.write(buf, 0, n)
                    }
                    session.fsync(out)
                }
            }
            val resultIntent = Intent(INSTALL_RESULT_ACTION)
            resultIntent.setPackage(context.packageName)
            val pi = PendingIntent.getBroadcast(context, sessionId,
                resultIntent, PendingIntent.FLAG_UPDATE_CURRENT or
                    PendingIntent.FLAG_MUTABLE)
            session.commit(pi.intentSender)
            session.close()
            if (downloaded) {
                apk.delete()
            }
            installStatus = "committed"
            installDetail = "waiting for the install confirmation"
            // P1-03 (CONS-014): never log the APK file name.
            BridgeLog.log("install_committed", "")
        } catch (e: Exception) {
            installStatus = "failed"
            installDetail = if (e.message == null)
                e.javaClass.simpleName else e.message!!
            installRunning = false
            // P1-03 (CONS-014): log the exception class only —
            // the message may carry a path or other user data.
            // (installDetail keeps the message: it is the
            // /install_status API answer to the operator,
            // behind token auth, not the log file.)
            BridgeLog.log("install_failed", e.javaClass.simpleName)
        }
    }

    private class ServiceUnavailable(msg: String) : Exception(msg)

    private class HeaderTooLarge : Exception("header too large")

    private fun errorJson(message: String): JSONObject {
        return JSONObject().put("ok", false).put("error", message)
    }

    /** P0-1 (v2.0): the structured "a11y off" body. `detail`
     * names the endpoint that needs the service. The `error`
     * field is kept alongside the new fields so
     * bridge_protocol.parse_error_body keeps working (it
     * requires "ok" and "error"). */
    private fun unavailableJson(detail: String): JSONObject {
        return JSONObject()
            .put("ok", false)
            .put("status", "unavailable")
            .put("reason", "ACCESSIBILITY_DISABLED")
            .put("required_mode", "FULL_ACCESS")
            .put("detail", detail)
            .put("error", "accessibility service not enabled")
    }

    /** A-4: unknown JSON fields are rejected (400 via JSONException)
     * so the canonical payload hash is computed over exactly the
     * fields the server understands. */
    private fun rejectUnknownFields(j: JSONObject, vararg allowed: String) {
        val ok = HashSet<String>()
        for (a in allowed) {
            ok.add(a)
        }
        val keys = j.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            if (!ok.contains(key)) {
                throw org.json.JSONException("unknown field: " + key)
            }
        }
    }

    private fun remainingMs(deadline: Long): Long {
        val rem = deadline - SystemClock.elapsedRealtime()
        return if (rem < 0) 0 else rem
    }

    private fun setPhaseTimeout(conn: Socket, phaseMs: Int, deadline: Long) {
        val rem = remainingMs(deadline)
        var ms = minOf(phaseMs.toLong(), rem).toInt()
        if (ms <= 0) {
            ms = 1
        }
        conn.soTimeout = ms
    }

    private fun readHead(input: InputStream): String {
        val bos = ByteArrayOutputStream()
        var prev = -1
        var b: Int
        while (input.read().also { b = it } != -1) {
            bos.write(b)
            if (bos.size() > MAX_HEADER_BYTES) {
                throw HeaderTooLarge()
            }
            if (prev == '\r'.code && b == '\n'.code) {
                val bytes = bos.toByteArray()
                val n = bytes.size
                if (n >= 4 && bytes[n - 4] == '\r'.code.toByte() && bytes[n - 3] == '\n'.code.toByte() &&
                    bytes[n - 2] == '\r'.code.toByte() && bytes[n - 1] == '\n'.code.toByte()
                ) {
                    break
                }
            }
            prev = b
        }
        return bos.toString("ISO-8859-1")
    }

    private fun sendJson(out: OutputStream, status: Int, json: JSONObject) {
        val body = json.toString().toByteArray(StandardCharsets.UTF_8)
        sendBytes(out, status, "application/json", body)
    }

    private fun sendBytes(out: OutputStream, status: Int, contentType: String, body: ByteArray) {
        sendBytes(out, status, contentType, body, "")
    }

    private fun sendBytes(
        out: OutputStream, status: Int, contentType: String, body: ByteArray,
        extraHeaders: String
    ) {
        val head = "HTTP/1.1 " + status + " " + reason(status) + "\r\n" +
            "Content-Type: " + contentType + "\r\n" +
            "Content-Length: " + body.size + "\r\n" +
            extraHeaders +
            "Connection: close\r\n\r\n"
        out.write(head.toByteArray(StandardCharsets.US_ASCII))
        out.write(body)
        out.flush()
    }

    private fun queryParam(query: String?, name: String): String? {
        if (query == null || query.isEmpty()) {
            return null
        }
        for (pair in query.split("&")) {
            val eq = pair.indexOf('=')
            if (eq > 0 && pair.substring(0, eq) == name) {
                return pair.substring(eq + 1)
            }
        }
        return null
    }

    private fun parseFloat(value: String?, fallback: Float): Float {
        if (value == null) {
            return fallback
        }
        try {
            return value.toFloat()
        } catch (ignored: NumberFormatException) {
            return fallback
        }
    }

    private fun parseInt(value: String?, fallback: Int): Int {
        if (value == null) {
            return fallback
        }
        try {
            return value.toInt()
        } catch (ignored: NumberFormatException) {
            return fallback
        }
    }

    private fun parseLong(value: String?, fallback: Long): Long {
        if (value == null) {
            return fallback
        }
        try {
            return value.toLong()
        } catch (ignored: NumberFormatException) {
            return fallback
        }
    }

    private fun reason(status: Int): String {
        return when (status) {
            200 -> "OK"
            400 -> "Bad Request"
            401 -> "Unauthorized"
            404 -> "Not Found"
            408 -> "Request Timeout"
            409 -> "Conflict"
            410 -> "Gone"
            413 -> "Content Too Large"
            422 -> "Unprocessable Entity"
            431 -> "Request Header Fields Too Large"
            500 -> "Internal Server Error"
            503 -> "Service Unavailable"
            else -> ""
        }
    }
}
