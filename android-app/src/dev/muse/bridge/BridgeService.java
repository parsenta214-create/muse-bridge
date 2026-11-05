package dev.muse.bridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;

/** Foreground service that hosts the bridge HTTP server while running. */
public class BridgeService extends Service {

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
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("Muse Bridge running")
                .setContentText(address + " — stop from the app when done")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
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
        server.start();
        running = true;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (server != null) {
            server.stop();
            server = null;
        }
        stopForeground(true);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
