package dev.muse.bridge;

import android.app.DownloadManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

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
 *   POST /click           {"x":..,"y":..}
 *   POST /click_node      {"node_id":"..","tree_hash":..} (v1.5)
 *   POST /type_node       {"node_id":"..","tree_hash":..,"text":".."} (v1.5)
 *   POST /batch           {"batch_id":"..","tree_hash":..,"actions":[..]} (v1.5)
 *   POST /swipe           {"x1":..,"y1":..,"x2":..,"y2":..,"duration_ms":..}
 *   POST /type_text       {"text":".."}
 *   POST /press_key       {"key":"HOME"|"BACK"|"RECENTS"|"POWER"}
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

    private final int port;
    private final TokenProvider tokens;
    private final Context context;
    private final ExecutorService pool = Executors.newFixedThreadPool(4);
    private final List<ServerSocket> listeners = new ArrayList<>();
    private volatile boolean running = false;

    public HttpBridgeServer(int port, TokenProvider tokens, Context context) {
        this.port = port;
        this.tokens = tokens;
        this.context = context.getApplicationContext();
    }

    public void start() {
        running = true;
        List<InetAddress> addrs = bindAddresses();
        for (InetAddress addr : addrs) {
            try {
                ServerSocket ss = new ServerSocket();
                ss.setReuseAddress(true);
                ss.bind(new InetSocketAddress(addr, port));
                listeners.add(ss);
                final ServerSocket sock = ss;
                pool.execute(new Runnable() {
                    @Override
                    public void run() {
                        acceptLoop(sock);
                    }
                });
                Log.i(TAG, "listening on " + addr.getHostAddress() + ":" + port);
            } catch (Exception e) {
                Log.w(TAG, "cannot bind " + addr.getHostAddress() + ": " + e.getMessage());
            }
        }
    }

    public void stop() {
        running = false;
        for (ServerSocket ss : listeners) {
            try {
                ss.close();
            } catch (Exception ignored) {
            }
        }
        listeners.clear();
        pool.shutdownNow();
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
        while (running) {
            try {
                final Socket conn = ss.accept();
                pool.execute(new Runnable() {
                    @Override
                    public void run() {
                        handle(conn);
                    }
                });
            } catch (Exception e) {
                if (running) {
                    Log.w(TAG, "accept failed: " + e.getMessage());
                }
            }
        }
    }

    private void handle(Socket conn) {
        String method = "?";
        String path = "?";
        int status = 500;
        try {
            conn.setSoTimeout(15000);
            InputStream in = conn.getInputStream();
            OutputStream out = conn.getOutputStream();

            String head = readHead(in);
            if (head == null || head.isEmpty()) {
                return;
            }
            String[] lines = head.split("\r\n");
            String[] req = lines[0].split(" ");
            if (req.length < 2) {
                return;
            }
            method = req[0];
            String target = req[1];
            int q = target.indexOf('?');
            path = (q >= 0) ? target.substring(0, q) : target;
            String query = (q >= 0) ? target.substring(q + 1) : "";
            String token = null;
            int contentLength = 0;
            for (int i = 1; i < lines.length; i++) {
                String line = lines[i];
                int colon = line.indexOf(':');
                if (colon < 0) {
                    continue;
                }
                String name = line.substring(0, colon).trim();
                String value = line.substring(colon + 1).trim();
                if ("X-Bridge-Token".equalsIgnoreCase(name)) {
                    token = value;
                } else if ("Content-Length".equalsIgnoreCase(name)) {
                    try {
                        contentLength = Integer.parseInt(value);
                    } catch (NumberFormatException ignored) {
                    }
                }
            }
            byte[] body = new byte[0];
            if (contentLength > 0 && contentLength <= 1024 * 1024) {
                body = in.readNBytes(contentLength);
            }

            String expected = tokens.currentToken();
            if (expected == null || !expected.equals(token)) {
                status = 401;
                sendJson(out, status, new JSONObject().put("ok", false).put("error", "unauthorized"));
                return;
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
                        .put("accessibility", svc != null));
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
                            .put("nodes", nodes)
                            .put("focused", st.focusedId == null
                                    ? JSONObject.NULL : st.focusedId)
                            .put("ime_visible", st.imeVisible)
                            .put("event_seq", st.eventSeq)
                            .put("truncated", st.truncated));
                }
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
            } else if ("/click_node".equals(path) && "POST".equals(method)) {
                // Phase 2: click by node id (tree_hash from the /state
                // the client used). 409 when the screen moved on.
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                try {
                    boolean ok = svc.clickNode(j.getString("node_id"), j.getInt("tree_hash"));
                    status = ok ? 200 : 422;
                    sendJson(out, status, new JSONObject().put("ok", ok)
                            .put("error", ok ? JSONObject.NULL : "node not clickable"));
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
                    boolean ok = svc.typeNode(j.getString("node_id"),
                            j.getInt("tree_hash"), j.getString("text"));
                    status = ok ? 200 : 422;
                    sendJson(out, status, new JSONObject().put("ok", ok)
                            .put("error", ok ? JSONObject.NULL : "node not editable"));
                } catch (BridgeAccessibilityService.StaleRevisionException e) {
                    status = 409;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                } catch (BridgeAccessibilityService.NodeNotFoundException e) {
                    status = 422;
                    sendJson(out, status, new JSONObject().put("ok", false)
                            .put("error", e.getMessage()));
                }
            } else if ("/batch".equals(path) && "POST".equals(method)) {
                // Phase 2: several actions in one request (batch_id,
                // tree_hash, actions[]). Re-sent batch_id returns the
                // cached result without re-executing (idempotency).
                JSONObject j = new JSONObject(new String(body, StandardCharsets.UTF_8));
                requireService(svc);
                try {
                    List<BridgeAccessibilityService.BatchAction> actions = new ArrayList<>();
                    JSONArray arr = j.getJSONArray("actions");
                    for (int i = 0; i < arr.length(); i++) {
                        JSONObject a = arr.getJSONObject(i);
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
                            j.getString("batch_id"), j.getInt("tree_hash"), actions);
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
                    status = 200;
                    sendJson(out, status, new JSONObject()
                            .put("ok", true)
                            .put("batch_id", br.batchId)
                            .put("replayed", br.replayed)
                            .put("results", results)
                            .put("stopped_at", br.stoppedAt)
                            .put("stop_reason", br.stopReason.isEmpty()
                                    ? JSONObject.NULL : br.stopReason));
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
        } catch (ServiceUnavailable e) {
            status = 503;
            try {
                sendJson(conn.getOutputStream(), status, new JSONObject()
                        .put("ok", false).put("error", e.getMessage()));
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

    private static void requireService(BridgeAccessibilityService svc) throws ServiceUnavailable {
        if (svc == null) {
            throw new ServiceUnavailable("accessibility service not enabled");
        }
    }

    private static class ServiceUnavailable extends Exception {
        ServiceUnavailable(String msg) {
            super(msg);
        }
    }

    private String readHead(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        int prev = -1;
        int b;
        while ((b = in.read()) != -1) {
            bos.write(b);
            if (prev == '\r' && b == '\n') {
                byte[] bytes = bos.toByteArray();
                int n = bytes.length;
                if (n >= 4 && bytes[n - 4] == '\r' && bytes[n - 3] == '\n'
                        && bytes[n - 2] == '\r' && bytes[n - 1] == '\n') {
                    break;
                }
            }
            prev = b;
            if (bos.size() > 65536) {
                break;
            }
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
            case 401: return "Unauthorized";
            case 404: return "Not Found";
            case 422: return "Unprocessable Entity";
            case 500: return "Internal Server Error";
            case 503: return "Service Unavailable";
            default: return "";
        }
    }
}
