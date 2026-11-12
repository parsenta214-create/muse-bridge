// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12
package dev.muse.bridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/**
 * Second Quick Settings tile: "Accessibility off" (SPEC-A11Y-OFF-002 v6).
 *
 * One tap calls the shared headless executor (A11yOffExecutor) — it
 * never opens MainActivity, so the Quick Settings panel stays open
 * and the tile's state change is visible in place. The tile mirrors
 * the accessibility state, inverted per SPEC-UI-003 v2 (frozen
 * 2026-11-10): DISABLED -> STATE_ACTIVE, ENABLED -> STATE_INACTIVE,
 * UNKNOWN -> STATE_UNAVAILABLE (UNKNOWN is never misclassified as
 * ACTIVE or INACTIVE). The tile reads as a mode name — lit means
 * the off state holds.
 *
 * State updates (D6): requestListeningState() alone is NOT relied
 * upon. onStartListening() is a transition callback — it is only
 * called when the tile moves INTO the listening state, and is not
 * re-invoked while the tile is already listening (mListening ==
 * true, i.e. while the panel is open). So the tile has a
 * refreshTile() helper that reads the real state and calls
 * qsTile.state = ... + qsTile.updateTile() directly,
 * called from three places: onStartListening(), the runtime
 * receiver's onReceive() (for ACTION_A11Y_STATE_CHANGED), and right
 * after onClick() calls the executor (to immediately correct
 * display drift on synchronous early returns like already-off /
 * UNKNOWN). qsTile is only valid between onStartListening and
 * onStopListening; duplicate updateTile() calls are idempotent.
 *
 * The runtime receiver is idempotent: a registered flag guards
 * against double registration when onStartListening is called
 * again without an intervening onStopListening.
 */
class A11yOffTileService : TileService() {

    private val a11yStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            refreshTile()
        }
    }
    private var receiverRegistered = false

    override fun onStartListening() {
        super.onStartListening()
        if (!receiverRegistered) {
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(a11yStateReceiver,
                    IntentFilter(
                        BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED),
                    Context.RECEIVER_NOT_EXPORTED)
            } else {
                registerReceiver(a11yStateReceiver,
                    IntentFilter(
                        BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED))
            }
            receiverRegistered = true
        }
        refreshTile()
    }

    override fun onStopListening() {
        if (receiverRegistered) {
            try {
                unregisterReceiver(a11yStateReceiver)
            } catch (ignored: IllegalArgumentException) {
                // Already unregistered; nothing to do.
            }
            receiverRegistered = false
        }
        super.onStopListening()
    }

    override fun onClick() {
        super.onClick()
        A11yOffExecutor.execute(this)
        // Correct display drift immediately, even on a synchronous
        // early return (already off / UNKNOWN) — do not wait for
        // the broadcast or the polling to refresh the tile.
        refreshTile()
    }

    /**
     * Reads the real accessibility state and updates the tile
     * directly. Called from onStartListening, the runtime
     * receiver, and right after onClick.
     */
    private fun refreshTile() {
        val tile = qsTile ?: return
        val state = A11yOffExecutor.getAccessibilityState(this)
        when (state) {
            A11yOffExecutor.A11yState.ENABLED -> tile.state = Tile.STATE_INACTIVE
            A11yOffExecutor.A11yState.DISABLED -> tile.state = Tile.STATE_ACTIVE
            else -> tile.state = Tile.STATE_UNAVAILABLE
        }
        tile.updateTile()
    }
}
