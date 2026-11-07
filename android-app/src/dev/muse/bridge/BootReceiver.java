package dev.muse.bridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/**
 * Starts the bridge foreground service at device boot (v1.9.5), so
 * a reboot no longer needs a manual start. START_STICKY alone does
 * not cover a reboot — the system only restarts a sticky service
 * while the device is up, not across a boot.
 *
 * BOOT_COMPLETED is one of the cases where Android lets an app
 * start a foreground service from the background, and the service
 * calls startForeground() immediately in onCreate, so the start
 * stays inside the allowed window.
 *
 * Note: a freshly installed app does not receive BOOT_COMPLETED
 * until it has been opened once — open the app after installing
 * the update, then the next boot works.
 */
public class BootReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) {
            return;
        }
        // SPEC-DRIVE-001: first-init check before any app-owned
        // state file is created (see MainActivity.onCreate).
        LogActivity.ensureDriveInit(context.getApplicationContext());
        BridgeLog.init(context.getApplicationContext());
        BridgeLog.log("boot", "BOOT_COMPLETED received; starting bridge");
        Intent svc = new Intent(context, BridgeService.class);
        if (Build.VERSION.SDK_INT >= 26) {
            context.startForegroundService(svc);
        } else {
            context.startService(svc);
        }
    }
}
