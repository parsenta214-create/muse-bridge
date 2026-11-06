package dev.muse.bridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.graphics.drawable.Icon;
import android.os.IBinder;

/** Foreground service that hosts the bridge HTTP server while running. */
public class BridgeService extends Service {

    /** Sent to this service (e.g. from the notification action) to stop it. */
    public static final String ACTION_STOP = "dev.muse.bridge.ACTION_STOP";

    private static final String CHANNEL_ID = "muse-bridge";
    private static final int NOTIFICATION_ID = 1;

    private static volatile boolean running = false;

    private HttpBridgeServer server;

    public static boolean isRunning() {
        return running;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "Muse Bridge", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("Shows while the Muse Bridge server is running.");
        nm.createNotificationChannel(channel);

        String ip = MainActivity.findTailscaleIp();
        String address = (ip != null ? "http://" + ip : "http://127.0.0.1")
                + ":" + HttpBridgeServer.PORT;
        // v1.7: a Stop action on the notification itself, so the bridge
        // can be turned off without opening the app (the Quick Settings
        // tile is the other one-tap route).
        Intent stopIntent = new Intent(this, BridgeService.class).setAction(ACTION_STOP);
        PendingIntent stopPending = PendingIntent.getService(
                this, 0, stopIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Action stopAction = new Notification.Action.Builder(
                Icon.createWithResource(this, android.R.drawable.ic_menu_close_clear_cancel),
                "停止", stopPending).build();
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("Muse Bridge running")
                .setContentText(address + " — tap Stop below, or use the Quick Settings tile")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .addAction(stopAction)
                .build();
        startForeground(NOTIFICATION_ID, notification);

        server = new HttpBridgeServer(
                HttpBridgeServer.PORT,
                new HttpBridgeServer.TokenProvider() {
                    @Override
                    public String currentToken() {
                        return MainActivity.getOrCreateToken(BridgeService.this);
                    }
                },
                this);
        // A-2 (v1.9): start() is false when no socket bound — the
        // service must not report running in that case (old code set
        // running = true unconditionally).
        running = server.start();
        if (!running) {
            android.util.Log.e("MuseBridge",
                    "bridge server failed to bind any address");
        }
        // v2.6.0 (SPEC-WIDGET.md, W3): push a widget refresh now
        // that running is set (also when the bind failed — the
        // widget must show stopped, not stale).
        BridgeWidgetProvider.updateAllWidgets(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // v1.7: the notification's Stop action (and the tile) stop the
        // service through the same entry point that starts it.
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (server != null) {
            server.stop();
            server = null;
        }
        // v2.6.0 (SPEC-WIDGET.md, W3): push a widget refresh so
        // the widget shows stopped.
        BridgeWidgetProvider.updateAllWidgets(this);
        stopForeground(true);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
