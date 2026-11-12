// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12
package dev.muse.bridge

import android.app.Notification
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.SystemClock
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

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
 *
 * NOTE (Kotlin): Java's static synchronized buffer methods
 * (hasLiveEvent / record / getEvents) locked the Class object.
 * Kotlin has no static methods; the three methods share one
 * explicit lock object, BUFFER_LOCK (the batch D/E pattern),
 * so the exclusion is the same. The quiesce lock is unchanged
 * — an explicit object in both languages — but wait/notify
 * need a cast to java.lang.Object, which Kotlin's Any does
 * not expose. No code outside this file locks on the class
 * object (verified by grep), so the monitor change is safe. */
class BridgeNotificationListener : NotificationListenerService() {

    companion object {
        private const val MAX_NOTIFICATION_EVENTS = 200
        private const val MAX_TEXT_CHARS = 200

        private val buffer: ArrayDeque<NotificationEvent> = ArrayDeque()
        private var seq: Long = 0
        private var lastDroppedSeq: Long = 0

        // CONS-150 P2-02: @Volatile — the Java original had no
        // visibility guarantee for this cross-thread field;
        // this is a Kotlin-only fix, no Java API change.
        @Volatile
        private var instance: BridgeNotificationListener? = null

        // SPEC-LOG-001 LOG-04 (P1-D): stop-time quiesce. The server's
        // stop() (1) stops accepting new notifications, (2) waits for
        // in-flight callbacks to finish (bounded), (3) flushes the
        // suppressed count. accepting=false makes new callbacks return
        // immediately; the in-flight counter lets stop() wait for the
        // ones already running. The accepting flag is re-checked right
        // before each log call, so a callback that started before the
        // stop can never write to the log after the final flush.
        private val quiesceLock = Any()
        private var inFlight: Int = 0
        @Volatile
        private var accepting: Boolean = true

        /** The one monitor for the buffer critical sections
         * (hasLiveEvent / record / getEvents). */
        private val BUFFER_LOCK = Any()

        /** Stop/start accepting new notification callbacks (server stop/start). */
        @JvmStatic
        fun setAccepting(accept: Boolean) {
            accepting = accept
        }

        /** Wait until no notification callback is in flight, or the
         * timeout expires. Returns true when quiesced, false on timeout
         * (the caller then records the incomplete items and continues).
         * The timeout is measured on the monotonic clock. */
        @JvmStatic
        fun awaitQuiesce(timeoutMs: Long): Boolean {
            val deadline = SystemClock.elapsedRealtime() + timeoutMs
            synchronized(quiesceLock) {
                while (inFlight > 0) {
                    val remaining = deadline - SystemClock.elapsedRealtime()
                    if (remaining <= 0) {
                        return false
                    }
                    try {
                        (quiesceLock as Object).wait(remaining)
                    } catch (e: InterruptedException) {
                        Thread.currentThread().interrupt()
                        return false
                    }
                }
                return true
            }
        }

        /** Number of notification callbacks currently in flight (for the stop log). */
        @JvmStatic
        fun inFlightCount(): Int {
            synchronized(quiesceLock) {
                return inFlight
            }
        }

        @JvmStatic
        fun getInstance(): BridgeNotificationListener? {
            return instance
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
        @JvmStatic
        fun isAccessGranted(ctx: Context): Boolean {
            // No public constant for this key in the SDK: the literal
            // setting name is "enabled_notification_listeners".
            val enabled = Settings.Secure.getString(
                    ctx.contentResolver, "enabled_notification_listeners")
            if (enabled == null) {
                return false
            }
            val cn = ComponentName(ctx, BridgeNotificationListener::class.java)
            return enabled.contains(cn.flattenToString())
                    || enabled.contains(cn.flattenToShortString())
        }

        /** True when the latest buffered event for this key is a post or
         * an update (i.e. the notification is currently live). */
        private fun hasLiveEvent(key: String?): Boolean {
            if (key == null) {
                return false
            }
            synchronized(BUFFER_LOCK) {
                // The buffer is oldest-first; the last match is the newest
                // event for this key. The buffer is capped at 200, so a
                // full walk is cheap.
                var last: NotificationEvent? = null
                for (e in buffer) {
                    if (key == e.key) {
                        last = e
                    }
                }
                return last != null
                        && (last.type == "posted" || last.type == "updated")
            }
        }

        private fun record(sbn: StatusBarNotification,
                type: String, removedT: Long) {
            synchronized(BUFFER_LOCK) {
                var title = ""
                var text = ""
                var ongoing = false
                var groupSummary = false
                val n = sbn.notification
                if (n != null && type != "removed") {
                    val extras = n.extras
                    if (extras != null) {
                        val t = extras.getCharSequence(Notification.EXTRA_TITLE)
                        if (t != null) {
                            title = cap(t.toString())
                        }
                        val x = extras.getCharSequence(Notification.EXTRA_TEXT)
                        if (x != null) {
                            text = cap(x.toString())
                        }
                    }
                    val flags = n.flags
                    ongoing = (flags and Notification.FLAG_ONGOING_EVENT) != 0
                    groupSummary = (flags and Notification.FLAG_GROUP_SUMMARY) != 0
                }
                // A tombstone carries only the key: never store the removed
                // notification's title/text.
                val e = NotificationEvent()
                e.seq = ++seq
                e.type = type
                e.key = sbn.key
                e.packageName = sbn.packageName
                e.id = sbn.id
                e.tag = sbn.tag
                e.title = title
                e.text = text
                e.t = sbn.postTime
                e.ongoing = ongoing
                e.groupSummary = groupSummary
                e.removedT = removedT
                buffer.addLast(e)
                if (buffer.size > MAX_NOTIFICATION_EVENTS) {
                    val dropped = buffer.removeFirst()
                    lastDroppedSeq = dropped.seq
                }
            }
        }

        /**
         * Events after `since` (exclusive), oldest first, capped at
         * `limit`, optionally filtered to one package. dropped is true
         * when the buffer evicted events the caller never saw (since
         * is older than the oldest evicted seq), so the caller should
         * refetch with since=0 instead of trusting the gap.
         */
        @JvmStatic
        fun getEvents(
                since: Long, limit: Int, packageFilter: String?): NotificationsResult {
            synchronized(BUFFER_LOCK) {
                val r = NotificationsResult()
                r.latestSeq = seq
                r.dropped = since < lastDroppedSeq
                for (e in buffer) {
                    if (e.seq > since
                            && (packageFilter == null
                                    || packageFilter == e.packageName)) {
                        r.events.add(e)
                    }
                    if (r.events.size >= limit) {
                        break
                    }
                }
                return r
            }
        }

        /** Cap to 200 Unicode characters, never splitting a surrogate pair. */
        private fun cap(s: String?): String {
            if (s == null) {
                return ""
            }
            if (s.codePointCount(0, s.length) <= MAX_TEXT_CHARS) {
                return s
            }
            return s.substring(0, s.offsetByCodePoints(0, MAX_TEXT_CHARS))
        }

        /**
         * Opens a live notification by its sbn key (decision 77):
         * fires its contentIntent, which brings the app forward.
         * Returns null on success, or an error string. The lookup
         * is live — nothing is stored.
         */
        @JvmStatic
        fun openNotification(key: String?): String? {
            val l = instance
            if (l == null) {
                return "notification_listener_not_connected"
            }
            val sbn = l.findLive(key)
            if (sbn == null) {
                return "notification not found or already gone"
            }
            val n = sbn.notification
            val intent = n?.contentIntent
            if (n == null || intent == null) {
                return "notification cannot be opened"
            }
            try {
                intent.send()
                BridgeLog.log("notification", "opened")
                return null
            } catch (e: PendingIntent.CanceledException) {
                return "notification action no longer valid"
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
        @JvmStatic
        fun replyNotification(key: String?, text: String?): String? {
            if (text == null || text.isEmpty()) {
                throw IllegalArgumentException("reply text must not be empty")
            }
            val l = instance
            if (l == null) {
                return "notification_listener_not_connected"
            }
            val sbn = l.findLive(key)
            if (sbn == null) {
                return "notification not found or already gone"
            }
            val n = sbn.notification
            val actions = n?.actions
            if (n == null || actions == null) {
                return "no reply action on this notification"
            }
            for (action in actions) {
                val inputs = action.remoteInputs
                if (inputs == null || inputs.isEmpty()) {
                    continue
                }
                val fillIn = Intent()
                val results = Bundle()
                results.putCharSequence(inputs[0].resultKey, text)
                RemoteInput.addResultsToIntent(inputs, fillIn, results)
                try {
                    action.actionIntent.send(l, 0, fillIn)
                    BridgeLog.log("notification", "replied")
                    return null
                } catch (e: PendingIntent.CanceledException) {
                    return "notification action no longer valid"
                }
            }
            return "no reply action on this notification"
        }
    }

    override fun onListenerConnected() {
        instance = this
        // Seed the buffer with what is already showing, so a client
        // that connects later still sees the current notifications.
        // Dedupe by key: a reconnect must not re-post them.
        try {
            for (sbn in getActiveNotifications()) {
                if (sbn != null && !hasLiveEvent(sbn.key)) {
                    record(sbn, "posted", 0)
                }
            }
        } catch (ignored: Exception) {
            // Best effort: the live stream still works.
        }
        BridgeLog.log("notification", "listener connected")
    }

    override fun onListenerDisconnected() {
        instance = null
        BridgeLog.log("notification", "listener disconnected")
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (!accepting) {
            return
        }
        synchronized(quiesceLock) {
            inFlight++
        }
        try {
            // The system calls this for new posts AND updates.
            val type = if (hasLiveEvent(sbn.key)) "updated" else "posted"
            record(sbn, type, 0)
            // Re-check: a stop that started after entry must not
            // let this callback write to the log after the flush.
            if (!accepting) {
                return
            }
            // SPEC-LOG-001 LOG-04: throttle the flood lines — every
            // notification used to add a log line and drown the
            // server lines. First one is immediate; repeats within
            // 10 s are counted as "(+N suppressed)".
            BridgeLog.logThrottled("notification", "notification", type)
        } finally {
            synchronized(quiesceLock) {
                inFlight--
                (quiesceLock as Object).notifyAll()
            }
        }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        if (!accepting) {
            return
        }
        synchronized(quiesceLock) {
            inFlight++
        }
        try {
            record(sbn, "removed", System.currentTimeMillis())
            if (!accepting) {
                return
            }
            BridgeLog.logThrottled("notification", "notification", "removed")
        } finally {
            synchronized(quiesceLock) {
                inFlight--
                (quiesceLock as Object).notifyAll()
            }
        }
    }

    /** The live StatusBarNotification for this key, or null. */
    private fun findLive(key: String?): StatusBarNotification? {
        if (key == null) {
            return null
        }
        try {
            for (sbn in getActiveNotifications()) {
                if (sbn != null && key == sbn.key) {
                    return sbn
                }
            }
        } catch (ignored: Exception) {
            // Best effort: a null result reads as "not found".
        }
        return null
    }

    /** One recorded notification event (post, update, or removal).
     *
     * NOTE (Kotlin): the fields are @JvmField so the Java caller
     * (HttpBridgeServer) keeps its field access (e.seq, e.type,
     * e.key, ...) — plain Kotlin properties would expose only
     * getters. The fields stay mutable: record() fills them. */
    class NotificationEvent {
        @JvmField var seq: Long = 0
        /** "posted" | "updated" | "removed". */
        @JvmField var type: String = ""
        /** StatusBarNotification.getKey(): the notification's identity. */
        @JvmField var key: String = ""
        @JvmField var packageName: String = ""
        @JvmField var id: Int = 0
        @JvmField var tag: String? = null
        /** Capped at 200 Unicode chars; empty for tombstones. */
        @JvmField var title: String = ""
        @JvmField var text: String = ""
        /** postTime: wall-clock ms, informational only. */
        @JvmField var t: Long = 0
        @JvmField var ongoing: Boolean = false
        @JvmField var groupSummary: Boolean = false
        /** Set only on "removed" events. */
        @JvmField var removedT: Long = 0
    }

    /** Result of getEvents: the slice plus the caller's bearings. */
    class NotificationsResult {
        @JvmField var events: MutableList<NotificationEvent> = ArrayList()
        @JvmField var latestSeq: Long = 0
        /** True when events the caller never saw were evicted. */
        @JvmField var dropped: Boolean = false
    }
}
