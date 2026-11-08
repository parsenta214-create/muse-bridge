// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-LOCALE-TILE-001, 2026-11-09
package dev.muse.bridge;

import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.provider.Settings;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;
import android.util.Log;
import android.widget.Toast;

import java.util.Locale;

/**
 * Quick Settings tile for the language settings (SPEC-LOCALE-TILE-001 v3).
 *
 * One tap opens the system language settings page, so the user no longer
 * has to open the Settings app and navigate to it by hand. The tile is a
 * launcher only: its state is always INACTIVE, because it does not
 * represent an on/off value. The tile subtitle shows the current language
 * (Locale.getDefault().getDisplayLanguage()), read fresh whenever the
 * Quick Settings panel opens (onStartListening); nothing is cached and
 * nothing is polled in the background.
 *
 * The tile only opens the settings page. It cannot change the language
 * itself — Android does not let an ordinary app change the device
 * language.
 *
 * v3 launch cascade (the v2 single attempt failed on the Galaxy A55,
 * One UI 7: tapping showed the failure toast and the language settings
 * never opened): resolve the locale intent and make it explicit, try
 * startActivityAndCollapse, then plain startActivity if that throws;
 * when the locale intent cannot be resolved at all, fall back to the
 * main Settings page with the same two stages; only when every attempt
 * fails, show a toast. Each failed stage and its exception class are
 * logged concisely (no persistent state, no device information beyond
 * the stage). There is no fallback to MainActivity.
 */
public class LocaleTileService extends TileService {

    private static final String TAG = "LocaleTile";

    @Override
    public void onStartListening() {
        super.onStartListening();
        refreshTile();
    }

    @Override
    public void onClick() {
        super.onClick();
        Intent locale = resolveExplicit(Settings.ACTION_LOCALE_SETTINGS,
                "locale");
        if (locale != null && tryLaunch(locale, "locale")) {
            return;
        }
        if (locale == null) {
            // The locale intent did not resolve: fall back to the
            // main Settings page (L3 step 3). From there the user
            // reaches Language in two taps (General management).
            Intent settings = resolveExplicit(Settings.ACTION_SETTINGS,
                    "settings");
            if (settings != null && tryLaunch(settings, "settings")) {
                return;
            }
        }
        // Every attempt failed (or the locale intent resolved but
        // neither launch worked). Say so; never open the app instead.
        Toast.makeText(this, R.string.toast_locale_open_failed,
                Toast.LENGTH_SHORT).show();
    }

    /**
     * Resolves an implicit Settings intent to an explicit one.
     * Returns null when the intent cannot be resolved.
     */
    private Intent resolveExplicit(String action, String label) {
        Intent intent = new Intent(action);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        ResolveInfo info = getPackageManager().resolveActivity(intent, 0);
        if (info == null || info.activityInfo == null) {
            Log.w(TAG, label + " intent did not resolve");
            return null;
        }
        intent.setComponent(new ComponentName(
                info.activityInfo.packageName, info.activityInfo.name));
        return intent;
    }

    /**
     * Tries to launch an explicit intent: startActivityAndCollapse
     * first, then plain startActivity if that throws. Returns true
     * when a launch was attempted without throwing.
     */
    private boolean tryLaunch(Intent intent, String label) {
        try {
            startActivityAndCollapse(intent);
            return true;
        } catch (Exception e) {
            Log.w(TAG, "startActivityAndCollapse " + label + " failed: "
                    + e.getClass().getSimpleName());
        }
        try {
            startActivity(intent);
            return true;
        } catch (Exception e) {
            Log.w(TAG, "startActivity " + label + " failed: "
                    + e.getClass().getSimpleName());
        }
        return false;
    }

    private void refreshTile() {
        Tile tile = getQsTile();
        if (tile == null) {
            return;
        }
        tile.setState(Tile.STATE_INACTIVE);
        String language = Locale.getDefault().getDisplayLanguage();
        if (language.isEmpty()) {
            // Do not leave a stale subtitle behind.
            language = getString(R.string.locale_unknown);
        }
        tile.setSubtitle(language);
        tile.updateTile();
    }
}
