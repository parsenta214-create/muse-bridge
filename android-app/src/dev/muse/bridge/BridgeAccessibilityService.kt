// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-13
package dev.muse.bridge

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import android.view.WindowManager
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.nio.charset.StandardCharsets
import java.util.concurrent.Callable
import java.util.concurrent.CountDownLatch
import java.util.concurrent.ExecutionException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.floor

/**
 * AccessibilityService that performs the on-device work for the bridge:
 * screenshots, taps, swipes, text entry, and global keys.
 *
 * The service itself does no networking and holds no credential. The HTTP
 * server (HttpBridgeServer) calls into the live instance.
 *
 * (Java note: the original imported ActivityNotFoundException but never
 * used it; the import is dropped here.)
 */
open class BridgeAccessibilityService : AccessibilityService() {

    companion object {
        // The companion stays public (CONS-163 P2-01 lesson): Kotlin
        // callers (MainActivity.kt:471, A11yOffExecutor.kt:122) resolve
        // getInstance() through it, and MainActivity.kt:388/394 reads
        // ACTION_A11Y_STATE_CHANGED from it. A private companion would
        // break those callers.
        @Volatile
        private var instance: BridgeAccessibilityService? = null

        @JvmStatic
        fun getInstance(): BridgeAccessibilityService? = instance

        private const val MAIN_THREAD_TIMEOUT_MS = 20000L

        /** Caps for the node tree (Phase 1): keeps one /state small and fast. */
        private const val MAX_NODES = 500
        private const val MAX_DEPTH = 25

        private const val MAX_EVENTS = 200
        private const val COALESCE_WINDOW_MS = 300L

        /** CONS-029 P1-2 fix: broadcast when the service connects or
         * disconnects, so MainActivity can refresh its status display
         * promptly instead of waiting for the next screen update.
         * App-internal only (MainActivity registers with
         * RECEIVER_NOT_EXPORTED). Best effort — a missed broadcast
         * is harmless, onResume refreshes anyway.
         * v2.6.0 (SPEC-WIDGET.md, W3): the broadcast is sent
         * EXPLICITLY (setPackage) — since API 26 a manifest
         * receiver cannot receive an implicit broadcast, so the
         * widget provider's manifest entry for this action would
         * never fire without it. Runtime receivers in this app
         * (MainActivity, RECEIVER_NOT_EXPORTED) still receive it. */
        const val ACTION_A11Y_STATE_CHANGED =
            "dev.muse.bridge.ACTION_A11Y_STATE_CHANGED"

        private fun supportsAction(node: AccessibilityNodeInfo, actionId: Int): Boolean {
            for (a in node.actionList) {
                if (a.id == actionId) {
                    return true
                }
            }
            return false
        }

        /** Maps a /node_action name to its AccessibilityAction id, or -1. */
        private fun actionIdForName(name: String?): Int {
            if (name == null) {
                return -1
            }
            return when (name) {
                "click" -> AccessibilityNodeInfo.ACTION_CLICK
                "long_click" -> AccessibilityNodeInfo.ACTION_LONG_CLICK
                "focus" -> AccessibilityNodeInfo.ACTION_FOCUS
                "clear_focus" -> AccessibilityNodeInfo.ACTION_CLEAR_FOCUS
                "accessibility_focus" -> AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS
                "clear_accessibility_focus" -> AccessibilityNodeInfo.ACTION_CLEAR_ACCESSIBILITY_FOCUS
                "select" -> AccessibilityNodeInfo.ACTION_SELECT
                "clear_selection" -> AccessibilityNodeInfo.ACTION_CLEAR_SELECTION
                "set_selection" -> AccessibilityNodeInfo.ACTION_SET_SELECTION
                "copy" -> AccessibilityNodeInfo.ACTION_COPY
                "cut" -> AccessibilityNodeInfo.ACTION_CUT
                "paste" -> AccessibilityNodeInfo.ACTION_PASTE
                "expand" -> AccessibilityNodeInfo.ACTION_EXPAND
                "collapse" -> AccessibilityNodeInfo.ACTION_COLLAPSE
                "dismiss" -> AccessibilityNodeInfo.ACTION_DISMISS
                "show_on_screen" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SHOW_ON_SCREEN.id
                "context_click" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_CONTEXT_CLICK.id
                "ime_enter" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id
                "scroll_forward" -> AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                "scroll_backward" -> AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
                "scroll_up" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_UP.id
                "scroll_down" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_DOWN.id
                "scroll_left" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_LEFT.id
                "scroll_right" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_RIGHT.id
                "page_up" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_PAGE_UP.id
                "page_down" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_PAGE_DOWN.id
                "page_left" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_PAGE_LEFT.id
                "page_right" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_PAGE_RIGHT.id
                "press_and_hold" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_PRESS_AND_HOLD.id
                "set_progress" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_PROGRESS.id
                "scroll_to_position" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_TO_POSITION.id
                "set_text" -> AccessibilityNodeInfo.ACTION_SET_TEXT
                else -> -1
            }
        }

        /**
         * Builds the argument Bundle for an action (decision 68). Only
         * five actions take args; the rest take none. A missing or
         * wrongly-typed required arg is an IllegalArgumentException
         * (-> HTTP 400), never a silent default.
         *
         * P1-07 (CONS-014): unknown argument keys are rejected
         * (they used to be ignored silently); numeric args must be
         * integers (1.8 used to truncate via intValue()); and the
         * ranged args are bounded (set_progress 0.0–1.0,
         * scroll_to_position row >= 0, set_selection start/end
         * >= 0, press_and_hold duration_ms 1..60000).
         */
        private fun nodeActionArgs(name: String, args: JSONObject?): Bundle {
            val b = Bundle()
            val a = args ?: JSONObject()
            val allowed = allowedNodeActionArgs(name)
            val keys = a.keys()
            while (keys.hasNext()) {
                val k = keys.next()
                if (!allowed.contains(k)) {
                    throw IllegalArgumentException("unknown argument for $name: $k")
                }
            }
            if ("set_selection" == name) {
                val start = requireIntArg(a, "start", name)
                val end = requireIntArg(a, "end", name)
                if (start < 0 || end < 0) {
                    throw IllegalArgumentException("set_selection needs start and end >= 0")
                }
                b.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, start)
                b.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, end)
            } else if ("set_progress" == name) {
                val value = a.opt("value")
                if (value !is Number) {
                    throw IllegalArgumentException("set_progress needs numeric value")
                }
                val v = value.toDouble()
                if (v.isNaN() || v.isInfinite() || v < 0.0 || v > 1.0) {
                    throw IllegalArgumentException("set_progress value must be between 0.0 and 1.0")
                }
                b.putFloat(AccessibilityNodeInfo.ACTION_ARGUMENT_PROGRESS_VALUE, v.toFloat())
            } else if ("scroll_to_position" == name) {
                val row = requireIntArg(a, "row", name)
                if (row < 0) {
                    throw IllegalArgumentException("scroll_to_position needs row >= 0")
                }
                b.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_ROW_INT, row)
            } else if ("press_and_hold" == name) {
                val duration = a.opt("duration_ms")
                if (duration != null) {
                    val ms = requireIntArg(a, "duration_ms", name)
                    if (ms < 1 || ms > 60000) {
                        throw IllegalArgumentException("press_and_hold duration_ms must be 1..60000")
                    }
                    b.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_PRESS_AND_HOLD_DURATION_MILLIS_INT, ms)
                }
            } else if ("set_text" == name) {
                val text = a.opt("text")
                if (text !is String) {
                    throw IllegalArgumentException("set_text needs text")
                }
                b.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
            }
            return b
        }

        /** P1-07 (CONS-014): the argument keys each action accepts. */
        private fun allowedNodeActionArgs(name: String): Set<String> {
            return when (name) {
                "set_selection" -> setOf("start", "end")
                "set_progress" -> setOf("value")
                "scroll_to_position" -> setOf("row")
                "press_and_hold" -> setOf("duration_ms")
                "set_text" -> setOf("text")
                else -> emptySet()
            }
        }

        /**
         * P1-07 (CONS-014): an integer argument. A JSON decimal
         * (1.8) used to truncate silently via Number.intValue(); a
         * missing or non-numeric value is a different error. An
         * integral double (2.0) IS an integer and is accepted, but
         * 1.8, NaN, infinities, and out-of-int-range values are
         * rejected with IllegalArgumentException (-> HTTP 400).
         */
        private fun requireIntArg(args: JSONObject, key: String, action: String): Int {
            val v = args.opt(key)
            if (v !is Number) {
                throw IllegalArgumentException("$action needs integer $key")
            }
            val d = v.toDouble()
            if (d.isNaN() || d.isInfinite() || d != floor(d)
                || d < Int.MIN_VALUE || d > Int.MAX_VALUE) {
                throw IllegalArgumentException("$action needs integer $key (got $v)")
            }
            return d.toInt()
        }

        /**
         * A-4: canonical form of a batch payload for the payload hash.
         * Fixed field order, defaults already filled by the HTTP layer,
         * strings length-prefixed so no separator can be ambiguous,
         * numbers and booleans in plain form. Two payloads hash equal
         * exactly when their normalized content is equal; the batch_id
         * is NOT part of the hash (it is the lookup key).
         */
        private fun canonicalBatchPayload(
            protocolVersion: Int,
            treeHash: Int,
            actions: List<BatchAction>
        ): String {
            val sb = StringBuilder()
            sb.append("v").append(protocolVersion)
            sb.append("|tree=").append(treeHash)
            sb.append("|actions=[")
            for (i in actions.indices) {
                val a = actions[i]
                if (i > 0) {
                    sb.append(";")
                }
                sb.append("{")
                appendCanon(sb, "id", a.actionId)
                sb.append(",")
                appendCanon(sb, "type", a.type)
                sb.append(",")
                appendCanon(sb, "node", a.nodeId)
                sb.append(",")
                sb.append("x=").append(a.x)
                sb.append(",y=").append(a.y)
                sb.append(",x1=").append(a.x1)
                sb.append(",y1=").append(a.y1)
                sb.append(",x2=").append(a.x2)
                sb.append(",y2=").append(a.y2)
                sb.append(",dur=").append(a.durationMs)
                sb.append(",")
                appendCanon(sb, "text", a.text)
                sb.append(",")
                appendCanon(sb, "key", a.key)
                sb.append(",wait=").append(a.waitMs)
                sb.append(",exp=").append(a.expectChange)
                sb.append("}")
            }
            sb.append("]")
            return sb.toString()
        }

        private fun appendCanon(sb: StringBuilder, name: String, value: String?) {
            val v = value ?: ""
            sb.append(name).append("=").append(v.length).append(":").append(v)
        }

        private fun sha256Hex(input: String): String {
            val md = java.security.MessageDigest.getInstance("SHA-256")
            val digest = md.digest(input.toByteArray(StandardCharsets.UTF_8))
            val hex = StringBuilder(digest.size * 2)
            for (b in digest) {
                hex.append(String.format("%02x", b.toInt() and 0xff))
            }
            return hex.toString()
        }

        /** Content hash of the tree: changes only when the screen changes. */
        private fun computeTreeHash(nodes: List<Node>): Int {
            var h = 17
            for (n in nodes) {
                h = 31 * h + n.className.hashCode()
                h = 31 * h + n.text.hashCode()
                h = 31 * h + n.left
                h = 31 * h + n.top
                h = 31 * h + n.right
                h = 31 * h + n.bottom
                h = 31 * h + if (n.clickable) 1 else 0
                h = 31 * h + if (n.enabled) 1 else 0
            }
            return h
        }

        /** Short names for the actions the operator cares about. */
        private fun actionName(id: Int): String? {
            return when (id) {
                AccessibilityNodeInfo.ACTION_CLICK -> "click"
                AccessibilityNodeInfo.ACTION_LONG_CLICK -> "long_click"
                AccessibilityNodeInfo.ACTION_FOCUS -> "focus"
                AccessibilityNodeInfo.ACTION_CLEAR_FOCUS -> "clear_focus"
                AccessibilityNodeInfo.ACTION_SET_TEXT -> "set_text"
                AccessibilityNodeInfo.ACTION_SCROLL_FORWARD -> "scroll_forward"
                AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD -> "scroll_backward"
                AccessibilityNodeInfo.ACTION_SELECT -> "select"
                AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS -> "accessibility_focus"
                else -> null
            }
        }
    }

    private val executor: ExecutorService = Executors.newSingleThreadExecutor()

    /**
     * A-3 (v1.9): the single execution context. Accessibility work
     * runs on the service main thread (Codex Phase A baseline): HTTP
     * worker threads never touch AccessibilityNodeInfo directly, they
     * post to the main thread and wait. runOnMainThread() runs the
     * task directly when already on the main thread, so a main-thread
     * caller (waitForChange inside runBatch) cannot deadlock itself.
     */
    private var mainHandler: Handler? = null

    /** Accessibility work timed out waiting for the main thread (-> 503). */
    open class ServiceTimeoutException(message: String) : Exception(message)

    private fun <T> runOnMainThread(task: Callable<T>): T {
        return runOnMainThread(task, MAIN_THREAD_TIMEOUT_MS)
    }

    /** Same as runOnMainThread(task) but with a caller-chosen
     * wait bound. P0-4 fix (CONS-027 P1 #1): the /launch_app
     * verification must not let one main-thread stall overrun
     * the ~3 s verification window, so the foreground read waits
     * at most the remaining verification budget. A timeout here
     * bounds only how long the CALLER waits: future.cancel(true)
     * interrupts the waiting get() but cannot stop work already
     * running on the main thread — that work runs to completion
     * there, and the caller treats the read as missed (a missed
     * poll, never a false success). */
    private fun <T> runOnMainThread(task: Callable<T>, timeoutMs: Long): T {
        val handler = mainHandler
        if (handler == null || Looper.myLooper() == Looper.getMainLooper()) {
            return task.call()
        }
        val future = FutureTask(task)
        handler.post(future)
        try {
            return future.get(timeoutMs, TimeUnit.MILLISECONDS)
        } catch (e: TimeoutException) {
            future.cancel(true)
            throw ServiceTimeoutException("accessibility work timed out on the main thread")
        } catch (e: ExecutionException) {
            val cause = e.cause
            if (cause is Exception) {
                throw cause
            }
            throw RuntimeException(cause)
        }
    }

    /** Monotonic snapshot counter (Phase 1): every /state gets a new revision. */
    private val revision = AtomicInteger(0)

    /**
     * Live nodes from the most recent tree walk (Phase 2, redefined
     * in A-3). They are the latest walk's nodes only: no action ever
     * touches them directly — every node action takes a fresh walk
     * first (resolveFresh) and uses that walk's nodes inside the
     * same operation. The map is recycled and rebuilt on every
     * currentState() call; a walk that fails recycles its partial
     * map instead of leaking it.
     */
    private val lastNodes: MutableMap<String, AccessibilityNodeInfo> = LinkedHashMap()
    private var lastRevision = -1
    private var lastTreeHash = 0

    /** SPEC-BATCH-PERSIST-001 v2.8 (C-01, Decision 103): the
     * BatchRegistry is the sole authority for batch identity, state,
     * and persistence. The old in-memory batchCache is gone (SPEC
     * §2.1: no dual management). The Registry is created when the
     * service connects; the HTTP server reaches it through
     * getBatchRegistry(). Volatile (CONS-152 P2-02): the field is
     * written on the service main thread in onServiceConnected()
     * and read from HTTP worker threads, so the publication must
     * be explicit. */
    @Volatile
    private var batchRegistry: BatchRegistry? = null

    /** The BatchRegistry for this service (null until the service
     * connects). The HTTP server uses it for admission and for the
     * batch management endpoints. */
    fun getBatchRegistry(): BatchRegistry? {
        return batchRegistry
    }

    /** A-4: same batch_id, different payload — the id is in use. */
    open class BatchMismatchException(message: String) : Exception(message)

    /** A-4: batch_id was used before but its result expired (tombstone). */
    open class BatchGoneException(message: String) : Exception(message)

    /**
     * Phase 3 (v1.6): event engine. Accessibility events arrive in
     * onAccessibilityEvent on the service main thread; the bridge
     * records a bounded, coalesced history that the VM pulls with
     * GET /events. HTTP handlers run on server threads, so every
     * method touching this state is synchronized on the service.
     * Events are a "something happened" signal, not precise state:
     * the VM refetches /state for the true current tree.
     */
    private val eventBuffer: java.util.ArrayDeque<UiEvent> = java.util.ArrayDeque()
    private val eventSeq = AtomicLong(0)
    /** Seq of the most recently evicted event (0 = nothing evicted). */
    private var lastEventDroppedSeq = 0L

    private fun broadcastA11yStateChanged() {
        try {
            sendBroadcast(Intent(ACTION_A11Y_STATE_CHANGED).setPackage(packageName))
        } catch (_: Exception) {
            // Best effort only; the UI refreshes on resume anyway.
        }
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        // A-3: the main-thread handler is the single execution
        // context for accessibility work.
        mainHandler = Handler(Looper.getMainLooper())
        // C-01 (SPEC-BATCH-PERSIST-001 §2.1): the BatchRegistry is
        // the sole authority for batch records. Created once; a
        // reconnect reuses it (init() is idempotent and the
        // Registry keeps its state across a disconnect — a batch
        // that was executing stays IN_PROGRESS until it reports
        // back or the process restarts, SPEC §2.8).
        if (batchRegistry == null) {
            val registry = BatchRegistry(filesDir)
            registry.init()
            batchRegistry = registry
        }
        broadcastA11yStateChanged()
    }

    override fun onDestroy() {
        instance = null
        executor.shutdownNow()
        broadcastA11yStateChanged()
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        // Phase 3 (v1.6): record a bounded, coalesced history for
        // GET /events. Never let event handling break the service.
        try {
            recordEvent(event)
        } catch (_: Exception) {
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
    @Synchronized
    private fun recordEvent(event: AccessibilityEvent) {
        val type = event.eventType
        if (type != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
            && type != AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED
            && type != AccessibilityEvent.TYPE_VIEW_CLICKED
            && type != AccessibilityEvent.TYPE_VIEW_FOCUSED
            && type != AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED
            && type != AccessibilityEvent.TYPE_VIEW_SCROLLED
            && type != AccessibilityEvent.TYPE_VIEW_SELECTED) {
            return
        }
        val e = UiEvent()
        e.t = event.eventTime
        e.type = AccessibilityEvent.eventTypeToString(type)
        val pkg = event.packageName
        e.packageName = pkg?.toString() ?: ""
        val cls = event.className
        e.className = cls?.toString() ?: ""
        e.left = -1
        e.top = -1
        e.right = -1
        e.bottom = -1
        var password = false
        var nodeText = ""
        var nodeDesc = ""
        var src: AccessibilityNodeInfo? = null
        try {
            src = event.source
            if (src != null) {
                password = src.isPassword()
                val b = Rect()
                src.getBoundsInScreen(b)
                e.left = b.left
                e.top = b.top
                e.right = b.right
                e.bottom = b.bottom
                val t = src.text
                if (t != null) {
                    nodeText = t.toString().trim()
                }
                val d = src.contentDescription
                if (d != null) {
                    nodeDesc = d.toString().trim()
                }
            }
        } catch (_: Exception) {
            // Best effort only; the event still records without bounds.
        } finally {
            if (src != null) {
                try {
                    src.recycle()
                } catch (_: Exception) {
                }
            }
        }
        if (password) {
            e.text = "[REDACTED]"
            e.desc = if (nodeDesc.isEmpty()) "" else "[REDACTED]"
        } else {
            val sb = StringBuilder()
            try {
                for (c in event.text) {
                    if (sb.isNotEmpty()) {
                        sb.append(' ')
                    }
                    sb.append(c)
                }
            } catch (_: Exception) {
            }
            var text = sb.toString().trim()
            if (text.isEmpty()) {
                text = nodeText
            }
            e.text = if (text.length > 120) text.substring(0, 120) else text
            e.desc = if (nodeDesc.length > 120) nodeDesc.substring(0, 120) else nodeDesc
        }
        val last = eventBuffer.peekLast()
        if (last != null && e.t - last.t <= COALESCE_WINDOW_MS
            && last.type == e.type
            && last.packageName == e.packageName
            && last.className == e.className
            && last.text == e.text) {
            last.coalesced++
            return
        }
        e.seq = eventSeq.incrementAndGet()
        eventBuffer.addLast(e)
        if (eventBuffer.size > MAX_EVENTS) {
            val dropped = eventBuffer.removeFirst()
            lastEventDroppedSeq = dropped.seq
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
    @Synchronized
    fun getEvents(since: Long, limit: Int): EventsResult {
        val r = EventsResult()
        r.latestSeq = eventSeq.get()
        r.dropped = since < lastEventDroppedSeq
        for (e in eventBuffer) {
            if (e.seq > since) {
                r.events.add(e)
            }
            if (r.events.size >= limit) {
                break
            }
        }
        r.treeHash = lastTreeHash
        r.revision = lastRevision
        return r
    }

    override fun onInterrupt() {
    }

    /** Screenshot of the default display as PNG bytes (API 30+). */
    @Throws(Exception::class)
    fun takeScreenshotPng(): ByteArray {
        return takeScreenshot(1.0f, "png", 100)
    }

    /**
     * Screenshot with optional downscale and JPEG compression (v1.2).
     * scale is clamped to 0.1–1.0; format is "png" or "jpeg"
     * (anything else falls back to PNG); quality 10–100 applies to
     * JPEG only. Full-res PNG stays the default for callers that
     * need every pixel; the VM client uses half-scale JPEG for
     * routine checks because a 1080x2340 wallpaper PNG is ~2 MB
     * and costs ~2 s of transfer over the tunnel.
     *
     * format is nullable: HttpBridgeServer passes the raw query
     * param, which is null when ?format= is absent — the Java
     * equalsIgnoreCase on the literal is null-safe, so null falls
     * back to PNG exactly as before.
     */
    @Throws(Exception::class)
    fun takeScreenshot(scale: Float, format: String?, quality: Int): ByteArray {
        val latch = CountDownLatch(1)
        val out = arrayOfNulls<ByteArray>(1)
        val err = intArrayOf(-1)
        val useScale = maxOf(0.1f, minOf(1.0f, scale))
        val useJpeg = "jpeg".equals(format, ignoreCase = true) || "jpg".equals(format, ignoreCase = true)
        val useQuality = maxOf(10, minOf(100, quality))
        takeScreenshot(Display.DEFAULT_DISPLAY, executor,
            object : TakeScreenshotCallback {
                override fun onSuccess(result: ScreenshotResult) {
                    var hb: android.hardware.HardwareBuffer? = null
                    try {
                        hb = result.hardwareBuffer
                        val hw = Bitmap.wrapHardwareBuffer(hb, result.colorSpace)
                        if (hw == null) {
                            err[0] = -3
                            return
                        }
                        var sw = hw.copy(Bitmap.Config.ARGB_8888, false)
                        if (useScale < 1.0f) {
                            val w = maxOf(1, Math.round(sw.width * useScale))
                            val h = maxOf(1, Math.round(sw.height * useScale))
                            val small = Bitmap.createScaledBitmap(sw, w, h, true)
                            sw.recycle()
                            sw = small
                        }
                        val bos = ByteArrayOutputStream()
                        if (useJpeg) {
                            sw.compress(Bitmap.CompressFormat.JPEG, useQuality, bos)
                        } else {
                            sw.compress(Bitmap.CompressFormat.PNG, 100, bos)
                        }
                        sw.recycle()
                        out[0] = bos.toByteArray()
                    } catch (e: Exception) {
                        err[0] = -2
                    } finally {
                        hb?.close()
                        latch.countDown()
                    }
                }

                override fun onFailure(errorCode: Int) {
                    err[0] = errorCode
                    latch.countDown()
                }
            })
        if (!latch.await(10, TimeUnit.SECONDS)) {
            throw Exception("screenshot timed out")
        }
        val bytes = out[0]
            ?: throw Exception("screenshot failed (error ${err[0]})")
        return bytes
    }

    @Throws(Exception::class)
    private fun runGesture(path: Path, durationMs: Long): Boolean {
        val desc = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0L, durationMs))
            .build()
        val latch = CountDownLatch(1)
        val ok = booleanArrayOf(false)
        val dispatched = dispatchGesture(desc, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription) {
                ok[0] = true
                latch.countDown()
            }

            override fun onCancelled(g: GestureDescription) {
                latch.countDown()
            }
        }, null)
        if (!dispatched) {
            throw Exception("gesture rejected by the system "
                + "(dispatchGesture returned false — is canPerformGestures set?)")
        }
        if (!latch.await(5, TimeUnit.SECONDS)) {
            throw Exception("gesture timed out")
        }
        return ok[0]
    }

    @Throws(Exception::class)
    fun tap(x: Int, y: Int): Boolean {
        val path = Path()
        path.moveTo(x.toFloat(), y.toFloat())
        // A moveTo-only path is degenerate: the gesture system may drop
        // it without ever calling the callback. A 1px line makes it a
        // real stroke.
        path.lineTo((x + 1).toFloat(), y.toFloat())
        return runGesture(path, 100)
    }

    @Throws(Exception::class)
    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Long): Boolean {
        val path = Path()
        path.moveTo(x1.toFloat(), y1.toFloat())
        path.lineTo(x2.toFloat(), y2.toFloat())
        return runGesture(path, durationMs)
    }

    /**
     * pressKey is null-tolerant like the Java original: the
     * equalsIgnoreCase calls sit on the literals, so a null key
     * matches nothing and returns false.
     */
    fun pressKey(key: String?): Boolean {
        val action: Int
        if ("HOME".equals(key, ignoreCase = true)) {
            action = GLOBAL_ACTION_HOME
        } else if ("BACK".equals(key, ignoreCase = true)) {
            action = GLOBAL_ACTION_BACK
        } else if ("RECENTS".equals(key, ignoreCase = true)) {
            action = GLOBAL_ACTION_RECENTS
        } else if ("POWER".equals(key, ignoreCase = true)) {
            action = GLOBAL_ACTION_POWER_DIALOG
        } else if ("DPAD_UP".equals(key, ignoreCase = true)) {
            // Decision 66 (v1.9.9): DPAD global actions (API 33+)
            // move focus, not the viewport — in a list, focus
            // movement scrolls as a side effect. Whether a given
            // app scrolls on DPAD_DOWN is the app's behaviour,
            // not the bridge's.
            action = GLOBAL_ACTION_DPAD_UP
        } else if ("DPAD_DOWN".equals(key, ignoreCase = true)) {
            action = GLOBAL_ACTION_DPAD_DOWN
        } else if ("DPAD_LEFT".equals(key, ignoreCase = true)) {
            action = GLOBAL_ACTION_DPAD_LEFT
        } else if ("DPAD_RIGHT".equals(key, ignoreCase = true)) {
            action = GLOBAL_ACTION_DPAD_RIGHT
        } else if ("DPAD_CENTER".equals(key, ignoreCase = true)) {
            action = GLOBAL_ACTION_DPAD_CENTER
        } else {
            return false
        }
        return performGlobalAction(action)
    }

    /**
     * Scrolls a node by id (decision 67, v1.9.10): ACTION_SCROLL_FORWARD
     * or ACTION_SCROLL_BACKWARD on the node or its first scrollable
     * ancestor (the scroll handler is often on an ancestor, like the
     * clickable one). A node that is not scrollable cannot run (422).
     * Runs on the main thread (A-3).
     */
    @Throws(Exception::class)
    fun scrollNode(nodeId: String?, treeHash: Int, direction: String?): ActionOutcome {
        return runOnMainThread(Callable {
            scrollNodeInternal(nodeId, treeHash, direction)
        })
    }

    @Synchronized
    @Throws(Exception::class)
    private fun scrollNodeInternal(
        nodeId: String?,
        treeHash: Int,
        direction: String?
    ): ActionOutcome {
        val action: Int
        if ("forward".equals(direction, ignoreCase = true)) {
            action = AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
        } else if ("backward".equals(direction, ignoreCase = true)) {
            action = AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
        } else {
            throw IllegalArgumentException("unknown direction: $direction")
        }
        val out = ActionOutcome()
        val node = resolveFresh(nodeId, treeHash)
        out.snapshotId = lastRevision
        var target: AccessibilityNodeInfo? = node
        while (target != null && !target.isScrollable()) {
            val parent = target.parent
            if (target !== node) {
                target.recycle()
            }
            target = parent
        }
        if (target == null) {
            out.ok = false
            return out
        }
        if (!target.isEnabled()) {
            if (target !== node) {
                target.recycle()
            }
            throw NodeNotFoundException("node $nodeId is disabled")
        }
        try {
            out.ok = target.performAction(action)
            return out
        } finally {
            if (target !== node) {
                target.recycle()
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
    @Throws(Exception::class)
    fun nodeAction(
        nodeId: String?,
        treeHash: Int,
        actionName: String?,
        args: JSONObject?
    ): ActionOutcome {
        return runOnMainThread(Callable {
            nodeActionInternal(nodeId, treeHash, actionName, args)
        })
    }

    @Synchronized
    @Throws(Exception::class)
    private fun nodeActionInternal(
        nodeId: String?,
        treeHash: Int,
        actionName: String?,
        args: JSONObject?
    ): ActionOutcome {
        val actionId = actionIdForName(actionName)
        if (actionId < 0) {
            throw IllegalArgumentException("unknown action: $actionName")
        }
        // actionName is non-null here: actionIdForName(null) is -1,
        // which threw above — mirroring the Java control flow.
        val bundle = nodeActionArgs(actionName!!, args)
        val out = ActionOutcome()
        val node = resolveFresh(nodeId, treeHash)
        out.snapshotId = lastRevision
        var target: AccessibilityNodeInfo? = node
        while (target != null && !supportsAction(target, actionId)) {
            val parent = target.parent
            if (target !== node) {
                target.recycle()
            }
            target = parent
        }
        if (target == null) {
            out.ok = false
            return out
        }
        if (!target.isEnabled()) {
            if (target !== node) {
                target.recycle()
            }
            throw NodeNotFoundException("node $nodeId is disabled")
        }
        try {
            out.ok = target.performAction(actionId, bundle)
            return out
        } finally {
            if (target !== node) {
                target.recycle()
            }
        }
    }

    /**
     * Set text on the currently focused editable field. Returns false when
     * there is no focused editable field (tap the field first).
     * Phase C-1 (P0-02): runs on the main thread like every other
     * node action (A-3), and recycles root/target on all paths.
     */
    @Throws(Exception::class)
    fun typeText(text: String?): Boolean {
        return runOnMainThread(Callable {
            typeTextInternal(text)
        })
    }

    private fun typeTextInternal(text: String?): Boolean {
        val root = rootInActiveWindow ?: return false
        try {
            var target = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            if (target == null) {
                target = root.findFocus(AccessibilityNodeInfo.FOCUS_ACCESSIBILITY)
            }
            if (target == null || !target.isEditable()) {
                if (target != null && target !== root) {
                    target.recycle()
                }
                return false
            }
            try {
                target.refresh()
                val args = Bundle()
                args.putCharSequence(
                    AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
                return target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
            } finally {
                if (target !== root) {
                    target.recycle()
                }
            }
        } finally {
            root.recycle()
        }
    }

    /**
     * P0-4 (v2.0): the package in the foreground right now, or
     * null when there is no active window. Runs on the main
     * thread (A-3) like every other accessibility read;
     * side-effect free — it only reads the root's package name
     * and recycles it. The HTTP server calls this after issuing
     * a launch to verify the target actually came forward;
     * callers must treat null as "not observed", never as a
     * launch failure.
     */
    @Throws(Exception::class)
    fun getForegroundPackage(): String? {
        return getForegroundPackage(MAIN_THREAD_TIMEOUT_MS)
    }

    /** Same as getForegroundPackage() but the caller bounds how
     * long the read may wait. P0-4 fix (CONS-027 P1 #1): the
     * /launch_app verification passes the remaining verification
     * budget here, so a main-thread stall cannot overrun the
     * ~3 s window. A timeout is a missed observation (the caller
     * keeps polling and only claims `launched` on a positive
     * match), never a launch failure and never a false success. */
    @Throws(Exception::class)
    fun getForegroundPackage(timeoutMs: Long): String? {
        return runOnMainThread(Callable {
            val root = rootInActiveWindow ?: return@Callable null
            try {
                val pkg = root.packageName
                pkg?.toString()
            } finally {
                root.recycle()
            }
        }, timeoutMs)
    }

    // ---------------------------------------------------------------
    // C-04: foreground guard (SPEC-FOREGROUND-GUARD-001 §2.2).
    // ---------------------------------------------------------------

    /** C-04 guard failure. `errorId` is the machine-readable
     * 409/503 identifier the server returns
     * (`foreground_mismatch`, `tree_hash_mismatch`,
     * `generation_mismatch`, `session_mismatch`,
     * `foreground_unverifiable`, `snapshot_unavailable`). A
     * thrown guard means nothing was dispatched. For a
     * foreground_mismatch the expected/current packages ride
     * along so the 409 response can name both (SPEC §2.2). */
    open class OperationGuardException(
        val errorId: String,
        message: String,
        val expectedPackage: String? = null,
        val currentPackage: String? = null
    ) : Exception(message)

    /**
     * Shared guard check (C-04 §2.2). The caller supplies the
     * foreground package — a fresh root read on the main thread,
     * or the getForegroundPackage() round-trip from a worker
     * thread. The snapshot pair (tree hash, revision) is read as
     * one unit under synchronized(this), so the two can never
     * come from different snapshots. `generation` is compared
     * against the LATEST snapshot revision (lastRevision), not a
     * fresh walk: a fresh walk would increment the revision and
     * the supplied generation could never match. A new snapshot
     * taken between the VM's /state read and this check is itself
     * the staleness signal.
     * @throws OperationGuardException on the first failing check.
     * @return the snapshot revision the checks validated against.
     */
    @Throws(OperationGuardException::class)
    fun checkGuard(
        foregroundPackage: String?,
        expectedPackage: String,
        treeHash: Int?,
        generation: Int?,
        isSessionValid: () -> Boolean
    ): Int {
        if (foregroundPackage == null) {
            throw OperationGuardException("foreground_unverifiable",
                "the foreground package cannot be determined")
        }
        val snapshotTreeHash: Int
        val snapshotRevision: Int
        synchronized(this) {
            snapshotTreeHash = lastTreeHash
            snapshotRevision = lastRevision
        }
        if (snapshotRevision < 0) {
            throw OperationGuardException("snapshot_unavailable",
                "no snapshot yet — fetch /state first")
        }
        if (foregroundPackage != expectedPackage) {
            throw OperationGuardException("foreground_mismatch",
                "foreground is $foregroundPackage, expected $expectedPackage",
                expectedPackage, foregroundPackage)
        }
        if (treeHash != null && treeHash != snapshotTreeHash) {
            throw OperationGuardException("tree_hash_mismatch",
                "screen changed since that /state (tree hash moved) — refetch state")
        }
        if (generation != null && generation != snapshotRevision) {
            throw OperationGuardException("generation_mismatch",
                "screen changed since that /state (generation moved) — refetch state")
        }
        if (!isSessionValid()) {
            throw OperationGuardException("session_mismatch",
                "the operation session is not active")
        }
        return snapshotRevision
    }

    /**
     * C-04 §2.2: the serialized execution admission boundary for
     * a non-node operation. The whole guard — fresh foreground
     * read, snapshot validation, session check, and the dispatch
     * itself — runs inside ONE runOnMainThread task, so no other
     * Bridge operation can interleave between validation and
     * dispatch (the main thread is the single execution context,
     * decision 48). The HTTP worker posts here and waits without
     * holding any lock (A-3, 20 s timeout → 503). The root is read
     * directly (not via getForegroundPackage(), which would be a
     * second main-thread round-trip from inside this task).
     * @throws OperationGuardException (nothing dispatched).
     */
    @Throws(Exception::class)
    fun <T> runGuardedOperation(
        expectedPackage: String,
        treeHash: Int?,
        generation: Int?,
        isSessionValid: () -> Boolean,
        action: () -> T
    ): T {
        return runOnMainThread(Callable {
            val root = rootInActiveWindow
            val currentPackage: String?
            try {
                currentPackage = root?.packageName?.toString()
            } finally {
                root?.recycle()
            }
            val validatedRevision = checkGuard(
                currentPackage, expectedPackage, treeHash, generation, isSessionValid)
            // Re-check: the SPEC's invalidation point. No snapshot
            // can be produced inside this main-thread task (snapshots
            // are taken on the main thread only), so this read cannot
            // differ — it documents the boundary.
            val revisionAfter: Int
            synchronized(this) {
                revisionAfter = lastRevision
            }
            if (revisionAfter != validatedRevision) {
                throw OperationGuardException("generation_mismatch",
                    "a new snapshot appeared between validation and dispatch")
            }
            action()
        })
    }

    // ---------------------------------------------------------------
    // Phase 2: node-targeted actions and batches.
    // ---------------------------------------------------------------

    /** The screen moved on since the /state the client used. */
    open class StaleRevisionException(message: String) : Exception(message)

    /** No live node with that id in the current snapshot. */
    open class NodeNotFoundException(message: String) : Exception(message)

    /** Result of a node action: whether it ran, and the snapshot
     * (fresh walk) it resolved against. */
    open class ActionOutcome {
        @JvmField var ok: Boolean = false
        @JvmField var snapshotId: Int = -1
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
    @Throws(StaleRevisionException::class, NodeNotFoundException::class)
    private fun resolveFresh(nodeId: String?, treeHash: Int): AccessibilityNodeInfo {
        val fresh = currentStateInternal()
        if (treeHash != fresh.treeHash) {
            throw StaleRevisionException(
                "screen changed since that /state (tree hash moved) — refetch state")
        }
        // lastNodes is keyed by non-null ids; a null id can never
        // match, so it takes the same NodeNotFound path as the Java
        // Map.get(null) (LinkedHashMap permits a null key lookup).
        val node: AccessibilityNodeInfo? = if (nodeId == null) null else lastNodes[nodeId]
        if (node == null) {
            throw NodeNotFoundException("no node $nodeId in this snapshot")
        }
        if (!node.isVisibleToUser()) {
            throw NodeNotFoundException("node $nodeId is not visible")
        }
        return node
    }

    /**
     * Clicks a node by id. When the node itself is not clickable, the
     * handler is often on an ancestor (GPT design), so the first
     * clickable ancestor is used instead; a clickable target that is
     * disabled cannot run (422). Runs on the main thread (A-3).
     */
    @Throws(Exception::class)
    fun clickNode(nodeId: String?, treeHash: Int): ActionOutcome {
        return runOnMainThread(Callable {
            clickNodeInternal(nodeId, treeHash)
        })
    }

    @Synchronized
    @Throws(Exception::class)
    private fun clickNodeInternal(nodeId: String?, treeHash: Int): ActionOutcome {
        val out = ActionOutcome()
        val node = resolveFresh(nodeId, treeHash)
        out.snapshotId = lastRevision
        var target: AccessibilityNodeInfo? = node
        while (target != null && !target.isClickable()) {
            val parent = target.parent
            if (target !== node) {
                target.recycle()
            }
            target = parent
        }
        if (target == null) {
            out.ok = false
            return out
        }
        if (!target.isEnabled()) {
            if (target !== node) {
                target.recycle()
            }
            throw NodeNotFoundException("node $nodeId is disabled")
        }
        try {
            out.ok = target.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            return out
        } finally {
            if (target !== node) {
                target.recycle()
            }
        }
    }

    /** Sets text on an editable node by id. Runs on the main thread (A-3). */
    @Throws(Exception::class)
    fun typeNode(nodeId: String?, treeHash: Int, text: String?): ActionOutcome {
        return runOnMainThread(Callable {
            typeNodeInternal(nodeId, treeHash, text)
        })
    }

    @Synchronized
    @Throws(Exception::class)
    private fun typeNodeInternal(
        nodeId: String?,
        treeHash: Int,
        text: String?
    ): ActionOutcome {
        val out = ActionOutcome()
        val node = resolveFresh(nodeId, treeHash)
        out.snapshotId = lastRevision
        // Password fields accept typed text (that is how a password is
        // entered); the privacy rule is that their value never leaves
        // the phone (redacted in /state and /events), not a refusal.
        if (!node.isEditable() || !node.isEnabled()) {
            out.ok = false
            return out
        }
        val args = Bundle()
        args.putCharSequence(
            AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        out.ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
        return out
    }

    /** One step of a batch. Only the fields its type needs are set. */
    open class BatchAction {
        @JvmField var actionId: String = ""
        @JvmField var type: String = ""
        @JvmField var nodeId: String = ""
        @JvmField var x: Int = 0
        @JvmField var y: Int = 0
        @JvmField var x1: Int = 0
        @JvmField var y1: Int = 0
        @JvmField var x2: Int = 0
        @JvmField var y2: Int = 0
        @JvmField var durationMs: Long = 300
        @JvmField var text: String = ""
        @JvmField var key: String = ""
        @JvmField var waitMs: Long = 0
        @JvmField var expectChange: Boolean = false
    }

    open class ActionResult {
        @JvmField var actionId: String = ""
        @JvmField var status: String = ""
        @JvmField var error: String = ""
        @JvmField var hashBefore: Int = 0
        @JvmField var hashAfter: Int = 0
    }

    // The explicit empty primary constructor matters: a class
    // with only a secondary constructor gets NO no-arg constructor
    // in Kotlin, and Java callers (HttpBridgeServer) and this file
    // both construct BatchResult() with no arguments.
    open class BatchResult() {
        @JvmField var batchId: String = ""
        @JvmField var replayed: Boolean = false
        @JvmField var results: MutableList<ActionResult> = ArrayList()
        @JvmField var stoppedAt: Int = -1
        @JvmField var stopReason: String = ""
        /** Revision of the snapshot the entry check used (A-3). */
        @JvmField var snapshotId: Int = -1
        /** A-4: "completed", "partial", "failed", or "running"
         * (a re-send that arrived while the first run was in flight). */
        @JvmField var status: String = ""
        /** A-4: SHA-256 of the canonical payload (see canonicalBatchPayload). */
        @JvmField var payloadHash: String = ""
        /** A-4: action_ids Android confirmed ran. "Ran" means the
         * action was dispatched and confirmed by Android — NOT that
         * the target app finished its business operation. */
        @JvmField var completedActionIds: MutableList<String> = ArrayList()
        /** A-4: the action_id that failed and stopped the batch ("" if none). */
        @JvmField var failedActionId: String = ""

        /** Copy for a replayed response (the original stays cached).
         * NOTE: `replayed` is deliberately NOT copied — the Java
         * copy constructor leaves it at its default (false) and
         * BatchRegistry sets it to true on the copy afterwards. */
        constructor(other: BatchResult) : this() {
            this.batchId = other.batchId
            this.results = other.results
            this.stoppedAt = other.stoppedAt
            this.stopReason = other.stopReason
            this.snapshotId = other.snapshotId
            this.status = other.status
            this.payloadHash = other.payloadHash
            this.completedActionIds = other.completedActionIds
            this.failedActionId = other.failedActionId
        }
    }

    /**
     * Runs a batch against one snapshot (Phase 2, coordinator in
     * A-4; C-01: the BatchRegistry is now the sole authority for
     * batch identity and persistence, SPEC-BATCH-PERSIST-001 §2.1).
     * Steps execute in order; the first failure stops the batch. A
     * step with expectChange waits up to 2 s for the tree hash to
     * move: when it moves, the batch stops after that step (the
     * screen changed, so later node ids are invalid — the client
     * refetches /state and sends the next batch).
     *
     * Fixed order (SPEC §2.3): the A-3 entry check below, then the
     * Registry admission (ID check + IN_PROGRESS persist, under the
     * Registry lock); execution on the main thread, outside the
     * Registry lock; the result persist; respond. A re-send with
     * the same id AND the same hash returns the stored result
     * (replayed: true) without re-executing; while the first run is
     * still in flight it returns status "running" (replayed: true);
     * an UNKNOWN id returns status "unknown" (replayed: false) and
     * is never re-executed. The same id with a different hash is a
     * 409; an id whose result expired is a 410 until its tombstone
     * lapses. Records now survive a process restart (decision 49's
     * memory-only constraint is superseded for the batch-record
     * scope by SPEC-BATCH-PERSIST-001).
     *
     * @param supersedes the batch_id of the UNKNOWN record this
     * batch replaces, or null (SPEC §2.6).
     */
    @Throws(Exception::class)
    fun runBatch(
        batchId: String,
        protocolVersion: Int,
        treeHash: Int,
        actions: List<BatchAction>,
        supersedes: String?,
        guardExpectedPackage: String? = null,
        guardTreeHash: Int? = null,
        guardGeneration: Int? = null,
        guardSessionValid: (() -> Boolean)? = null,
        // CONS-167 P1-02: invoked inside runBatchInternal, after the
        // final in-task guard passes and inside the same main-thread
        // task as the actions — the server's operation hook
        // (noteOperation) runs there, never before. Null means no
        // hook (a direct runBatch caller with no session to start).
        onFinalGuardPassed: (() -> Unit)? = null
    ): BatchResult {
        val payloadHash = sha256Hex(canonicalBatchPayload(protocolVersion, treeHash, actions))
        // A-3 entry check (unchanged): the snapshot the batch was
        // built against must still be current. This runs before the
        // Registry admission, so a stale batch never creates a
        // record — and a replay with a moved tree hash still gets a
        // 409, as before.
        val entrySnapshotId: Int
        synchronized(this) {
            if (lastRevision < 0) {
                throw NodeNotFoundException("no /state snapshot yet — fetch /state first")
            }
            if (treeHash != lastTreeHash) {
                throw StaleRevisionException(
                    "screen changed since that /state (tree hash moved) — refetch state")
            }
            entrySnapshotId = lastRevision
        }
        val registry = batchRegistry
            ?: throw BatchStoreException(
                "batch_store_unavailable",
                "batch registry is not ready — the accessibility service is still connecting")
        // C-04 (SPEC-FOREGROUND-GUARD-001 §2.2): the worker-side
        // guard validation, next to the A-3 entry check and before
        // admission, so a guard-rejected batch never creates a
        // record. The foreground comes from the getForegroundPackage()
        // round-trip (a worker cannot read the root directly); the
        // snapshot pair is read inside checkGuard. This is the
        // pre-admission check only — runBatchInternal re-validates
        // inside the execution task, which is the true boundary.
        if (guardExpectedPackage != null) {
            val foregroundPackage = getForegroundPackage()
            checkGuard(foregroundPackage, guardExpectedPackage, guardTreeHash,
                guardGeneration, guardSessionValid ?: { true })
        }
        // Registry admission (SPEC §2.3): the ID check and the
        // IN_PROGRESS persist, serialized under the Registry lock.
        // The 409/410/503 cases throw here; nothing has executed.
        val decision = registry.admit(batchId, payloadHash, supersedes)
        when (decision.kind) {
            AdmitKind.REPLAY -> return decision.result!!
            AdmitKind.RUNNING -> {
                val running = BatchResult()
                running.batchId = batchId
                running.replayed = true
                running.status = "running"
                running.payloadHash = payloadHash
                running.snapshotId = entrySnapshotId
                return running
            }
            AdmitKind.UNKNOWN -> {
                val unknown = BatchResult()
                unknown.batchId = batchId
                unknown.replayed = false
                unknown.status = "unknown"
                unknown.payloadHash = payloadHash
                unknown.snapshotId = entrySnapshotId
                return unknown
            }
            AdmitKind.NEW -> {
                // Fall through to execution below.
            }
            else -> throw IllegalStateException("unknown admission decision: ${decision.kind}")
        }
        val snapshotId = entrySnapshotId
        val result = try {
            runOnMainThread(Callable {
                runBatchInternal(batchId, treeHash, actions, snapshotId,
                    guardExpectedPackage, guardTreeHash, guardGeneration, guardSessionValid,
                    onFinalGuardPassed)
            })
        } catch (e: OperationGuardException) {
            // C-04: the in-task guard refused the batch after
            // admission — remove the IN_PROGRESS record so a re-send
            // does not answer "running" for a batch that never ran.
            // CONS-167 P2-01: a failed discard is logged, not silent
            // — the record is UNKNOWN in memory then (see
            // discard()), never "running".
            if (!registry.discard(batchId, payloadHash)) {
                BridgeLog.log("batch_registry",
                    "discard did not remove the record for " + batchId + "; see registry log")
            }
            throw e
        }
        result.payloadHash = payloadHash
        // Step 4: persist the result. If that persist fails, the
        // Registry marks the record UNKNOWN in memory (a re-send
        // gets "unknown", never a re-execution); the result is
        // still returned to this caller (SPEC §2.4).
        registry.complete(batchId, payloadHash, result)
        return result
    }

    @Synchronized
    @Throws(Exception::class)
    private fun runBatchInternal(
        batchId: String,
        treeHash: Int,
        actions: List<BatchAction>,
        entrySnapshotId: Int,
        guardExpectedPackage: String? = null,
        guardTreeHash: Int? = null,
        guardGeneration: Int? = null,
        guardSessionValid: (() -> Boolean)? = null,
        onFinalGuardPassed: (() -> Unit)? = null
    ): BatchResult {
        // C-04: the in-task guard — the true admission boundary.
        // This runs inside the same main-thread task as the actions
        // below, so no other Bridge operation can change the context
        // between validation and the first dispatch. A failure here
        // throws before any action executes; runBatch() discards the
        // admitted record on the way out.
        if (guardExpectedPackage != null) {
            val root = rootInActiveWindow
            val currentPackage: String?
            try {
                currentPackage = root?.packageName?.toString()
            } finally {
                root?.recycle()
            }
            checkGuard(currentPackage, guardExpectedPackage, guardTreeHash,
                guardGeneration, guardSessionValid ?: { true })
        }
        // CONS-167 P1-02: the operation hook runs here, after the
        // final guard passed and inside the same main-thread task as
        // the actions — a guard-refused batch starts no session and
        // posts no notification. When there is no guard to pass, the
        // hook still runs at this same point: the start of execution.
        // Lock order: this method holds the service monitor
        // (@Synchronized); the hook takes operationLock — the same
        // order checkGuard uses, so no new lock cycle.
        onFinalGuardPassed?.invoke()
        val result = BatchResult()
        result.batchId = batchId
        result.snapshotId = entrySnapshotId
        for (i in actions.indices) {
            val a = actions[i]
            val r = ActionResult()
            r.actionId = a.actionId
            r.hashBefore = lastTreeHash
            try {
                val ok = executeAction(a, treeHash)
                r.status = if (ok) "executed" else "failed"
                if (!ok) {
                    r.error = "action returned false"
                }
            } catch (e: Exception) {
                r.status = "failed"
                r.error = e.message ?: e.toString()
            }
            r.hashAfter = lastTreeHash
            result.results.add(r)
            if (r.status == "executed") {
                result.completedActionIds.add(r.actionId)
            } else {
                result.failedActionId = r.actionId
            }
            if (r.status == "failed") {
                result.stoppedAt = i
                result.stopReason = r.error
                break
            }
            if (a.expectChange) {
                val changed: Boolean
                try {
                    changed = waitForChange(r.hashBefore, 2000)
                } catch (e: Exception) {
                    r.status = "failed"
                    r.error = e.message ?: e.toString()
                    result.stoppedAt = i
                    result.stopReason = r.error
                    break
                }
                if (changed) {
                    r.hashAfter = lastTreeHash
                    result.stoppedAt = i
                    result.stopReason = "screen changed — refetch state for the next batch"
                    break
                }
                r.status = "failed"
                r.error = "no screen change within 2s"
                result.stoppedAt = i
                result.stopReason = r.error
                break
            }
        }
        // A-4: 0 successes = failed; >= 1 success then stop = partial.
        if (result.completedActionIds.isEmpty()) {
            result.status = "failed"
        } else if (result.stoppedAt >= 0) {
            result.status = "partial"
        } else {
            result.status = "completed"
        }
        // v1.9.3: batch outcome in the debug log (status + counts
        // only — no batch_id, no payload, no node text).
        BridgeLog.log("batch", result.status + " "
            + result.completedActionIds.size + "/" + actions.size)
        return result
    }

    @Throws(Exception::class)
    private fun executeAction(a: BatchAction, treeHash: Int): Boolean {
        return when (a.type) {
            "click_node" -> clickNodeInternal(a.nodeId, treeHash).ok
            "tap" -> tap(a.x, a.y)
            "type_node" -> typeNodeInternal(a.nodeId, treeHash, a.text).ok
            "type" -> typeText(a.text)
            "key" -> pressKey(a.key)
            "swipe" -> swipe(a.x1, a.y1, a.x2, a.y2, a.durationMs)
            "wait" -> {
                Thread.sleep(maxOf(0L, a.waitMs))
                true
            }
            else -> throw IllegalArgumentException("unknown action type: " + a.type)
        }
    }

    /** Polls for a tree-hash change (fresh snapshot each poll). */
    @Throws(Exception::class)
    private fun waitForChange(hashBefore: Int, timeoutMs: Long): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            try {
                Thread.sleep(250)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                return false
            }
            currentState()
            if (lastTreeHash != hashBefore) {
                return true
            }
        }
        return false
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
    @Throws(Exception::class)
    fun currentState(): State {
        return runOnMainThread(Callable {
            currentStateInternal()
        })
    }

    @Synchronized
    private fun currentStateInternal(): State {
        val state = State()
        state.revision = revision.incrementAndGet()
        state.snapshotId = state.revision
        val seqBefore = eventSeq.get()
        try {
            val bounds = (getSystemService(WINDOW_SERVICE) as WindowManager)
                .currentWindowMetrics.bounds
            state.width = bounds.width()
            state.height = bounds.height()
        } catch (e: Exception) {
            state.width = -1
            state.height = -1
        }
        synchronized(this) {
            recycleLastNodes()
            lastRevision = state.revision
        }
        val root = rootInActiveWindow
        if (root != null) {
            // String.valueOf(null) is "null" in the Java original;
            // the Elvis keeps that exact behaviour.
            state.packageName = root.packageName?.toString() ?: "null"
            state.texts = ArrayList()
            collectTexts(root, state.texts, 20)
            state.nodes = ArrayList()
            // Focus is read during the walk (isFocused /
            // isAccessibilityFocused): no separate findFocus call, so
            // the focused id can never point at the wrong twin node.
            val focus = arrayOfNulls<String>(1)
            val a11yFocus = arrayOfNulls<String>(1)
            // The walk keeps the live nodes it visits in lastNodes
            // (Phase 2): they are recycled on the next snapshot.
            val live: MutableMap<String, AccessibilityNodeInfo> = LinkedHashMap()
            var walkOk = false
            try {
                state.truncated = collectNodes(root, null, 0, state.nodes, focus, a11yFocus, live)
                state.focusedId = focus[0] ?: a11yFocus[0]
                state.treeHash = computeTreeHash(state.nodes)
                synchronized(this) {
                    lastNodes.putAll(live)
                    lastTreeHash = state.treeHash
                }
                walkOk = true
            } finally {
                if (!walkOk) {
                    // A-3: a failed walk recycles its partial nodes
                    // instead of leaking them; the root is recycled
                    // only when it never reached the live map.
                    for (n in live.values) {
                        try {
                            n.recycle()
                        } catch (_: Exception) {
                        }
                    }
                    if (!live.containsValue(root)) {
                        try {
                            root.recycle()
                        } catch (_: Exception) {
                        }
                    }
                }
                // The root is n0 in the map on success; do not recycle it here.
            }
        }
        state.imeVisible = isImeVisible()
        state.eventSeq = eventSeq.get()
        state.snapshotStable = seqBefore == state.eventSeq
        state.snapshotValid = true
        return state
    }

    @Synchronized
    private fun recycleLastNodes() {
        for (n in lastNodes.values) {
            try {
                n.recycle()
            } catch (_: Exception) {
            }
        }
        lastNodes.clear()
    }

    /** True when an input-method (keyboard) window is present. */
    private fun isImeVisible(): Boolean {
        try {
            val windows = windows ?: return false
            for (w in windows) {
                try {
                    if (w.type == AccessibilityWindowInfo.TYPE_INPUT_METHOD) {
                        return true
                    }
                } finally {
                    w.recycle()
                }
            }
        } catch (_: Exception) {
            // Best effort only; the field just stays false.
        }
        return false
    }

    /**
     * Depth-first walk of the node tree. Node ids are "n0", "n1", ...
     * in walk order and are only valid for this revision (Phase 1):
     * a later /state gets a new revision and new ids. Returns true when
     * the walk stopped early at MAX_NODES / MAX_DEPTH.
     */
    private fun collectNodes(
        node: AccessibilityNodeInfo?,
        parentId: String?,
        depth: Int,
        out: MutableList<Node>,
        focus: Array<String?>,
        a11yFocus: Array<String?>,
        live: MutableMap<String, AccessibilityNodeInfo>
    ): Boolean {
        if (node == null || depth > MAX_DEPTH) {
            // P1-01 (CONS-014): this early return happens BEFORE
            // the node reaches the live map, so nobody else will
            // recycle it — and the parent obtained it via
            // getChild(), so the parent will not recycle it
            // either. Recycle it here, or it leaks. (The root
            // can never reach this path: depth 0, out empty.)
            if (node != null) {
                try {
                    node.recycle()
                } catch (_: Exception) {
                }
            }
            return depth > MAX_DEPTH
        }
        if (out.size >= MAX_NODES) {
            // P1-01 (CONS-014): same leak — the node is not
            // registered in the live map on this path, so it
            // must be recycled here. (The root can never reach
            // this path: out starts empty.)
            try {
                node.recycle()
            } catch (_: Exception) {
            }
            return true
        }
        val n = Node()
        var added = false
        try {
            n.id = "n" + out.size
            n.parentId = parentId
            val cls = node.className
            n.className = cls?.toString() ?: ""
            // Password fields never expose their value: the operator gets
            // "[REDACTED]" instead (GPT design, Phase 1 privacy rule).
            n.password = node.isPassword()
            val text = node.text
            n.text = if (n.password) "[REDACTED]" else text?.toString() ?: ""
            val desc = node.contentDescription
            // Phase C-1 (P0-01): a password node's description is
            // redacted too — it must never carry the value by
            // another field.
            n.contentDescription = if (n.password) "[REDACTED]" else desc?.toString() ?: ""
            val r = Rect()
            node.getBoundsInScreen(r)
            n.left = r.left
            n.top = r.top
            n.right = r.right
            n.bottom = r.bottom
            n.clickable = node.isClickable()
            n.focusable = node.isFocusable()
            n.focused = node.isFocused()
            n.editable = node.isEditable()
            n.scrollable = node.isScrollable()
            n.enabled = node.isEnabled()
            n.selected = node.isSelected()
            n.inputType = node.inputType
            val resId = node.viewIdResourceName
            n.resourceId = resId?.toString() ?: ""
            n.actions = ArrayList()
            for (a in node.actionList) {
                val name = actionName(a.id)
                if (name != null) {
                    n.actions.add(name)
                }
            }
            out.add(n)
            // A-3: the live node belongs to this walk's map; a node
            // action re-walks before acting, so it never uses a node
            // held from an earlier snapshot. Recycled on the next
            // snapshot (or by the walk-level finally when this walk
            // fails after the put).
            live[n.id] = node
            added = true
            if (n.focused && focus[0] == null) {
                focus[0] = n.id
            }
            if (node.isAccessibilityFocused() && a11yFocus[0] == null) {
                a11yFocus[0] = n.id
            }
            var truncated = false
            for (i in 0 until node.childCount) {
                val child = node.getChild(i) ?: continue
                // No recycle here: the child stays live in the map.
                if (collectNodes(child, n.id, depth + 1, out, focus, a11yFocus, live)) {
                    truncated = true
                    break
                }
                if (out.size >= MAX_NODES) {
                    truncated = true
                    break
                }
            }
            return truncated
        } catch (e: Exception) {
            // A-3: a node that failed before reaching the live map is
            // recycled here; one already in the map is recycled by the
            // walk-level finally in currentStateInternal.
            if (!added) {
                try {
                    node.recycle()
                } catch (_: Exception) {
                }
            }
            throw e
        }
    }

    private fun collectTexts(node: AccessibilityNodeInfo?, out: MutableList<String>, limit: Int) {
        if (node == null || out.size >= limit) {
            return
        }
        val text = node.text
        // Phase C-1 (P0-01): a password node's value must never
        // reach /state's texts[] (the node list already redacts
        // it, but this path did not). Children are still walked.
        if (text != null && node.isVisibleToUser() && !node.isPassword()) {
            val s = text.toString().trim()
            if (s.isNotEmpty() && s.length <= 120) {
                out.add(s)
            }
        }
        for (i in 0 until node.childCount) {
            if (out.size >= limit) {
                break
            }
            val child = node.getChild(i)
            try {
                collectTexts(child, out, limit)
            } finally {
                child?.recycle()
            }
        }
    }

    open class State {
        @JvmField var width: Int = -1
        @JvmField var height: Int = -1
        @JvmField var packageName: String = ""
        @JvmField var texts: MutableList<String> = ArrayList()
        /** Phase 1: monotonic snapshot counter; ids are valid only within it. */
        @JvmField var revision: Int = 0
        /** Phase 2: content hash — changes only when the screen changes. */
        @JvmField var treeHash: Int = 0
        /** Phase 3: latest recorded event seq (0 = none yet). */
        @JvmField var eventSeq: Long = 0
        /** A-3: snapshot id — the same value as revision, exposed so
         * the client can name the snapshot an action ran against. */
        @JvmField var snapshotId: Int = 0
        /** A-3: true when this snapshot is the latest complete walk. */
        @JvmField var snapshotValid: Boolean = false
        /** A-3: false when an accessibility event arrived mid-walk
         * (the tree may be mid-change); the VM should refetch /state
         * before important operations. */
        @JvmField var snapshotStable: Boolean = false
        @JvmField var nodes: MutableList<Node> = ArrayList()
        @JvmField var focusedId: String? = null
        @JvmField var imeVisible: Boolean = false
        @JvmField var truncated: Boolean = false
    }

    /** One node of the screen tree (Phase 1). Bounds are screen pixels. */
    open class Node {
        @JvmField var id: String = ""
        @JvmField var parentId: String? = null
        @JvmField var className: String = ""
        @JvmField var text: String = ""
        @JvmField var contentDescription: String = ""
        @JvmField var left: Int = 0
        @JvmField var top: Int = 0
        @JvmField var right: Int = 0
        @JvmField var bottom: Int = 0
        @JvmField var clickable: Boolean = false
        @JvmField var focusable: Boolean = false
        @JvmField var focused: Boolean = false
        @JvmField var editable: Boolean = false
        @JvmField var scrollable: Boolean = false
        @JvmField var enabled: Boolean = false
        @JvmField var selected: Boolean = false
        @JvmField var password: Boolean = false
        @JvmField var inputType: Int = 0
        @JvmField var resourceId: String = ""
        @JvmField var actions: MutableList<String> = ArrayList()
    }

    /** One recorded accessibility event (Phase 3). */
    open class UiEvent {
        @JvmField var seq: Long = 0
        /** event.getEventTime(): uptime ms, monotonic on-device. */
        @JvmField var t: Long = 0
        @JvmField var type: String = ""
        @JvmField var packageName: String = ""
        @JvmField var className: String = ""
        /** "[REDACTED]" when the source is a password field. */
        @JvmField var text: String = ""
        @JvmField var desc: String = ""
        @JvmField var left: Int = -1
        @JvmField var top: Int = -1
        @JvmField var right: Int = -1
        @JvmField var bottom: Int = -1
        /** Identical repeats merged into this record. */
        @JvmField var coalesced: Int = 0
    }

    /** Result of getEvents: the slice plus the caller's bearings. */
    open class EventsResult {
        @JvmField var events: MutableList<UiEvent> = ArrayList()
        @JvmField var latestSeq: Long = 0
        /** True when events the caller never saw were evicted. */
        @JvmField var dropped: Boolean = false
        @JvmField var treeHash: Int = 0
        @JvmField var revision: Int = -1
    }
}
