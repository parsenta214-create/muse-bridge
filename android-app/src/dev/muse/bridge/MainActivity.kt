// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-13
package dev.muse.bridge

import android.Manifest
import android.app.Activity
import android.app.StatusBarManager
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.Icon
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.View
import android.widget.Button
import android.widget.TextView
import android.widget.Toast

import java.net.Inet4Address
import java.net.InetAddress
import java.net.NetworkInterface
import java.util.Collections
import java.util.UUID

/** Single screen: status, bridge address, runtime token, start/stop. */
// The Java original was a non-final public class; keep it open so
// Java subclassing stays possible (D4 contract, CONS-161 P2-02).
open class MainActivity : Activity() {

    companion object {
        const val PREFS = "bridge"
        const val KEY_TOKEN = "token"

        private const val REQUEST_NOTIFICATIONS = 1
        private const val REQUEST_RUN_COMMAND = 2
        // Decision 95 (v2.5.0, TARMUX_KICKASS step 2): Termux's
        // RUN_COMMAND permission, a dangerous permission defined
        // by Termux itself (not in android.Manifest).
        private const val PERMISSION_RUN_COMMAND =
            "com.termux.permission.RUN_COMMAND"

        /** SPEC-UI-001 v2.6.3: fully masked — no partial characters. */
        private const val MASKED = "●●●●●●●●"

        /** Returns the runtime token, generating and storing one on first use. */
        @JvmStatic
        fun getOrCreateToken(ctx: Context): String {
            val prefs: SharedPreferences =
                ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val existing = prefs.getString(KEY_TOKEN, null)
            if (!existing.isNullOrEmpty()) {
                return existing
            }
            val token = UUID.randomUUID().toString()
            prefs.edit().putString(KEY_TOKEN, token).apply()
            return token
        }

        /** P0-2/P0-3 (v2.0): the persisted startup mode. Only the two
         * a11y-off modes are persisted — FULL_ACCESS is derived from
         * the live a11y state (see HttpBridgeServer.currentMode), so
         * it is never stored. Unknown values fall back to MINIMAL.
         * The token key is untouched; no secret is ever stored here. */
        const val KEY_MODE = "mode"
        const val MODE_MINIMAL = "MINIMAL"
        const val MODE_BANKING_SAFE = "BANKING_SAFE"

        @JvmStatic
        fun getPersistedMode(ctx: Context): String {
            val m = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY_MODE, MODE_MINIMAL)
            return if (MODE_BANKING_SAFE == m) MODE_BANKING_SAFE else MODE_MINIMAL
        }

        @JvmStatic
        fun setPersistedMode(ctx: Context, mode: String?) {
            // mode is nullable: the Java original used
            // MODE_BANKING_SAFE.equals(mode), which is null-safe and
            // falls back to MODE_MINIMAL for null. Keep that contract —
            // a non-null String parameter would throw NPE on a Java
            // null call (CONS-161 P2-01).
            val m = if (MODE_BANKING_SAFE == mode) MODE_BANKING_SAFE else MODE_MINIMAL
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().putString(KEY_MODE, m).apply()
        }

        /** Tailscale IPv4 range 100.64.0.0/10 (CGNAT): first octet
         * 100, second octet 64–127. CONS-029 P1-3 fix: the old
         * startsWith("100.") matched all of 100.0.0.0/8. This is the
         * Tailscale IP DETECTION for display (/health tailscale_ips,
         * the service notification address) — best effort only.
         * SPEC-LOG-001 BIND-01 (v2.8.6): the server bind decision no
         * longer uses this test (100.64/10 is also carrier CGNAT);
         * it uses VpnBindSelector + the ConnectivityManager VPN
         * discovery instead. The range check lives in
         * VpnBindSelector; this method delegates to it. */
        @JvmStatic
        fun isTailscaleIp(ip: String?): Boolean {
            return VpnBindSelector.isTailscaleRange(ip)
        }

        /** Best-effort Tailscale IPv4 addresses (100.64.0.0/10), or an
         * empty list when absent. P1-3 (CONS-022): the /health
         * tailscale_ips must be the CURRENT addresses, re-fetched
         * on each call — not the bound-at-start list, which goes
         * stale if Tailscale reassigns the IP. */
        @JvmStatic
        fun findTailscaleIps(): List<String> {
            val out = ArrayList<String>()
            try {
                for (ni in Collections.list(NetworkInterface.getNetworkInterfaces())) {
                    for (addr in Collections.list(ni.inetAddresses)) {
                        if (addr is Inet4Address && !addr.isLoopbackAddress) {
                            val ip = addr.hostAddress
                            // hostAddress is a platform type (String?);
                            // isTailscaleRange(null) is false, but the
                            // compiler cannot see that, so check null
                            // here to keep out.add(ip) type-safe.
                            if (ip != null && isTailscaleIp(ip)) {
                                out.add(ip)
                            }
                        }
                    }
                }
            } catch (_: Exception) {
                // Best effort only; the address line just stays empty.
            }
            return out
        }

        /** Best-effort Tailscale IPv4 address (100.x), or null when absent. */
        @JvmStatic
        fun findTailscaleIp(): String? {
            val ips = findTailscaleIps()
            return if (ips.isEmpty()) null else ips[0]
        }

        @JvmStatic
        fun isAccessibilityEnabled(ctx: Context): Boolean {
            val enabled = Settings.Secure.getString(
                ctx.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
            )
            if (enabled == null) {
                return false
            }
            // The setting stores flattened component names in FULL form
            // ("dev.muse.bridge/dev.muse.bridge.BridgeAccessibility
            // Service"); the short form (".BridgeAccessibilityService")
            // never matches, so check both (decision 64).
            val cn = ComponentName(ctx, BridgeAccessibilityService::class.java)
            return enabled.contains(cn.flattenToString())
                || enabled.contains(cn.flattenToShortString())
        }
    }

    // SPEC-UI-002: the bridge state is a dial (StatusDialView), the
    // mode / a11y / notification states live in cards (SPEC-UI-001
    // rows, same IDs), and the connection flow shows device ->
    // Tailscale -> server.
    private lateinit var statusDial: StatusDialView
    private lateinit var flowNodeDevice: View
    private lateinit var flowNodeTailscale: View
    private lateinit var flowNodeServer: View
    private lateinit var flowArrowTailscale: TextView
    private lateinit var flowArrowServer: TextView
    private lateinit var flowDotDevice: TextView
    private lateinit var flowDotTailscale: TextView
    private lateinit var flowDotServer: TextView
    private lateinit var flowStatusDevice: TextView
    private lateinit var flowStatusTailscale: TextView
    private lateinit var flowStatusServer: TextView
    /** SPEC-PING-001: the phone-to-client probe row under the flow. */
    private lateinit var pingRow: View
    private lateinit var pingText: TextView
    private lateinit var pingDetail: TextView
    private lateinit var modeStatusValue: TextView
    private lateinit var a11yStatusValue: TextView
    private lateinit var notifStatusValue: TextView
    private lateinit var addressText: TextView
    private lateinit var addressToggleLabel: TextView
    private lateinit var tokenText: TextView
    private lateinit var tokenToggleLabel: TextView
    private lateinit var versionText: TextView
    private lateinit var modeButton: Button
    private lateinit var startButton: Button
    private lateinit var stopButton: Button
    private lateinit var a11yButton: Button
    private lateinit var notifButton: Button
    /** SPEC-UI-001 v2.6.2: enabled text colours, captured once so
     * refresh() can restore them after setting the disabled colour. */
    private var a11yButtonDefaultTextColor: Int = 0
    private var notifButtonDefaultTextColor: Int = 0
    /** SPEC-UI-001: the token is masked by default; tap to reveal. */
    private var tokenRevealed = false
    /** SPEC-UI-001 v2.6.3: the address is fully masked by default; tap to reveal. */
    private var addressRevealed = false
    /** SPEC-UI-001 v2.6.3: the computed address text (shown when revealed). */
    private var currentAddress = ""

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
    private val a11yStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            refresh()
        }
    }
    private var a11yReceiverRegistered = false

    // SPEC-A11Y-OFF-002 v6: the disable logic (the three-valued
    // state, the state read, the disable handler, the completion
    // check, the finish step, the token, the shortcut) moved to
    // A11yOffExecutor. This Activity no longer disables a11y.

    // SPEC-A11Y-OFF-002 v6: the three-valued state read moved to
    // A11yOffExecutor. isAccessibilityEnabled() below stays — it is
    // the Settings-string check used for display / diagnostics only.

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // SPEC-DRIVE-001: the first-init check must run before
        // any app-owned state file is created, on every entry
        // point — otherwise the run-meta file (created by
        // BridgeLog.init) would make a fresh install look like
        // a re-init and the picker would refuse forever.
        LogActivity.ensureDriveInit(this)
        setContentView(R.layout.activity_main)

        // v1.3: update notifications need this on Android 13+.
        // Asked once here; the /notify endpoint reports back when
        // it is still missing so the VM can say so instead of
        // failing silently.
        if (Build.VERSION.SDK_INT >= 33
            && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(
                arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                REQUEST_NOTIFICATIONS
            )
        }

        // Decision 95 (v2.5.0): ask for Termux RUN_COMMAND once,
        // so POST /repair_termux can work. If the user denies
        // it, the endpoint answers 403 with the Settings path.
        if (checkSelfPermission(PERMISSION_RUN_COMMAND)
            != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(
                arrayOf(PERMISSION_RUN_COMMAND),
                REQUEST_RUN_COMMAND
            )
        }

        statusDial = findViewById(R.id.status_dial)
        flowNodeDevice = findViewById(R.id.flow_node_device)
        flowNodeTailscale = findViewById(R.id.flow_node_tailscale)
        flowNodeServer = findViewById(R.id.flow_node_server)
        flowArrowTailscale = findViewById(R.id.flow_arrow_tailscale)
        flowArrowServer = findViewById(R.id.flow_arrow_server)
        flowDotDevice = findViewById(R.id.flow_dot_device)
        flowDotTailscale = findViewById(R.id.flow_dot_tailscale)
        flowDotServer = findViewById(R.id.flow_dot_server)
        flowStatusDevice = findViewById(R.id.flow_status_device)
        flowStatusTailscale = findViewById(R.id.flow_status_tailscale)
        flowStatusServer = findViewById(R.id.flow_status_server)
        pingRow = findViewById(R.id.ping_row)
        pingText = findViewById(R.id.ping_text)
        pingDetail = findViewById(R.id.ping_detail)
        // SPEC-PING-001: tap the ping row to re-probe (throttled
        // to 30 s inside PingProber).
        pingRow.setOnClickListener { probePing() }
        modeStatusValue = findViewById(R.id.status_mode_value)
        a11yStatusValue = findViewById(R.id.status_a11y_value)
        notifStatusValue = findViewById(R.id.status_notif_value)
        addressText = findViewById(R.id.address_text)
        tokenText = findViewById(R.id.token_text)
        versionText = findViewById(R.id.version_text)

        // SPEC-UI-001: tap the token (or the show/hide label) to
        // toggle masked / revealed. v2.6.2: the label makes the
        // affordance explicit (GPT CONS-047 P1).
        val tokenToggle = View.OnClickListener {
            tokenRevealed = !tokenRevealed
            refreshToken()
        }
        tokenText.setOnClickListener(tokenToggle)
        tokenToggleLabel = findViewById(R.id.token_toggle_label)
        tokenToggleLabel.setOnClickListener(tokenToggle)

        // SPEC-UI-001 v2.6.3: the address uses the same tap-to-reveal pattern.
        val addressToggle = View.OnClickListener {
            addressRevealed = !addressRevealed
            refreshAddress()
        }
        addressText.setOnClickListener(addressToggle)
        addressToggleLabel = findViewById(R.id.address_toggle_label)
        addressToggleLabel.setOnClickListener(addressToggle)

        startButton = findViewById(R.id.start_button)
        stopButton = findViewById(R.id.stop_button)
        a11yButton = findViewById(R.id.accessibility_button)
        val regen: Button = findViewById(R.id.regenerate_button)
        val log: Button = findViewById(R.id.log_button)
        log.setOnClickListener {
            startActivity(Intent(this, LogActivity::class.java))
        }

        startButton.setOnClickListener {
            startForegroundService(Intent(this, BridgeService::class.java))
            refresh()
        }
        stopButton.setOnClickListener {
            // SPEC-LOG-001 LOG-03: the app Stop button is a manual stop.
            BridgeService.pendingStopReason =
                HttpBridgeServer.StopReason.MANUAL
            stopService(Intent(this, BridgeService::class.java))
            refresh()
        }
        a11yButton.setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        // P0-2 (v2.0): toggle the persisted mode between MINIMAL
        // and BANKING SAFE. The mode only takes effect while the
        // accessibility service is off (a11y on means FULL ACCESS);
        // the preference is sticky, so it re-appears when a11y
        // goes off. The bridge never enables a11y itself.
        modeButton = findViewById(R.id.mode_button)
        modeButton.setOnClickListener {
            val current = getPersistedMode(this)
            setPersistedMode(
                this,
                if (MODE_BANKING_SAFE == current) MODE_MINIMAL else MODE_BANKING_SAFE
            )
            refresh()
            // v2.6.0 (SPEC-WIDGET.md, W3): push a widget
            // refresh so the mode line updates.
            BridgeWidgetProvider.updateAllWidgets(this)
        }
        notifButton = findViewById(R.id.notification_button)
        notifButton.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }
        val addTile: Button = findViewById(R.id.add_tile_button)
        addTile.setOnClickListener {
            requestAddTile()
        }
        regen.setOnClickListener {
            val token = UUID.randomUUID().toString()
            getSharedPreferences(PREFS, MODE_PRIVATE)
                .edit().putString(KEY_TOKEN, token).apply()
            refresh()
        }
        // SPEC-UI-001 v2.6.2: remember the enabled text colours so
        // refresh() can restore them after setting the disabled colour.
        a11yButtonDefaultTextColor = a11yButton.currentTextColor
        notifButtonDefaultTextColor = notifButton.currentTextColor
        refresh()
    }

    override fun onResume() {
        super.onResume()
        // CONS-029 P1-2: listen for service connect/disconnect
        // while visible (registered before refresh so no event
        // between the two is missed).
        if (!a11yReceiverRegistered) {
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(
                    a11yStateReceiver,
                    IntentFilter(BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED),
                    Context.RECEIVER_NOT_EXPORTED
                )
            } else {
                registerReceiver(
                    a11yStateReceiver,
                    IntentFilter(BridgeAccessibilityService.ACTION_A11Y_STATE_CHANGED)
                )
            }
            a11yReceiverRegistered = true
        }
        refresh()
    }

    override fun onPause() {
        if (a11yReceiverRegistered) {
            try {
                unregisterReceiver(a11yStateReceiver)
            } catch (_: IllegalArgumentException) {
                // Already unregistered; nothing to do.
            }
            a11yReceiverRegistered = false
        }
        super.onPause()
    }

    /**
     * Ask the system to add the Quick Settings tile (v1.7, Android 13+).
     * The system shows a one-tap "Add tile?" dialog; the user still
     * confirms once, but never has to hunt through the QS edit panel.
     * On older Android, point at the manual route instead.
     */
    private fun requestAddTile() {
        if (Build.VERSION.SDK_INT >= 33) {
            val sbm = getSystemService(StatusBarManager::class.java)
            if (sbm == null) {
                Toast.makeText(this, R.string.toast_tile_failed, Toast.LENGTH_LONG).show()
                return
            }
            sbm.requestAddTileService(
                ComponentName(this, BridgeTileService::class.java),
                getString(R.string.tile_label),
                Icon.createWithResource(this, R.drawable.ic_tile),
                mainExecutor
            ) { result ->
                val message = when (result) {
                    StatusBarManager.TILE_ADD_REQUEST_RESULT_TILE_ADDED ->
                        R.string.toast_tile_added
                    StatusBarManager.TILE_ADD_REQUEST_RESULT_TILE_ALREADY_ADDED ->
                        R.string.toast_tile_already_added
                    else -> R.string.toast_tile_failed
                }
                Toast.makeText(this, message, Toast.LENGTH_LONG).show()
            }
        } else {
            Toast.makeText(this, R.string.toast_tile_failed, Toast.LENGTH_LONG).show()
        }
    }

    private fun refresh() {
        // v1.8: the installed version, so the user (and Muse, via
        // /state) can tell at a glance which build is on the phone.
        versionText.text = "v" + AppVersion.name(this) +
            " (" + AppVersion.code(this) + ")"

        // SPEC-UI-001: each status row shows its state word in the
        // colour for that state, with the explanation in grey after
        // it (setStatusRow). Colour never carries information alone.
        val running = BridgeService.isRunning()
        val ip = findTailscaleIp()

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
        val a11yOn = BridgeAccessibilityService.getInstance() != null
        val mode = if (a11yOn) "FULL ACCESS" else getPersistedMode(this)

        // SPEC-UI-002: the dial shows the server state (ring +
        // state word + mode sub-line); the flow shows device ->
        // Tailscale -> server.
        val modeShort = if (a11yOn) getString(R.string.mode_short_full)
            else if (MODE_BANKING_SAFE == mode) getString(R.string.status_mode_banking)
            else getString(R.string.status_mode_minimal)
        statusDial.setRunning(running)
        statusDial.setModeLabel(modeShort)
        // CONS-050 P1-3: the dial's Canvas text is not announced by
        // a screen reader, so set the spoken description here.
        statusDial.contentDescription = "Bridge " +
            getString(if (running) R.string.status_running else R.string.status_stopped) +
            "、" + modeShort
        // CONS-050 P1-1: the flow words describe what was actually
        // checked — the app runs on this device, the Tailscale IP
        // was obtained (not that the tunnel is proven up), the
        // service is running. They do not claim end-to-end
        // connectivity.
        setFlowNode(
            flowNodeDevice, flowDotDevice, flowStatusDevice,
            getString(R.string.flow_device),
            getString(R.string.flow_app_running), getColor(R.color.status_ok)
        )
        setFlowNode(
            flowNodeTailscale, flowDotTailscale, flowStatusTailscale,
            getString(R.string.flow_tailscale),
            getString(if (ip != null) R.string.flow_ip_found else R.string.flow_not_found),
            getColor(if (ip != null) R.color.status_ok else R.color.status_bad)
        )
        setFlowNode(
            flowNodeServer, flowDotServer, flowStatusServer,
            getString(R.string.flow_server),
            getString(if (running) R.string.status_running else R.string.status_stopped),
            getColor(if (running) R.color.status_ok else R.color.status_bad)
        )
        // CONS-050 P2-4: the arrows reflect the link state — green
        // only when the downstream node is up, grey otherwise.
        flowArrowTailscale.setTextColor(
            getColor(if (ip != null) R.color.status_ok else R.color.status_neutral)
        )
        flowArrowServer.setTextColor(
            getColor(if (running) R.color.status_ok else R.color.status_neutral)
        )

        // SPEC-PING-001: probe the client when the screen is shown
        // (throttled to 30 s inside PingProber) and show the latest
        // result.
        probePing()

        if (a11yOn) {
            setStatusRow(
                modeStatusValue, getString(R.string.status_mode_full),
                getColor(R.color.status_ok), null
            )
        } else if (MODE_BANKING_SAFE == mode) {
            setStatusRow(
                modeStatusValue, getString(R.string.status_mode_banking),
                getColor(R.color.status_warn), null
            )
        } else {
            setStatusRow(
                modeStatusValue, getString(R.string.status_mode_minimal),
                getColor(R.color.status_neutral), null
            )
        }

        if (a11yOn) {
            setStatusRow(
                a11yStatusValue, getString(R.string.status_enabled),
                getColor(R.color.status_ok), null
            )
        } else if (MODE_BANKING_SAFE == mode) {
            // P1-1 (CONS-022): in BANKING_SAFE the service stays
            // off BY DESIGN — never prompt the user to enable
            // it.
            setStatusRow(
                a11yStatusValue, getString(R.string.status_not_enabled),
                getColor(R.color.status_warn),
                getString(R.string.status_note_a11y_banking)
            )
        } else {
            // MINIMAL only: prompt to enable a11y.
            setStatusRow(
                a11yStatusValue, getString(R.string.status_not_enabled),
                getColor(R.color.status_bad),
                getString(R.string.status_note_a11y_minimal)
            )
        }

        val notifGranted = BridgeNotificationListener.isAccessGranted(this)
        setStatusRow(
            notifStatusValue,
            getString(if (notifGranted) R.string.status_granted else R.string.status_not_granted),
            getColor(if (notifGranted) R.color.status_ok else R.color.status_bad),
            if (notifGranted) null else getString(R.string.status_note_notif)
        )

        // SPEC-UI-001: only the action that applies is shown/enabled.
        // v2.6.2 (GPT CONS-047 P1): a disabled button keeps a readable
        // medium-grey text instead of fading away (no alpha dim).
        startButton.visibility = if (running) View.GONE else View.VISIBLE
        stopButton.visibility = if (running) View.VISIBLE else View.GONE
        a11yButton.isEnabled = !a11yOn
        a11yButton.setTextColor(
            if (a11yOn) getColor(R.color.button_disabled_text) else a11yButtonDefaultTextColor
        )
        notifButton.isEnabled = !notifGranted
        notifButton.setTextColor(
            if (notifGranted) getColor(R.color.button_disabled_text) else notifButtonDefaultTextColor
        )

        // P0-2 (v2.0): the mode button shows the persisted mode
        // (what applies when a11y is off) and what a tap does.
        // SPEC-UI-001 fix: while a11y is on, the effective mode is
        // FULL ACCESS, so the button says when the persisted mode
        // applies — otherwise "banking safe: ON" looks active
        // next to a "FULL ACCESS" status row.
        val bankingSafe = MODE_BANKING_SAFE == getPersistedMode(this)
        if (a11yOn) {
            modeButton.setText(
                if (bankingSafe) R.string.btn_mode_banking_safe_a11y_on
                else R.string.btn_mode_minimal_a11y_on
            )
        } else {
            modeButton.setText(
                if (bankingSafe) R.string.btn_mode_banking_safe
                else R.string.btn_mode_minimal
            )
        }

        currentAddress = if (ip != null)
            "http://" + ip + ":" + HttpBridgeServer.PORT +
                "  (and http://127.0.0.1:" + HttpBridgeServer.PORT + ")"
        else
            "http://127.0.0.1:" + HttpBridgeServer.PORT +
                "  (Tailscale address not found)"
        refreshAddress()

        refreshToken()
    }

    /** SPEC-UI-001: the token is fully masked by default (v2.6.3). */
    private fun refreshToken() {
        val token = getOrCreateToken(this)
        tokenText.text = if (tokenRevealed) token else MASKED
        // v2.6.2 (GPT CONS-047 P1): the label says what a tap does.
        tokenToggleLabel.setText(
            if (tokenRevealed) R.string.token_hide else R.string.token_show
        )
    }

    /** SPEC-UI-001 v2.6.3: the address is fully masked by default. */
    private fun refreshAddress() {
        addressText.text = if (addressRevealed) currentAddress else MASKED
        addressToggleLabel.setText(
            if (addressRevealed) R.string.token_hide else R.string.token_show
        )
    }

    /** SPEC-PING-001: start a phone-to-client probe (background
     * thread, throttled inside PingProber) and refresh the ping row
     * when the result arrives. */
    private fun probePing() {
        PingProber.probe { runOnUiThread { updatePingDisplay() } }
        updatePingDisplay()
    }

    /** SPEC-PING-001: show the latest probe result in the ping row.
     * No result yet: either no client has connected, or a probe is
     * running. */
    private fun updatePingDisplay() {
        val r = PingProber.getLastResult()
        val target = PingProber.getLastClientIp()
        if (r == null) {
            if (target == null) {
                pingText.setText(R.string.ping_no_client)
                pingDetail.text = ""
            } else {
                pingText.setText(R.string.ping_measuring)
                pingDetail.text = getString(R.string.ping_target, target)
            }
            pingRow.contentDescription = getString(R.string.ping_row_desc)
            return
        }
        val method = if ("icmp" == r.method) "ICMP" else "TCP"
        var detail = getString(R.string.ping_target, r.target) +
            "（" + method + "）・タップで再計測"
        if (r.ok()) {
            pingText.setText(
                getString(
                    R.string.ping_result, r.rttMs,
                    r.received, r.sent, formatAge(r.measuredAtElapsedMs)
                )
            )
            pingDetail.text = detail
        } else {
            pingText.setText(getString(R.string.ping_failed, r.error))
            // CONS-052 PING-02: a refused TCP connect is a failure,
            // but the measured time is still useful — show it.
            if (r.rttMs >= 0) {
                detail += "・応答 " + r.rttMs + "ms"
            }
            pingDetail.text = detail
        }
        // CONS-052 PING-06: while a fresh probe is running, say so —
        // the numbers above are the previous result, with its age.
        if (PingProber.isProbing()) {
            pingDetail.text = pingDetail.text.toString() + "・計測中"
        }
        pingRow.contentDescription =
            pingText.text.toString() + "。" + pingDetail.text.toString()
    }

    /** SPEC-PING-001: "たった今" / "N 秒前" / "N 分前" / "N 時間前".
     * CONS-052 PING-07: elapsedRealtime (monotonic) for the age,
     * so a system-clock change cannot skew it. */
    private fun formatAge(measuredAtElapsedMs: Long): String {
        var ageSec = (android.os.SystemClock.elapsedRealtime()
            - measuredAtElapsedMs) / 1000
        if (ageSec < 0) {
            ageSec = 0
        }
        return if (ageSec < 5) {
            getString(R.string.ping_just_now)
        } else if (ageSec < 60) {
            getString(R.string.ping_seconds_ago, ageSec)
        } else if (ageSec < 3600) {
            getString(R.string.ping_minutes_ago, ageSec / 60)
        } else {
            getString(R.string.ping_hours_ago, ageSec / 3600)
        }
    }

    /** SPEC-UI-002: one flow node — a coloured dot (circle) plus a
     * status word in the same colour. Colour never carries
     * information alone (the word is always shown). */
    private fun setFlowNode(
        node: View, dot: TextView, status: TextView,
        label: String, word: String, color: Int
    ) {
        val circle = GradientDrawable()
        circle.shape = GradientDrawable.OVAL
        circle.setColor(color)
        dot.background = circle
        status.text = word
        status.setTextColor(color)
        // CONS-050 P1-3: the node's inner views are marked
        // importantForAccessibility="no" in the layout, so the
        // screen reader announces this combined description once.
        node.contentDescription = "$label、$word"
    }

    /** SPEC-UI-001: state word in its colour, optional note in grey. */
    private fun setStatusRow(view: TextView, state: String, stateColor: Int, note: String?) {
        val sb = SpannableStringBuilder(state)
        sb.setSpan(
            ForegroundColorSpan(stateColor), 0, state.length,
            Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
        )
        if (note != null && note.isNotEmpty()) {
            val start = sb.length
            sb.append(" — ").append(note)
            sb.setSpan(
                ForegroundColorSpan(getColor(R.color.status_neutral)),
                start, sb.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
            )
        }
        view.text = sb
    }
}
