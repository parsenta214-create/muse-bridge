package dev.muse.bridge;

import android.app.Notification;
import android.app.PendingIntent;
import android.app.RemoteInput;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.os.SystemClock;
import android.provider.Settings;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

/**
 * Notification reading (decision 63, v1.9.7): an event stream of
 * posted / updated / removed notifications, pull-based only.
 *
 * Posts, updates, and removals are EACH new events with a new seq;
 * a removal is a tombstone event, not an edit of the old record —
 * a client that already fetched seq=10 would otherwise never
 * learn of the removal via since=10. The buffer is static, so it
 * survives a listener reconnect inside the same process; it is
 * memory only and resets when the process dies (since is valid
 * only within the process lifetime).
 *
 * Privacy: title/text are capped at 200 Unicode characters, the
 * extras Bundle and notification actions are never stored, and
 * nothing about a notification's content is ever written to the
 * debug log (only the event type). The VM pulls via
 * GET /notifications; nothing is ever pushed anywhere.
 *
 * Acting (decision 77, v1.9.18): POST /notification_action opens
 * a notification or sends an inline reply. Actions are NOT
 * stored — the handler looks the notification up live in
 * getActiveNotifications() at action time, so a key that is
 * gone (or was never live) is 422.
 */
public class BridgeNotificationListener extends NotificationListenerService {

    private static final int MAX_NOTIFICATION_EVENTS = 200;
    private static final int MAX_TEXT_CHARS = 200;

    private static final Deque<NotificationEvent> buffer = new ArrayDeque<>();
    private static long seq = 0;
    private static long lastDroppedSeq = 0;

    private static BridgeNotificationListener instance;

    // SPEC-LOG-001 LOG-04 (P1-D): stop-time quiesce. The server's
    // stop() (1) stops accepting new notifications, (2) waits for
    // in-flight callbacks to finish (bounded), (3) flushes the
    // suppressed count. accepting=false makes new callbacks return
    // immediately; the in-flight counter lets stop() wait for the
    // ones already running. The accepting flag is re-checked right
    // before each log call, so a callback that started before the
    // stop can never write to the log after the final flush.
    private static final Object quiesceLock = new Object();
    private static int inFlight = 0;
    private static volatile boolean accepting = true;

    /** Stop/start accepting new notification callbacks (server stop/start). */
    public static void setAccepting(boolean accept) {
        accepting = accept;
    }

    /** Wait until no notification callback is in flight, or the
     * timeout expires. Returns true when quiesced, false on timeout
     * (the caller then records the incomplete items and continues).
     * The timeout is measured on the monotonic clock. */
    public static boolean awaitQuiesce(long timeoutMs) {
        long deadline = SystemClock.elapsedRealtime() + timeoutMs;
        synchronized (quiesceLock) {
            while (inFlight > 0) {
                long remaining = deadline - SystemClock.elapsedRealtime();
                if (remaining <= 0) {
                    return false;
                }
                try {
                    quiesceLock.wait(remaining);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return false;
                }
            }
            return true;
        }
    }

    /** Number of notification callbacks currently in flight (for the stop log). */
    public static int inFlightCount() {
        synchronized (quiesceLock) {
            return inFlight;
        }
    }

    public static BridgeNotificationListener getInstance() {
        return instance;
    }

    /**
     * Whether the user granted notification access in Settings.
     * This is NOT the same as the service being connected: the
     * grant can exist while the system has not bound the service
     * yet, so /notifications distinguishes the two 503s.
     *
     * The setting stores flattened component names in FULL form
     * ("dev.muse.bridge/dev.muse.bridge.BridgeNotification
     * Listener"); the short form (".BridgeNotificationListener")
     * never matches, so check both (decision 64).
     */
    public static boolean isAccessGranted(Context ctx) {
        // No public constant for this key in the SDK: the literal
        // setting name is "enabled_notification_listeners".
        String enabled = Settings.Secure.getString(
                ctx.getContentResolver(), "enabled_notification_listeners");
        if (enabled == null) {
            return false;
        }
        ComponentName cn = new ComponentName(ctx, BridgeNotificationListener.class);
        return enabled.contains(cn.flattenToString())
                || enabled.contains(cn.flattenToShortString());
    }

    @Override
    public void onListenerConnected() {
        instance = this;
        // Seed the buffer with what is already showing, so a client
        // that connects later still sees the current notifications.
        // Dedupe by key: a reconnect must not re-post them.
        try {
            for (StatusBarNotification sbn : getActiveNotifications()) {
                if (sbn != null && !hasLiveEvent(sbn.getKey())) {
                    record(sbn, "posted", 0);
                }
            }
        } catch (Exception ignored) {
            // Best effort: the live stream still works.
        }
        BridgeLog.log("notification", "listener connected");
    }

    @Override
    public void onListenerDisconnected() {
        instance = null;
        BridgeLog.log("notification", "listener disconnected");
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        if (!accepting) {
            return;
        }
        synchronized (quiesceLock) {
            inFlight++;
        }
        try {
            // The system calls this for new posts AND updates.
            String type = hasLiveEvent(sbn.getKey()) ? "updated" : "posted";
            record(sbn, type, 0);
            // Re-check: a stop that started after entry must not
            // let this callback write to the log after the flush.
            if (!accepting) {
                return;
            }
            // SPEC-LOG-001 LOG-04: throttle the flood lines — every
            // notification used to add a log line and drown the
            // server lines. First one is immediate; repeats within
            // 10 s are counted as "(+N suppressed)".
            BridgeLog.logThrottled("notification", "notification", type);
        } finally {
            synchronized (quiesceLock) {
                inFlight--;
                quiesceLock.notifyAll();
            }
        }
    }

    @Override
    public void onNotificationRemoved(StatusBarNotification sbn) {
        if (!accepting) {
            return;
        }
        synchronized (quiesceLock) {
            inFlight++;
        }
        try {
            record(sbn, "removed", System.currentTimeMillis());
            if (!accepting) {
                return;
            }
            BridgeLog.logThrottled("notification", "notification", "removed");
        } finally {
            synchronized (quiesceLock) {
                inFlight--;
                quiesceLock.notifyAll();
            }
        }
    }

    /** True when the latest buffered event for this key is a post or
     * an update (i.e. the notification is currently live). */
    private static synchronized boolean hasLiveEvent(String key) {
        if (key == null) {
            return false;
        }
        // The buffer is oldest-first; the last match is the newest
        // event for this key. The buffer is capped at 200, so a
        // full walk is cheap.
        NotificationEvent last = null;
        for (NotificationEvent e : buffer) {
            if (key.equals(e.key)) {
                last = e;
            }
        }
        return last != null
                && ("posted".equals(last.type) || "updated".equals(last.type));
    }

    private static synchronized void record(StatusBarNotification sbn,
            String type, long removedT) {
        String title = "";
        String text = "";
        boolean ongoing = false;
        boolean groupSummary = false;
        Notification n = sbn.getNotification();
        if (n != null && !"removed".equals(type)) {
            Bundle extras = n.extras;
            if (extras != null) {
                CharSequence t = extras.getCharSequence(Notification.EXTRA_TITLE);
                if (t != null) {
                    title = cap(t.toString());
                }
                CharSequence x = extras.getCharSequence(Notification.EXTRA_TEXT);
                if (x != null) {
                    text = cap(x.toString());
                }
            }
            int flags = n.flags;
            ongoing = (flags & Notification.FLAG_ONGOING_EVENT) != 0;
            groupSummary = (flags & Notification.FLAG_GROUP_SUMMARY) != 0;
        }
        // A tombstone carries only the key: never store the removed
        // notification's title/text.
        NotificationEvent e = new NotificationEvent();
        e.seq = ++seq;
        e.type = type;
        e.key = sbn.getKey();
        e.packageName = sbn.getPackageName();
        e.id = sbn.getId();
        e.tag = sbn.getTag();
        e.title = title;
        e.text = text;
        e.t = sbn.getPostTime();
        e.ongoing = ongoing;
        e.groupSummary = groupSummary;
        e.removedT = removedT;
        buffer.addLast(e);
        if (buffer.size() > MAX_NOTIFICATION_EVENTS) {
            NotificationEvent dropped = buffer.removeFirst();
            lastDroppedSeq = dropped.seq;
        }
    }

    /**
     * Events after `since` (exclusive), oldest first, capped at
     * `limit`, optionally filtered to one package. dropped is true
     * when the buffer evicted events the caller never saw (since
     * is older than the oldest evicted seq), so the caller should
     * refetch with since=0 instead of trusting the gap.
     */
    public static synchronized NotificationsResult getEvents(
            long since, int limit, String packageFilter) {
        NotificationsResult r = new NotificationsResult();
        r.latestSeq = seq;
        r.dropped = since < lastDroppedSeq;
        for (NotificationEvent e : buffer) {
            if (e.seq > since
                    && (packageFilter == null
                            || packageFilter.equals(e.packageName))) {
                r.events.add(e);
            }
            if (r.events.size() >= limit) {
                break;
            }
        }
        return r;
    }

    /** Cap to 200 Unicode characters, never splitting a surrogate pair. */
    private static String cap(String s) {
        if (s == null) {
            return "";
        }
        if (s.codePointCount(0, s.length()) <= MAX_TEXT_CHARS) {
            return s;
        }
        return s.substring(0, s.offsetByCodePoints(0, MAX_TEXT_CHARS));
    }

    /**
     * Opens a live notification by its sbn key (decision 77):
     * fires its contentIntent, which brings the app forward.
     * Returns null on success, or an error string. The lookup
     * is live — nothing is stored.
     */
    public static String openNotification(String key) {
        BridgeNotificationListener l = instance;
        if (l == null) {
            return "notification_listener_not_connected";
        }
        StatusBarNotification sbn = l.findLive(key);
        if (sbn == null) {
            return "notification not found or already gone";
        }
        Notification n = sbn.getNotification();
        if (n == null || n.contentIntent == null) {
            return "notification cannot be opened";
        }
        try {
            n.contentIntent.send();
            BridgeLog.log("notification", "opened");
            return null;
        } catch (PendingIntent.CanceledException e) {
            return "notification action no longer valid";
        }
    }

    /**
     * Sends an inline reply to a live notification by its sbn
     * key (decision 77): finds the first action carrying a
     * RemoteInput and sends the text through it. Returns null
     * on success, or an error string. Throws
     * IllegalArgumentException on empty text. This SENDS a
     * message — the caller must have the user's approval of
     * the exact text first.
     */
    public static String replyNotification(String key, String text) {
        if (text == null || text.isEmpty()) {
            throw new IllegalArgumentException("reply text must not be empty");
        }
        BridgeNotificationListener l = instance;
        if (l == null) {
            return "notification_listener_not_connected";
        }
        StatusBarNotification sbn = l.findLive(key);
        if (sbn == null) {
            return "notification not found or already gone";
        }
        Notification n = sbn.getNotification();
        if (n == null || n.actions == null) {
            return "no reply action on this notification";
        }
        for (Notification.Action action : n.actions) {
            RemoteInput[] inputs = action.getRemoteInputs();
            if (inputs == null || inputs.length == 0) {
                continue;
            }
            Intent fillIn = new Intent();
            Bundle results = new Bundle();
            results.putCharSequence(inputs[0].getResultKey(), text);
            RemoteInput.addResultsToIntent(inputs, fillIn, results);
            try {
                action.actionIntent.send(l, 0, fillIn);
                BridgeLog.log("notification", "replied");
                return null;
            } catch (PendingIntent.CanceledException e) {
                return "notification action no longer valid";
            }
        }
        return "no reply action on this notification";
    }

    /** The live StatusBarNotification for this key, or null. */
    private StatusBarNotification findLive(String key) {
        if (key == null) {
            return null;
        }
        try {
            for (StatusBarNotification sbn : getActiveNotifications()) {
                if (sbn != null && key.equals(sbn.getKey())) {
                    return sbn;
                }
            }
        } catch (Exception ignored) {
            // Best effort: a null result reads as "not found".
        }
        return null;
    }

    /** One recorded notification event (post, update, or removal). */
    public static class NotificationEvent {
        public long seq = 0;
        /** "posted" | "updated" | "removed". */
        public String type = "";
        /** StatusBarNotification.getKey(): the notification's identity. */
        public String key = "";
        public String packageName = "";
        public int id = 0;
        public String tag = null;
        /** Capped at 200 Unicode chars; empty for tombstones. */
        public String title = "";
        public String text = "";
        /** postTime: wall-clock ms, informational only. */
        public long t = 0;
        public boolean ongoing = false;
        public boolean groupSummary = false;
        /** Set only on "removed" events. */
        public long removedT = 0;
    }

    /** Result of getEvents: the slice plus the caller's bearings. */
    public static class NotificationsResult {
        public List<NotificationEvent> events = new ArrayList<>();
        public long latestSeq = 0;
        /** True when events the caller never saw were evicted. */
        public boolean dropped = false;
    }
}
