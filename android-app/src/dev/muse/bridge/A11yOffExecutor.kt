// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12
package dev.muse.bridge

import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.service.quicksettings.TileService
import android.util.Log
import android.widget.Toast

/**
 * Shared headless executor for turning the accessibility service off
 * (SPEC-A11Y-OFF-002 v6). Extracted from MainActivity (SPEC-A11Y-OFF-001
 * v2.9.3): the widget's "Accessibility off" button and the second QS
 * tile both call [execute]; neither opens MainActivity.
 * No token is involved (D4): the widget is a non-exported receiver
 * with an explicit PendingIntent, and the tile is bound by the system
 * permission BIND_QUICK_SETTINGS_TILE.
 *
 * Semantics (D7, from 001): three-valued state (DISABLED / ENABLED /
 * UNKNOWN); disableSelf() then a 2 s x 3 completion check; never assert
 * success without DISABLED; the server is NOT stopped. The broadcast
 * ACTION_A11Y_STATE_CHANGED is a fast path; the polling is the
 * fallback. Repeated triggers during a disable are ignored silently
 * (D9) — no toast flood.
 *
 * Concurrency contract (D11): the entry is synchronized (LOCK). The
 * guard and the generation are captured in the same synchronized
 * block, and only after every early return has been passed. Completion
 * ownership is acquired atomically in the synchronized block
 * (D11c). The receiver is owned per operation (D11c): finishDisable
 * unregisters only the receiver registered by its own generation.
 * Start-time exceptions are handled by stage (D11b): before
 * disableSelf() (stage A) and disableSelf() itself (stage B) roll
 * back; after disableSelf() succeeds (stage C) the guard and the
 * receiver are kept, and a fallback completion mechanism independent
 * of the failed polling guarantees the operation always completes.
 */
object A11yOffExecutor {

    private const val TAG = "A11yOff"

    /** Three-valued Android-side accessibility state (from 001). */
    enum class A11yState { ENABLED, DISABLED, UNKNOWN }

    private const val DISABLE_CHECK_INTERVAL_MS = 2000L
    private const val DISABLE_CHECK_MAX = 3
    private val MAIN_HANDLER = Handler(Looper.getMainLooper())

    /** Guard for the disable operation and the generation counter. */
    private val LOCK = Any()
    /** Static (shared within the process); guarded by LOCK. */
    private var disableInProgress = false
    /** Operation generation; guarded by LOCK. */
    private var disableGeneration = 0

    /**
     * The receiver registered by the in-progress operation, and the
     * generation it belongs to. Guarded by LOCK; the two are always
     * set and cleared together, so a generation and its receiver are
     * managed as one unit (D11c). Null / 0 when no operation is in
     * progress.
     */
    private var currentReceiver: BroadcastReceiver? = null
    private var currentReceiverGen: Int = 0

    /** Three-valued read of the Android-side state (from 001).
     * getString() null means DISABLED; an exception means UNKNOWN. */
    @JvmStatic
    fun getAccessibilityState(ctx: Context): A11yState {
        try {
            val enabled = Settings.Secure.getString(
                ctx.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
            )
            if (enabled == null) {
                return A11yState.DISABLED
            }
            val cn = ComponentName(ctx, BridgeAccessibilityService::class.java)
            return if (enabled.contains(cn.flattenToString())
                || enabled.contains(cn.flattenToShortString())
            ) {
                A11yState.ENABLED
            } else {
                A11yState.DISABLED
            }
        } catch (e: Exception) {
            return A11yState.UNKNOWN
        }
    }

    /**
     * Disables the accessibility service (headless). Safe to call
     * from the widget receiver or the QS tile: it never opens an
     * Activity. Entry ordering (D11b): (1) if a disable is already
     * in progress, return immediately (log ignored-during-execution,
     * no toast); (2) read the service instance and the state, then
     * the early returns (UNKNOWN / DISABLED / ENABLED with a null
     * instance) — the guard stays false in all three; (3) only when
     * ENABLED and the instance is non-null, set the guard and
     * capture the generation, then proceed to the async check.
     */
    @JvmStatic
    fun execute(appContext: Context) {
        val app = appContext.applicationContext
        Log.i(TAG, "request")
        val service: BridgeAccessibilityService?
        val opGen: Int
        val receiver: BroadcastReceiver
        synchronized(LOCK) {
            // (1) A disable already in progress: ignore silently (D9).
            if (disableInProgress) {
                Log.i(TAG, "ignored-during-execution")
                return
            }
            service = BridgeAccessibilityService.getInstance()
            val state = getAccessibilityState(app)
            // (2) Early returns. The outcome is decided by STATE
            // first (D7): UNKNOWN and DISABLED apply regardless of
            // the instance; only ENABLED consults the instance.
            if (state == A11yState.UNKNOWN) {
                Log.i(TAG, "UNKNOWN")
                Toast.makeText(app, R.string.toast_a11y_status_unknown,
                    Toast.LENGTH_LONG).show()
                return
            }
            if (state == A11yState.DISABLED) {
                Log.i(TAG, "already-off")
                Toast.makeText(app, R.string.toast_a11y_already_off,
                    Toast.LENGTH_LONG).show()
                return
            }
            // state == ENABLED
            if (service == null) {
                Log.i(TAG, "not-connected")
                Toast.makeText(app, R.string.toast_a11y_not_connected,
                    Toast.LENGTH_LONG).show()
                return
            }
            // (3) ENABLED and the instance is non-null: proceed.
            // Guard acquisition and generation capture happen in
            // this same synchronized block.
            disableInProgress = true
            opGen = ++disableGeneration
            receiver = DisableBroadcastReceiver(opGen)
            currentReceiver = receiver
            currentReceiverGen = opGen
        }

        // Stage A (D11b): register the broadcast receiver, before
        // disableSelf(). If this throws, the disable has not
        // started, so a full rollback is safe.
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                app.registerReceiver(receiver,
                    IntentFilter(
                        BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED),
                    Context.RECEIVER_NOT_EXPORTED)
            } else {
                app.registerReceiver(receiver,
                    IntentFilter(
                        BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED))
            }
        } catch (e: RuntimeException) {
            Log.w(TAG, "stage A registerReceiver failed: "
                + e.javaClass.simpleName)
            rollback(app, opGen, false)
            Toast.makeText(app, R.string.toast_a11y_off_failed,
                Toast.LENGTH_LONG).show()
            return
        }

        // Stage B (D11b): disableSelf(). If this throws, the
        // disable did not take effect, so a full rollback is safe.
        // service is non-null here: the null case returned inside
        // the synchronized block above.
        try {
            service!!.disableSelf()
        } catch (e: RuntimeException) {
            Log.w(TAG, "stage B disableSelf failed: "
                + e.javaClass.simpleName)
            rollback(app, opGen, true)
            Toast.makeText(app, R.string.toast_a11y_off_failed,
                Toast.LENGTH_LONG).show()
            return
        }

        // Stage C (D11b): first polling registration. If this
        // throws, the disable IS in progress, so do NOT roll back.
        // Keep the guard and the receiver; the broadcast fast path
        // stays active, and a fallback completion mechanism
        // independent of the failed polling guarantees completion.
        // CONS-110 P1-01: postDelayed() can return false without
        // throwing — a false return is a registration failure too.
        try {
            if (!scheduleDisableCheck(app, opGen, 1)) {
                Log.w(TAG, "stage C scheduleDisableCheck returned false")
                stageCFallback(app, opGen)
            }
        } catch (e: RuntimeException) {
            Log.w(TAG, "stage C scheduleDisableCheck failed: "
                + e.javaClass.simpleName)
            stageCFallback(app, opGen)
        }
    }

    /**
     * Stage C fallback (D11b): the first polling registration failed
     * after disableSelf() succeeded. The guard and the receiver are
     * kept. Do one immediate state re-check; if already DISABLED,
     * complete normally. Otherwise register a one-shot delayed
     * re-check independent of the failed polling, so the operation
     * always completes (the broadcast may arrive before
     * Settings.Secure reflects the change, and the polling is gone).
     * If even the fallback registration fails, complete immediately
     * with a final state check — the guard never stays set forever.
     * CONS-110 P1-01: a false return from postDelayed() is a
     * registration failure, not just an exception. CONS-110 P2-01:
     * the state is logged distinctly (DISABLED / ENABLED / UNKNOWN)
     * so a failed confirmation is diagnosable.
     */
    private fun stageCFallback(app: Context, opGen: Int) {
        val now = getAccessibilityState(app)
        Log.i(TAG, "stage C fallback state=$now")
        if (now == A11yState.DISABLED) {
            finishDisable(app, opGen, R.string.toast_a11y_off_done, "fallback")
            return
        }
        var posted: Boolean
        try {
            posted = MAIN_HANDLER.postDelayed({
                val state = getAccessibilityState(app)
                Log.i(TAG, "fallback state=$state")
                if (state == A11yState.DISABLED) {
                    finishDisable(app, opGen,
                        R.string.toast_a11y_off_done, "fallback")
                } else {
                    finishDisable(app, opGen,
                        R.string.toast_a11y_off_unconfirmed, "fallback")
                }
            }, DISABLE_CHECK_INTERVAL_MS * DISABLE_CHECK_MAX)
        } catch (e: RuntimeException) {
            Log.w(TAG, "stage C fallback registration failed: "
                + e.javaClass.simpleName)
            posted = false
        }
        if (!posted) {
            // The fallback was not registered (false return or
            // exception). Complete immediately with a final state
            // check so the guard never stays set forever.
            Log.w(TAG, "stage C fallback not posted")
            val state = getAccessibilityState(app)
            Log.i(TAG, "fallback immediate state=$state")
            if (state == A11yState.DISABLED) {
                finishDisable(app, opGen, R.string.toast_a11y_off_done, "fallback")
            } else {
                finishDisable(app, opGen,
                    R.string.toast_a11y_off_unconfirmed, "fallback")
            }
        }
    }

    /** Full rollback for stage A / stage B (D11b): the disable never
     * started or did not take effect. Unregisters this generation's
     * receiver (when it was actually registered — stage B only;
     * stage A never registered it) and releases the guard. */
    private fun rollback(app: Context, opGen: Int, unregisterReceiver: Boolean) {
        var toUnregister: BroadcastReceiver? = null
        synchronized(LOCK) {
            if (currentReceiverGen == opGen) {
                toUnregister = if (unregisterReceiver) currentReceiver else null
                currentReceiver = null
                currentReceiverGen = 0
            }
            if (disableInProgress && opGen == disableGeneration) {
                disableInProgress = false
            }
        }
        if (toUnregister != null) {
            try {
                app.unregisterReceiver(toUnregister)
            } catch (ignored: IllegalArgumentException) {
                // Already unregistered; nothing to do.
            }
        }
    }

    /** Completion check on the 2 s timer: re-check the Android-side
     * state, up to 3 times (6 s). Never asserts success without
     * DISABLED. Stale generations are discarded. Returns true when
     * the check was posted, false when postDelayed() failed to
     * enqueue it (CONS-110 P1-01) — the caller then moves to the
     * stage C fallback so monitoring is never abandoned. */
    private fun scheduleDisableCheck(app: Context, opGen: Int,
        attempt: Int): Boolean {
        return MAIN_HANDLER.postDelayed({
            synchronized(LOCK) {
                if (!disableInProgress || opGen != disableGeneration) {
                    return@postDelayed
                }
            }
            val state = getAccessibilityState(app)
            if (state == A11yState.DISABLED) {
                finishDisable(app, opGen, R.string.toast_a11y_off_done, "poll")
            } else if (state == A11yState.ENABLED && attempt < DISABLE_CHECK_MAX) {
                if (!scheduleDisableCheck(app, opGen, attempt + 1)) {
                    // Next polling registration failed (false
                    // return): move to the independent fallback.
                    Log.w(TAG, "polling registration failed at attempt "
                        + (attempt + 1))
                    stageCFallback(app, opGen)
                }
            } else {
                finishDisable(app, opGen,
                    R.string.toast_a11y_off_unconfirmed, "poll")
            }
        }, DISABLE_CHECK_INTERVAL_MS)
    }

    /**
     * Completion (D11c): runs exactly once. Completion ownership is
     * acquired atomically in the synchronized block — only the caller
     * that flips disableInProgress to false owns the completion.
     * The receiver owned by this generation is detached in the same
     * block (generation + receiver managed as one unit), and only
     * that detached reference is unregistered, so a later
     * generation's receiver is never touched. The toast is shown
     * exactly once. Then the open tile and the widget are notified:
     * requestListeningState (for a tile that is not listening) plus
     * an explicit package-scoped ACTION_A11Y_STATE_CHANGED broadcast
     * (for a tile that is already listening — requestListeningState
     * alone does not re-run onStartListening when mListening is
     * true), and a widget refresh.
     */
    private fun finishDisable(app: Context, opGen: Int, toastRes: Int,
        trigger: String) {
        var toUnregister: BroadcastReceiver? = null
        synchronized(LOCK) {
            if (!disableInProgress || opGen != disableGeneration) {
                return
            }
            // Atomically claim completion and detach this
            // generation's receiver.
            disableInProgress = false
            if (currentReceiverGen == opGen) {
                toUnregister = currentReceiver
                currentReceiver = null
                currentReceiverGen = 0
            }
        }
        if (toUnregister != null) {
            try {
                app.unregisterReceiver(toUnregister)
            } catch (ignored: IllegalArgumentException) {
                // Already unregistered; nothing to do.
            }
        }
        val success = (toastRes == R.string.toast_a11y_off_done)
        Log.i(TAG, (if (success) "success" else "timeout") + " via $trigger")
        Toast.makeText(app, toastRes, Toast.LENGTH_LONG).show()
        // Notify the open tile and the widget (D6).
        TileService.requestListeningState(app,
            ComponentName(app, A11yOffTileService::class.java))
        try {
            app.sendBroadcast(Intent(
                BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED)
                .setPackage(app.packageName))
        } catch (ignored: Exception) {
            // Best effort only; the tile also re-reads on click.
        }
        BridgeWidgetProvider.updateAllWidgets(app)
    }

    /**
     * Broadcast fast path: re-check and complete only when DISABLED.
     * A broadcast that arrives before Settings.Secure reflects the
     * change simply does not complete; the polling (or the stage C
     * fallback) checks again.
     */
    private class DisableBroadcastReceiver(private val opGen: Int) : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val app = context?.applicationContext ?: return
            if (getAccessibilityState(app) == A11yState.DISABLED) {
                finishDisable(app, opGen, R.string.toast_a11y_off_done, "broadcast")
            }
        }
    }
}
