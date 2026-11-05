package dev.muse.bridge;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import android.view.WindowManager;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * AccessibilityService that performs the on-device work for the bridge:
 * screenshots, taps, swipes, text entry, and global keys.
 *
 * The service itself does no networking and holds no credential. The HTTP
 * server (HttpBridgeServer) calls into the live instance.
 */
public class BridgeAccessibilityService extends AccessibilityService {

    private static volatile BridgeAccessibilityService instance;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    /**
     * A-3 (v1.9): the single execution context. Accessibility work
     * runs on the service main thread (Codex Phase A baseline): HTTP
     * worker threads never touch AccessibilityNodeInfo directly, they
     * post to the main thread and wait. runOnMainThread() runs the
     * task directly when already on the main thread, so a main-thread
     * caller (waitForChange inside runBatch) cannot deadlock itself.
     */
    private Handler mainHandler;
    private static final long MAIN_THREAD_TIMEOUT_MS = 20000;

    /** Accessibility work timed out waiting for the main thread (-> 503). */
    public static class ServiceTimeoutException extends Exception {
        public ServiceTimeoutException(String message) {
            super(message);
        }
    }

    private <T> T runOnMainThread(Callable<T> task) throws Exception {
        if (mainHandler == null
                || Looper.myLooper() == Looper.getMainLooper()) {
            return task.call();
        }
        FutureTask<T> future = new FutureTask<>(task);
        mainHandler.post(future);
        try {
            return future.get(MAIN_THREAD_TIMEOUT_MS, TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            future.cancel(true);
            throw new ServiceTimeoutException(
                    "accessibility work timed out on the main thread");
        } catch (ExecutionException e) {
            Throwable cause = e.getCause();
            if (cause instanceof Exception) {
                throw (Exception) cause;
            }
            throw new RuntimeException(cause);
        }
    }

    /** Monotonic snapshot counter (Phase 1): every /state gets a new revision. */
    private final AtomicInteger revision = new AtomicInteger(0);

    /** Caps for the node tree (Phase 1): keeps one /state small and fast. */
    private static final int MAX_NODES = 500;
    private static final int MAX_DEPTH = 25;

    /**
     * Live nodes from the most recent tree walk (Phase 2, redefined
     * in A-3). They are the latest walk's nodes only: no action ever
     * touches them directly — every node action takes a fresh walk
     * first (resolveFresh) and uses that walk's nodes inside the
     * same operation. The map is recycled and rebuilt on every
     * currentState() call; a walk that fails recycles its partial
     * map instead of leaking it.
     */
    private final Map<String, AccessibilityNodeInfo> lastNodes = new LinkedHashMap<>();
    private int lastRevision = -1;
    private int lastTreeHash = 0;

    /** Batch results by batch_id for idempotency (Phase 2, max 50).
     * A-4: the entry carries the payload hash, the lifecycle status,
     * and the expiry; a re-sent id is answered from the entry, never
     * by re-executing. */
    private final Map<String, BatchCacheEntry> batchCache =
            new LinkedHashMap<String, BatchCacheEntry>() {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, BatchCacheEntry> e) {
            return size() > 50;
        }
    };

    /** A-4: batch result lifetime (Codex Phase A initial values). */
    private static final long BATCH_TTL_MS = 10 * 60 * 1000;
    /** After the result expires, the id + hash stay as a tombstone
     * for another TTL; reuse is refused (410) while it lives. */
    private static final long BATCH_TOMBSTONE_MS = 10 * 60 * 1000;

    /** A-4: same batch_id, different payload — the id is in use. */
    public static class BatchMismatchException extends Exception {
        public BatchMismatchException(String message) {
            super(message);
        }
    }

    /** A-4: batch_id was used before but its result expired (tombstone). */
    public static class BatchGoneException extends Exception {
        public BatchGoneException(String message) {
            super(message);
        }
    }

    /** A-4: one cache entry. result is null while RUNNING. */
    private static class BatchCacheEntry {
        String payloadHash = "";
        BatchResult result;
        /** "running", "completed", "partial", or "failed". */
        String status = "running";
        long createdAt = 0;
        long expiresAt = 0;
        long tombstoneUntil = 0;
    }

    /**
     * Phase 3 (v1.6): event engine. Accessibility events arrive in
     * onAccessibilityEvent on the service main thread; the bridge
     * records a bounded, coalesced history that the VM pulls with
     * GET /events. HTTP handlers run on server threads, so every
     * method touching this state is synchronized on the service.
     * Events are a "something happened" signal, not precise state:
     * the VM refetches /state for the true current tree.
     */
    private static final int MAX_EVENTS = 200;
    private static final long COALESCE_WINDOW_MS = 300;
    private final Deque<UiEvent> eventBuffer = new ArrayDeque<>();
    private final AtomicLong eventSeq = new AtomicLong(0);
    /** Seq of the most recently evicted event (0 = nothing evicted). */
    private long lastEventDroppedSeq = 0;

    public static BridgeAccessibilityService getInstance() {
        return instance;
    }

    @Override
    public void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        // A-3: the main-thread handler is the single execution
        // context for accessibility work.
        mainHandler = new Handler(Looper.getMainLooper());
    }

    @Override
    public void onDestroy() {
        instance = null;
        executor.shutdownNow();
        super.onDestroy();
    }

    @Override
    public void onAccessibilityEvent(android.view.accessibility.AccessibilityEvent event) {
        // Phase 3 (v1.6): record a bounded, coalesced history for
        // GET /events. Never let event handling break the service.
        try {
            recordEvent(event);
        } catch (Exception ignored) {
        }
    }

    /**
     * Record one accessibility event, or merge it into the previous
     * record when it is an identical repeat within the coalescing
     * window (CONTENT_CHANGED floods on every layout pass). Only the
     * whitelisted types are recorded; the config already asks for
     * typeAllMask with notificationTimeout 100, so the flood is
     * throttled twice. Password sources are always "[REDACTED]":
     * no password text, before-text, or description ever leaves the
     * phone in an event. The event time is uptimeMillis — monotonic
     * on-device, never compared with VM wall clocks.
     */
    private synchronized void recordEvent(android.view.accessibility.AccessibilityEvent event) {
        int type = event.getEventType();
        if (type != android.view.accessibility.AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
                && type != android.view.accessibility.AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED
                && type != android.view.accessibility.AccessibilityEvent.TYPE_VIEW_CLICKED
                && type != android.view.accessibility.AccessibilityEvent.TYPE_VIEW_FOCUSED
                && type != android.view.accessibility.AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED
                && type != android.view.accessibility.AccessibilityEvent.TYPE_VIEW_SCROLLED
                && type != android.view.accessibility.AccessibilityEvent.TYPE_VIEW_SELECTED) {
            return;
        }
        UiEvent e = new UiEvent();
        e.t = event.getEventTime();
        e.type = android.view.accessibility.AccessibilityEvent.eventTypeToString(type);
        CharSequence pkg = event.getPackageName();
        e.packageName = pkg == null ? "" : pkg.toString();
        CharSequence cls = event.getClassName();
        e.className = cls == null ? "" : cls.toString();
        e.left = -1;
        e.top = -1;
        e.right = -1;
        e.bottom = -1;
        boolean password = false;
        String nodeText = "";
        String nodeDesc = "";
        AccessibilityNodeInfo src = null;
        try {
            src = event.getSource();
            if (src != null) {
                password = src.isPassword();
                Rect b = new Rect();
                src.getBoundsInScreen(b);
                e.left = b.left;
                e.top = b.top;
                e.right = b.right;
                e.bottom = b.bottom;
                CharSequence t = src.getText();
                if (t != null) {
                    nodeText = t.toString().trim();
                }
                CharSequence d = src.getContentDescription();
                if (d != null) {
                    nodeDesc = d.toString().trim();
                }
            }
        } catch (Exception ignored) {
            // Best effort only; the event still records without bounds.
        } finally {
            if (src != null) {
                try {
                    src.recycle();
                } catch (Exception ignored) {
                }
            }
        }
        if (password) {
            e.text = "[REDACTED]";
            e.desc = nodeDesc.isEmpty() ? "" : "[REDACTED]";
        } else {
            StringBuilder sb = new StringBuilder();
            try {
                for (CharSequence c : event.getText()) {
                    if (sb.length() > 0) {
                        sb.append(' ');
                    }
                    sb.append(c);
                }
            } catch (Exception ignored) {
            }
            String text = sb.toString().trim();
            if (text.isEmpty()) {
                text = nodeText;
            }
            e.text = text.length() > 120 ? text.substring(0, 120) : text;
            e.desc = nodeDesc.length() > 120 ? nodeDesc.substring(0, 120) : nodeDesc;
        }
        UiEvent last = eventBuffer.peekLast();
        if (last != null && e.t - last.t <= COALESCE_WINDOW_MS
                && last.type.equals(e.type)
                && last.packageName.equals(e.packageName)
                && last.className.equals(e.className)
                && last.text.equals(e.text)) {
            last.coalesced++;
            return;
        }
        e.seq = eventSeq.incrementAndGet();
        eventBuffer.addLast(e);
        if (eventBuffer.size() > MAX_EVENTS) {
            UiEvent dropped = eventBuffer.removeFirst();
            lastEventDroppedSeq = dropped.seq;
        }
    }

    /**
     * Events after `since` (exclusive), oldest first, capped at
     * `limit`. dropped is true when the buffer evicted events the
     * caller never saw (since is older than the oldest evicted seq),
     * so the caller should refetch /state instead of trusting the
     * gap. treeHash/revision are the latest snapshot's, so the
     * caller can tell whether the screen also moved.
     */
    public synchronized EventsResult getEvents(long since, int limit) {
        EventsResult r = new EventsResult();
        r.latestSeq = eventSeq.get();
        r.dropped = since < lastEventDroppedSeq;
        for (UiEvent e : eventBuffer) {
            if (e.seq > since) {
                r.events.add(e);
            }
            if (r.events.size() >= limit) {
                break;
            }
        }
        r.treeHash = lastTreeHash;
        r.revision = lastRevision;
        return r;
    }

    @Override
    public void onInterrupt() {
    }

    /** Screenshot of the default display as PNG bytes (API 30+). */
    public byte[] takeScreenshotPng() throws Exception {
        return takeScreenshot(1.0f, "png", 100);
    }

    /**
     * Screenshot with optional downscale and JPEG compression (v1.2).
     * scale is clamped to 0.1–1.0; format is "png" or "jpeg"
     * (anything else falls back to PNG); quality 10–100 applies to
     * JPEG only. Full-res PNG stays the default for callers that
     * need every pixel; the VM client uses half-scale JPEG for
     * routine checks because a 1080x2340 wallpaper PNG is ~2 MB
     * and costs ~2 s of transfer over the tunnel.
     */
    public byte[] takeScreenshot(float scale, String format, int quality) throws Exception {
        final CountDownLatch latch = new CountDownLatch(1);
        final byte[][] out = new byte[1][];
        final int[] err = new int[]{-1};
        final float useScale = Math.max(0.1f, Math.min(1.0f, scale));
        final boolean useJpeg = "jpeg".equalsIgnoreCase(format) || "jpg".equalsIgnoreCase(format);
        final int useQuality = Math.max(10, Math.min(100, quality));
        takeScreenshot(Display.DEFAULT_DISPLAY, executor,
                new TakeScreenshotCallback() {
                    @Override
                    public void onSuccess(ScreenshotResult result) {
                        android.hardware.HardwareBuffer hb = null;
                        try {
                            hb = result.getHardwareBuffer();
                            Bitmap hw = Bitmap.wrapHardwareBuffer(hb, result.getColorSpace());
                            if (hw == null) {
                                err[0] = -3;
                                return;
                            }
                            Bitmap sw = hw.copy(Bitmap.Config.ARGB_8888, false);
                            if (useScale < 1.0f) {
                                int w = Math.max(1, Math.round(sw.getWidth() * useScale));
                                int h = Math.max(1, Math.round(sw.getHeight() * useScale));
                                Bitmap small = Bitmap.createScaledBitmap(sw, w, h, true);
                                sw.recycle();
                                sw = small;
                            }
                            ByteArrayOutputStream bos = new ByteArrayOutputStream();
                            if (useJpeg) {
                                sw.compress(Bitmap.CompressFormat.JPEG, useQuality, bos);
                            } else {
                                sw.compress(Bitmap.CompressFormat.PNG, 100, bos);
                            }
                            sw.recycle();
                            out[0] = bos.toByteArray();
                        } catch (Exception e) {
                            err[0] = -2;
                        } finally {
                            if (hb != null) {
                                hb.close();
                            }
                            latch.countDown();
                        }
                    }

                    @Override
                    public void onFailure(int errorCode) {
                        err[0] = errorCode;
                        latch.countDown();
                    }
                });
        if (!latch.await(10, TimeUnit.SECONDS)) {
            throw new Exception("screenshot timed out");
        }
        if (out[0] == null) {
            throw new Exception("screenshot failed (error " + err[0] + ")");
        }
        return out[0];
    }

    private boolean runGesture(Path path, long durationMs) throws Exception {
        GestureDescription desc = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, durationMs))
                .build();
        final CountDownLatch latch = new CountDownLatch(1);
        final boolean[] ok = new boolean[]{false};
        boolean dispatched = dispatchGesture(desc, new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription g) {
                ok[0] = true;
                latch.countDown();
            }

            @Override
            public void onCancelled(GestureDescription g) {
                latch.countDown();
            }
        }, null);
        if (!dispatched) {
            throw new Exception("gesture rejected by the system "
                    + "(dispatchGesture returned false — is canPerformGestures set?)");
        }
        if (!latch.await(5, TimeUnit.SECONDS)) {
            throw new Exception("gesture timed out");
        }
        return ok[0];
    }

    public boolean tap(int x, int y) throws Exception {
        Path path = new Path();
        path.moveTo(x, y);
        // A moveTo-only path is degenerate: the gesture system may drop
        // it without ever calling the callback. A 1px line makes it a
        // real stroke.
        path.lineTo(x + 1, y);
        return runGesture(path, 100);
    }

    public boolean swipe(int x1, int y1, int x2, int y2, long durationMs) throws Exception {
        Path path = new Path();
        path.moveTo(x1, y1);
        path.lineTo(x2, y2);
        return runGesture(path, durationMs);
    }

    public boolean pressKey(String key) {
        int action;
        if ("HOME".equalsIgnoreCase(key)) {
            action = GLOBAL_ACTION_HOME;
        } else if ("BACK".equalsIgnoreCase(key)) {
            action = GLOBAL_ACTION_BACK;
        } else if ("RECENTS".equalsIgnoreCase(key)) {
            action = GLOBAL_ACTION_RECENTS;
        } else if ("POWER".equalsIgnoreCase(key)) {
            action = GLOBAL_ACTION_POWER_DIALOG;
        } else if ("DPAD_UP".equalsIgnoreCase(key)) {
            // Decision 66 (v1.9.9): DPAD global actions (API 33+)
            // move focus, not the viewport — in a list, focus
            // movement scrolls as a side effect. Whether a given
            // app scrolls on DPAD_DOWN is the app's behaviour,
            // not the bridge's.
            action = GLOBAL_ACTION_DPAD_UP;
        } else if ("DPAD_DOWN".equalsIgnoreCase(key)) {
            action = GLOBAL_ACTION_DPAD_DOWN;
        } else if ("DPAD_LEFT".equalsIgnoreCase(key)) {
            action = GLOBAL_ACTION_DPAD_LEFT;
        } else if ("DPAD_RIGHT".equalsIgnoreCase(key)) {
            action = GLOBAL_ACTION_DPAD_RIGHT;
        } else if ("DPAD_CENTER".equalsIgnoreCase(key)) {
            action = GLOBAL_ACTION_DPAD_CENTER;
        } else {
            return false;
        }
        return performGlobalAction(action);
    }

    /**
     * Launches an app by package name (decision 69, v1.9.12):
     * the package's launcher intent, started from this service
     * (FLAG_ACTIVITY_NEW_TASK, required outside an activity).
     * Runs on the HTTP worker thread — no accessibility work, so
     * no main-thread hop. Returns false when Android refuses the
     * start (background-activity-start restriction); throws
     * IllegalArgumentException when the package is not installed
     * or has no launcher activity.
     */
    public boolean launchApp(String pkg) {
        if (pkg == null || pkg.isEmpty()) {
            throw new IllegalArgumentException("package must not be empty");
        }
        Intent intent = getPackageManager().getLaunchIntentForPackage(pkg);
        if (intent == null) {
            throw new IllegalArgumentException(
                    "package not installed or has no launcher: " + pkg);
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            startActivity(intent);
            return true;
        } catch (ActivityNotFoundException | SecurityException e) {
            BridgeLog.log("launch_blocked", pkg);
            return false;
        }
    }

    /**
     * Scrolls a node by id (decision 67, v1.9.10): ACTION_SCROLL_FORWARD
     * or ACTION_SCROLL_BACKWARD on the node or its first scrollable
     * ancestor (the scroll handler is often on an ancestor, like the
     * clickable one). A node that is not scrollable cannot run (422).
     * Runs on the main thread (A-3).
     */
    public ActionOutcome scrollNode(final String nodeId, final int treeHash,
            final String direction) throws Exception {
        return runOnMainThread(new Callable<ActionOutcome>() {
            @Override
            public ActionOutcome call() throws Exception {
                return scrollNodeInternal(nodeId, treeHash, direction);
            }
        });
    }

    private synchronized ActionOutcome scrollNodeInternal(String nodeId, int treeHash,
            String direction) throws Exception {
        int action;
        if ("forward".equalsIgnoreCase(direction)) {
            action = AccessibilityNodeInfo.ACTION_SCROLL_FORWARD;
        } else if ("backward".equalsIgnoreCase(direction)) {
            action = AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD;
        } else {
            throw new IllegalArgumentException("unknown direction: " + direction);
        }
        ActionOutcome out = new ActionOutcome();
        AccessibilityNodeInfo node = resolveFresh(nodeId, treeHash);
        out.snapshotId = lastRevision;
        AccessibilityNodeInfo target = node;
        while (target != null && !target.isScrollable()) {
            AccessibilityNodeInfo parent = target.getParent();
            if (target != node) {
                target.recycle();
            }
            target = parent;
        }
        if (target == null) {
            out.ok = false;
            return out;
        }
        if (!target.isEnabled()) {
            if (target != node) {
                target.recycle();
            }
            throw new NodeNotFoundException("node " + nodeId + " is disabled");
        }
        try {
            out.ok = target.performAction(action);
            return out;
        } finally {
            if (target != node) {
                target.recycle();
            }
        }
    }

    /**
     * Runs any one-shot node action by name (decision 68, v1.9.11):
     * POST /node_action. The action runs on the node or the first
     * ancestor whose action list contains it (the same fallback idea
     * as click_node/scroll_node, generalized: the client sees the
     * same lists in /state's "actions"). Runs on the main thread (A-3).
     */
    public ActionOutcome nodeAction(final String nodeId, final int treeHash,
            final String actionName, final JSONObject args) throws Exception {
        return runOnMainThread(new Callable<ActionOutcome>() {
            @Override
            public ActionOutcome call() throws Exception {
                return nodeActionInternal(nodeId, treeHash, actionName, args);
            }
        });
    }

    private synchronized ActionOutcome nodeActionInternal(String nodeId, int treeHash,
            String actionName, JSONObject args) throws Exception {
        int actionId = actionIdForName(actionName);
        if (actionId < 0) {
            throw new IllegalArgumentException("unknown action: " + actionName);
        }
        Bundle bundle = nodeActionArgs(actionName, args);
        ActionOutcome out = new ActionOutcome();
        AccessibilityNodeInfo node = resolveFresh(nodeId, treeHash);
        out.snapshotId = lastRevision;
        AccessibilityNodeInfo target = node;
        while (target != null && !supportsAction(target, actionId)) {
            AccessibilityNodeInfo parent = target.getParent();
            if (target != node) {
                target.recycle();
            }
            target = parent;
        }
        if (target == null) {
            out.ok = false;
            return out;
        }
        if (!target.isEnabled()) {
            if (target != node) {
                target.recycle();
            }
            throw new NodeNotFoundException("node " + nodeId + " is disabled");
        }
        try {
            out.ok = target.performAction(actionId, bundle);
            return out;
        } finally {
            if (target != node) {
                target.recycle();
            }
        }
    }

    private static boolean supportsAction(AccessibilityNodeInfo node, int actionId) {
        for (AccessibilityNodeInfo.AccessibilityAction a : node.getActionList()) {
            if (a.getId() == actionId) {
                return true;
            }
        }
        return false;
    }

    /** Maps a /node_action name to its AccessibilityAction id, or -1. */
    private static int actionIdForName(String name) {
        if (name == null) {
            return -1;
        }
        switch (name) {
            case "click": return AccessibilityNodeInfo.ACTION_CLICK;
            case "long_click": return AccessibilityNodeInfo.ACTION_LONG_CLICK;
            case "focus": return AccessibilityNodeInfo.ACTION_FOCUS;
            case "clear_focus": return AccessibilityNodeInfo.ACTION_CLEAR_FOCUS;
            case "accessibility_focus":
                return AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS;
            case "clear_accessibility_focus":
                return AccessibilityNodeInfo.ACTION_CLEAR_ACCESSIBILITY_FOCUS;
            case "select": return AccessibilityNodeInfo.ACTION_SELECT;
            case "clear_selection": return AccessibilityNodeInfo.ACTION_CLEAR_SELECTION;
            case "set_selection": return AccessibilityNodeInfo.ACTION_SET_SELECTION;
            case "copy": return AccessibilityNodeInfo.ACTION_COPY;
            case "cut": return AccessibilityNodeInfo.ACTION_CUT;
            case "paste": return AccessibilityNodeInfo.ACTION_PASTE;
            case "expand": return AccessibilityNodeInfo.ACTION_EXPAND;
            case "collapse": return AccessibilityNodeInfo.ACTION_COLLAPSE;
            case "dismiss": return AccessibilityNodeInfo.ACTION_DISMISS;
            case "show_on_screen":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_SHOW_ON_SCREEN.getId();
            case "context_click":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_CONTEXT_CLICK.getId();
            case "ime_enter":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId();
            case "scroll_forward": return AccessibilityNodeInfo.ACTION_SCROLL_FORWARD;
            case "scroll_backward": return AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD;
            case "scroll_up":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_UP.getId();
            case "scroll_down":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_DOWN.getId();
            case "scroll_left":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_LEFT.getId();
            case "scroll_right":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_RIGHT.getId();
            case "page_up":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_PAGE_UP.getId();
            case "page_down":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_PAGE_DOWN.getId();
            case "page_left":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_PAGE_LEFT.getId();
            case "page_right":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_PAGE_RIGHT.getId();
            case "press_and_hold":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_PRESS_AND_HOLD.getId();
            case "set_progress":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_PROGRESS.getId();
            case "scroll_to_position":
                return AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_TO_POSITION.getId();
            case "set_text": return AccessibilityNodeInfo.ACTION_SET_TEXT;
            default: return -1;
        }
    }

    /**
     * Builds the argument Bundle for an action (decision 68). Only
     * four actions take args; the rest take none. A missing or
     * wrongly-typed required arg is an IllegalArgumentException
     * (-> HTTP 400), never a silent default.
     */
    private static Bundle nodeActionArgs(String name, JSONObject args) {
        Bundle b = new Bundle();
        if (args == null) {
            args = new JSONObject();
        }
        if ("set_selection".equals(name)) {
            Object start = args.opt("start");
            Object end = args.opt("end");
            if (!(start instanceof Number) || !(end instanceof Number)) {
                throw new IllegalArgumentException(
                        "set_selection needs integer start and end");
            }
            b.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT,
                    ((Number) start).intValue());
            b.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT,
                    ((Number) end).intValue());
        } else if ("set_progress".equals(name)) {
            Object value = args.opt("value");
            if (!(value instanceof Number)) {
                throw new IllegalArgumentException("set_progress needs numeric value");
            }
            b.putFloat(AccessibilityNodeInfo.ACTION_ARGUMENT_PROGRESS_VALUE,
                    ((Number) value).floatValue());
        } else if ("scroll_to_position".equals(name)) {
            Object row = args.opt("row");
            if (!(row instanceof Number)) {
                throw new IllegalArgumentException("scroll_to_position needs integer row");
            }
            b.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_ROW_INT,
                    ((Number) row).intValue());
        } else if ("press_and_hold".equals(name)) {
            Object duration = args.opt("duration_ms");
            if (duration != null && !(duration instanceof Number)) {
                throw new IllegalArgumentException(
                        "press_and_hold duration_ms must be an integer");
            }
            if (duration != null) {
                b.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_PRESS_AND_HOLD_DURATION_MILLIS_INT,
                        ((Number) duration).intValue());
            }
        } else if ("set_text".equals(name)) {
            Object text = args.opt("text");
            if (!(text instanceof String)) {
                throw new IllegalArgumentException("set_text needs text");
            }
            b.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                    (String) text);
        }
        return b;
    }

    /**
     * Set text on the currently focused editable field. Returns false when
     * there is no focused editable field (tap the field first).
     */
    public boolean typeText(String text) {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) {
            return false;
        }
        AccessibilityNodeInfo target = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (target == null) {
            target = root.findFocus(AccessibilityNodeInfo.FOCUS_ACCESSIBILITY);
        }
        if (target == null || !target.isEditable()) {
            return false;
        }
        target.refresh();
        Bundle args = new Bundle();
        args.putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        return target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
    }

    // ---------------------------------------------------------------
    // Phase 2: node-targeted actions and batches.
    // ---------------------------------------------------------------

    /** The screen moved on since the /state the client used. */
    public static class StaleRevisionException extends Exception {
        public StaleRevisionException(String message) {
            super(message);
        }
    }

    /** No live node with that id in the current snapshot. */
    public static class NodeNotFoundException extends Exception {
        public NodeNotFoundException(String message) {
            super(message);
        }
    }

    /** Result of a node action: whether it ran, and the snapshot
     * (fresh walk) it resolved against. */
    public static class ActionOutcome {
        public boolean ok;
        public int snapshotId = -1;
    }

    /**
     * A-3: resolves a node id against a FRESH tree walk taken just
     * before the action, using the same traversal order (so the ids
     * match the client's /state). Checks, in order: the client's
     * tree_hash still matches the fresh walk (else 409 stale), the
     * node exists in the fresh walk, and it is visible to the user.
     * The node comes from the fresh walk and is used inside this
     * operation only — no action ever touches a node held from an
     * earlier /state.
     */
    private AccessibilityNodeInfo resolveFresh(String nodeId, int treeHash)
            throws StaleRevisionException, NodeNotFoundException {
        State fresh = currentStateInternal();
        if (treeHash != fresh.treeHash) {
            throw new StaleRevisionException(
                    "screen changed since that /state (tree hash moved) — refetch state");
        }
        AccessibilityNodeInfo node = lastNodes.get(nodeId);
        if (node == null) {
            throw new NodeNotFoundException("no node " + nodeId + " in this snapshot");
        }
        if (!node.isVisibleToUser()) {
            throw new NodeNotFoundException("node " + nodeId + " is not visible");
        }
        return node;
    }

    /**
     * Clicks a node by id. When the node itself is not clickable, the
     * handler is often on an ancestor (GPT design), so the first
     * clickable ancestor is used instead; a clickable target that is
     * disabled cannot run (422). Runs on the main thread (A-3).
     */
    public ActionOutcome clickNode(final String nodeId, final int treeHash)
            throws Exception {
        return runOnMainThread(new Callable<ActionOutcome>() {
            @Override
            public ActionOutcome call() throws Exception {
                return clickNodeInternal(nodeId, treeHash);
            }
        });
    }

    private synchronized ActionOutcome clickNodeInternal(String nodeId, int treeHash)
            throws Exception {
        ActionOutcome out = new ActionOutcome();
        AccessibilityNodeInfo node = resolveFresh(nodeId, treeHash);
        out.snapshotId = lastRevision;
        AccessibilityNodeInfo target = node;
        while (target != null && !target.isClickable()) {
            AccessibilityNodeInfo parent = target.getParent();
            if (target != node) {
                target.recycle();
            }
            target = parent;
        }
        if (target == null) {
            out.ok = false;
            return out;
        }
        if (!target.isEnabled()) {
            if (target != node) {
                target.recycle();
            }
            throw new NodeNotFoundException("node " + nodeId + " is disabled");
        }
        try {
            out.ok = target.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            return out;
        } finally {
            if (target != node) {
                target.recycle();
            }
        }
    }

    /** Sets text on an editable node by id. Runs on the main thread (A-3). */
    public ActionOutcome typeNode(final String nodeId, final int treeHash,
            final String text) throws Exception {
        return runOnMainThread(new Callable<ActionOutcome>() {
            @Override
            public ActionOutcome call() throws Exception {
                return typeNodeInternal(nodeId, treeHash, text);
            }
        });
    }

    private synchronized ActionOutcome typeNodeInternal(String nodeId, int treeHash,
            String text) throws Exception {
        ActionOutcome out = new ActionOutcome();
        AccessibilityNodeInfo node = resolveFresh(nodeId, treeHash);
        out.snapshotId = lastRevision;
        // Password fields accept typed text (that is how a password is
        // entered); the privacy rule is that their value never leaves
        // the phone (redacted in /state and /events), not a refusal.
        if (!node.isEditable() || !node.isEnabled()) {
            out.ok = false;
            return out;
        }
        Bundle args = new Bundle();
        args.putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        out.ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
        return out;
    }

    /** One step of a batch. Only the fields its type needs are set. */
    public static class BatchAction {
        public String actionId = "";
        public String type = "";
        public String nodeId = "";
        public int x;
        public int y;
        public int x1;
        public int y1;
        public int x2;
        public int y2;
        public long durationMs = 300;
        public String text = "";
        public String key = "";
        public long waitMs;
        public boolean expectChange;
    }

    public static class ActionResult {
        public String actionId = "";
        public String status = "";
        public String error = "";
        public int hashBefore;
        public int hashAfter;
    }

    public static class BatchResult {
        public String batchId = "";
        public boolean replayed;
        public List<ActionResult> results = new ArrayList<>();
        public int stoppedAt = -1;
        public String stopReason = "";
        /** Revision of the snapshot the entry check used (A-3). */
        public int snapshotId = -1;
        /** A-4: "completed", "partial", "failed", or "running"
         * (a re-send that arrived while the first run was in flight). */
        public String status = "";
        /** A-4: SHA-256 of the canonical payload (see canonicalBatchPayload). */
        public String payloadHash = "";
        /** A-4: action_ids Android confirmed ran. "Ran" means the
         * action was dispatched and confirmed by Android — NOT that
         * the target app finished its business operation. */
        public List<String> completedActionIds = new ArrayList<>();
        /** A-4: the action_id that failed and stopped the batch ("" if none). */
        public String failedActionId = "";

        public BatchResult() {
        }

        /** Copy for a replayed response (the original stays cached). */
        public BatchResult(BatchResult other) {
            this.batchId = other.batchId;
            this.results = other.results;
            this.stoppedAt = other.stoppedAt;
            this.stopReason = other.stopReason;
            this.snapshotId = other.snapshotId;
            this.status = other.status;
            this.payloadHash = other.payloadHash;
            this.completedActionIds = other.completedActionIds;
            this.failedActionId = other.failedActionId;
        }
    }

    /**
     * A-4: canonical form of a batch payload for the payload hash.
     * Fixed field order, defaults already filled by the HTTP layer,
     * strings length-prefixed so no separator can be ambiguous,
     * numbers and booleans in plain form. Two payloads hash equal
     * exactly when their normalized content is equal; the batch_id
     * is NOT part of the hash (it is the lookup key).
     */
    private static String canonicalBatchPayload(int protocolVersion, int treeHash,
            List<BatchAction> actions) {
        StringBuilder sb = new StringBuilder();
        sb.append("v").append(protocolVersion);
        sb.append("|tree=").append(treeHash);
        sb.append("|actions=[");
        for (int i = 0; i < actions.size(); i++) {
            BatchAction a = actions.get(i);
            if (i > 0) {
                sb.append(";");
            }
            sb.append("{");
            appendCanon(sb, "id", a.actionId);
            sb.append(",");
            appendCanon(sb, "type", a.type);
            sb.append(",");
            appendCanon(sb, "node", a.nodeId);
            sb.append(",");
            sb.append("x=").append(a.x);
            sb.append(",y=").append(a.y);
            sb.append(",x1=").append(a.x1);
            sb.append(",y1=").append(a.y1);
            sb.append(",x2=").append(a.x2);
            sb.append(",y2=").append(a.y2);
            sb.append(",dur=").append(a.durationMs);
            sb.append(",");
            appendCanon(sb, "text", a.text);
            sb.append(",");
            appendCanon(sb, "key", a.key);
            sb.append(",wait=").append(a.waitMs);
            sb.append(",exp=").append(a.expectChange);
            sb.append("}");
        }
        sb.append("]");
        return sb.toString();
    }

    private static void appendCanon(StringBuilder sb, String name, String value) {
        String v = value == null ? "" : value;
        sb.append(name).append("=").append(v.length()).append(":").append(v);
    }

    private static String sha256Hex(String input) throws Exception {
        java.security.MessageDigest md =
                java.security.MessageDigest.getInstance("SHA-256");
        byte[] digest = md.digest(input.getBytes(StandardCharsets.UTF_8));
        StringBuilder hex = new StringBuilder(digest.length * 2);
        for (byte b : digest) {
            hex.append(String.format("%02x", b & 0xff));
        }
        return hex.toString();
    }

    /**
     * Runs a batch against one snapshot (Phase 2, coordinator in
     * A-4). Steps execute in order; the first failure stops the
     * batch. A step with expectChange waits up to 2 s for the tree
     * hash to move: when it moves, the batch stops after that step
     * (the screen changed, so later node ids are invalid — the
     * client refetches /state and sends the next batch).
     *
     * Idempotency (A-4): the batch_id + payload_hash decides. A
     * re-send with the same id AND the same hash returns the cached
     * result (replayed: true) without re-executing; while the first
     * run is still in flight it returns status "running" (replayed:
     * true). The same id with a different hash is a 409; an id whose
     * result expired is a 410 until its tombstone lapses. Results
     * live 10 minutes in memory only — no exactly-once across a
     * process restart (a stated design constraint).
     */
    public BatchResult runBatch(final String batchId, final int protocolVersion,
            final int treeHash, final List<BatchAction> actions) throws Exception {
        final String payloadHash =
                sha256Hex(canonicalBatchPayload(protocolVersion, treeHash, actions));
        final long now = System.currentTimeMillis();
        final int entrySnapshotId;
        synchronized (this) {
            if (lastRevision < 0) {
                throw new NodeNotFoundException(
                        "no /state snapshot yet — fetch /state first");
            }
            if (treeHash != lastTreeHash) {
                throw new StaleRevisionException(
                        "screen changed since that /state (tree hash moved) — refetch state");
            }
            BatchCacheEntry entry = batchCache.get(batchId);
            if (entry != null) {
                if (now > entry.tombstoneUntil) {
                    batchCache.remove(batchId);
                    entry = null;
                } else if (now > entry.expiresAt) {
                    throw new BatchGoneException(
                            "batch_id " + batchId + " expired — use a new batch_id");
                } else if (!entry.payloadHash.equals(payloadHash)) {
                    throw new BatchMismatchException(
                            "batch_id " + batchId + " is already in use with different content");
                } else if ("running".equals(entry.status)) {
                    BatchResult running = new BatchResult();
                    running.batchId = batchId;
                    running.replayed = true;
                    running.status = "running";
                    running.payloadHash = payloadHash;
                    running.snapshotId = lastRevision;
                    return running;
                } else {
                    BatchResult replay = new BatchResult(entry.result);
                    replay.replayed = true;
                    return replay;
                }
            }
            entrySnapshotId = lastRevision;
            BatchCacheEntry fresh = new BatchCacheEntry();
            fresh.payloadHash = payloadHash;
            fresh.status = "running";
            fresh.createdAt = now;
            fresh.expiresAt = now + BATCH_TTL_MS;
            fresh.tombstoneUntil = now + BATCH_TTL_MS + BATCH_TOMBSTONE_MS;
            batchCache.put(batchId, fresh);
        }
        final int snapshotId = entrySnapshotId;
        BatchResult result = runOnMainThread(new Callable<BatchResult>() {
            @Override
            public BatchResult call() throws Exception {
                return runBatchInternal(batchId, treeHash, actions, snapshotId);
            }
        });
        result.payloadHash = payloadHash;
        synchronized (this) {
            BatchCacheEntry entry = batchCache.get(batchId);
            if (entry != null && entry.payloadHash.equals(payloadHash)) {
                entry.result = result;
                entry.status = result.status;
            }
        }
        return result;
    }

    private synchronized BatchResult runBatchInternal(String batchId, int treeHash,
            List<BatchAction> actions, int entrySnapshotId) throws Exception {
        BatchResult result = new BatchResult();
        result.batchId = batchId;
        result.snapshotId = entrySnapshotId;
        for (int i = 0; i < actions.size(); i++) {
            BatchAction a = actions.get(i);
            ActionResult r = new ActionResult();
            r.actionId = a.actionId;
            r.hashBefore = lastTreeHash;
            try {
                boolean ok = executeAction(a, treeHash);
                r.status = ok ? "executed" : "failed";
                if (!ok) {
                    r.error = "action returned false";
                }
            } catch (Exception e) {
                r.status = "failed";
                r.error = e.getMessage() == null ? e.toString() : e.getMessage();
            }
            r.hashAfter = lastTreeHash;
            result.results.add(r);
            if ("executed".equals(r.status)) {
                result.completedActionIds.add(r.actionId);
            } else {
                result.failedActionId = r.actionId;
            }
            if ("failed".equals(r.status)) {
                result.stoppedAt = i;
                result.stopReason = r.error;
                break;
            }
            if (a.expectChange) {
                boolean changed;
                try {
                    changed = waitForChange(r.hashBefore, 2000);
                } catch (Exception e) {
                    r.status = "failed";
                    r.error = e.getMessage() == null ? e.toString() : e.getMessage();
                    result.stoppedAt = i;
                    result.stopReason = r.error;
                    break;
                }
                if (changed) {
                    r.hashAfter = lastTreeHash;
                    result.stoppedAt = i;
                    result.stopReason = "screen changed — refetch state for the next batch";
                    break;
                }
                r.status = "failed";
                r.error = "no screen change within 2s";
                result.stoppedAt = i;
                result.stopReason = r.error;
                break;
            }
        }
        // A-4: 0 successes = failed; >= 1 success then stop = partial.
        if (result.completedActionIds.isEmpty()) {
            result.status = "failed";
        } else if (result.stoppedAt >= 0) {
            result.status = "partial";
        } else {
            result.status = "completed";
        }
        // v1.9.3: batch outcome in the debug log (status + counts
        // only — no batch_id, no payload, no node text).
        BridgeLog.log("batch", result.status + " "
                + result.completedActionIds.size() + "/" + actions.size());
        return result;
    }

    private boolean executeAction(BatchAction a, int treeHash) throws Exception {
        if ("click_node".equals(a.type)) {
            return clickNodeInternal(a.nodeId, treeHash).ok;
        } else if ("tap".equals(a.type)) {
            return tap(a.x, a.y);
        } else if ("type_node".equals(a.type)) {
            return typeNodeInternal(a.nodeId, treeHash, a.text).ok;
        } else if ("type".equals(a.type)) {
            return typeText(a.text);
        } else if ("key".equals(a.type)) {
            return pressKey(a.key);
        } else if ("swipe".equals(a.type)) {
            return swipe(a.x1, a.y1, a.x2, a.y2, a.durationMs);
        } else if ("wait".equals(a.type)) {
            Thread.sleep(Math.max(0, a.waitMs));
            return true;
        }
        throw new IllegalArgumentException("unknown action type: " + a.type);
    }

    /** Polls for a tree-hash change (fresh snapshot each poll). */
    private boolean waitForChange(int hashBefore, long timeoutMs) throws Exception {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            try {
                Thread.sleep(250);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
            currentState();
            if (lastTreeHash != hashBefore) {
                return true;
            }
        }
        return false;
    }

    /**
     * Screen size, foreground package, visible texts, and (Phase 1) the
     * node tree: every visible node with its screen bounds, focus and
     * input state, plus the focused node and whether the keyboard is up.
     * The tree is what lets the machine operator decide the next action
     * without taking a screenshot: "tap the node with this text" instead
     * of "tap pixel (x, y) found in an image". Runs on the main thread
     * (A-3). snapshot_stable is false when an accessibility event
     * arrived mid-walk: the tree may be mid-change and the VM should
     * refetch /state before important operations.
     */
    public State currentState() throws Exception {
        return runOnMainThread(new Callable<State>() {
            @Override
            public State call() {
                return currentStateInternal();
            }
        });
    }

    private synchronized State currentStateInternal() {
        State state = new State();
        state.revision = revision.incrementAndGet();
        state.snapshotId = state.revision;
        long seqBefore = eventSeq.get();
        try {
            Rect bounds = ((WindowManager) getSystemService(WINDOW_SERVICE))
                    .getCurrentWindowMetrics().getBounds();
            state.width = bounds.width();
            state.height = bounds.height();
        } catch (Exception e) {
            state.width = -1;
            state.height = -1;
        }
        synchronized (this) {
            recycleLastNodes();
            lastRevision = state.revision;
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root != null) {
            state.packageName = String.valueOf(root.getPackageName());
            state.texts = new ArrayList<>();
            collectTexts(root, state.texts, 20);
            state.nodes = new ArrayList<>();
            // Focus is read during the walk (isFocused /
            // isAccessibilityFocused): no separate findFocus call, so
            // the focused id can never point at the wrong twin node.
            String[] focus = new String[1];
            String[] a11yFocus = new String[1];
            // The walk keeps the live nodes it visits in lastNodes
            // (Phase 2): they are recycled on the next snapshot.
            Map<String, AccessibilityNodeInfo> live = new LinkedHashMap<>();
            boolean walkOk = false;
            try {
                state.truncated = collectNodes(root, null, 0, state.nodes, focus, a11yFocus, live);
                state.focusedId = focus[0] != null ? focus[0] : a11yFocus[0];
                state.treeHash = computeTreeHash(state.nodes);
                synchronized (this) {
                    lastNodes.putAll(live);
                    lastTreeHash = state.treeHash;
                }
                walkOk = true;
            } finally {
                if (!walkOk) {
                    // A-3: a failed walk recycles its partial nodes
                    // instead of leaking them; the root is recycled
                    // only when it never reached the live map.
                    for (AccessibilityNodeInfo n : live.values()) {
                        try {
                            n.recycle();
                        } catch (Exception ignored) {
                        }
                    }
                    if (!live.containsValue(root)) {
                        try {
                            root.recycle();
                        } catch (Exception ignored) {
                        }
                    }
                }
                // The root is n0 in the map on success; do not recycle it here.
            }
        }
        state.imeVisible = isImeVisible();
        state.eventSeq = eventSeq.get();
        state.snapshotStable = (seqBefore == state.eventSeq);
        state.snapshotValid = true;
        return state;
    }

    private synchronized void recycleLastNodes() {
        for (AccessibilityNodeInfo n : lastNodes.values()) {
            try {
                n.recycle();
            } catch (Exception ignored) {
            }
        }
        lastNodes.clear();
    }

    /** Content hash of the tree: changes only when the screen changes. */
    private static int computeTreeHash(List<Node> nodes) {
        int h = 17;
        for (Node n : nodes) {
            h = 31 * h + n.className.hashCode();
            h = 31 * h + n.text.hashCode();
            h = 31 * h + n.left;
            h = 31 * h + n.top;
            h = 31 * h + n.right;
            h = 31 * h + n.bottom;
            h = 31 * h + (n.clickable ? 1 : 0);
            h = 31 * h + (n.enabled ? 1 : 0);
        }
        return h;
    }

    /** True when an input-method (keyboard) window is present. */
    private boolean isImeVisible() {
        try {
            List<AccessibilityWindowInfo> windows = getWindows();
            if (windows == null) {
                return false;
            }
            for (AccessibilityWindowInfo w : windows) {
                try {
                    if (w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD) {
                        return true;
                    }
                } finally {
                    w.recycle();
                }
            }
        } catch (Exception ignored) {
            // Best effort only; the field just stays false.
        }
        return false;
    }

    /**
     * Depth-first walk of the node tree. Node ids are "n0", "n1", ...
     * in walk order and are only valid for this revision (Phase 1):
     * a later /state gets a new revision and new ids. Returns true when
     * the walk stopped early at MAX_NODES / MAX_DEPTH.
     */
    private boolean collectNodes(AccessibilityNodeInfo node, String parentId,
            int depth, List<Node> out, String[] focus, String[] a11yFocus,
            Map<String, AccessibilityNodeInfo> live) {
        if (node == null || depth > MAX_DEPTH) {
            return depth > MAX_DEPTH;
        }
        if (out.size() >= MAX_NODES) {
            return true;
        }
        Node n = new Node();
        boolean added = false;
        try {
            n.id = "n" + out.size();
            n.parentId = parentId;
            CharSequence cls = node.getClassName();
            n.className = cls == null ? "" : cls.toString();
            // Password fields never expose their value: the operator gets
            // "[REDACTED]" instead (GPT design, Phase 1 privacy rule).
            n.password = node.isPassword();
            CharSequence text = node.getText();
            n.text = n.password ? "[REDACTED]"
                    : (text == null ? "" : text.toString());
            CharSequence desc = node.getContentDescription();
            n.contentDescription = desc == null ? "" : desc.toString();
            Rect r = new Rect();
            node.getBoundsInScreen(r);
            n.left = r.left;
            n.top = r.top;
            n.right = r.right;
            n.bottom = r.bottom;
            n.clickable = node.isClickable();
            n.focusable = node.isFocusable();
            n.focused = node.isFocused();
            n.editable = node.isEditable();
            n.scrollable = node.isScrollable();
            n.enabled = node.isEnabled();
            n.selected = node.isSelected();
            n.inputType = node.getInputType();
            CharSequence resId = node.getViewIdResourceName();
            n.resourceId = resId == null ? "" : resId.toString();
            n.actions = new ArrayList<>();
            for (AccessibilityNodeInfo.AccessibilityAction a : node.getActionList()) {
                String name = actionName(a.getId());
                if (name != null) {
                    n.actions.add(name);
                }
            }
            out.add(n);
            // A-3: the live node belongs to this walk's map; a node
            // action re-walks before acting, so it never uses a node
            // held from an earlier snapshot. Recycled on the next
            // snapshot (or by the walk-level finally when this walk
            // fails after the put).
            live.put(n.id, node);
            added = true;
            if (n.focused && focus[0] == null) {
                focus[0] = n.id;
            }
            if (node.isAccessibilityFocused() && a11yFocus[0] == null) {
                a11yFocus[0] = n.id;
            }
            boolean truncated = false;
            for (int i = 0; i < node.getChildCount(); i++) {
                AccessibilityNodeInfo child = node.getChild(i);
                if (child == null) {
                    continue;
                }
                // No recycle here: the child stays live in the map.
                if (collectNodes(child, n.id, depth + 1, out, focus, a11yFocus, live)) {
                    truncated = true;
                    break;
                }
                if (out.size() >= MAX_NODES) {
                    truncated = true;
                    break;
                }
            }
            return truncated;
        } catch (Exception e) {
            // A-3: a node that failed before reaching the live map is
            // recycled here; one already in the map is recycled by the
            // walk-level finally in currentStateInternal.
            if (!added) {
                try {
                    node.recycle();
                } catch (Exception ignored) {
                }
            }
            throw e;
        }
    }

    /** Short names for the actions the operator cares about. */
    private static String actionName(int id) {
        switch (id) {
            case AccessibilityNodeInfo.ACTION_CLICK: return "click";
            case AccessibilityNodeInfo.ACTION_LONG_CLICK: return "long_click";
            case AccessibilityNodeInfo.ACTION_FOCUS: return "focus";
            case AccessibilityNodeInfo.ACTION_CLEAR_FOCUS: return "clear_focus";
            case AccessibilityNodeInfo.ACTION_SET_TEXT: return "set_text";
            case AccessibilityNodeInfo.ACTION_SCROLL_FORWARD: return "scroll_forward";
            case AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD: return "scroll_backward";
            case AccessibilityNodeInfo.ACTION_SELECT: return "select";
            case AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS: return "accessibility_focus";
            default: return null;
        }
    }

    private void collectTexts(AccessibilityNodeInfo node, List<String> out, int limit) {
        if (node == null || out.size() >= limit) {
            return;
        }
        CharSequence text = node.getText();
        if (text != null && node.isVisibleToUser()) {
            String s = text.toString().trim();
            if (!s.isEmpty() && s.length() <= 120) {
                out.add(s);
            }
        }
        for (int i = 0; i < node.getChildCount() && out.size() < limit; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            try {
                collectTexts(child, out, limit);
            } finally {
                if (child != null) {
                    child.recycle();
                }
            }
        }
    }

    public static class State {
        public int width = -1;
        public int height = -1;
        public String packageName = "";
        public List<String> texts = new ArrayList<>();
        /** Phase 1: monotonic snapshot counter; ids are valid only within it. */
        public int revision = 0;
        /** Phase 2: content hash — changes only when the screen changes. */
        public int treeHash = 0;
        /** Phase 3: latest recorded event seq (0 = none yet). */
        public long eventSeq = 0;
        /** A-3: snapshot id — the same value as revision, exposed so
         * the client can name the snapshot an action ran against. */
        public int snapshotId = 0;
        /** A-3: true when this snapshot is the latest complete walk. */
        public boolean snapshotValid = false;
        /** A-3: false when an accessibility event arrived mid-walk
         * (the tree may be mid-change); the VM should refetch /state
         * before important operations. */
        public boolean snapshotStable = false;
        public List<Node> nodes = new ArrayList<>();
        public String focusedId = null;
        public boolean imeVisible = false;
        public boolean truncated = false;
    }

    /** One node of the screen tree (Phase 1). Bounds are screen pixels. */
    public static class Node {
        public String id = "";
        public String parentId = null;
        public String className = "";
        public String text = "";
        public String contentDescription = "";
        public int left;
        public int top;
        public int right;
        public int bottom;
        public boolean clickable;
        public boolean focusable;
        public boolean focused;
        public boolean editable;
        public boolean scrollable;
        public boolean enabled;
        public boolean selected;
        public boolean password;
        public int inputType;
        public String resourceId = "";
        public List<String> actions = new ArrayList<>();
    }

    /** One recorded accessibility event (Phase 3). */
    public static class UiEvent {
        public long seq = 0;
        /** event.getEventTime(): uptime ms, monotonic on-device. */
        public long t = 0;
        public String type = "";
        public String packageName = "";
        public String className = "";
        /** "[REDACTED]" when the source is a password field. */
        public String text = "";
        public String desc = "";
        public int left = -1;
        public int top = -1;
        public int right = -1;
        public int bottom = -1;
        /** Identical repeats merged into this record. */
        public int coalesced = 0;
    }

    /** Result of getEvents: the slice plus the caller's bearings. */
    public static class EventsResult {
        public List<UiEvent> events = new ArrayList<>();
        public long latestSeq = 0;
        /** True when events the caller never saw were evicted. */
        public boolean dropped = false;
        public int treeHash = 0;
        public int revision = -1;
    }
}
