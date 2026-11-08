// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-LOCALE-TILE-001, 2026-11-09
package dev.muse.bridge;

import android.content.Intent;
import android.provider.Settings;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;
import android.util.Log;
import android.widget.Toast;

import java.util.Locale;

/**
 * Quick Settings tile for the language settings (SPEC-LOCALE-TILE-001 v4).
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
 * v4 launch (the v3 cascade failed harder on the Galaxy A55, One UI 7:
 * tapping did nothing at all, not even the failure toast): use
 * startActivity with the IMPLICIT intent — the same path as
 * `am start -a android.settings.LOCALE_SETTINGS`, which opened the
 * Language settings on this device (2026-11-09). The v3 explicit
 * intent (resolveActivity + setComponent) and startActivityAndCollapse
 * are both dropped: the explicit-ification is the suspect for the
 * silent failure, and startActivityAndCollapse(Intent) is deprecated
 * in API 34 and throws UnsupportedOperationException at targetSdk 34
 * (this app targets 34, which also explains why v2.9.4 showed the
 * failure toast). When the locale launch throws, fall back to the
 * main Settings page with startActivity; only when every attempt
 * fails, show the failure toast. Each failed stage and its exception
 * class are logged concisely (no persistent state, no device
 * information beyond the stage). There is no fallback to MainActivity.
 *
 * A launch-confirmation toast (v4 diagnostic, temporary, removed in
 * v5) is shown when a launch returns without throwing. It confirms
 * the launch method returned, not that the Settings screen opened.
 * If the Settings open, the toast hides behind them; if only the
 * toast is visible, the launch failed silently.
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
        Intent locale = new Intent(Settings.ACTION_LOCALE_SETTINGS);
        locale.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (tryLaunch(locale, "locale")) {
            return;
        }
        // The locale launch threw: fall back to the main Settings
        // page (L3 step 2). From there the user reaches Language in
        // two taps (General management).
        Intent settings = new Intent(Settings.ACTION_SETTINGS);
        settings.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (tryLaunch(settings, "settings")) {
            return;
        }
        // Every attempt failed. Say so; never open the app instead.
        Toast.makeText(this, R.string.toast_locale_open_failed,
                Toast.LENGTH_SHORT).show();
    }

    /**
     * Tries to launch an implicit intent with startActivity.
     * Returns true when the launch returned without throwing (and
     * shows the launch-confirmation toast, v4 diagnostic only).
     * Returns false when the launch threw.
     */
    private boolean tryLaunch(Intent intent, String label) {
        try {
            startActivity(intent);
            // Diagnostic (v4 only, removed in v5): confirms the
            // launch method returned without throwing. It does NOT
            // guarantee the Settings screen actually opened. Shown
            // immediately after startActivity() returns.
            Toast.makeText(this, R.string.toast_locale_opened,
                    Toast.LENGTH_SHORT).show();
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
