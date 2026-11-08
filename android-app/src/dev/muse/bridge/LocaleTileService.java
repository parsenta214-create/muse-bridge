// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-LOCALE-TILE-001, 2026-11-09
package dev.muse.bridge;

import android.content.Intent;
import android.provider.Settings;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;
import android.widget.Toast;

import java.util.Locale;

/**
 * Quick Settings tile for the language settings (SPEC-LOCALE-TILE-001 v2).
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
 * language. If the settings page cannot be opened, a toast says so;
 * there is no fallback to MainActivity.
 */
public class LocaleTileService extends TileService {

    @Override
    public void onStartListening() {
        super.onStartListening();
        refreshTile();
    }

    @Override
    public void onClick() {
        super.onClick();
        Intent intent = new Intent(Settings.ACTION_LOCALE_SETTINGS);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            startActivityAndCollapse(intent);
        } catch (Exception e) {
            // The settings page could not be opened (no locale
            // settings activity on this device, or the system refused
            // the launch). Say so; never open the app instead.
            Toast.makeText(this, R.string.toast_locale_open_failed,
                    Toast.LENGTH_SHORT).show();
        }
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
