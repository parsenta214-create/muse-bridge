package dev.muse.bridge;

import android.Manifest;
import android.app.Activity;
import android.app.StatusBarManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Collections;
import java.util.UUID;

/** Single screen: status, bridge address, runtime token, start/stop. */
public class MainActivity extends Activity {

    public static final String PREFS = "bridge";
    public static final String KEY_TOKEN = "token";
    private static final int REQUEST_NOTIFICATIONS = 1;

    private TextView statusText;
    private TextView addressText;
    private TextView tokenText;
    private TextView versionText;

    /** Returns the runtime token, generating and storing one on first use. */
    public static String getOrCreateToken(Context ctx) {
        SharedPreferences prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String token = prefs.getString(KEY_TOKEN, null);
        if (token == null || token.isEmpty()) {
            token = UUID.randomUUID().toString();
            prefs.edit().putString(KEY_TOKEN, token).apply();
        }
        return token;
    }

    /** Best-effort Tailscale IPv4 address (100.x), or null when absent. */
    public static String findTailscaleIp() {
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                for (InetAddress addr : Collections.list(ni.getInetAddresses())) {
                    if (addr instanceof Inet4Address && !addr.isLoopbackAddress()) {
                        String ip = addr.getHostAddress();
                        if (ip != null && ip.startsWith("100.")) {
                            return ip;
                        }
                    }
                }
            }
        } catch (Exception ignored) {
            // Best effort only; the address line just stays empty.
        }
        return null;
    }

    public static boolean isAccessibilityEnabled(Context ctx) {
        String enabled = Settings.Secure.getString(
                ctx.getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        if (enabled == null) {
            return false;
        }
        // The setting stores flattened component names in FULL form
        // ("dev.muse.bridge/dev.muse.bridge.BridgeAccessibility
        // Service"); the short form (".BridgeAccessibilityService")
        // never matches, so check both (decision 64).
        ComponentName cn = new ComponentName(ctx, BridgeAccessibilityService.class);
        return enabled.contains(cn.flattenToString())
                || enabled.contains(cn.flattenToShortString());
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        // v1.3: update notifications need this on Android 13+.
        // Asked once here; the /notify endpoint reports back when
        // it is still missing so the VM can say so instead of
        // failing silently.
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                        != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{Manifest.permission.POST_NOTIFICATIONS},
                    REQUEST_NOTIFICATIONS);
        }

        statusText = findViewById(R.id.status_text);
        addressText = findViewById(R.id.address_text);
        tokenText = findViewById(R.id.token_text);
        versionText = findViewById(R.id.version_text);

        Button start = findViewById(R.id.start_button);
        Button stop = findViewById(R.id.stop_button);
        Button a11y = findViewById(R.id.accessibility_button);
        Button regen = findViewById(R.id.regenerate_button);
        Button log = findViewById(R.id.log_button);
        log.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startActivity(new Intent(MainActivity.this, LogActivity.class));
            }
        });

        start.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startForegroundService(new Intent(MainActivity.this, BridgeService.class));
                refresh();
            }
        });
        stop.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                stopService(new Intent(MainActivity.this, BridgeService.class));
                refresh();
            }
        });
        a11y.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
            }
        });
        Button notif = findViewById(R.id.notification_button);
        notif.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));
            }
        });
        Button addTile = findViewById(R.id.add_tile_button);
        addTile.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                requestAddTile();
            }
        });
        regen.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                String token = UUID.randomUUID().toString();
                getSharedPreferences(PREFS, MODE_PRIVATE)
                        .edit().putString(KEY_TOKEN, token).apply();
                refresh();
            }
        });
        refresh();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refresh();
    }

    /**
     * Ask the system to add the Quick Settings tile (v1.7, Android 13+).
     * The system shows a one-tap "Add tile?" dialog; the user still
     * confirms once, but never has to hunt through the QS edit panel.
     * On older Android, point at the manual route instead.
     */
    private void requestAddTile() {
        if (Build.VERSION.SDK_INT >= 33) {
            StatusBarManager sbm = getSystemService(StatusBarManager.class);
            if (sbm == null) {
                Toast.makeText(this, R.string.toast_tile_failed, Toast.LENGTH_LONG).show();
                return;
            }
            sbm.requestAddTileService(
                    new ComponentName(this, BridgeTileService.class),
                    getString(R.string.tile_label),
                    Icon.createWithResource(this, R.drawable.ic_tile),
                    getMainExecutor(),
                    new java.util.function.Consumer<Integer>() {
                        @Override
                        public void accept(Integer result) {
                            int message;
                            if (result == StatusBarManager.TILE_ADD_REQUEST_RESULT_TILE_ADDED) {
                                message = R.string.toast_tile_added;
                            } else if (result == StatusBarManager.TILE_ADD_REQUEST_RESULT_TILE_ALREADY_ADDED) {
                                message = R.string.toast_tile_already_added;
                            } else {
                                message = R.string.toast_tile_failed;
                            }
                            Toast.makeText(MainActivity.this, message, Toast.LENGTH_LONG).show();
                        }
                    });
        } else {
            Toast.makeText(this, R.string.toast_tile_failed, Toast.LENGTH_LONG).show();
        }
    }

    private void refresh() {
        // v1.8: the installed version, so the user (and Muse, via
        // /state) can tell at a glance which build is on the phone.
        versionText.setText("v" + AppVersion.name(this)
                + " (" + AppVersion.code(this) + ")");

        StringBuilder status = new StringBuilder();
        status.append(BridgeService.isRunning() ? "Bridge: running" : "Bridge: stopped");
        status.append("\nAccessibility service: ")
                .append(isAccessibilityEnabled(this) ? "enabled" : "NOT enabled");
        if (!isAccessibilityEnabled(this)) {
            status.append(" — open accessibility settings and turn on Muse Bridge");
        }
        status.append("\nNotification access: ")
                .append(BridgeNotificationListener.isAccessGranted(this)
                        ? "granted" : "NOT granted");
        if (!BridgeNotificationListener.isAccessGranted(this)) {
            status.append(" — open notification access settings and turn on Muse Bridge");
        }
        statusText.setText(status.toString());

        String ip = findTailscaleIp();
        addressText.setText(ip != null
                ? "http://" + ip + ":" + HttpBridgeServer.PORT + "  (and http://127.0.0.1:" + HttpBridgeServer.PORT + ")"
                : "http://127.0.0.1:" + HttpBridgeServer.PORT + "  (Tailscale address not found)");

        tokenText.setText(getOrCreateToken(this));
    }
}
