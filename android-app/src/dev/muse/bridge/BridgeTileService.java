package dev.muse.bridge;

import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;

/**
 * Quick Settings tile for the Muse Bridge (v1.7).
 *
 * One tap starts or stops the bridge HTTP server (BridgeService), so the
 * user no longer has to open the app and tap Start/Stop. The tile state
 * follows BridgeService.isRunning(): ACTIVE while the server runs,
 * INACTIVE while it is stopped. The state is refreshed whenever the
 * Quick Settings panel opens (onStartListening) and shortly after a
 * tap, because stopService/startForegroundService take effect
 * asynchronously and the static flag only flips in onCreate/onDestroy.
 *
 * The tile toggles the server only. It does not enable the accessibility
 * service — Android requires that by hand in Settings — so if the
 * accessibility service is off, the server runs but gestures and
 * screenshots fail (the app's status screen still shows that).
 *
 * Starting a foreground service from a tile tap: onClick is a user
 * interaction, so startForegroundService is normally allowed. If the
 * platform still refuses (ForegroundServiceStartNotAllowedException),
 * fall back to opening the app so the user can tap Start there.
 */
public class BridgeTileService extends TileService {

    private final Handler handler = new Handler(Looper.getMainLooper());

    @Override
    public void onStartListening() {
        super.onStartListening();
        refreshTile();
    }

    @Override
    public void onClick() {
        super.onClick();
        if (BridgeService.isRunning()) {
            // SPEC-LOG-001 LOG-03: a tile stop is a widget stop.
            BridgeService.pendingStopReason =
                    HttpBridgeServer.StopReason.WIDGET;
            stopService(new Intent(this, BridgeService.class));
        } else {
            try {
                startForegroundService(new Intent(this, BridgeService.class));
            } catch (Exception e) {
                // Fall back to the app: the user can tap Start there.
                Intent openApp = new Intent(this, MainActivity.class);
                openApp.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(openApp);
            }
        }
        // The service flips its flag in onCreate/onDestroy, so refresh
        // now (best effort) and again once it has settled.
        refreshTile();
        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                refreshTile();
            }
        }, 600);
    }

    private void refreshTile() {
        Tile tile = getQsTile();
        if (tile == null) {
            return;
        }
        tile.setState(BridgeService.isRunning()
                ? Tile.STATE_ACTIVE : Tile.STATE_INACTIVE);
        tile.updateTile();
    }
}
