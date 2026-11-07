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
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.SpannableStringBuilder;
import android.text.Spanned;
import android.text.style.ForegroundColorSpan;
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

    // SPEC-UI-002: the bridge state is a dial (StatusDialView), the
    // mode / a11y / notification states live in cards (SPEC-UI-001
    // rows, same IDs), and the connection flow shows device ->
    // Tailscale -> server.
    private StatusDialView statusDial;
    private View flowNodeDevice;
    private View flowNodeTailscale;
    private View flowNodeServer;
    private TextView flowArrowTailscale;
    private TextView flowArrowServer;
    private TextView flowDotDevice;
    private TextView flowDotTailscale;
    private TextView flowDotServer;
    private TextView flowStatusDevice;
    private TextView flowStatusTailscale;
    private TextView flowStatusServer;
    /** SPEC-PING-001: the phone-to-client probe row under the flow. */
    private View pingRow;
    private TextView pingText;
    private TextView pingDetail;
    private TextView modeStatusValue;
    private TextView a11yStatusValue;
    private TextView notifStatusValue;
    private TextView addressText;
    private TextView addressToggleLabel;
    private TextView tokenText;
    private TextView tokenToggleLabel;
    private TextView versionText;
    private Button modeButton;
    private Button startButton;
    private Button stopButton;
    private Button a11yButton;
    private Button notifButton;
    /** SPEC-UI-001 v2.6.2: enabled text colours, captured once so
     * refresh() can restore them after setting the disabled colour. */
    private int a11yButtonDefaultTextColor;
    private int notifButtonDefaultTextColor;
    /** SPEC-UI-001: the token is masked by default; tap to reveal. */
    private boolean tokenRevealed = false;
    /** SPEC-UI-001 v2.6.3: the address is fully masked by default; tap to reveal. */
    private boolean addressRevealed = false;
    /** SPEC-UI-001 v2.6.3: the computed address text (shown when revealed). */
    private String currentAddress = "";
    /** SPEC-UI-001 v2.6.3: fully masked — no partial characters. */
    private static final String MASKED = "●●●●●●●●";

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
     * the Tailscale IP DETECTION; the server bind check
     * (HttpBridgeServer.bindAddresses) uses the same test
     * (CONS-014 P1-02). */
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

        statusDial = findViewById(R.id.status_dial);
        flowNodeDevice = findViewById(R.id.flow_node_device);
        flowNodeTailscale = findViewById(R.id.flow_node_tailscale);
        flowNodeServer = findViewById(R.id.flow_node_server);
        flowArrowTailscale = findViewById(R.id.flow_arrow_tailscale);
        flowArrowServer = findViewById(R.id.flow_arrow_server);
        flowDotDevice = findViewById(R.id.flow_dot_device);
        flowDotTailscale = findViewById(R.id.flow_dot_tailscale);
        flowDotServer = findViewById(R.id.flow_dot_server);
        flowStatusDevice = findViewById(R.id.flow_status_device);
        flowStatusTailscale = findViewById(R.id.flow_status_tailscale);
        flowStatusServer = findViewById(R.id.flow_status_server);
        pingRow = findViewById(R.id.ping_row);
        pingText = findViewById(R.id.ping_text);
        pingDetail = findViewById(R.id.ping_detail);
        // SPEC-PING-001: tap the ping row to re-probe (throttled
        // to 30 s inside PingProber).
        pingRow.setOnClickListener(v -> probePing());
        modeStatusValue = findViewById(R.id.status_mode_value);
        a11yStatusValue = findViewById(R.id.status_a11y_value);
        notifStatusValue = findViewById(R.id.status_notif_value);
        addressText = findViewById(R.id.address_text);
        tokenText = findViewById(R.id.token_text);
        versionText = findViewById(R.id.version_text);

        // SPEC-UI-001: tap the token (or the show/hide label) to
        // toggle masked / revealed. v2.6.2: the label makes the
        // affordance explicit (GPT CONS-047 P1).
        View.OnClickListener tokenToggle = new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                tokenRevealed = !tokenRevealed;
                refreshToken();
            }
        };
        tokenText.setOnClickListener(tokenToggle);
        tokenToggleLabel = findViewById(R.id.token_toggle_label);
        tokenToggleLabel.setOnClickListener(tokenToggle);

        // SPEC-UI-001 v2.6.3: the address uses the same tap-to-reveal pattern.
        View.OnClickListener addressToggle = new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                addressRevealed = !addressRevealed;
                refreshAddress();
            }
        };
        addressText.setOnClickListener(addressToggle);
        addressToggleLabel = findViewById(R.id.address_toggle_label);
        addressToggleLabel.setOnClickListener(addressToggle);

        startButton = findViewById(R.id.start_button);
        stopButton = findViewById(R.id.stop_button);
        a11yButton = findViewById(R.id.accessibility_button);
        Button regen = findViewById(R.id.regenerate_button);
        Button log = findViewById(R.id.log_button);
        log.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startActivity(new Intent(MainActivity.this, LogActivity.class));
            }
        });

        startButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startForegroundService(new Intent(MainActivity.this, BridgeService.class));
                refresh();
            }
        });
        stopButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                stopService(new Intent(MainActivity.this, BridgeService.class));
                refresh();
            }
        });
        a11yButton.setOnClickListener(new View.OnClickListener() {
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
                // v2.6.0 (SPEC-WIDGET.md, W3): push a widget
                // refresh so the mode line updates.
                BridgeWidgetProvider.updateAllWidgets(MainActivity.this);
            }
        });
        notifButton = findViewById(R.id.notification_button);
        notifButton.setOnClickListener(new View.OnClickListener() {
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
        // SPEC-UI-001 v2.6.2: remember the enabled text colours so
        // refresh() can restore them after setting the disabled colour.
        a11yButtonDefaultTextColor = a11yButton.getCurrentTextColor();
        notifButtonDefaultTextColor = notifButton.getCurrentTextColor();
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

        // SPEC-UI-001: each status row shows its state word in the
        // colour for that state, with the explanation in grey after
        // it (setStatusRow). Colour never carries information alone.
        boolean running = BridgeService.isRunning();
        String ip = findTailscaleIp();

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

        // SPEC-UI-002: the dial shows the server state (ring +
        // state word + mode sub-line); the flow shows device ->
        // Tailscale -> server.
        String modeShort = a11yOn ? getString(R.string.mode_short_full)
                : MODE_BANKING_SAFE.equals(mode)
                        ? getString(R.string.status_mode_banking)
                        : getString(R.string.status_mode_minimal);
        statusDial.setRunning(running);
        statusDial.setModeLabel(modeShort);
        // CONS-050 P1-3: the dial's Canvas text is not announced by
        // a screen reader, so set the spoken description here.
        statusDial.setContentDescription("Bridge "
                + getString(running ? R.string.status_running : R.string.status_stopped)
                + "、" + modeShort);
        // CONS-050 P1-1: the flow words describe what was actually
        // checked — the app runs on this device, the Tailscale IP
        // was obtained (not that the tunnel is proven up), the
        // service is running. They do not claim end-to-end
        // connectivity.
        setFlowNode(flowNodeDevice, flowDotDevice, flowStatusDevice,
                getString(R.string.flow_device),
                getString(R.string.flow_app_running), getColor(R.color.status_ok));
        setFlowNode(flowNodeTailscale, flowDotTailscale, flowStatusTailscale,
                getString(R.string.flow_tailscale),
                getString(ip != null ? R.string.flow_ip_found : R.string.flow_not_found),
                getColor(ip != null ? R.color.status_ok : R.color.status_bad));
        setFlowNode(flowNodeServer, flowDotServer, flowStatusServer,
                getString(R.string.flow_server),
                getString(running ? R.string.status_running : R.string.status_stopped),
                getColor(running ? R.color.status_ok : R.color.status_bad));
        // CONS-050 P2-4: the arrows reflect the link state — green
        // only when the downstream node is up, grey otherwise.
        flowArrowTailscale.setTextColor(
                getColor(ip != null ? R.color.status_ok : R.color.status_neutral));
        flowArrowServer.setTextColor(
                getColor(running ? R.color.status_ok : R.color.status_neutral));

        // SPEC-PING-001: probe the client when the screen is shown
        // (throttled to 30 s inside PingProber) and show the latest
        // result.
        probePing();

        if (a11yOn) {
            setStatusRow(modeStatusValue, getString(R.string.status_mode_full),
                    getColor(R.color.status_ok), null);
        } else if (MODE_BANKING_SAFE.equals(mode)) {
            setStatusRow(modeStatusValue, getString(R.string.status_mode_banking),
                    getColor(R.color.status_warn), null);
        } else {
            setStatusRow(modeStatusValue, getString(R.string.status_mode_minimal),
                    getColor(R.color.status_neutral), null);
        }

        if (a11yOn) {
            setStatusRow(a11yStatusValue, getString(R.string.status_enabled),
                    getColor(R.color.status_ok), null);
        } else if (MODE_BANKING_SAFE.equals(mode)) {
            // P1-1 (CONS-022): in BANKING_SAFE the service stays
            // off BY DESIGN — never prompt the user to enable
            // it.
            setStatusRow(a11yStatusValue, getString(R.string.status_not_enabled),
                    getColor(R.color.status_warn),
                    getString(R.string.status_note_a11y_banking));
        } else {
            // MINIMAL only: prompt to enable a11y.
            setStatusRow(a11yStatusValue, getString(R.string.status_not_enabled),
                    getColor(R.color.status_bad),
                    getString(R.string.status_note_a11y_minimal));
        }

        boolean notifGranted = BridgeNotificationListener.isAccessGranted(this);
        setStatusRow(notifStatusValue,
                getString(notifGranted ? R.string.status_granted : R.string.status_not_granted),
                getColor(notifGranted ? R.color.status_ok : R.color.status_bad),
                notifGranted ? null : getString(R.string.status_note_notif));

        // SPEC-UI-001: only the action that applies is shown/enabled.
        // v2.6.2 (GPT CONS-047 P1): a disabled button keeps a readable
        // medium-grey text instead of fading away (no alpha dim).
        startButton.setVisibility(running ? View.GONE : View.VISIBLE);
        stopButton.setVisibility(running ? View.VISIBLE : View.GONE);
        a11yButton.setEnabled(!a11yOn);
        a11yButton.setTextColor(a11yOn
                ? getColor(R.color.button_disabled_text)
                : a11yButtonDefaultTextColor);
        notifButton.setEnabled(!notifGranted);
        notifButton.setTextColor(notifGranted
                ? getColor(R.color.button_disabled_text)
                : notifButtonDefaultTextColor);

        // P0-2 (v2.0): the mode button shows the persisted mode
        // (what applies when a11y is off) and what a tap does.
        // SPEC-UI-001 fix: while a11y is on, the effective mode is
        // FULL ACCESS, so the button says when the persisted mode
        // applies — otherwise "banking safe: ON" looks active
        // next to a "FULL ACCESS" status row.
        boolean bankingSafe = MODE_BANKING_SAFE.equals(getPersistedMode(this));
        if (a11yOn) {
            modeButton.setText(bankingSafe
                    ? R.string.btn_mode_banking_safe_a11y_on
                    : R.string.btn_mode_minimal_a11y_on);
        } else {
            modeButton.setText(bankingSafe
                    ? R.string.btn_mode_banking_safe : R.string.btn_mode_minimal);
        }

        currentAddress = ip != null
                ? "http://" + ip + ":" + HttpBridgeServer.PORT + "  (and http://127.0.0.1:" + HttpBridgeServer.PORT + ")"
                : "http://127.0.0.1:" + HttpBridgeServer.PORT + "  (Tailscale address not found)";
        refreshAddress();

        refreshToken();
    }

    /** SPEC-UI-001: the token is fully masked by default (v2.6.3). */
    private void refreshToken() {
        String token = getOrCreateToken(this);
        tokenText.setText(tokenRevealed ? token : MASKED);
        // v2.6.2 (GPT CONS-047 P1): the label says what a tap does.
        tokenToggleLabel.setText(tokenRevealed ? R.string.token_hide : R.string.token_show);
    }

    /** SPEC-UI-001 v2.6.3: the address is fully masked by default. */
    private void refreshAddress() {
        addressText.setText(addressRevealed ? currentAddress : MASKED);
        addressToggleLabel.setText(addressRevealed ? R.string.token_hide : R.string.token_show);
    }

    /** SPEC-PING-001: start a phone-to-client probe (background
     * thread, throttled inside PingProber) and refresh the ping row
     * when the result arrives. */
    private void probePing() {
        PingProber.probe(result -> runOnUiThread(this::updatePingDisplay));
        updatePingDisplay();
    }

    /** SPEC-PING-001: show the latest probe result in the ping row.
     * No result yet: either no client has connected, or a probe is
     * running. */
    private void updatePingDisplay() {
        PingProber.Result r = PingProber.getLastResult();
        String target = PingProber.getLastClientIp();
        if (r == null) {
            if (target == null) {
                pingText.setText(R.string.ping_no_client);
                pingDetail.setText("");
            } else {
                pingText.setText(R.string.ping_measuring);
                pingDetail.setText(getString(R.string.ping_target, target));
            }
            pingRow.setContentDescription(getString(R.string.ping_row_desc));
            return;
        }
        String method = "icmp".equals(r.method) ? "ICMP" : "TCP";
        String detail = getString(R.string.ping_target, r.target)
                + "（" + method + "）・タップで再計測";
        if (r.ok()) {
            pingText.setText(getString(R.string.ping_result, r.rttMs,
                    r.received, r.sent, formatAge(r.measuredAtElapsedMs)));
            pingDetail.setText(detail);
        } else {
            pingText.setText(getString(R.string.ping_failed, r.error));
            // CONS-052 PING-02: a refused TCP connect is a failure,
            // but the measured time is still useful — show it.
            if (r.rttMs >= 0) {
                detail += "・応答 " + r.rttMs + "ms";
            }
            pingDetail.setText(detail);
        }
        // CONS-052 PING-06: while a fresh probe is running, say so —
        // the numbers above are the previous result, with its age.
        if (PingProber.isProbing()) {
            pingDetail.setText(pingDetail.getText() + "・計測中");
        }
        pingRow.setContentDescription(
                pingText.getText() + "。" + pingDetail.getText());
    }

    /** SPEC-PING-001: "たった今" / "N 秒前" / "N 分前" / "N 時間前".
     * CONS-052 PING-07: elapsedRealtime (monotonic) for the age,
     * so a system-clock change cannot skew it. */
    private String formatAge(long measuredAtElapsedMs) {
        long ageSec = (android.os.SystemClock.elapsedRealtime()
                - measuredAtElapsedMs) / 1000;
        if (ageSec < 0) {
            ageSec = 0;
        }
        if (ageSec < 5) {
            return getString(R.string.ping_just_now);
        } else if (ageSec < 60) {
            return getString(R.string.ping_seconds_ago, ageSec);
        } else if (ageSec < 3600) {
            return getString(R.string.ping_minutes_ago, ageSec / 60);
        } else {
            return getString(R.string.ping_hours_ago, ageSec / 3600);
        }
    }

    /** SPEC-UI-002: one flow node — a coloured dot (circle) plus a
     * status word in the same colour. Colour never carries
     * information alone (the word is always shown). */
    private void setFlowNode(View node, TextView dot, TextView status,
                             String label, String word, int color) {
        GradientDrawable circle = new GradientDrawable();
        circle.setShape(GradientDrawable.OVAL);
        circle.setColor(color);
        dot.setBackground(circle);
        status.setText(word);
        status.setTextColor(color);
        // CONS-050 P1-3: the node's inner views are marked
        // importantForAccessibility="no" in the layout, so the
        // screen reader announces this combined description once.
        node.setContentDescription(label + "、" + word);
    }

    /** SPEC-UI-001: state word in its colour, optional note in grey. */
    private void setStatusRow(TextView view, String state, int stateColor, String note) {
        SpannableStringBuilder sb = new SpannableStringBuilder(state);
        sb.setSpan(new ForegroundColorSpan(stateColor), 0, state.length(),
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        if (note != null && !note.isEmpty()) {
            int start = sb.length();
            sb.append(" — ").append(note);
            sb.setSpan(new ForegroundColorSpan(getColor(R.color.status_neutral)),
                    start, sb.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        view.setText(sb);
    }
}
