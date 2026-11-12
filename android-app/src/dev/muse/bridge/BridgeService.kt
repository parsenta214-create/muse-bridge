// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12
package dev.muse.bridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.graphics.drawable.Icon
import android.os.IBinder

/** Foreground service that hosts the bridge HTTP server while running. */
class BridgeService : Service() {

    companion object {
        /** Sent to this service (e.g. from the notification action) to stop it. */
        @JvmField
        val ACTION_STOP = "dev.muse.bridge.ACTION_STOP"

        private const val CHANNEL_ID = "muse-bridge"
        private const val NOTIFICATION_ID = 1

        @Volatile
        private var running = false

        /** SPEC-LOG-001 LOG-03: the reason for the next stop, set by
         * the stop entry points (app button / notification action =
         * MANUAL, widget / tile = WIDGET) before they stop the service.
         * onDestroy reads and clears it; when unset, the stop is a
         * service destroy. */
        @JvmField
        @Volatile
        var pendingStopReason: HttpBridgeServer.StopReason? = null

        @JvmStatic
        fun isRunning(): Boolean {
            return running
        }
    }

    private var server: HttpBridgeServer? = null

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID, "Muse Bridge", NotificationManager.IMPORTANCE_LOW)
        channel.description = "Shows while the Muse Bridge server is running."
        nm.createNotificationChannel(channel)

        val ip = MainActivity.findTailscaleIp()
        val address = (if (ip != null) "http://$ip" else "http://127.0.0.1") +
            ":" + HttpBridgeServer.PORT
        // v1.7: a Stop action on the notification itself, so the bridge
        // can be turned off without opening the app (the Quick Settings
        // tile is the other one-tap route).
        val stopIntent = Intent(this, BridgeService::class.java).setAction(ACTION_STOP)
        val stopPending = PendingIntent.getService(
            this, 0, stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val stopAction = Notification.Action.Builder(
            Icon.createWithResource(this, android.R.drawable.ic_menu_close_clear_cancel),
            "停止", stopPending).build()
        val notification = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Muse Bridge running")
            .setContentText("$address — tap Stop below, or use the Quick Settings tile")
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .addAction(stopAction)
            .build()
        startForeground(NOTIFICATION_ID, notification)

        val newServer = HttpBridgeServer(
            HttpBridgeServer.PORT,
            object : HttpBridgeServer.TokenProvider {
                override fun currentToken(): String {
                    return MainActivity.getOrCreateToken(this@BridgeService)
                }
            },
            this)
        server = newServer
        // A-2 (v1.9): start() is false when no socket bound — the
        // service must not report running in that case (old code set
        // running = true unconditionally).
        running = newServer.start()
        if (!running) {
            android.util.Log.e("MuseBridge",
                "bridge server failed to bind any address")
        }
        // v2.6.0 (SPEC-WIDGET.md, W3): push a widget refresh now
        // that running is set (also when the bind failed — the
        // widget must show stopped, not stale).
        BridgeWidgetProvider.updateAllWidgets(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // v1.7: the notification's Stop action (and the tile) stop the
        // service through the same entry point that starts it.
        if (intent != null && ACTION_STOP == intent.action) {
            // SPEC-LOG-001 LOG-03: a notification Stop is a manual stop.
            pendingStopReason = HttpBridgeServer.StopReason.MANUAL
            stopSelf()
            return START_NOT_STICKY
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        val s = server
        if (s != null) {
            // SPEC-LOG-001 LOG-03: pass the stop reason (MANUAL /
            // WIDGET when a stop entry point set it, else a service
            // destroy, e.g. the system killing the service).
            val r = pendingStopReason
            pendingStopReason = null
            s.stop(r ?: HttpBridgeServer.StopReason.SERVICE_DESTROY)
            server = null
        }
        // v2.6.0 (SPEC-WIDGET.md, W3): push a widget refresh so
        // the widget shows stopped.
        BridgeWidgetProvider.updateAllWidgets(this)
        stopForeground(true)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? {
        return null
    }
}
