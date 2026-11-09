package dev.muse.bridge;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.widget.RemoteViews;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Home-screen widget for the Muse Bridge (v2.6.0, SPEC-WIDGET.md).
 *
 * Shows the bridge state at a glance — server running/stopped, the
 * effective mode (same derivation as /health and MainActivity:
 * a11y on means FULL ACCESS, otherwise the persisted MINIMAL /
 * BANKING_SAFE), whether the accessibility service is enabled, and
 * when the widget last refreshed — with a Start/Stop toggle and an
 * Open-app button, so the System Development Lead can control the bridge without
 * opening the app (the same one-tap convenience as the Quick
 * Settings tile, on the home screen).
 *
 * Locked rules (W1–W7): the widget shows NO IP address, NO token
 * and NO notification content; it never enables the accessibility
 * service itself; it never touches the HTTP API or the token — it
 * talks to the service via Intents only (same UID). Updates are
 * event-driven only (updatePeriodMillis="0"): widget added,
 * service start/stop, a11y state change, mode change, and after a
 * toggle tap (now + ~600 ms delayed, QS-tile pattern — the
 * service flips its flag asynchronously in onCreate/onDestroy).
 *
 * The Start/Stop control path does NOT live here (CONS-037 P1):
 * this provider is exported, so handling a toggle action here
 * would let ANY app toggle the service with an explicit
 * broadcast. The toggle goes to the non-exported
 * BridgeWidgetActionReceiver instead; this provider handles only
 * display + system updates + the a11y-state redraw.
 *
 * The a11y-state broadcast reaches this provider only because
 * BridgeAccessibilityService sends it explicitly (setPackage):
 * since API 26 a manifest receiver cannot receive an implicit
 * broadcast. MainActivity's runtime receiver is unaffected
 * (same app, RECEIVER_NOT_EXPORTED).
 */
public class BridgeWidgetProvider extends AppWidgetProvider {

    private static final int REQUEST_TOGGLE = 1;
    private static final int REQUEST_OPEN = 2;
    /** SPEC-A11Y-OFF-002 v6: distinct request code for the
     * "Accessibility off" button (must differ from TOGGLE/OPEN). */
    private static final int REQUEST_A11Y_OFF = 3;

    @Override
    public void onUpdate(Context context, AppWidgetManager appWidgetManager,
                         int[] appWidgetIds) {
        updateAllWidgets(context);
    }

    @Override
    public void onEnabled(Context context) {
        updateAllWidgets(context);
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        // CONS-037 P1: this exported provider handles ONLY the
        // a11y-state redraw. The toggle action lives in the
        // non-exported BridgeWidgetActionReceiver — handling it
        // here would let any app toggle the service.
        if (BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED.equals(action)) {
            updateAllWidgets(context);
            return;
        }
        super.onReceive(context, intent);
    }

    /** Refresh every placed widget. Reads only the same state the
     * MainActivity status screen reads — no token, no IP (W1/W5).
     * Best effort: a failure here must never break the caller
     * (BridgeService.onCreate/onDestroy call this). */
    public static void updateAllWidgets(Context context) {
        try {
            AppWidgetManager manager = AppWidgetManager.getInstance(context);
            if (manager == null) {
                return;
            }
            int[] ids = manager.getAppWidgetIds(
                    new ComponentName(context, BridgeWidgetProvider.class));
            if (ids == null || ids.length == 0) {
                return;
            }
            RemoteViews views = buildViews(context);
            for (int id : ids) {
                manager.updateAppWidget(id, views);
            }
        } catch (Exception e) {
            android.util.Log.w("MuseBridge",
                    "widget update failed: " + e.getClass().getSimpleName());
        }
    }

    private static RemoteViews buildViews(Context context) {
        boolean running = BridgeService.isRunning();
        // Same derivation as /health and MainActivity.refresh():
        // a11y on means FULL ACCESS, otherwise the persisted mode.
        boolean a11yOn = BridgeAccessibilityService.getInstance() != null;
        String mode = a11yOn
                ? "FULL ACCESS" : MainActivity.getPersistedMode(context);

        RemoteViews views = new RemoteViews(
                context.getPackageName(), R.layout.widget_bridge);
        views.setTextViewText(R.id.widget_status_text,
                running ? "Bridge: running" : "Bridge: stopped");
        views.setTextViewText(R.id.widget_mode_text, "Mode: " + mode);
        views.setTextViewText(R.id.widget_a11y_text,
                "Accessibility: " + (a11yOn ? "enabled" : "NOT enabled"));
        views.setTextViewText(R.id.widget_updated_text,
                "更新: " + new SimpleDateFormat("HH:mm", Locale.getDefault())
                        .format(new Date()));
        views.setTextViewText(R.id.widget_toggle_button,
                running ? "停止" : "開始");
        views.setTextViewText(R.id.widget_open_button, "開く");
        views.setTextViewText(R.id.widget_a11y_off_button,
                context.getString(R.string.widget_a11y_off_button));

        // Toggle: broadcast to the NON-exported action receiver
        // (CONS-037 P1) — never to this exported provider.
        Intent toggle = new Intent(context, BridgeWidgetActionReceiver.class)
                .setAction(BridgeWidgetActionReceiver.ACTION_WIDGET_TOGGLE);
        PendingIntent togglePending = PendingIntent.getBroadcast(
                context, REQUEST_TOGGLE, toggle,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        views.setOnClickPendingIntent(R.id.widget_toggle_button, togglePending);

        // SPEC-A11Y-OFF-002 v6: "Accessibility off" — explicit
        // intent to the NON-exported action receiver, dedicated
        // action + request code, FLAG_UPDATE_CURRENT | FLAG_IMMUTABLE.
        // Same pattern as the toggle; the receiver stays
        // exported=false with no intent-filter.
        Intent a11yOff = new Intent(context, BridgeWidgetActionReceiver.class)
                .setAction(BridgeWidgetActionReceiver.ACTION_WIDGET_A11Y_OFF);
        PendingIntent a11yOffPending = PendingIntent.getBroadcast(
                context, REQUEST_A11Y_OFF, a11yOff,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        views.setOnClickPendingIntent(R.id.widget_a11y_off_button, a11yOffPending);

        // Open app: MainActivity (also the body tap target).
        Intent open = new Intent(context, MainActivity.class);
        PendingIntent openPending = PendingIntent.getActivity(
                context, REQUEST_OPEN, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        views.setOnClickPendingIntent(R.id.widget_open_button, openPending);
        views.setOnClickPendingIntent(R.id.widget_root, openPending);
        return views;
    }
}
