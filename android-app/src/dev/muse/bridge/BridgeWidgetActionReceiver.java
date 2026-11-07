package dev.muse.bridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;

/**
 * Non-exported receiver for the home-screen widget's Start/Stop
 * button (v2.6.0 fix round, CONS-037 P1).
 *
 * The widget's toggle PendingIntent targets this receiver, NOT the
 * exported BridgeWidgetProvider: an exported receiver that handled
 * the toggle action could be triggered by ANY app sending an
 * explicit broadcast with that action, toggling BridgeService from
 * outside. This receiver is exported=false with no intent-filter,
 * so only explicit intents from our own app (the widget's
 * PendingIntent, which carries our UID) can reach it. The exported
 * provider keeps handling only what the system needs:
 * APPWIDGET_UPDATE (via super.onReceive) and
 * ACTION_A11Y_STATE_CHANGED (redraw only — an external spoof
 * there can only force a redraw, no info leak).
 *
 * Toggle semantics (W2/W6, same as the QS tile): start/stop the
 * foreground BridgeService only, never a11y. A sync failure of
 * startForegroundService falls back to MainActivity immediately.
 * An async failure (the start call succeeds but
 * BridgeService.server.start() returns false, e.g. a bind
 * failure) is caught by the delayed check below (CONS-037 P2):
 * if the service is still not running after the check delay,
 * MainActivity opens so the user sees the failure and can tap
 * Start there.
 */
public class BridgeWidgetActionReceiver extends BroadcastReceiver {

    /** Toggle tap: broadcast from the widget button. */
    public static final String ACTION_WIDGET_TOGGLE =
            "dev.muse.bridge.ACTION_WIDGET_TOGGLE";

    /** Widget refresh settle delay — same as the QS tile. */
    private static final long TOGGLE_SETTLE_DELAY_MS = 600;

    /** Delay for the async start-failure check. Deliberately
     * longer than the refresh settle delay: a cold service start
     * can take a while on a loaded phone, and opening MainActivity
     * on a slow but succeeding start would be a false fallback.
     * 2 s is conservative — a bind failure is decided
     * synchronously in BridgeService.onCreate, so if the service
     * is still not running 2 s after the start request, it almost
     * certainly failed. Residual risk (honest note): on a very
     * slow phone the service could still be starting at 2 s, and
     * the app would open unnecessarily — annoying, not harmful
     * (the app then shows the true state, running once the
     * service starts). Live verification must watch for this. */
    private static final long START_FAILURE_CHECK_DELAY_MS = 2000;

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !ACTION_WIDGET_TOGGLE.equals(intent.getAction())) {
            return;
        }
        boolean wasRunning = BridgeService.isRunning();
        if (wasRunning) {
            context.stopService(new Intent(context, BridgeService.class));
        } else {
            try {
                context.startForegroundService(
                        new Intent(context, BridgeService.class));
            } catch (Exception e) {
                // W6 sync failure: fall back to the app so the
                // user can tap Start there.
                openMainActivity(context);
                BridgeWidgetProvider.updateAllWidgets(context);
                return;
            }
        }
        // The service flips its flag in onCreate/onDestroy, so
        // refresh now (best effort) and again once it has settled
        // — same pattern as BridgeTileService.
        BridgeWidgetProvider.updateAllWidgets(context);
        final Context appContext = context.getApplicationContext();
        final boolean started = !wasRunning;
        Handler handler = new Handler(Looper.getMainLooper());
        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                BridgeWidgetProvider.updateAllWidgets(appContext);
            }
        }, TOGGLE_SETTLE_DELAY_MS);
        if (started) {
            // W6 async failure (CONS-037 P2): only on the start
            // path — never after a stop tap (the service is
            // correctly not running then).
            handler.postDelayed(new Runnable() {
                @Override
                public void run() {
                    BridgeWidgetProvider.updateAllWidgets(appContext);
                    if (!BridgeService.isRunning()) {
                        openMainActivity(appContext);
                    }
                }
            }, START_FAILURE_CHECK_DELAY_MS);
        }
    }

    private static void openMainActivity(Context context) {
        Intent openApp = new Intent(context, MainActivity.class);
        openApp.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(openApp);
    }
}
