package dev.muse.bridge;

import android.app.DownloadManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ActivityNotFoundException;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageInstaller;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Build;
import android.os.PowerManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/**
 * Minimal raw-socket HTTP server for the bridge. No external dependencies.
 *
 * Binds to the Tailscale IPv4 address when present, plus 127.0.0.1.
 * Every request needs the runtime token in the X-Bridge-Token header.
 * Only the method, path, and status are logged — never the token or body.
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
 *   POST /click           {"x":..,"y":..}
 *   POST /click_node      {"node_id":"..","tree_hash":..} (v1.5)
 *   POST /type_node       {"node_id":"..","tree_hash":..,"text":".."} (v1.5)
 *   POST /batch           {"batch_id":"..","tree_hash":..,"actions":[..]} (v1.5)
 *   POST /swipe           {"x1":..,"y1":..,"x2":..,"y2":..,"duration_ms":..}
 *   POST /type_text       {"text":".."}
 *   POST /press_key       {"key":"HOME"|"BACK"|"RECENTS"|"POWER"|
 *                         "DPAD_UP"|"DPAD_DOWN"|"DPAD_LEFT"|"DPAD_RIGHT"|
 *                         "DPAD_CENTER"} (v1.9.9: DPAD keys)
 *   POST /scroll_node     {"node_id":"..","tree_hash":..,
 *                         "direction":"forward"|"backward"} (v1.9.10)
 *   POST /node_action     {"node_id":"..","tree_hash":..,"action":"..",
 *                         "args":{..}} (v1.9.11: any one-shot node action)
 *   POST /launch_app      {"package":"com.example.app"} (v1.9.12)
 *   POST /open            {"uri":"https://example.com"} (v1.9.15)
 *   POST /install_apk     {"url":"https://...apk"} or {"path":"/...apk"} (v1.9.16)
 *   GET  /install_status  (v1.9.16)
 *   GET  /clipboard       (v1.9.17)
 *   POST /clipboard       {"text":"..."} (v1.9.17)
 *   POST /notification_action {"key":"...","action":"open"|"reply","text":"..."} (v1.9.18)
 *   POST /notify          {"title":"..","text":".."} -> posts a notification
 *                         (v1.3; tap opens Downloads so the user can tap
 *                         the pushed APK to install it)
 *   POST /operation_end   {} -> {"ok":true,"was_active":..} (v2.1: ends
 *                         the current operation session; posts
 *                         "操作終了" when a session was active)
 */
public class HttpBridgeServer {

    private static final String TAG = "MuseBridge";
    public static final int PORT = 8080;
    private static final String UPDATE_CHANNEL_ID = "muse-bridge-updates";
    private static final int UPDATE_NOTIFICATION_ID = 2;

    // v2.1: operation notifications ("操作中" / "操作終了"). The
    // bridge posts them while the VM operates the phone screen, so
    // the user can see at a glance that Muse is operating the
    // phone and when it finished. Dedicated channel (not the
    // update channel); ID 3 is the ongoing "操作中", ID 4 the
    // one-shot "操作終了". See SPEC-OPERATION-NOTIFY.md.
    private static final String OPERATION_CHANNEL_ID = "muse-bridge-operation";
    private static final int OPERATION_NOTIFICATION_ID = 3;
    private static final int OPERATION_END_NOTIFICATION_ID = 4;
    private static final long OPERATION_AUTO_END_TIMEOUT_MS = 10_000;

    // v2.1 fix (CONS-023 P2, D6): the one-shot node actions the
    // service accepts. Canonical list is
    // BridgeAccessibilityService.actionIdForName(); this copy is
    // checked before the /node_action notification hook, so an
    // unknown action name can never reach a notification label.
    // Case-sensitive, like the service's switch. Fail-closed: if
    // the service gains a new action, the server 400s it until
    // this list is updated.
    private static final Set<String> KNOWN_NODE_ACTIONS = new HashSet<>(Arrays.asList(
            "click", "long_click", "focus", "clear_focus",
            "accessibility_focus", "clear_accessibility_focus",
            "select", "clear_selection", "set_selection",
            "copy", "cut", "paste", "expand", "collapse", "dismiss",
            "show_on_screen", "context_click", "ime_enter",
            "scroll_forward", "scroll_backward",
            "scroll_up", "scroll_down", "scroll_left", "scroll_right",
            "page_up", "page_down", "page_left", "page_right",
            "press_and_hold", "set_progress", "scroll_to_position",
            "set_text"));

    public interface TokenProvider {
        String currentToken();
    }

    // A-1 (v1.9): input-validation limits and per-phase deadlines.
    // These are initial design values (Codex Phase A); tune them from
    // measured load, not from first principles.
    private static final int MAX_HEADER_BYTES = 8 * 1024;
    private static final int MAX_BODY_BYTES = 256 * 1024;
    private static final int HEADER_TIMEOUT_MS = 5000;
    private static final int BODY_TIMEOUT_MS = 10000;
    private static final long TOTAL_TIMEOUT_MS = 45000;

    // A-2 (v1.9): executor separation. Accept threads never run HTTP
    // handling; the worker pool is bounded so a flood cannot queue
    // forever; at most MAX_CONNECTIONS sockets are held at once.
    // These are the Codex Phase A initial values (decision 47) —
    // tune them from measured load, not from first principles.
    private static final int HTTP_WORKERS = 4;
    private static final int HTTP_QUEUE = 8;
    private static final int MAX_CONNECTIONS = 16;

    /** Server lifecycle (A-2). start() only reports RUNNING when at
     * least one listener socket is bound; a bind failure leaves
     * FAILED instead of pretending to run. */
    public enum ServerState { NEW, STARTING, RUNNING, FAILED, STOPPING, STOPPED }

    private final int port;
    private final TokenProvider tokens;
    private final Context context;
    private final Semaphore connectionSlots = new Semaphore(MAX_CONNECTIONS);
    private ThreadPoolExecutor workers;
    private final List<ServerSocket> listeners = new ArrayList<>();
    private final List<Thread> acceptThreads = new ArrayList<>();
    private volatile ServerState state = ServerState.NEW;

    // Decision 75 (v1.9.16): install-apk job state. One job at a
    // time; the status is visible via GET /install_status and the
    // detail goes to BridgeLog. The PackageInstaller result
    // arrives on this receiver (dynamic registration — the
    // foreground service outlives the user's tap on the
    // confirmation dialog).
    public static final String INSTALL_RESULT_ACTION =
            "dev.muse.bridge.INSTALL_RESULT";
    private volatile String installStatus = "idle";
    private volatile String installDetail = "";
    private volatile boolean installRunning = false;
    private final BroadcastReceiver installReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context ctx, Intent intent) {
            int st = intent.getIntExtra(PackageInstaller.EXTRA_STATUS,
                    PackageInstaller.STATUS_FAILURE);
            String msg = intent.getStringExtra(
                    PackageInstaller.EXTRA_STATUS_MESSAGE);
            // Decision 79 (v1.9.20): also read the legacy
            // INSTALL_FAILED_* code — the status alone came back
            // as -1 with no message on the first Termux install,
            // which says nothing about the cause. A negative
            // legacy code is the real reason (e.g. -25 version
            // downgrade, -7 update incompatible / signature).
            // EXTRA_LEGACY_STATUS is a hidden API, so use its
            // literal key ("android.content.pm.extra.LEGACY_STATUS").
            int legacy = intent.getIntExtra(
                    "android.content.pm.extra.LEGACY_STATUS", 0);
            String pkg = intent.getStringExtra(
                    PackageInstaller.EXTRA_PACKAGE_NAME);
            String detail = "status=" + st + " legacy=" + legacy
                    + (pkg == null ? "" : " pkg=" + pkg)
                    + (msg == null ? "" : " " + msg);
            if (st == PackageInstaller.STATUS_SUCCESS) {
                installStatus = "installed";
                installDetail = detail;
                BridgeLog.log("install_result", "success");
                installRunning = false;
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
                Intent confirm = intent.getParcelableExtra(
                        Intent.EXTRA_INTENT);
                if (confirm != null) {
                    confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    ctx.startActivity(confirm);
                    installStatus = "pending_user_action";
                    installDetail = "waiting for the install confirmation";
                    BridgeLog.log("install_result",
                            "pending user action; confirmation shown");
                } else {
                    installStatus = "failed";
                    installDetail = detail + " (no confirmation intent)";
                    BridgeLog.log("install_result", "failed " + detail);
                    installRunning = false;
                }
            } else {
                installStatus = "failed";
                installDetail = detail;
                BridgeLog.log("install_result", "failed " + detail);
                installRunning = false;
            }
        }
    };

    // v2.1: operation-session tracker (SPEC-OPERATION-NOTIFY.md).
    // One global session: the server posts "操作中" when a
    // screen-changing operation starts, and "操作終了" when the
    // VM sends POST /operation_end or 10 s pass with no operation
    // activity and none in flight. All fields are guarded by
    // operationLock; NotificationManager calls are thread-safe
    // binder calls and may run on worker or scheduler threads.
    private final Object operationLock = new Object();
    private boolean operationActive = false;
    private long operationLastActivityMs = 0;
    private int operationInFlight = 0;
    private ScheduledFuture<?> operationAutoEndFuture = null;
    private ScheduledExecutorService operationScheduler;
    // v2.1 fix (CONS-023 P1): session generation.
    // operationInFlight counts only operations of the current
    // generation. A request that started in an older session and
    // finishes after an explicit end (or after a new session
    // began) is stale and must not touch the new session's
    // counter — operationFinished() ignores it by generation.
    private long operationGeneration = 0;
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
    private volatile long serverLifecycleGeneration = 0;

    /** Auto-end task: ends the session only when nothing is in
     * flight and the last activity is at least the timeout old;
     * otherwise reschedules (this debounce keeps a long /batch
     * from auto-ending mid-run). Runs on the scheduler thread. */
    private final Runnable operationAutoEndTask = new Runnable() {
        @Override
        public void run() {
            synchronized (operationLock) {
                if (!operationActive) {
                    return;
                }
                long now = android.os.SystemClock.elapsedRealtime();
                if (operationInFlight == 0
                        && now - operationLastActivityMs
                                >= OPERATION_AUTO_END_TIMEOUT_MS) {
                    endOperationLocked();
                } else {
                    scheduleAutoEndLocked();
                }
            }
        }
    };

    public HttpBridgeServer(int port, TokenProvider tokens, Context context) {
        this.port = port;
        this.tokens = tokens;
        this.context = context.getApplicationContext();
        // v2.1: the operation-notification timer (single daemon
        // thread). Recreated in start() if a previous stop() shut
        // it down, mirroring the worker pool.
        ensureOperationScheduler();
        // v1.9.3: the debug log reads the previous run's tail and
        // opens the file once, when the server is created.
        BridgeLog.init(this.context);
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
            context.registerReceiver(installReceiver,
                    new IntentFilter(INSTALL_RESULT_ACTION),
                    Context.RECEIVER_NOT_EXPORTED);
        } else {
            context.registerReceiver(installReceiver,
                    new IntentFilter(INSTALL_RESULT_ACTION));
        }
    }

    /** Binds the listeners and starts one dedicated accept thread per
     * listener. Returns true only when at least one socket is bound.
     * A second start after STOPPED creates a fresh worker pool. */
    public synchronized boolean start() {
        if (state == ServerState.RUNNING || state == ServerState.STARTING) {
            return state == ServerState.RUNNING;
        }
        // v2.1 fix (CONS-024 P1-NEW): a new lifecycle. Bump
        // before state becomes STARTING, so any request accepted
        // by this lifecycle's accept threads carries the new
        // generation, and any worker accepted in an old lifecycle
        // is refused by the generation check even if it reaches
        // noteOperation() during the STARTING window.
        serverLifecycleGeneration++;
        state = ServerState.STARTING;
        // v2.1 fix (CONS-023 P2): clear operation notifications
        // left in the shade by a previous process BEFORE the
        // bind attempts — a total bind failure must not leave a
        // stale "操作中" behind. (The old position, after
        // RUNNING, is removed: it would also cancel a
        // legitimate "操作中" posted by a request accepted
        // during the STARTING window.)
        cancelOperationNotifications();
        // v2.1: a second start after STOPPED needs a fresh timer
        // (stop() shut the old one down), like the worker pool.
        ensureOperationScheduler();
        workers = new ThreadPoolExecutor(HTTP_WORKERS, HTTP_WORKERS,
                0L, TimeUnit.MILLISECONDS,
                new ArrayBlockingQueue<Runnable>(HTTP_QUEUE),
                new ThreadPoolExecutor.AbortPolicy());
        List<InetAddress> addrs = bindAddresses();
        for (InetAddress addr : addrs) {
            try {
                ServerSocket ss = new ServerSocket();
                ss.setReuseAddress(true);
                ss.bind(new InetSocketAddress(addr, port));
                listeners.add(ss);
                final ServerSocket sock = ss;
                Thread t = new Thread(new Runnable() {
                    @Override
                    public void run() {
                        acceptLoop(sock);
                    }
                }, "muse-bridge-accept-" + addr.getHostAddress());
                acceptThreads.add(t);
                t.start();
                Log.i(TAG, "listening on " + addr.getHostAddress() + ":" + port);
                BridgeLog.log("listen", addr.getHostAddress() + ":" + port);
            } catch (Exception e) {
                Log.w(TAG, "cannot bind " + addr.getHostAddress() + ": " + e.getMessage());
                BridgeLog.log("bind-failed",
                        addr.getHostAddress() + ": " + e.getMessage());
            }
        }
        if (listeners.isEmpty()) {
            state = ServerState.FAILED;
            workers.shutdownNow();
            workers = null;
            Log.e(TAG, "no listener bound; server FAILED");
            BridgeLog.log("server", "FAILED: no listener bound");
            return false;
        }
        state = ServerState.RUNNING;
        BridgeLog.log("server", "RUNNING");
        return true;
    }

    public synchronized void stop() {
        if (state == ServerState.STOPPED || state == ServerState.NEW) {
            return;
        }
        state = ServerState.STOPPING;
        for (ServerSocket ss : listeners) {
            try {
                ss.close();
            } catch (Exception ignored) {
            }
        }
        listeners.clear();
        acceptThreads.clear();
        if (workers != null) {
            workers.shutdownNow();
            workers = null;
        }
        // v2.1: stop the operation timer and clear the ongoing
        // notification silently — the bridge is going away, not
        // ending an operation cleanly, so no "操作終了" is posted.
        synchronized (operationLock) {
            if (operationAutoEndFuture != null) {
                operationAutoEndFuture.cancel(false);
                operationAutoEndFuture = null;
            }
            operationActive = false;
            // v2.1 fix (CONS-024 P1-NEW): invalidate the session
            // — an in-flight request's finally must become stale
            // (generation mismatch) and the counter must not leak
            // into the next lifecycle. GPT's stop/restart trace:
            // A gen 1 inFlight 1 -> stop (inFlight 0, gen 2) ->
            // start -> B gen 3 inFlight 1 -> A finishes, stale,
            // ignored -> B finishes, inFlight 0.
            operationInFlight = 0;
            operationGeneration++;
        }
        if (operationScheduler != null) {
            operationScheduler.shutdownNow();
            operationScheduler = null;
        }
        cancelOperationNotifications();
        state = ServerState.STOPPED;
        BridgeLog.log("server", "STOPPED");
    }

    public boolean isRunning() {
        return state == ServerState.RUNNING;
    }

    private List<InetAddress> bindAddresses() {
        List<InetAddress> out = new ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                for (InetAddress addr : Collections.list(ni.getInetAddresses())) {
                    if (addr instanceof Inet4Address && !addr.isLoopbackAddress()) {
                        String ip = addr.getHostAddress();
                        if (ip != null && ip.startsWith("100.")) {
                            out.add(addr);
                        }
                    }
                }
            }
        } catch (Exception ignored) {
        }
        try {
            out.add(InetAddress.getByName("127.0.0.1"));
        } catch (Exception ignored) {
        }
        return out;
    }

    private void acceptLoop(ServerSocket ss) {
        // A-2: this thread only accepts. HTTP handling runs on the
        // bounded worker pool; the connection slot is released when
        // the worker finishes (or immediately when we shed the load).
        // A-5 fix: accept threads are started while the server is
        // still STARTING, so the loop must run in STARTING too — the
        // old "RUNNING only" condition made the thread exit at once
        // (the socket stayed bound but deaf, and start() still
        // reported running). It now runs until STOPPING/STOPPED.
        final ThreadPoolExecutor pool = workers;
        while (state == ServerState.RUNNING || state == ServerState.STARTING) {
            try {
                final Socket conn = ss.accept();
                // v2.1 fix (CONS-024 P1-NEW): capture the server
                // lifecycle generation at accept time. A worker
                // that outlives its lifecycle (accepted before
                // stop(), handled after restart) carries the old
                // generation and is refused by noteOperation() /
                // endOperation() — capturing at the top of
                // handle() would be too late, handle() itself
                // runs after the restart.
                final long lifecycleGeneration = serverLifecycleGeneration;
                if (pool == null || pool.isShutdown()) {
                    try {
                        conn.close();
                    } catch (Exception ignored) {
                    }
                    continue;
                }
                if (!connectionSlots.tryAcquire()) {
                    // At the connection cap: shed without reading the
                    // request — reading is what we protect the
                    // workers from.
                    try {
                        conn.close();
                    } catch (Exception ignored) {
                    }
                    Log.w(TAG, "connection cap reached; closed a connection");
                    BridgeLog.logThrottled("conn-cap", "conn-cap",
                            "closed a connection at cap");
                    continue;
                }
                try {
                    pool.execute(new Runnable() {
                        @Override
                        public void run() {
                            try {
                                int st = handle(conn, lifecycleGeneration);
                                if (st >= 400) {
                                    BridgeLog.logThrottled("http-" + st,
                                            "http-" + st, "");
                                }
                            } finally {
                                connectionSlots.release();
                            }
                        }
                    });
                } catch (RejectedExecutionException e) {
                    // Worker queue full: answer 503 without reading the
                    // request (reading it would defeat the shedding)
                    // and release the slot.
                    connectionSlots.release();
                    BridgeLog.logThrottled("overload-503", "overload-503",
                            "worker queue full");
                    sendOverload(conn);
                }
            } catch (Exception e) {
                if (state == ServerState.RUNNING || state == ServerState.STARTING) {
                    Log.w(TAG, "accept failed: " + e.getMessage());
                    BridgeLog.logThrottled("accept-failed", "accept-failed",
                            e.getMessage());
                }
            }
        }
    }

    /** 503 for an overloaded server, sent without reading the request:
     * the point of shedding load is to not spend work on it. */
    private void sendOverload(Socket conn) {
        try {
            JSONObject body = errorJson("server busy");
            byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
            String head = "HTTP/1.1 503 Service Unavailable\r\n"
                    + "Content-Type: application/json\r\n"
                    + "Content-Length: " + bytes.length + "\r\n"
                    + "Connection: close\r\n\r\n";
            OutputStream out = conn.getOutputStream();
            out.write(head.getBytes(StandardCharsets.US_ASCII));
            out.write(bytes);
            out.flush();
        } catch (Exception ignored) {
        } finally {
            try {
                conn.close();
            } catch (Exception ignored) {
            }
        }
    }

    private int handle(Socket conn, long lifecycleGeneration) {
        String method = "?";
        String path = "?";
        int status = 500;
        // A-1 (v1.9): per-request monotonic deadline; each phase gets
        // the smaller of its own timeout and the remaining total time.
        long deadline = android.os.SystemClock.elapsedRealtime() + TOTAL_TIMEOUT_MS;
        // v2.1: the generation noteOperation() returned when
        // this request started a screen operation (-1 when it
        // noted none); the finally block passes it to
        // operationFinished() so a stale request cannot touch
        // another session's counter (CONS-023 P1).
        // v2.1 fix (CONS-024 P1-NEW): lifecycleGeneration is the
        // server lifecycle captured at accept time; noteOperation()
        // and endOperation() refuse a mismatch with the current
        // lifecycle (a request that outlived a stop/restart).
        long opGeneration = -1;
        try {
            InputStream in = conn.getInputStream();
            OutputStream out = conn.getOutputStream();

            // Phase 1: headers, capped at MAX_HEADER_BYTES (over -> 431),
            // deadline HEADER_TIMEOUT_MS (timeout -> 408).
            setPhaseTimeout(conn, HEADER_TIMEOUT_MS, deadline);
            String head = readHead(in);
            if (head == null || head.isEmpty()) {
                return status;
            }
            String[] lines = head.split("\r\n");
            String[] req = lines[0].split(" ");
            if (req.length != 3
                    || !("GET".equals(req[0]) || "POST".equals(req[0]))
                    || !req[1].startsWith("/")
                    || !("HTTP/1.0".equals(req[2]) || "HTTP/1.1".equals(req[2]))) {
                status = 400;
                sendJson(out, status, errorJson("bad request line"));
                return status;
            }
            method = req[0];
            String target = req[1];
            int q = target.indexOf('?');
            path = (q >= 0) ? target.substring(0, q) : target;
            String query = (q >= 0) ? target.substring(q + 1) : "";
            String token = null;
            Long contentLength = null;
            boolean sawTransferEncoding = false;
            for (int i = 1; i < lines.length; i++) {
                String line = lines[i];
                int colon = line.indexOf(':');
                if (colon <= 0) {
                    status = 400;
                    sendJson(out, status, errorJson("bad header line"));
                    return status;
                }
                String name = line.substring(0, colon).trim();
                String value = line.substring(colon + 1).trim();
                if ("X-Bridge-Token".equalsIgnoreCase(name)) {
                    if (token != null) {
                        status = 400;
                        sendJson(out, status, errorJson("duplicate X-Bridge-Token"));
                        return status;
                    }
                    token = value;
                } else if ("Content-Length".equalsIgnoreCase(name)) {
                    if (contentLength != null) {
                        status = 400;
                        sendJson(out, status, errorJson("duplicate Content-Length"));
                        return status;
                    }
                    try {
                        contentLength = Long.parseLong(value);
                    } catch (NumberFormatException e) {
                        status = 400;
                        sendJson(out, status, errorJson("bad Content-Length"));
                        return status;
                    }
                } else if ("Transfer-Encoding".equalsIgnoreCase(name)) {
                    sawTransferEncoding = true;
                }
            }
            if (sawTransferEncoding) {
                status = 400;
                sendJson(out, status, errorJson("Transfer-Encoding not supported"));
                return status;
            }

            // Phase 2: token check BEFORE the body is read, so an
            // unauthenticated peer cannot make us consume a body.
            String expected = tokens.currentToken();
            if (expected == null || !expected.equals(token)) {
                status = 401;
                sendJson(out, status, errorJson("unauthorized"));
                return status;
            }

            // Phase 3: Content-Length validation.
            long cl = contentLength == null ? 0 : contentLength.longValue();
            if (cl < 0) {
                status = 400;
                sendJson(out, status, errorJson("negative Content-Length"));
                return status;
            }
            if (cl > MAX_BODY_BYTES) {
                status = 413;
                sendJson(out, status, errorJson("body too large"));
                return status;
            }

            // Phase 4: body, capped at MAX_BODY_BYTES, deadline
            // BODY_TIMEOUT_MS (timeout -> 408, short body -> 400).
            byte[] body = new byte[0];
            if (cl > 0) {
                setPhaseTimeout(conn, BODY_TIMEOUT_MS, deadline);
                body = in.readNBytes((int) cl);
                if (body.length != cl) {
                    status = 400;
                    sendJson(out, status, errorJson("short body"));
                    return status;
                }
            }

            BridgeAccessibilityService svc = BridgeAccessibilityService.getInstance();
            if ("/health".equals(path)) {
                // P0-3 (v2.0): the extended health body. All 7
                // legacy fields are kept byte-identical; the 5 new
                // fields (mode, overall_status, modules,
                // capabilities, tailscale_ips) are appended.
                boolean a11yOn = svc != null;
                boolean listenerGranted =
                        BridgeNotificationListener.isAccessGranted(context);
                boolean listenerConnected =
                        BridgeNotificationListener.getInstance() != null;
                String mode = currentMode(a11yOn);
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
                List<String> tailscaleIps = MainActivity.findTailscaleIps();
                List<String> boundIps = boundTailscaleIps();
                JSONObject modules = buildModules(a11yOn, listenerGranted,
                        listenerConnected, tailscaleIps, boundIps);
                JSONObject caps = buildCapabilities(a11yOn, listenerGranted,
                        listenerConnected);
                String overall = overallStatus(modules);
                status = 200;
                sendJson(out, status, new JSONObject()
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
                        .put("tailscale_ips", new JSONArray(tailscaleIps))
                        .put("tailscale_ips_bound", new JSONArray(boundIps)));
            } else if ("/capture_screen".equals(path)) {
                if (svc == null) {
                    status = 503;
                    sendJson(out, status, unavailableJson(
                            "accessibility service is not enabled; "
                                    + "/capture_screen needs FULL_ACCESS"));
                } else {
                    // v1.2: ?scale=0.5&format=jpeg&quality=80 for a light
                    // routine shot; defaults keep the old full-res PNG.
                    float scale = parseFloat(queryParam(query, "scale"), 1.0f);
                    String format = queryParam(query, "format");
                    int quality = parseInt(queryParam(query, "quality"), 80);
                    boolean jpeg = "jpeg".equalsIgnoreCase(format)
                            || "jpg".equalsIgnoreCase(format);
                    long t0 = android.os.SystemClock.elapsedRealtime();
                    byte[] shot = svc.takeScreenshot(scale, format, quality);
                    long captureMs = android.os.SystemClock.elapsedRealtime() - t0;
                    status = 200;
                    sendBytes(out, status, jpeg ? "image/jpeg" : "image/png", shot,
                            "X-Capture-Ms: " + captureMs + "\r\n");
                }
            } else if ("/state".equals(path)) {
                if (svc == null) {
                    status = 503;
                    sendJson(out, status, unavailableJson(
                            "accessibility service is not enabled; "
                                    + "/state needs FULL_ACCESS"));
                } else {
                    BridgeAccessibilityService.State st = svc.currentState();
                    JSONArray texts = new JSONArray();
                    for (String t : st.texts) {
                        texts.put(t);
                    }
                    JSONArray nodes = new JSONArray();
                    for (BridgeAccessibilityService.Node n : st.nodes) {
                        JSONObject b = new JSONObject()
                                .put("left", n.left).put("top", n.top)
                                .put("right", n.right).put("bottom", n.bottom);
                        JSONArray actions = new JSONArray();
                        for (String a : n.actions) {
                            actions.put(a);
                        }
                        nodes.put(new JSONObject()
                                .put("id", n.id)
                                .put("parent", n.parentId == null
                                        ? JSONObject.NULL : n.parentId)
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
                                .put("actions", actions));
                    }
                    status = 200;
                    sendJson(out, status, new JSONObject()
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
                            .put("focused", st.focusedId == null
                                    ? JSONObject.NULL : st.focusedId)
                            .put("ime_visible", st.imeVisible)
                            .put("event_seq", st.eventSeq)
                            .put("device", deviceState())
                            .put("truncated", st.truncated));
                }
            } else if ("/logs".equals(path)) {
                // v1.9.3: debug log, pull-based and token-authenticated
                // like every endpoint. ?limit= (default 100, max 200)
                // takes the newest lines of the current run; the
                // previous run's tail rides along separately.
                int limit = parseInt(queryParam(query, "limit"), 100);
                if (limit < 1) {
                    limit = 1;
                }
                if (limit > 200) {
                    limit = 200;
                }
                List<String> all = BridgeLog.snapshot();
                JSONArray arr = new JSONArray();
                int start = Math.max(0, all.size() - limit);
                for (int i = start; i < all.size(); i++) {
                    arr.put(all.get(i));
                }
                JSONArray prev = new JSONArray();
                for (String l : BridgeLog.previousSnapshot()) {
                    prev.put(l);
                }
                status = 200;
                sendJson(out, status, new JSONObject()
                        .put("ok", true)
                        .put("app_version", AppVersion.name(context))
                        .put("app_version_code", AppVersion.code(context))
                        .put("lines", arr)
                        .put("previous_lines", prev));
                return status;
            } else if ("/events".equals(path)) {
                // Phase 3 (v1.6): the recorded event history after
                // `since` (default 0 = everything buffered), oldest
                // first, capped at `limit` (default 100, max 200).
                // dropped=true means the buffer evicted events the
                // caller never saw — refetch /state instead.
                if (svc == null) {
                    status = 503;
                    sendJson(out, status, unavailableJson(
                            "accessibility service is not enabled; "
                                    + "/events needs FULL_ACCESS"));
                } else {
                    long since = parseLong(queryParam(query, "since"), 0);
                    int limit = parseInt(queryParam(query, "limit"), 100);
                    if (limit < 1) {
                        limit = 1;
                    }
                    if (limit > 200) {
                        limit = 200;
                    }
                    BridgeAccessibilityService.EventsResult r =
                            svc.getEvents(since, limit);
                    JSONArray events = new JSONArray();
                    for (BridgeAccessibilityService.UiEvent e : r.events) {
                        JSONObject b = new JSONObject()
                                .put("left", e.left).put("top", e.top)
                                .put("right", e.right).put("bottom", e.bottom);
                        events.put(new JSONObject()
                                .put("seq", e.seq)
                                .put("t", e.t)
                                .put("type", e.type)
                                .put("package", e.packageName)
                                .put("class", e.className)
                                .put("text", e.text)
                                .put("desc", e.desc)
                                .put("bounds", b)
                                .put("coalesced", e.coalesced));
                    }
                    status = 200;
                    sendJson(out, status, new JSONObject()
                            .put("ok", true)
                            .put("events", events)
                            .put("latest_seq", r.latestSeq)
                            .put("dropped", r.dropped)
                            .put("tree_hash", r.treeHash)
                            .put("revision", r.revision));
                }
            } else if ("/notifications".equals(path)) {
                // Decision 63 (v1.9.7): the notification event stream.
                // Needs neither the accessibility service nor the
                // bridge token beyond the check every endpoint gets.
                // 503 when the listener is not granted, and a
                // different 503 when granted but not connected.
                if (!BridgeNotificationListener.isAccessGranted(context)) {
                    status = 503;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "notification_listener_access_not_granted"));
                } else if (BridgeNotificationListener.getInstance() == null) {
                    status = 503;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "notification_listener_not_connected"));
                } else {
                    long since = parseLong(queryParam(query, "since"), 0);
                    int limit = parseInt(queryParam(query, "limit"), 100);
                    if (limit < 1) {
                        limit = 1;
                    }
                    if (limit > 200) {
                        limit = 200;
                    }
                    String pkg = queryParam(query, "package");
                    if (pkg != null && pkg.isEmpty()) {
                        pkg = null;
                    }
                    BridgeNotificationListener.NotificationsResult nr =
                            BridgeNotificationListener.getEvents(since, limit, pkg);
                    JSONArray notifs = new JSONArray();
                    for (BridgeNotificationListener.NotificationEvent e : nr.events) {
                        JSONObject o = new JSONObject()
                                .put("seq", e.seq)
                                .put("type", e.type)
                                .put("key", e.key)
                                .put("package", e.packageName)
                                .put("id", e.id)
                                .put("tag", e.tag == null ? JSONObject.NULL : e.tag)
                                .put("title", e.title)
                                .put("text", e.text)
                                .put("t", e.t)
                                .put("ongoing", e.ongoing)
                                .put("group_summary", e.groupSummary);
                        if ("removed".equals(e.type)) {
                            o.put("removed_t", e.removedT);
                        }
                        notifs.put(o);
                    }
                    status = 200;
                    sendJson(out, status, new JSONObject()
                            .put("ok", true)
                            .put("events", notifs)
                            .put("latest_seq", nr.latestSeq)
                            .put("dropped", nr.dropped));
                }
            } else if ("/notification_action".equals(path) && "POST".equals(method)) {
                // Decision 77 (v1.9.18): act on a live notification.
                // {"key": "...", "action": "open"} fires its
                // contentIntent; {"action": "reply", "text": "..."}
                // sends an inline reply through its RemoteInput.
                // The lookup is live (no stored actions), so a
                // gone key is 422. 503 when the listener is not
                // granted or not connected (same as /notifications).
                // Reply SENDS a message: the caller must have the
                // user's approval of the exact text first.
                if (!BridgeNotificationListener.isAccessGranted(context)) {
                    status = 503;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "notification_listener_access_not_granted"));
                } else if (BridgeNotificationListener.getInstance() == null) {
                    status = 503;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "notification_listener_not_connected"));
                } else {
                    JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                    String key = j.getString("key");
                    String action = j.optString("action", "");
                    String err;
                    try {
                        if ("open".equals(action)) {
                            // v2.1: opening a notification changes
                            // the foreground app — show "操作中".
                            // ("reply" sends a message without
                            // changing the screen, so no hook.)
                            opGeneration = noteOperation(lifecycleGeneration, "通知を開いています");
                            err = BridgeNotificationListener.openNotification(key);
                        } else if ("reply".equals(action)) {
                            String text = j.getString("text");
                            err = BridgeNotificationListener.replyNotification(key, text);
                        } else {
                            throw new org.json.JSONException("unknown action: " + action);
                        }
                        status = err == null ? 200 : 422;
                        sendJson(out, status, new JSONObject().put("ok", err == null)
                                .put("error", err == null ? JSONObject.NULL : err));
                    } catch (IllegalArgumentException e) {
                        status = 422;
                        sendJson(out, status, new JSONObject().put("ok", false)
                                .put("error", e.getMessage()));
                    }
                }
            } else if ("/click_node".equals(path) && "POST".equals(method)) {
                // Phase 2: click by node id (tree_hash from the /state
                // the client used). 409 when the screen moved on.
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                String nodeId = j.getString("node_id");
                int nodeTreeHash = j.getInt("tree_hash");
                // v2.1: the operation is accepted — show "操作中".
                opGeneration = noteOperation(lifecycleGeneration, "タップ中");
                try {
                    BridgeAccessibilityService.ActionOutcome o =
                            svc.clickNode(nodeId, nodeTreeHash);
                    status = o.ok ? 200 : 422;
                    sendJson(out, status, new JSONObject().put("ok", o.ok)
                            .put("snapshot_id", o.snapshotId)
                            .put("error", o.ok ? JSONObject.NULL : "node not clickable"));
                } catch (BridgeAccessibilityService.StaleRevisionException e) {
                    status = 409;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                } catch (BridgeAccessibilityService.NodeNotFoundException e) {
                    status = 422;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                }
            } else if ("/type_node".equals(path) && "POST".equals(method)) {
                // Phase 2: set text on an editable node by id.
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                String nodeId = j.getString("node_id");
                int nodeTreeHash = j.getInt("tree_hash");
                String nodeText = j.getString("text");
                // v2.1: the operation is accepted — show "操作中".
                // The typed text never goes into the notification.
                opGeneration = noteOperation(lifecycleGeneration, "文字を入力中");
                try {
                    BridgeAccessibilityService.ActionOutcome o = svc.typeNode(nodeId,
                            nodeTreeHash, nodeText);
                    status = o.ok ? 200 : 422;
                    sendJson(out, status, new JSONObject().put("ok", o.ok)
                            .put("snapshot_id", o.snapshotId)
                            .put("error", o.ok ? JSONObject.NULL : "node not editable"));
                } catch (BridgeAccessibilityService.StaleRevisionException e) {
                    status = 409;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                } catch (BridgeAccessibilityService.NodeNotFoundException e) {
                    status = 422;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                }
            } else if ("/scroll_node".equals(path) && "POST".equals(method)) {
                // Decision 67 (v1.9.10): scroll a node (or its first
                // scrollable ancestor). 409 when the screen moved on.
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                String direction = j.optString("direction", "forward");
                if (!"forward".equalsIgnoreCase(direction)
                        && !"backward".equalsIgnoreCase(direction)) {
                    status = 400;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "unknown direction: " + direction));
                } else {
                    String nodeId = j.getString("node_id");
                    int nodeTreeHash = j.getInt("tree_hash");
                    // v2.1: the operation is accepted — show "操作中".
                    opGeneration = noteOperation(lifecycleGeneration, "スクロール中");
                    try {
                        BridgeAccessibilityService.ActionOutcome o = svc.scrollNode(
                                nodeId, nodeTreeHash, direction);
                        status = o.ok ? 200 : 422;
                        sendJson(out, status, new JSONObject().put("ok", o.ok)
                                .put("snapshot_id", o.snapshotId)
                                .put("error", o.ok ? JSONObject.NULL : "node not scrollable"));
                    } catch (BridgeAccessibilityService.StaleRevisionException e) {
                        status = 409;
                        sendJson(out, status, new JSONObject().put("ok", false)
                                .put("error", e.getMessage()));
                    } catch (BridgeAccessibilityService.NodeNotFoundException e) {
                        status = 422;
                        sendJson(out, status, new JSONObject().put("ok", false)
                                .put("error", e.getMessage()));
                    }
                }
            } else if ("/node_action".equals(path) && "POST".equals(method)) {
                // Decision 68 (v1.9.11): any one-shot node action by
                // name, with optional args. 409 when the screen moved on.
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                JSONObject args = j.optJSONObject("args");
                String nodeId = j.getString("node_id");
                int nodeTreeHash = j.getInt("tree_hash");
                String actionName = j.getString("action");
                // v2.1 fix (CONS-023 P2, D6): reject unknown
                // actions before the hook — the notification
                // label must never contain an unvalidated value.
                if (!KNOWN_NODE_ACTIONS.contains(actionName)) {
                    status = 400;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "unknown action: " + actionName));
                    return status;
                }
                // v2.1: the operation is accepted — show "操作中"
                // with the action name (fixed enum, safe).
                opGeneration = noteOperation(lifecycleGeneration, "操作を実行中（" + actionName + "）");
                try {
                    BridgeAccessibilityService.ActionOutcome o = svc.nodeAction(
                            nodeId, nodeTreeHash, actionName, args);
                    status = o.ok ? 200 : 422;
                    sendJson(out, status, new JSONObject().put("ok", o.ok)
                            .put("snapshot_id", o.snapshotId)
                            .put("error", o.ok ? JSONObject.NULL : "action not supported"));
                } catch (IllegalArgumentException e) {
                    status = 400;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                } catch (BridgeAccessibilityService.StaleRevisionException e) {
                    status = 409;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                } catch (BridgeAccessibilityService.NodeNotFoundException e) {
                    status = 422;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                }
            } else if ("/launch_app".equals(path) && "POST".equals(method)) {
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
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                String pkg = j.getString("package");
                // P0-4 deny list (Saardeim decision 2026-11-06
                // 20:16, README decision 91): refuse BEFORE any
                // launch attempt and BEFORE the operation
                // notification hook — a refused launch posts no
                // "操作中" notification and never reaches
                // getLaunchIntentForPackage. HTTP 403, old error
                // format (a policy refusal, not a launch outcome).
                if (LAUNCH_DENY_LIST.contains(pkg)) {
                    BridgeLog.log("launch_denied", pkg);
                    status = 403;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "launch denied: " + pkg
                                    + " is on the deny list"));
                    return status;
                }
                // v2.1: the operation is accepted — show "操作中"
                // with the app label (falls back to the package
                // name when no label exists).
                opGeneration = noteOperation(lifecycleGeneration, "アプリを起動中：" + operationAppLabel(pkg));
                try {
                    // P0-4: record the request time BEFORE issuing
                    // the start, and note whether the target is
                    // already in front — the verification below only
                    // counts observations after this request.
                    // P0-4 fix (CONS-027 P2 #5): elapsedRealtime
                    // (monotonic) — the window is a duration, so a
                    // wall-clock correction must not move it. The
                    // pre-request read is bounded (500 ms): it is
                    // informational only and must not delay the
                    // launch on a main-thread stall.
                    long requestTimeMs = android.os.SystemClock.elapsedRealtime();
                    boolean wasForeground = svc != null
                            && pkg.equals(foregroundPackageSafe(svc,
                                    LAUNCH_PRE_READ_TIMEOUT_MS));
                    if (wasForeground) {
                        BridgeLog.log("launch_already_foreground", pkg);
                    }
                    boolean issued = launchApp(pkg);
                    if (!issued) {
                        status = 503;
                        sendJson(out, status, new JSONObject().put("ok", false)
                                .put("error", "launch blocked by the system"));
                    } else {
                        status = 200;
                        sendJson(out, status, verifyLaunchForeground(
                                pkg, svc, requestTimeMs, wasForeground));
                    }
                } catch (IllegalArgumentException e) {
                    status = 422;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                }
            } else if ("/open".equals(path) && "POST".equals(method)) {
                // Decision 74 (v1.9.15): open a URI with ACTION_VIEW
                // (web URL or an app's deep link). Deliberately no
                // requireService() — opening needs only startActivity
                // from this server's own context, so it works even
                // when the accessibility service is off (unlike
                // /launch_app today — see P1-05). 422 when no app
                // handles the URI; 503 when Android refuses the
                // start.
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                String uri = j.getString("uri");
                // v2.1: the operation is accepted — show "操作中".
                // The URI itself never goes into the notification
                // (it can carry tokens).
                opGeneration = noteOperation(lifecycleGeneration, "リンクを開いています");
                try {
                    boolean ok = openUri(uri);
                    status = ok ? 200 : 422;
                    sendJson(out, status, new JSONObject().put("ok", ok)
                            .put("error", ok ? JSONObject.NULL : "could not open uri"));
                } catch (IllegalArgumentException e) {
                    status = 422;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                } catch (ActivityNotFoundException e) {
                    status = 422;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "no app can open this link"));
                } catch (SecurityException e) {
                    status = 503;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "open blocked by the system"));
                }
            } else if ("/clipboard".equals(path) && "GET".equals(method)) {
                // Decision 76 (v1.9.17): read the clipboard. Runs
                // on this server's own context, no accessibility
                // service needed. text is null when the clipboard
                // is empty — or when Android 10+ blocks the read
                // while the app runs in the background (the live
                // test settles which one it is). Capped at 64 KiB
                // with truncated=true. The content is never written
                // to BridgeLog (it may hold a password).
                ClipboardManager cm = (ClipboardManager)
                        context.getSystemService(Context.CLIPBOARD_SERVICE);
                ClipData clip = null;
                try {
                    clip = cm == null ? null : cm.getPrimaryClip();
                } catch (SecurityException e) {
                    // Background read blocked on this device — the
                    // same outcome as an empty clipboard (null).
                    clip = null;
                }
                CharSequence cs = null;
                if (clip != null && clip.getItemCount() > 0) {
                    cs = clip.getItemAt(0).coerceToText(context);
                }
                String text = cs == null ? null : cs.toString();
                boolean truncated = false;
                if (text != null && text.length() > 65536) {
                    text = text.substring(0, 65536);
                    truncated = true;
                }
                status = 200;
                sendJson(out, status, new JSONObject()
                        .put("ok", true)
                        .put("text", text == null ? JSONObject.NULL : text)
                        .put("truncated", truncated));
            } else if ("/clipboard".equals(path) && "POST".equals(method)) {
                // Decision 76 (v1.9.17): write the clipboard.
                // Empty text clears it. Writing works from the
                // background (only reading is restricted).
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                String text = j.getString("text");
                ClipboardManager cm = (ClipboardManager)
                        context.getSystemService(Context.CLIPBOARD_SERVICE);
                cm.setPrimaryClip(ClipData.newPlainText("muse-bridge", text));
                status = 200;
                sendJson(out, status, new JSONObject().put("ok", true));
            } else if ("/install_status".equals(path) && "GET".equals(method)) {
                // Decision 75 (v1.9.16): the install-apk job state
                // (idle / downloading / installing / committed /
                // installed / failed). Read-only.
                status = 200;
                sendJson(out, status, new JSONObject()
                        .put("ok", true)
                        .put("status", installStatus)
                        .put("detail", installDetail));
            } else if ("/install_apk".equals(path) && "POST".equals(method)) {
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
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                String url = j.optString("url", null);
                String apkPath = j.optString("path", null);
                if ((url == null) == (apkPath == null)) {
                    status = 400;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "give exactly one of url, path"));
                } else if (url != null && !url.startsWith("https://")) {
                    status = 400;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "url must be https"));
                } else if (apkPath != null && !new File(apkPath).isFile()) {
                    status = 422;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "apk file not found: " + apkPath));
                } else if (installRunning) {
                    status = 409;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "install already in progress"));
                } else {
                    final String fUrl = url;
                    final String fPath = apkPath;
                    new Thread(() -> runInstall(fUrl, fPath), "install-apk").start();
                    status = 200;
                    sendJson(out, status, new JSONObject().put("ok", true)
                            .put("status", "started"));
                }
            } else if ("/batch".equals(path) && "POST".equals(method)) {
                // Phase 2 (A-4): several actions in one request
                // (batch_id, protocol_version, tree_hash, actions[]).
                // Re-sent batch_id + same payload returns the cached
                // result without re-executing (idempotency); same id
                // with different content is 409, an expired id is 410.
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                try {
                    rejectUnknownFields(j, "batch_id", "protocol_version",
                            "tree_hash", "actions");
                    int protocolVersion = j.optInt("protocol_version", 1);
                    if (protocolVersion != 1) {
                        throw new org.json.JSONException(
                                "unsupported protocol_version: " + protocolVersion);
                    }
                    List<BridgeAccessibilityService.BatchAction> actions = new ArrayList<>();
                    JSONArray arr = j.getJSONArray("actions");
                    // A-5: a batch is capped at 20 actions (Codex Phase A)
                    // so even all-expect_change batches stay inside the
                    // 45 s request deadline (20 x 2 s + action time).
                    if (arr.length() > 20) {
                        throw new org.json.JSONException(
                                "batch has too many actions (max 20): " + arr.length());
                    }
                    for (int i = 0; i < arr.length(); i++) {
                        JSONObject a = arr.getJSONObject(i);
                        rejectUnknownFields(a, "action_id", "type", "node_id",
                                "x", "y", "x1", "y1", "x2", "y2", "duration_ms",
                                "text", "key", "wait_ms", "expect_change");
                        BridgeAccessibilityService.BatchAction ba =
                                new BridgeAccessibilityService.BatchAction();
                        ba.actionId = a.optString("action_id", "a" + i);
                        ba.type = a.getString("type");
                        ba.nodeId = a.optString("node_id", "");
                        ba.x = a.optInt("x", 0);
                        ba.y = a.optInt("y", 0);
                        ba.x1 = a.optInt("x1", 0);
                        ba.y1 = a.optInt("y1", 0);
                        ba.x2 = a.optInt("x2", 0);
                        ba.y2 = a.optInt("y2", 0);
                        ba.durationMs = a.optLong("duration_ms", 300);
                        ba.text = a.optString("text", "");
                        ba.key = a.optString("key", "");
                        ba.waitMs = a.optLong("wait_ms", 0);
                        ba.expectChange = a.optBoolean("expect_change", false);
                        actions.add(ba);
                    }
                    String batchId = j.getString("batch_id");
                    int batchTreeHash = j.getInt("tree_hash");
                    // v2.1: the actions parsed — show "操作中" for
                    // the whole batch (never a per-action summary).
                    opGeneration = noteOperation(lifecycleGeneration, "複数の操作を実行中");
                    BridgeAccessibilityService.BatchResult br = svc.runBatch(
                            batchId, protocolVersion, batchTreeHash, actions);
                    JSONArray results = new JSONArray();
                    for (BridgeAccessibilityService.ActionResult r : br.results) {
                        results.put(new JSONObject()
                                .put("action_id", r.actionId)
                                .put("status", r.status)
                                .put("error", r.error.isEmpty()
                                        ? JSONObject.NULL : r.error)
                                .put("hash_before", r.hashBefore)
                                .put("hash_after", r.hashAfter));
                    }
                    JSONArray completed = new JSONArray();
                    for (String id : br.completedActionIds) {
                        completed.put(id);
                    }
                    status = 200;
                    sendJson(out, status, new JSONObject()
                            .put("ok", true)
                            .put("batch_id", br.batchId)
                            .put("snapshot_id", br.snapshotId)
                            .put("status", br.status)
                            .put("payload_hash", br.payloadHash)
                            .put("replayed", br.replayed)
                            .put("results", results)
                            .put("completed_action_ids", completed)
                            .put("failed_action_id", br.failedActionId.isEmpty()
                                    ? JSONObject.NULL : br.failedActionId)
                            .put("stopped_at", br.stoppedAt)
                            .put("stop_reason", br.stopReason.isEmpty()
                                    ? JSONObject.NULL : br.stopReason));
                } catch (BridgeAccessibilityService.BatchMismatchException e) {
                    status = 409;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                } catch (BridgeAccessibilityService.BatchGoneException e) {
                    status = 410;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                } catch (BridgeAccessibilityService.StaleRevisionException e) {
                    status = 409;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                } catch (BridgeAccessibilityService.NodeNotFoundException e) {
                    status = 422;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                }
            } else if ("/click".equals(path) && "POST".equals(method)) {
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                int x = j.getInt("x");
                int y = j.getInt("y");
                // v2.1: the operation is accepted — show "操作中".
                opGeneration = noteOperation(lifecycleGeneration, "タップ中");
                boolean ok = svc.tap(x, y);
                status = 200;
                sendJson(out, status, new JSONObject().put("ok", ok));
            } else if ("/swipe".equals(path) && "POST".equals(method)) {
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                int x1 = j.getInt("x1");
                int y1 = j.getInt("y1");
                int x2 = j.getInt("x2");
                int y2 = j.getInt("y2");
                long durationMs = j.optLong("duration_ms", 300);
                // v2.1: the operation is accepted — show "操作中".
                opGeneration = noteOperation(lifecycleGeneration, "スワイプ中");
                boolean ok = svc.swipe(x1, y1, x2, y2, durationMs);
                status = 200;
                sendJson(out, status, new JSONObject().put("ok", ok));
            } else if ("/type_text".equals(path) && "POST".equals(method)) {
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                String text = j.getString("text");
                // v2.1: the operation is accepted — show "操作中".
                // The typed text never goes into the notification.
                opGeneration = noteOperation(lifecycleGeneration, "文字を入力中");
                boolean ok = svc.typeText(text);
                status = ok ? 200 : 422;
                sendJson(out, status, new JSONObject().put("ok", ok)
                        .put("error", ok ? JSONObject.NULL
                                : "no focused editable field — tap the field first"));
            } else if ("/press_key".equals(path) && "POST".equals(method)) {
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                String key = j.getString("key");
                // v2.1 fix (CONS-023 P2, D6): reject unknown keys
                // before the hook — the notification label must
                // never contain an unvalidated value.
                if (!isKnownPressKey(key)) {
                    status = 400;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "unknown key: " + key));
                    return status;
                }
                // v2.1: the operation is accepted — show "操作中"
                // with the key name (fixed enum, safe).
                opGeneration = noteOperation(lifecycleGeneration, "キー操作中（" + key + "）");
                boolean ok = svc.pressKey(key);
                status = ok ? 200 : 422;
                sendJson(out, status, new JSONObject().put("ok", ok)
                        .put("error", ok ? JSONObject.NULL : "unknown key"));
            } else if ("/notify".equals(path) && "POST".equals(method)) {
                // v1.3: the VM posts this after pushing a new APK so the
                // phone tells the user an update is waiting. No polling,
                // no new server, no new dependency. Tapping the
                // notification opens Downloads; the user taps the APK
                // file once to install (Android requires that tap).
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                String title = j.optString("title", "Muse Bridge の更新");
                String text = j.optString("text", "");
                boolean posted = postUpdateNotification(title, text);
                status = posted ? 200 : 422;
                sendJson(out, status, new JSONObject().put("ok", posted)
                        .put("error", posted ? JSONObject.NULL
                                : "notifications not permitted — grant notification permission in the Muse Bridge app"));
            } else if ("/operation_end".equals(path) && "POST".equals(method)) {
                // v2.1: end the current operation session (the VM's
                // explicit end signal). No accessibility service
                // needed — it must work in MINIMAL. An empty body
                // or {} is accepted; a non-empty body must be
                // valid JSON (fields are ignored).
                if (body.length > 0) {
                    new JSONObject(new String(body, StandardCharsets.UTF_8));
                }
                boolean wasActive = endOperation(lifecycleGeneration);
                BridgeLog.log("operation_end", wasActive ? "ended" : "idle");
                status = 200;
                sendJson(out, status, new JSONObject().put("ok", true)
                        .put("was_active", wasActive));
            } else {
                status = 404;
                sendJson(out, status, new JSONObject().put("ok", false).put("error", "not found"));
            }
        } catch (HeaderTooLarge e) {
            status = 431;
            try {
                sendJson(conn.getOutputStream(), status, errorJson("header too large"));
            } catch (Exception ignored) {
            }
        } catch (java.net.SocketTimeoutException e) {
            status = 408;
            try {
                sendJson(conn.getOutputStream(), status, errorJson("request timeout"));
            } catch (Exception ignored) {
            }
        } catch (org.json.JSONException e) {
            // A-5 fix: carry the specific message (unknown field, too
            // many actions, unsupported version) instead of a fixed
            // "invalid JSON" — a genuine parse failure still has the
            // parser's own message in e.getMessage().
            status = 400;
            try {
                String msg = e.getMessage();
                sendJson(conn.getOutputStream(), status, errorJson(
                        msg == null || msg.isEmpty() ? "invalid JSON" : msg));
            } catch (Exception ignored) {
            }
        } catch (ServiceUnavailable e) {
            // P0-1 (v2.0): the structured "a11y off" body — one
            // edit covers all requireService endpoints.
            status = 503;
            try {
                sendJson(conn.getOutputStream(), status,
                        unavailableJson("accessibility service is not enabled"));
            } catch (Exception ignored) {
            }
        } catch (BridgeAccessibilityService.ServiceTimeoutException e) {
            // A-3: accessibility work timed out on the main thread.
            status = 503;
            try {
                sendJson(conn.getOutputStream(), status, errorJson(e.getMessage()));
            } catch (Exception ignored) {
            }
        } catch (Exception e) {
            status = 500;
            try {
                sendJson(conn.getOutputStream(), status, new JSONObject()
                        .put("ok", false).put("error", String.valueOf(e.getMessage())));
            } catch (Exception ignored) {
            }
        } finally {
            // v2.1: the operation this request started (if any) has
            // finished — release its in-flight slot so the auto-end
            // timer can fire. Runs on every exit path, including
            // the early returns above. A generation of -1 means
            // this request noted no operation.
            if (opGeneration >= 0) {
                operationFinished(opGeneration);
            }
            Log.i(TAG, method + " " + path + " -> " + status);
            try {
                conn.close();
            } catch (Exception ignored) {
            }
        }
        return status;
    }

    /**
     * Posts an update notification (v1.3). Returns false when
     * notifications are not permitted, so the caller can tell the
     * user to grant the permission instead of failing silently.
     */
    private boolean postUpdateNotification(String title, String text) {
        NotificationManager nm = (NotificationManager)
                context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null || !nm.areNotificationsEnabled()) {
            return false;
        }
        NotificationChannel channel = new NotificationChannel(
                UPDATE_CHANNEL_ID, "Muse Bridge の更新",
                NotificationManager.IMPORTANCE_DEFAULT);
        channel.setDescription("新しいバージョンのAPKが届いたときのお知らせ。");
        nm.createNotificationChannel(channel);

        Intent downloads = new Intent(DownloadManager.ACTION_VIEW_DOWNLOADS);
        downloads.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        PendingIntent tap = PendingIntent.getActivity(
                context, 0, downloads,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification notification = new Notification.Builder(context, UPDATE_CHANNEL_ID)
                .setContentTitle(title)
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentIntent(tap)
                .setAutoCancel(true)
                .build();
        nm.notify(UPDATE_NOTIFICATION_ID, notification);
        return true;
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
    private void ensureOperationScheduler() {
        if (operationScheduler == null || operationScheduler.isShutdown()) {
            operationScheduler = Executors.newSingleThreadScheduledExecutor(
                    new ThreadFactory() {
                        @Override
                        public Thread newThread(Runnable r) {
                            Thread t = new Thread(r, "muse-bridge-op-timer");
                            t.setDaemon(true);
                            return t;
                        }
                    });
        }
    }

    /** v2.1 fix (CONS-023 P2, D6): the keys the service accepts.
     * Canonical set is BridgeAccessibilityService.pressKey()
     * (case-insensitive chain). Checked before the /press_key
     * notification hook, so an unknown key can never reach a
     * notification label. Fail-closed like KNOWN_NODE_ACTIONS. */
    private static boolean isKnownPressKey(String key) {
        return "HOME".equalsIgnoreCase(key)
                || "BACK".equalsIgnoreCase(key)
                || "RECENTS".equalsIgnoreCase(key)
                || "POWER".equalsIgnoreCase(key)
                || "DPAD_UP".equalsIgnoreCase(key)
                || "DPAD_DOWN".equalsIgnoreCase(key)
                || "DPAD_LEFT".equalsIgnoreCase(key)
                || "DPAD_RIGHT".equalsIgnoreCase(key)
                || "DPAD_CENTER".equalsIgnoreCase(key);
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
     * where the state check alone would pass. */
    private long noteOperation(long lifecycleGeneration, String label) {
        synchronized (operationLock) {
            // v2.1 fix (CONS-024 P1-NEW): a request accepted in
            // an old lifecycle must not start a session in the
            // new one — refuse before touching any tracker state.
            if (lifecycleGeneration != serverLifecycleGeneration) {
                return -1;
            }
            if (state != ServerState.RUNNING && state != ServerState.STARTING) {
                return -1;
            }
            if (!operationActive) {
                operationGeneration++;
                operationActive = true;
                // A stale "操作終了" from the previous session must
                // not sit next to the new "操作中".
                NotificationManager nm = (NotificationManager)
                        context.getSystemService(Context.NOTIFICATION_SERVICE);
                if (nm != null) {
                    nm.cancel(OPERATION_END_NOTIFICATION_ID);
                }
            }
            operationLastActivityMs = android.os.SystemClock.elapsedRealtime();
            operationInFlight++;
            if (!postOperationRunning(label)) {
                BridgeLog.logThrottled("op-notify-disabled", "op-notify-disabled",
                        "operation notifications disabled");
            }
            scheduleAutoEndLocked();
            return operationGeneration;
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
    private void operationFinished(long generation) {
        synchronized (operationLock) {
            if (generation != operationGeneration) {
                return;
            }
            if (operationInFlight > 0) {
                operationInFlight--;
            }
            if (operationInFlight == 0 && operationActive) {
                operationLastActivityMs = android.os.SystemClock.elapsedRealtime();
                scheduleAutoEndLocked();
            }
        }
    }

    /** Explicit end of the current operation session (POST
     * /operation_end). Returns true when a session was active
     * (and "操作終了" was posted), false when idle. */
    private boolean endOperation(long lifecycleGeneration) {
        synchronized (operationLock) {
            // v2.1 fix (CONS-024 P1-NEW): same lifecycle rule as
            // noteOperation() — a stale /operation_end accepted
            // before stop() must not end a session that began
            // after the restart.
            if (lifecycleGeneration != serverLifecycleGeneration) {
                return false;
            }
            return endOperationLocked();
        }
    }

    /** Lock-held half of endOperation(); also used by the auto-end
     * task. Caller must hold operationLock. */
    private boolean endOperationLocked() {
        if (!operationActive) {
            return false;
        }
        operationActive = false;
        // v2.1 fix (CONS-023 P1): in-flight requests of the ended
        // session are orphaned — their finally calls are stale
        // (generation mismatch) and ignored. Reset the counter
        // here so it cannot leak into the next session.
        operationInFlight = 0;
        if (operationAutoEndFuture != null) {
            operationAutoEndFuture.cancel(false);
            operationAutoEndFuture = null;
        }
        NotificationManager nm = (NotificationManager)
                context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) {
            nm.cancel(OPERATION_NOTIFICATION_ID);
        }
        postOperationEnded();
        return true;
    }

    /** (Re)schedules the auto-end task for lastActivity + timeout.
     * Caller must hold operationLock. A delay already in the past
     * (a long operation started more than the timeout ago) falls
     * back to one full timeout — the task re-checks the in-flight
     * count when it fires, so it cannot end a session mid-run. */
    private void scheduleAutoEndLocked() {
        if (operationAutoEndFuture != null) {
            operationAutoEndFuture.cancel(false);
            operationAutoEndFuture = null;
        }
        if (operationScheduler == null || operationScheduler.isShutdown()) {
            return;
        }
        long delay = operationLastActivityMs + OPERATION_AUTO_END_TIMEOUT_MS
                - android.os.SystemClock.elapsedRealtime();
        if (delay < 0) {
            delay = OPERATION_AUTO_END_TIMEOUT_MS;
        }
        try {
            operationAutoEndFuture = operationScheduler.schedule(
                    operationAutoEndTask, delay, TimeUnit.MILLISECONDS);
        } catch (RejectedExecutionException ignored) {
            // Scheduler shut down concurrently with stop(); the
            // session simply has no auto-end — stop() clears the
            // notification anyway.
        }
    }

    /** Posts or updates the ongoing "操作中" notification.
     * Returns false when notifications are not permitted. */
    private boolean postOperationRunning(String label) {
        NotificationManager nm = (NotificationManager)
                context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null || !nm.areNotificationsEnabled()) {
            return false;
        }
        ensureOperationChannel(nm);
        Notification notification = new Notification.Builder(context, OPERATION_CHANNEL_ID)
                .setContentTitle("操作中")
                .setContentText(label)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentIntent(operationTapIntent(OPERATION_NOTIFICATION_ID))
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .build();
        nm.notify(OPERATION_NOTIFICATION_ID, notification);
        return true;
    }

    /** Posts the one-shot "操作終了" notification. It stays in
     * the shade until swiped (setAutoCancel, no auto-dismiss
     * timer); the fixed ID is reused, so at most one ever sits
     * there. Returns false when notifications are not permitted. */
    private boolean postOperationEnded() {
        NotificationManager nm = (NotificationManager)
                context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null || !nm.areNotificationsEnabled()) {
            return false;
        }
        ensureOperationChannel(nm);
        Notification notification = new Notification.Builder(context, OPERATION_CHANNEL_ID)
                .setContentTitle("操作終了")
                .setContentText("Muse の操作が終わりました")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentIntent(operationTapIntent(OPERATION_END_NOTIFICATION_ID))
                .setAutoCancel(true)
                .build();
        nm.notify(OPERATION_END_NOTIFICATION_ID, notification);
        return true;
    }

    /** Idempotent channel creation, same pattern as
     * postUpdateNotification. IMPORTANCE_DEFAULT: the first
     * "操作中" post of a session alerts once (the user asked for
     * visibility); updates are silenced by setOnlyAlertOnce. */
    private void ensureOperationChannel(NotificationManager nm) {
        NotificationChannel channel = new NotificationChannel(
                OPERATION_CHANNEL_ID, "Muse Bridge の操作",
                NotificationManager.IMPORTANCE_DEFAULT);
        channel.setDescription("Muse の操作中・操作終了のお知らせ。");
        nm.createNotificationChannel(channel);
    }

    /** Tap target for both operation notifications: the Bridge
     * app's own MainActivity, and nothing else. It must never
     * launch another app — a notification PendingIntent can start
     * activities from the background, so using it for anything
     * else would bypass the background-activity-start
     * restriction. */
    private PendingIntent operationTapIntent(int requestCode) {
        Intent intent = new Intent(context, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        return PendingIntent.getActivity(context, requestCode, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    /** Cancels both operation notifications (start/stop cleanup;
     * notifications survive process death). */
    private void cancelOperationNotifications() {
        NotificationManager nm = (NotificationManager)
                context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) {
            return;
        }
        nm.cancel(OPERATION_NOTIFICATION_ID);
        nm.cancel(OPERATION_END_NOTIFICATION_ID);
    }

    /** Display label for a package, for the /launch_app hook.
     * Falls back to the package name when the package is not
     * installed or has no label (never throws). */
    private String operationAppLabel(String pkg) {
        try {
            CharSequence label = context.getPackageManager().getApplicationLabel(
                    context.getPackageManager().getApplicationInfo(pkg, 0));
            if (label != null && label.length() > 0) {
                return label.toString();
            }
        } catch (Exception ignored) {
            // Not installed (launchApp reports 422) — fall through.
        }
        return pkg;
    }

    /**
     * Device condition (decision 62, v1.9.6): battery, network
     * transport, and interactivity, read on the worker thread (not
     * the a11y main thread). Best effort only — unreadable values
     * fall back to -1 / false / "unknown" and never fail /state.
     * battery_pct=-1 marks "unknown": charging=false alone means
     * "not charging" only when battery_pct >= 0.
     */
    private JSONObject deviceState() {
        int batteryPct = -1;
        boolean charging = false;
        try {
            IntentFilter filter = new IntentFilter(Intent.ACTION_BATTERY_CHANGED);
            Intent battery = context.registerReceiver(null, filter);
            if (battery != null) {
                int level = battery.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                int scale = battery.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
                if (level >= 0 && scale > 0) {
                    batteryPct = Math.round(level * 100f / scale);
                    if (batteryPct < 0) {
                        batteryPct = 0;
                    }
                    if (batteryPct > 100) {
                        batteryPct = 100;
                    }
                }
                int plugged = battery.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0);
                charging = plugged != 0;
            }
        } catch (Exception ignored) {
            // Best effort: leave the fallbacks.
        }
        String network = networkTransport();
        boolean interactive = false;
        try {
            PowerManager pm = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                interactive = pm.isInteractive();
            }
        } catch (Exception ignored) {
            // Best effort: leave the fallback.
        }
        try {
            return new JSONObject()
                    .put("battery_pct", batteryPct)
                    .put("charging", charging)
                    .put("network", network)
                    .put("interactive", interactive);
        } catch (org.json.JSONException e) {
            // Unreachable for these put() calls, but never fail /state.
            return new JSONObject();
        }
    }

    /**
     * The default network's transport type (not internet
     * reachability). When the default network is a VPN, use its
     * underlying network's transport (wifi > mobile > other):
     * the phone's Tailscale VPN is the default network, so
     * without this the field would almost always read "other".
     */
    private String networkTransport() {
        try {
            ConnectivityManager cm = (ConnectivityManager)
                    context.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return "unknown";
            }
            Network active = cm.getActiveNetwork();
            if (active == null) {
                return "none";
            }
            NetworkCapabilities caps = cm.getNetworkCapabilities(active);
            if (caps == null) {
                return "unknown";
            }
            return transportOf(caps, cm);
        } catch (Exception ignored) {
            return "unknown";
        }
    }

    private String transportOf(NetworkCapabilities caps, ConnectivityManager cm) {
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
            return "wifi";
        }
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) {
            return "mobile";
        }
        if (caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
            // A VPN rides on an underlying network: scan the other
            // networks for it (getUnderlyingNetworks is a system API
            // and not callable here). Prefer wifi, then mobile.
            try {
                String best = "other";
                for (Network n : cm.getAllNetworks()) {
                    NetworkCapabilities uc = cm.getNetworkCapabilities(n);
                    if (uc == null
                            || uc.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                        continue;
                    }
                    if (uc.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                        return "wifi";
                    }
                    if (uc.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) {
                        best = "mobile";
                    }
                }
                return best;
            } catch (Exception ignored) {
                // Best effort: fall through to "other".
            }
        }
        return "other";
    }

    private static void requireService(BridgeAccessibilityService svc) throws ServiceUnavailable {
        if (svc == null) {
            throw new ServiceUnavailable("accessibility service not enabled");
        }
    }

    /** P0-3 (v2.0): the current startup mode. a11y on wins
     * (FULL_ACCESS); a11y off falls back to the persisted
     * MINIMAL / BANKING_SAFE. */
    private String currentMode(boolean a11yOn) {
        if (a11yOn) return "FULL_ACCESS";
        return MainActivity.getPersistedMode(context);
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
    private JSONObject buildModules(boolean a11yOn, boolean listenerGranted,
                                    boolean listenerConnected,
                                    List<String> tailscaleIps,
                                    List<String> boundTailscaleIps)
            throws org.json.JSONException {
        String http = state == ServerState.STARTING ? "RECOVERING" : "HEALTHY";
        String tailscale;
        if (tailscaleIps.isEmpty()) {
            tailscale = "UNAVAILABLE";
        } else {
            boolean anyBound = false;
            for (String ip : tailscaleIps) {
                if (boundTailscaleIps.contains(ip)) {
                    anyBound = true;
                    break;
                }
            }
            tailscale = anyBound ? "HEALTHY" : "DEGRADED";
        }
        String accessibility = a11yOn ? "ENABLED" : "DISABLED";
        String notificationAccess;
        if (!listenerGranted) {
            notificationAccess = "NOT_GRANTED";
        } else if (!listenerConnected) {
            notificationAccess = "GRANTED_NOT_CONNECTED";
        } else {
            notificationAccess = "GRANTED";
        }
        return new JSONObject()
                .put("http", http)
                .put("tailscale", tailscale)
                .put("accessibility", accessibility)
                .put("notification_access", notificationAccess)
                .put("termux_ssh", "UNKNOWN");
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
    private JSONObject buildCapabilities(boolean a11yOn, boolean listenerGranted,
                                         boolean listenerConnected)
            throws org.json.JSONException {
        boolean notificationRead = listenerGranted && listenerConnected;
        NotificationManager nm = (NotificationManager)
                context.getSystemService(Context.NOTIFICATION_SERVICE);
        boolean notificationPost = nm != null && nm.areNotificationsEnabled();
        boolean canInstallApk =
                context.getPackageManager().canRequestPackageInstalls();
        return new JSONObject()
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
                .put("install_apk_permitted", canInstallApk);
    }

    /** P0-3 (v2.0): the overall status for /health. CRITICAL is
     * never emitted here — a broken HTTP server returns nothing
     * and broken auth returns 401 before this handler runs, so
     * CRITICAL is a VM-side verdict (see the P0 spec).
     * CONS-029 P1-3 fix: a tailscale DEGRADED (current IP
     * exists but the server is not bound to it) degrades the
     * overall status too. */
    private String overallStatus(JSONObject modules) {
        if (state == ServerState.STARTING) return "RECOVERING";
        if ("UNAVAILABLE".equals(modules.optString("tailscale"))
                || "DEGRADED".equals(modules.optString("tailscale"))
                || "NOT_GRANTED".equals(modules.optString("notification_access"))
                || "GRANTED_NOT_CONNECTED".equals(modules.optString("notification_access"))) {
            return "DEGRADED";
        }
        return "HEALTHY";
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
    public synchronized List<String> boundTailscaleIps() {
        List<String> out = new ArrayList<>();
        for (ServerSocket ss : listeners) {
            InetAddress a = ss.getInetAddress();
            if (a != null && !a.isLoopbackAddress() && a.getHostAddress() != null) {
                out.add(a.getHostAddress());
            }
        }
        return out;
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
    private boolean openUri(String uriString) {
        if (uriString == null || uriString.isEmpty()) {
            throw new IllegalArgumentException("uri must not be empty");
        }
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(uriString));
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(intent);
        return true;
    }

    /** P0-4 (v2.0): poll offsets (ms after the launch request)
     * for the foreground check. ~500 ms first (a fast launch),
     * then ~1 s, ~2 s, and a final ~3 s check; the whole
     * verification is bounded to ~3 s so the HTTP round trip
     * stays within ~3–4 s. A slow app must not be failed on a
     * short timeout alone — the full window is used before
     * reporting `unconfirmed`. */
    private static final long[] LAUNCH_VERIFY_OFFSETS_MS = {500, 1000, 2000, 3000};
    private static final long LAUNCH_VERIFY_MAX_MS = 3000;

    /** P0-4 fix (CONS-027 P1 #1): the pre-request foreground
     * read (wasForeground) is informational only — it must not
     * let a main-thread stall delay the launch itself. It waits
     * at most this long; a timeout just means "not observed". */
    private static final long LAUNCH_PRE_READ_TIMEOUT_MS = 500;

    /** P0-4 deny list (Saardeim decision 2026-11-06 20:16,
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
    private static final Set<String> LAUNCH_DENY_LIST =
            new HashSet<>(Arrays.asList(
                    "com.kasikorn.retail.mbanking.wap", // K PLUS (KBank)
                    "com.bbl.mobilebanking", // Bualuang mBanking
                    "com.scb.phone", // SCB EASY
                    "com.krungsri.kma")); // KMA (Krungsri)

    /** P0-4: read the foreground package, never throwing. A
     * failure to read (service timeout, no active window) is a
     * missed observation, not a launch failure — the caller keeps
     * polling and only claims `launched` on a positive match.
     * P0-4 fix (CONS-027 P1 #1): `timeoutMs` bounds how long
     * this read may wait — the caller passes the remaining
     * verification budget, so one main-thread stall cannot
     * overrun the ~3 s window. */
    private String foregroundPackageSafe(BridgeAccessibilityService svc,
            long timeoutMs) {
        if (svc == null) {
            return null;
        }
        try {
            return svc.getForegroundPackage(timeoutMs);
        } catch (Exception e) {
            BridgeLog.log("launch_fg_read_failed",
                    e.getClass().getSimpleName());
            return null;
        }
    }

    /** P0-4: build the launch result body. `foregroundConfirmed`
     * is null when verification was unavailable (a11y off). */
    private static JSONObject launchResultJson(String pkg, boolean ok,
            String status, String reason, Boolean foregroundConfirmed,
            String verification) throws org.json.JSONException {
        JSONObject j = new JSONObject()
                .put("ok", ok)
                .put("status", status)
                .put("accepted", true)
                .put("package", pkg)
                .put("launch_requested", true)
                .put("foreground_confirmed", foregroundConfirmed == null
                        ? JSONObject.NULL : foregroundConfirmed)
                .put("verification", verification);
        if (reason != null) {
            j.put("reason", reason);
        }
        return j;
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
    private JSONObject verifyLaunchForeground(String pkg,
            BridgeAccessibilityService svc, long requestTimeMs,
            boolean wasForeground) throws org.json.JSONException {
        if (svc == null) {
            BridgeLog.log("launch_unverified", pkg);
            return launchResultJson(pkg, false, "unconfirmed",
                    "VERIFICATION_UNAVAILABLE", null, "none");
        }
        long deadlineMs = requestTimeMs + LAUNCH_VERIFY_MAX_MS;
        for (long offset : LAUNCH_VERIFY_OFFSETS_MS) {
            long wait = requestTimeMs + offset
                    - android.os.SystemClock.elapsedRealtime();
            if (wait > 0) {
                try {
                    Thread.sleep(wait);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
            // Remaining budget bounds this read: a stalled main
            // thread cannot push the response past the deadline.
            long remainingMs = deadlineMs
                    - android.os.SystemClock.elapsedRealtime();
            if (remainingMs <= 0) {
                break;
            }
            if (pkg.equals(foregroundPackageSafe(svc, remainingMs))) {
                BridgeLog.log("launch_confirmed", wasForeground
                        ? pkg + " (already foreground)" : pkg);
                return launchResultJson(pkg, true, "launched",
                        null, Boolean.TRUE, "accessibility");
            }
            if (android.os.SystemClock.elapsedRealtime() - requestTimeMs
                    >= LAUNCH_VERIFY_MAX_MS) {
                break;
            }
        }
        BridgeLog.log("launch_unconfirmed", pkg);
        return launchResultJson(pkg, false, "unconfirmed",
                "FOREGROUND_NOT_CONFIRMED", Boolean.FALSE, "accessibility");
    }

    /**
     * Launches an app by package name (decision 69, v1.9.12;
     * moved from BridgeAccessibilityService in P0-1, v2.0).
     * Runs on this server's own application context, so it
     * needs no accessibility service — the same reason /open
     * works with a11y off. Returns false when Android refuses
     * the start (background-activity-start restriction); throws
     * IllegalArgumentException when the package is not installed
     * or has no launcher activity. P0-4: true means only that
     * startActivity() was ISSUED without throwing — it does NOT
     * mean the app opened (the BAL restriction can swallow the
     * start silently). The caller verifies the foreground
     * separately (verifyLaunchForeground) and reports the
     * truthful result.
     */
    private boolean launchApp(String pkg) {
        if (pkg == null || pkg.isEmpty()) {
            throw new IllegalArgumentException("package must not be empty");
        }
        Intent intent = context.getPackageManager().getLaunchIntentForPackage(pkg);
        if (intent == null) {
            throw new IllegalArgumentException(
                    "package not installed or has no launcher: " + pkg);
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            context.startActivity(intent);
            return true;
        } catch (ActivityNotFoundException | SecurityException e) {
            BridgeLog.log("launch_blocked", pkg);
            return false;
        }
    }

    /**
     * Install-apk job (decision 75, v1.9.16). Runs on its own
     * thread: downloads the APK when a URL was given (into the
     * app's private cache, so no storage permission is needed),
     * then hands it to PackageInstaller and commits. The
     * commit shows the system confirmation dialog; the result
     * (installed / failed) arrives on installReceiver. The
     * downloaded file is deleted after the commit. Never logs
     * the APK bytes, only the URL and sizes.
     */
    private void runInstall(String url, String apkPath) {
        installRunning = true;
        File apk = null;
        boolean downloaded = false;
        try {
            if (url != null) {
                installStatus = "downloading";
                installDetail = url;
                BridgeLog.log("install_download_start", url);
                String name = url.substring(url.lastIndexOf('/') + 1);
                if (name.isEmpty() || name.contains("..")) {
                    name = "app.apk";
                }
                File dir = new File(context.getCacheDir(), "install");
                dir.mkdirs();
                apk = new File(dir, name);
                HttpURLConnection conn =
                        (HttpURLConnection) new URL(url).openConnection();
                conn.setConnectTimeout(15000);
                conn.setReadTimeout(30000);
                conn.connect();
                if (conn.getResponseCode() != HttpURLConnection.HTTP_OK) {
                    throw new java.io.IOException(
                            "download HTTP " + conn.getResponseCode());
                }
                long total = 0;
                try (InputStream in = conn.getInputStream();
                     OutputStream out = new FileOutputStream(apk)) {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) != -1) {
                        out.write(buf, 0, n);
                        total += n;
                    }
                }
                downloaded = true;
                BridgeLog.log("install_downloaded", total + " bytes");
            } else {
                apk = new File(apkPath);
            }
            installStatus = "installing";
            installDetail = apk.getName();
            PackageInstaller installer =
                    context.getPackageManager().getPackageInstaller();
            PackageInstaller.SessionParams params =
                    new PackageInstaller.SessionParams(
                            PackageInstaller.SessionParams.MODE_FULL_INSTALL);
            int sessionId = installer.createSession(params);
            PackageInstaller.Session session = installer.openSession(sessionId);
            try (OutputStream out = session.openWrite("package", 0, -1);
                 InputStream in = new FileInputStream(apk)) {
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) != -1) {
                    out.write(buf, 0, n);
                }
                session.fsync(out);
            }
            Intent resultIntent = new Intent(INSTALL_RESULT_ACTION);
            resultIntent.setPackage(context.getPackageName());
            PendingIntent pi = PendingIntent.getBroadcast(context, sessionId,
                    resultIntent, PendingIntent.FLAG_UPDATE_CURRENT
                            | PendingIntent.FLAG_MUTABLE);
            session.commit(pi.getIntentSender());
            session.close();
            if (downloaded) {
                apk.delete();
            }
            installStatus = "committed";
            installDetail = "waiting for the install confirmation";
            BridgeLog.log("install_committed", apk.getName());
        } catch (Exception e) {
            installStatus = "failed";
            installDetail = e.getMessage() == null
                    ? e.getClass().getSimpleName() : e.getMessage();
            installRunning = false;
            BridgeLog.log("install_failed", installDetail);
        }
    }

    private static class ServiceUnavailable extends Exception {
        ServiceUnavailable(String msg) {
            super(msg);
        }
    }

    private static class HeaderTooLarge extends Exception {
        HeaderTooLarge() {
            super("header too large");
        }
    }

    private static JSONObject errorJson(String message) throws org.json.JSONException {
        return new JSONObject().put("ok", false).put("error", message);
    }

    /** P0-1 (v2.0): the structured "a11y off" body. `detail`
     * names the endpoint that needs the service. The `error`
     * field is kept alongside the new fields so
     * bridge_protocol.parse_error_body keeps working (it
     * requires "ok" and "error"). */
    private static JSONObject unavailableJson(String detail) throws org.json.JSONException {
        return new JSONObject()
                .put("ok", false)
                .put("status", "unavailable")
                .put("reason", "ACCESSIBILITY_DISABLED")
                .put("required_mode", "FULL_ACCESS")
                .put("detail", detail)
                .put("error", "accessibility service not enabled");
    }

    /** A-4: unknown JSON fields are rejected (400 via JSONException)
     * so the canonical payload hash is computed over exactly the
     * fields the server understands. */
    private static void rejectUnknownFields(JSONObject j, String... allowed)
            throws org.json.JSONException {
        java.util.Set<String> ok = new java.util.HashSet<>();
        for (String a : allowed) {
            ok.add(a);
        }
        java.util.Iterator<String> keys = j.keys();
        while (keys.hasNext()) {
            String key = keys.next();
            if (!ok.contains(key)) {
                throw new org.json.JSONException("unknown field: " + key);
            }
        }
    }

    private static long remainingMs(long deadline) {
        long rem = deadline - android.os.SystemClock.elapsedRealtime();
        return rem < 0 ? 0 : rem;
    }

    private static void setPhaseTimeout(Socket conn, int phaseMs, long deadline)
            throws java.net.SocketException {
        long rem = remainingMs(deadline);
        int ms = (int) Math.min((long) phaseMs, rem);
        if (ms <= 0) {
            ms = 1;
        }
        conn.setSoTimeout(ms);
    }

    private String readHead(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        int prev = -1;
        int b;
        while ((b = in.read()) != -1) {
            bos.write(b);
            if (bos.size() > MAX_HEADER_BYTES) {
                throw new HeaderTooLarge();
            }
            if (prev == '\r' && b == '\n') {
                byte[] bytes = bos.toByteArray();
                int n = bytes.length;
                if (n >= 4 && bytes[n - 4] == '\r' && bytes[n - 3] == '\n'
                        && bytes[n - 2] == '\r' && bytes[n - 1] == '\n') {
                    break;
                }
            }
            prev = b;
        }
        return bos.toString("ISO-8859-1");
    }

    private void sendJson(OutputStream out, int status, JSONObject json) throws Exception {
        byte[] body = json.toString().getBytes(StandardCharsets.UTF_8);
        sendBytes(out, status, "application/json", body);
    }

    private void sendBytes(OutputStream out, int status, String contentType, byte[] body) throws Exception {
        sendBytes(out, status, contentType, body, "");
    }

    private void sendBytes(OutputStream out, int status, String contentType, byte[] body,
                           String extraHeaders) throws Exception {
        String head = "HTTP/1.1 " + status + " " + reason(status) + "\r\n"
                + "Content-Type: " + contentType + "\r\n"
                + "Content-Length: " + body.length + "\r\n"
                + extraHeaders
                + "Connection: close\r\n\r\n";
        out.write(head.getBytes(StandardCharsets.US_ASCII));
        out.write(body);
        out.flush();
    }

    private static String queryParam(String query, String name) {
        if (query == null || query.isEmpty()) {
            return null;
        }
        for (String pair : query.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0 && pair.substring(0, eq).equals(name)) {
                return pair.substring(eq + 1);
            }
        }
        return null;
    }

    private static float parseFloat(String value, float fallback) {
        if (value == null) {
            return fallback;
        }
        try {
            return Float.parseFloat(value);
        } catch (NumberFormatException ignored) {
            return fallback;
        }
    }

    private static int parseInt(String value, int fallback) {
        if (value == null) {
            return fallback;
        }
        try {
            return Integer.parseInt(value);
        } catch (NumberFormatException ignored) {
            return fallback;
        }
    }

    private static long parseLong(String value, long fallback) {
        if (value == null) {
            return fallback;
        }
        try {
            return Long.parseLong(value);
        } catch (NumberFormatException ignored) {
            return fallback;
        }
    }

    private String reason(int status) {
        switch (status) {
            case 200: return "OK";
            case 400: return "Bad Request";
            case 401: return "Unauthorized";
            case 404: return "Not Found";
            case 408: return "Request Timeout";
            case 409: return "Conflict";
            case 410: return "Gone";
            case 413: return "Content Too Large";
            case 422: return "Unprocessable Entity";
            case 431: return "Request Header Fields Too Large";
            case 500: return "Internal Server Error";
            case 503: return "Service Unavailable";
            default: return "";
        }
    }
}
