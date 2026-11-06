package dev.muse.bridge;

import android.Manifest;
import android.app.Activity;
import android.app.StatusBarManager;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
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
    private static final int REQUEST_RUN_COMMAND = 2;
    // Decision 95 (v2.5.0, TARMUX_KICKASS step 2): Termux's
    // RUN_COMMAND permission, a dangerous permission defined
    // by Termux itself (not in android.Manifest).
    private static final String PERMISSION_RUN_COMMAND =
            "com.termux.permission.RUN_COMMAND";

    private TextView statusText;
    private TextView addressText;
    private TextView tokenText;
    private TextView versionText;
    private Button modeButton;

    /** CONS-029 P1-2 fix: refresh the status display when the
     * AccessibilityService connects or disconnects. refresh()
     * otherwise runs only on screen updates (onCreate/onResume/
     * button taps), so right after the user toggles a11y in
     * Settings the display could stay stale until the next
     * update. The service broadcasts
     * BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED on
     * connect and on destroy; this receiver is registered only
     * while the activity is resumed (the UI cannot be stale
     * while it is not visible — onResume refreshes anyway).
     * The broadcast is app-internal, so NOT_EXPORTED (same
     * pattern as the install receiver in HttpBridgeServer). */
    private final BroadcastReceiver a11yStateReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            refresh();
        }
    };
    private boolean a11yReceiverRegistered = false;

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

    /** P0-2/P0-3 (v2.0): the persisted startup mode. Only the two
     * a11y-off modes are persisted — FULL_ACCESS is derived from
     * the live a11y state (see HttpBridgeServer.currentMode), so
     * it is never stored. Unknown values fall back to MINIMAL.
     * The token key is untouched; no secret is ever stored here. */
    public static final String KEY_MODE = "mode";
    public static final String MODE_MINIMAL = "MINIMAL";
    public static final String MODE_BANKING_SAFE = "BANKING_SAFE";

    public static String getPersistedMode(Context ctx) {
        String m = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY_MODE, MODE_MINIMAL);
        return MODE_BANKING_SAFE.equals(m) ? MODE_BANKING_SAFE : MODE_MINIMAL;
    }

    public static void setPersistedMode(Context ctx, String mode) {
        String m = MODE_BANKING_SAFE.equals(mode) ? MODE_BANKING_SAFE : MODE_MINIMAL;
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().putString(KEY_MODE, m).apply();
    }

    /** Tailscale IPv4 range 100.64.0.0/10 (CGNAT): first octet
     * 100, second octet 64–127. CONS-029 P1-3 fix: the old
     * startsWith("100.") matched all of 100.0.0.0/8. This is
     * the Tailscale IP DETECTION only — the server bind check
     * (HttpBridgeServer.bindAddresses, CONS-014 P1-02) is a
     * separate item and is NOT changed here. */
    public static boolean isTailscaleIp(String ip) {
        if (ip == null) return false;
        String[] parts = ip.split("\\.");
        if (parts.length != 4) return false;
        try {
            int first = Integer.parseInt(parts[0]);
            int second = Integer.parseInt(parts[1]);
            return first == 100 && second >= 64 && second <= 127;
        } catch (NumberFormatException e) {
            return false;
        }
    }

    /** Best-effort Tailscale IPv4 addresses (100.64.0.0/10), or an
     * empty list when absent. P1-3 (CONS-022): the /health
     * tailscale_ips must be the CURRENT addresses, re-fetched
     * on each call — not the bound-at-start list, which goes
     * stale if Tailscale reassigns the IP. */
    public static java.util.List<String> findTailscaleIps() {
        java.util.List<String> out = new java.util.ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                for (InetAddress addr : Collections.list(ni.getInetAddresses())) {
                    if (addr instanceof Inet4Address && !addr.isLoopbackAddress()) {
                        String ip = addr.getHostAddress();
                        if (isTailscaleIp(ip)) {
                            out.add(ip);
                        }
                    }
                }
            }
        } catch (Exception ignored) {
            // Best effort only; the address line just stays empty.
        }
        return out;
    }

    /** Best-effort Tailscale IPv4 address (100.x), or null when absent. */
    public static String findTailscaleIp() {
        java.util.List<String> ips = findTailscaleIps();
        return ips.isEmpty() ? null : ips.get(0);
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

        // Decision 95 (v2.5.0): ask for Termux RUN_COMMAND once,
        // so POST /repair_termux can work. If the user denies
        // it, the endpoint answers 403 with the Settings path.
        if (checkSelfPermission(PERMISSION_RUN_COMMAND)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{PERMISSION_RUN_COMMAND},
                    REQUEST_RUN_COMMAND);
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
        // P0-2 (v2.0): toggle the persisted mode between MINIMAL
        // and BANKING SAFE. The mode only takes effect while the
        // accessibility service is off (a11y on means FULL ACCESS);
        // the preference is sticky, so it re-appears when a11y
        // goes off. The bridge never enables a11y itself.
        modeButton = findViewById(R.id.mode_button);
        modeButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                String current = getPersistedMode(MainActivity.this);
                setPersistedMode(MainActivity.this,
                        MODE_BANKING_SAFE.equals(current)
                                ? MODE_MINIMAL : MODE_BANKING_SAFE);
                refresh();
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
        // CONS-029 P1-2: listen for service connect/disconnect
        // while visible (registered before refresh so no event
        // between the two is missed).
        if (!a11yReceiverRegistered) {
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(a11yStateReceiver,
                        new IntentFilter(BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED),
                        Context.RECEIVER_NOT_EXPORTED);
            } else {
                registerReceiver(a11yStateReceiver,
                        new IntentFilter(BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED));
            }
            a11yReceiverRegistered = true;
        }
        refresh();
    }

    @Override
    protected void onPause() {
        if (a11yReceiverRegistered) {
            try {
                unregisterReceiver(a11yStateReceiver);
            } catch (IllegalArgumentException ignored) {
                // Already unregistered; nothing to do.
            }
            a11yReceiverRegistered = false;
        }
        super.onPause();
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
        // P0-2 (v2.0): the effective mode. a11y on means FULL
        // ACCESS; a11y off means the persisted MINIMAL /
        // BANKING SAFE.
        // P1-2 (CONS-022): the a11y state comes from the
        // connected service (BridgeAccessibilityService.
        // getInstance()), the SAME source /health uses — not
        // from the Settings string isAccessibilityEnabled().
        // The two could disagree transiently right after the
        // user toggles a11y (Settings says ON, service not yet
        // connected); using the service-connection state here
        // makes the UI and /health agree. (isAccessibilityEnabled
        // is kept as the Settings-state check for diagnostics.)
        boolean a11yOn = BridgeAccessibilityService.getInstance() != null;
        String mode = a11yOn ? "FULL ACCESS" : getPersistedMode(this);
        status.append("\nMode: ").append(mode);
        if (a11yOn) {
            status.append(" — screen control available");
        }
        // CONS-029 P1-1 fix: the BANKING_SAFE explanation used to
        // appear here AND on the Accessibility row ("banking apps
        // take priority" twice). The Mode row no longer repeats
        // it — the Accessibility row below is the one place that
        // explains why the service is off.
        status.append("\nAccessibility service: ")
                .append(a11yOn ? "enabled" : "NOT enabled");
        if (!a11yOn && MODE_BANKING_SAFE.equals(mode)) {
            // P1-1 (CONS-022): in BANKING_SAFE the service stays
            // off BY DESIGN — never prompt the user to enable
            // it. The old code appended the enable prompt
            // whenever a11y was off, including BANKING_SAFE,
            // contradicting the spec.
            status.append(" — left off on purpose, banking apps take priority");
        } else if (!a11yOn) {
            // MINIMAL only: prompt to enable a11y.
            status.append(" — open accessibility settings and turn on Muse Bridge");
        }
        status.append("\nNotification access: ")
                .append(BridgeNotificationListener.isAccessGranted(this)
                        ? "granted" : "NOT granted");
        if (!BridgeNotificationListener.isAccessGranted(this)) {
            status.append(" — open notification access settings and turn on Muse Bridge");
        }
        statusText.setText(status.toString());

        // P0-2 (v2.0): the mode button shows the persisted mode
        // (what applies when a11y is off) and what a tap does.
        modeButton.setText(MODE_BANKING_SAFE.equals(getPersistedMode(this))
                ? R.string.btn_mode_banking_safe : R.string.btn_mode_minimal);

        String ip = findTailscaleIp();
        addressText.setText(ip != null
                ? "http://" + ip + ":" + HttpBridgeServer.PORT + "  (and http://127.0.0.1:" + HttpBridgeServer.PORT + ")"
                : "http://127.0.0.1:" + HttpBridgeServer.PORT + "  (Tailscale address not found)");

        tokenText.setText(getOrCreateToken(this));
    }
}
