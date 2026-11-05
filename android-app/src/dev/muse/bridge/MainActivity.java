package dev.muse.bridge;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;

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
        return enabled != null && enabled.contains("dev.muse.bridge/.BridgeAccessibilityService");
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

        Button start = findViewById(R.id.start_button);
        Button stop = findViewById(R.id.stop_button);
        Button a11y = findViewById(R.id.accessibility_button);
        Button regen = findViewById(R.id.regenerate_button);

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

    private void refresh() {
        StringBuilder status = new StringBuilder();
        status.append(BridgeService.isRunning() ? "Bridge: running" : "Bridge: stopped");
        status.append("\nAccessibility service: ")
                .append(isAccessibilityEnabled(this) ? "enabled" : "NOT enabled");
        if (!isAccessibilityEnabled(this)) {
            status.append(" — open accessibility settings and turn on Muse Bridge");
        }
        statusText.setText(status.toString());

        String ip = findTailscaleIp();
        addressText.setText(ip != null
                ? "http://" + ip + ":" + HttpBridgeServer.PORT + "  (and http://127.0.0.1:" + HttpBridgeServer.PORT + ")"
                : "http://127.0.0.1:" + HttpBridgeServer.PORT + "  (Tailscale address not found)");

        tokenText.setText(getOrCreateToken(this));
    }
}
