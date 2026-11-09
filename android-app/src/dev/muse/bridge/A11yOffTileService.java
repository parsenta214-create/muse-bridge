// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-A11Y-OFF-002 v6, 2026-11-09
package dev.muse.bridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;

/**
 * Second Quick Settings tile: "Accessibility off" (SPEC-A11Y-OFF-002 v6).
 *
 * One tap calls the shared headless executor (A11yOffExecutor) — it
 * never opens MainActivity, so the Quick Settings panel stays open
 * and the tile's state change is visible in place. The tile mirrors
 * the accessibility state: ENABLED -> STATE_ACTIVE, DISABLED ->
 * STATE_INACTIVE, UNKNOWN -> STATE_UNAVAILABLE (UNKNOWN is never
 * misclassified as ACTIVE or INACTIVE).
 *
 * State updates (D6): requestListeningState() alone is NOT relied
 * upon. onStartListening() is a transition callback — it is only
 * called when the tile moves INTO the listening state, and is not
 * re-invoked while the tile is already listening (mListening ==
 * true, i.e. while the panel is open). So the tile has a
 * refreshTile() helper that reads the real state and calls
 * getQsTile().setState(...) + getQsTile().updateTile() directly,
 * called from three places: onStartListening(), the runtime
 * receiver's onReceive() (for ACTION_A11Y_STATE_CHANGED), and right
 * after onClick() calls the executor (to immediately correct
 * display drift on synchronous early returns like already-off /
 * UNKNOWN). getQsTile() is only valid between onStartListening and
 * onStopListening; duplicate updateTile() calls are idempotent.
 *
 * The runtime receiver is idempotent: a registered flag guards
 * against double registration when onStartListening is called
 * again without an intervening onStopListening.
 */
public class A11yOffTileService extends TileService {

    private final BroadcastReceiver a11yStateReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            refreshTile();
        }
    };
    private boolean receiverRegistered = false;

    @Override
    public void onStartListening() {
        super.onStartListening();
        if (!receiverRegistered) {
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(a11yStateReceiver,
                        new IntentFilter(
                                BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED),
                        Context.RECEIVER_NOT_EXPORTED);
            } else {
                registerReceiver(a11yStateReceiver,
                        new IntentFilter(
                                BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED));
            }
            receiverRegistered = true;
        }
        refreshTile();
    }

    @Override
    public void onStopListening() {
        if (receiverRegistered) {
            try {
                unregisterReceiver(a11yStateReceiver);
            } catch (IllegalArgumentException ignored) {
                // Already unregistered; nothing to do.
            }
            receiverRegistered = false;
        }
        super.onStopListening();
    }

    @Override
    public void onClick() {
        super.onClick();
        A11yOffExecutor.execute(this);
        // Correct display drift immediately, even on a synchronous
        // early return (already off / UNKNOWN) — do not wait for
        // the broadcast or the polling to refresh the tile.
        refreshTile();
    }

    /**
     * Reads the real accessibility state and updates the tile
     * directly. Called from onStartListening, the runtime
     * receiver, and right after onClick.
     */
    private void refreshTile() {
        Tile tile = getQsTile();
        if (tile == null) {
            return;
        }
        A11yOffExecutor.A11yState state =
                A11yOffExecutor.getAccessibilityState(this);
        switch (state) {
            case ENABLED:
                tile.setState(Tile.STATE_ACTIVE);
                break;
            case DISABLED:
                tile.setState(Tile.STATE_INACTIVE);
                break;
            default:
                tile.setState(Tile.STATE_UNAVAILABLE);
                break;
        }
        tile.updateTile();
    }
}
