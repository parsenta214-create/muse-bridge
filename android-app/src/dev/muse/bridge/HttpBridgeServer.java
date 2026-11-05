package dev.muse.bridge;

import android.app.DownloadManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.PowerManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.Semaphore;
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
 *   POST /notify          {"title":"..","text":".."} -> posts a notification
 *                         (v1.3; tap opens Downloads so the user can tap
 *                         the pushed APK to install it)
 */
public class HttpBridgeServer {

    private static final String TAG = "MuseBridge";
    public static final int PORT = 8080;
    private static final String UPDATE_CHANNEL_ID = "muse-bridge-updates";
    private static final int UPDATE_NOTIFICATION_ID = 2;

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

    public HttpBridgeServer(int port, TokenProvider tokens, Context context) {
        this.port = port;
        this.tokens = tokens;
        this.context = context.getApplicationContext();
        // v1.9.3: the debug log reads the previous run's tail and
        // opens the file once, when the server is created.
        BridgeLog.init(this.context);
    }

    /** Binds the listeners and starts one dedicated accept thread per
     * listener. Returns true only when at least one socket is bound.
     * A second start after STOPPED creates a fresh worker pool. */
    public synchronized boolean start() {
        if (state == ServerState.RUNNING || state == ServerState.STARTING) {
            return state == ServerState.RUNNING;
        }
        state = ServerState.STARTING;
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
                                int st = handle(conn);
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

    private int handle(Socket conn) {
        String method = "?";
        String path = "?";
        int status = 500;
        // A-1 (v1.9): per-request monotonic deadline; each phase gets
        // the smaller of its own timeout and the remaining total time.
        long deadline = android.os.SystemClock.elapsedRealtime() + TOTAL_TIMEOUT_MS;
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
                status = 200;
                sendJson(out, status, new JSONObject()
                        .put("ok", true)
                        .put("service", "muse-bridge")
                        .put("version", 1)
                        .put("app_version", AppVersion.name(context))
                        .put("app_version_code", AppVersion.code(context))
                        .put("accessibility", svc != null)
                        .put("notification_listener",
                                BridgeNotificationListener.isAccessGranted(context)));
            } else if ("/capture_screen".equals(path)) {
                if (svc == null) {
                    status = 503;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "accessibility service not enabled"));
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
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "accessibility service not enabled"));
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
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", "accessibility service not enabled"));
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
            } else if ("/click_node".equals(path) && "POST".equals(method)) {
                // Phase 2: click by node id (tree_hash from the /state
                // the client used). 409 when the screen moved on.
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                try {
                    BridgeAccessibilityService.ActionOutcome o =
                            svc.clickNode(j.getString("node_id"), j.getInt("tree_hash"));
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
                try {
                    BridgeAccessibilityService.ActionOutcome o = svc.typeNode(j.getString("node_id"),
                            j.getInt("tree_hash"), j.getString("text"));
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
                    try {
                        BridgeAccessibilityService.ActionOutcome o = svc.scrollNode(
                                j.getString("node_id"), j.getInt("tree_hash"), direction);
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
                try {
                    BridgeAccessibilityService.ActionOutcome o = svc.nodeAction(
                            j.getString("node_id"), j.getInt("tree_hash"),
                            j.getString("action"), args);
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
                // (background-activity-start restriction) — fall back
                // to tapping the home-screen icon.
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                try {
                    boolean ok = svc.launchApp(j.getString("package"));
                    status = ok ? 200 : 503;
                    sendJson(out, status, new JSONObject().put("ok", ok)
                            .put("error", ok ? JSONObject.NULL
                                    : "launch blocked by the system"));
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
                try {
                    boolean ok = openUri(j.getString("uri"));
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
                    BridgeAccessibilityService.BatchResult br = svc.runBatch(
                            j.getString("batch_id"), protocolVersion,
                            j.getInt("tree_hash"), actions);
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
                boolean ok = svc.tap(j.getInt("x"), j.getInt("y"));
                status = 200;
                sendJson(out, status, new JSONObject().put("ok", ok));
            } else if ("/swipe".equals(path) && "POST".equals(method)) {
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                boolean ok = svc.swipe(j.getInt("x1"), j.getInt("y1"),
                        j.getInt("x2"), j.getInt("y2"),
                        j.optLong("duration_ms", 300));
                status = 200;
                sendJson(out, status, new JSONObject().put("ok", ok));
            } else if ("/type_text".equals(path) && "POST".equals(method)) {
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                boolean ok = svc.typeText(j.getString("text"));
                status = ok ? 200 : 422;
                sendJson(out, status, new JSONObject().put("ok", ok)
                        .put("error", ok ? JSONObject.NULL
                                : "no focused editable field — tap the field first"));
            } else if ("/press_key".equals(path) && "POST".equals(method)) {
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                boolean ok = svc.pressKey(j.getString("key"));
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
            status = 503;
            try {
                sendJson(conn.getOutputStream(), status, new JSONObject()
                        .put("ok", false).put("error", e.getMessage()));
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
