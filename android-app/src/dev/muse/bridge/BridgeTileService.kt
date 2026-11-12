// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12
package dev.muse.bridge

import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/**
 * Quick Settings tile for the Muse Bridge (v1.7).
 *
 * One tap starts or stops the bridge HTTP server (BridgeService), so the
 * user no longer has to open the app and tap Start/Stop. The tile state
 * follows BridgeService.isRunning(): ACTIVE while the server runs,
 * INACTIVE while it is stopped. The state is refreshed whenever the
 * Quick Settings panel opens (onStartListening) and shortly after a
 * tap, because stopService/startForegroundService take effect
 * asynchronously and the static flag only flips in onCreate/onDestroy.
 *
 * The tile toggles the server only. It does not enable the accessibility
 * service — Android requires that by hand in Settings — so if the
 * accessibility service is off, the server runs but gestures and
 * screenshots fail (the app's status screen still shows that).
 *
 * Starting a foreground service from a tile tap: onClick is a user
 * interaction, so startForegroundService is normally allowed. If the
 * platform still refuses (ForegroundServiceStartNotAllowedException),
 * fall back to opening the app so the user can tap Start there.
 *
 * Converted from BridgeTileService.java in SPEC-KOTLIN-MIGRATION-001
 * Stage 1 batch B (v2.2). Behaviour unchanged.
 */
class BridgeTileService : TileService() {

    private val handler = Handler(Looper.getMainLooper())

    override fun onStartListening() {
        super.onStartListening()
        refreshTile()
    }

    override fun onClick() {
        super.onClick()
        if (BridgeService.isRunning()) {
            // SPEC-LOG-001 LOG-03: a tile stop is a widget stop.
            BridgeService.pendingStopReason =
                HttpBridgeServer.StopReason.WIDGET
            stopService(Intent(this, BridgeService::class.java))
        } else {
            try {
                startForegroundService(Intent(this, BridgeService::class.java))
            } catch (e: Exception) {
                // Fall back to the app: the user can tap Start there.
                val openApp = Intent(this, MainActivity::class.java)
                openApp.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                startActivity(openApp)
            }
        }
        // The service flips its flag in onCreate/onDestroy, so refresh
        // now (best effort) and again once it has settled.
        refreshTile()
        handler.postDelayed({ refreshTile() }, 600)
    }

    private fun refreshTile() {
        val tile = qsTile ?: return
        tile.state = if (BridgeService.isRunning()) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        tile.updateTile()
    }
}
