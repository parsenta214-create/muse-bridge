// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-09
package dev.muse.bridge

/**
 * SPEC-LOG-001 BIND-01: the pure VPN bind decision, separated from
 * Android APIs so the boundary conditions are unit-testable without
 * a device (§5.1). Inputs are plain data — the VPN networks found
 * by the ConnectivityManager callback, each with its interface name
 * and IPv4 addresses. No Android class is referenced here.
 *
 * Selection is network-first, then address (§3.6 steps 6–7):
 * the VPN network count is checked BEFORE any address is looked
 * at. Zero VPN networks, or two or more, is DEGRADED (fail-closed);
 * only with exactly one VPN network are its addresses evaluated,
 * and only exactly one 100.64/10 address in it may be bound.
 *
 * 100.64.0.0/10 is Tailscale's range AND carrier CGNAT's range, so
 * it is only an auxiliary selector among VPN networks — never the
 * sole criterion (CONS-059 P0-01). A non-Tailscale VPN holding a
 * 100.64/10 address cannot be distinguished by the public API;
 * that is a stated SPEC limit, guarded by the operating premise
 * that Tailscale is the device's sole VPN.
 *
 * Converted from VpnBindSelector.java in SPEC-KOTLIN-MIGRATION-001
 * batch A (v2.2). Java callers are unchanged: HttpBridgeServer
 * calls VpnBindSelector.select(...) and reads the Decision fields,
 * MainActivity calls VpnBindSelector.isTailscaleRange(...).
 * The public API is preserved with @JvmStatic / @JvmField /
 * const val, so the generated JVM shape matches the Java original
 * (static methods, public final fields, nested VpnNetwork /
 * Decision classes).
 */
class VpnBindSelector private constructor() {

    companion object {
        /** No VPN network, or no 100.64/10 candidate in the one VPN. */
        const val DEGRADED_NO_VPN = "tailscale-interface-unavailable"
        /** More than one VPN network, or more than one 100.64/10 address. */
        const val DEGRADED_AMBIGUOUS = "tailscale-network-ambiguous"

        /** 100.64.0.0/10 (Tailscale range; also carrier CGNAT). */
        @JvmStatic
        fun isTailscaleRange(ip: String?): Boolean {
            if (ip == null) {
                return false
            }
            // Regex split, like Java's String.split: trailing empty
            // strings are dropped, so "100.64.1.2." still parses as
            // a four-part quad (the Java original accepted it).
            val parts = ip.split("\\.".toRegex())
            if (parts.size != 4) {
                return false
            }
            return try {
                val first = parts[0].toInt()
                val second = parts[1].toInt()
                first == 100 && second in 64..127
            } catch (_: NumberFormatException) {
                false
            }
        }

        /**
         * Network-first, address-second. Count the VPN networks first:
         * 0 or 2+ is DEGRADED. With exactly one, evaluate its IPv4
         * addresses: 0 or 2+ 100.64/10 candidates is DEGRADED; exactly
         * one is bound.
         */
        @JvmStatic
        fun select(networks: List<VpnNetwork>?): Decision {
            if (networks.isNullOrEmpty()) {
                return Decision.degraded(DEGRADED_NO_VPN)
            }
            if (networks.size > 1) {
                return Decision.degraded(DEGRADED_AMBIGUOUS)
            }
            val net = networks[0]
            val candidates = ArrayList<String>()
            for (ip in net.ipv4Addresses) {
                if (isTailscaleRange(ip)) {
                    candidates.add(ip)
                }
            }
            if (candidates.isEmpty()) {
                return Decision.degraded(DEGRADED_NO_VPN)
            }
            if (candidates.size > 1) {
                return Decision.degraded(DEGRADED_AMBIGUOUS)
            }
            return Decision.bind(candidates[0], net.interfaceName)
        }
    }

    /** One VPN network as seen by the ConnectivityManager callback. */
    class VpnNetwork(
        /** Interface name from LinkProperties; may be null. */
        @JvmField val interfaceName: String?,
        ipv4Addresses: List<String>?
    ) {
        /** IPv4 addresses (dotted quads) from LinkProperties. */
        @JvmField
        val ipv4Addresses: List<String> = ipv4Addresses ?: emptyList()
    }

    /** The decision: bind to one address, or DEGRADED with a reason. */
    class Decision private constructor(
        /** Address to bind; null when DEGRADED. */
        @JvmField val bindIp: String?,
        /** Interface of the bind address; null when DEGRADED. */
        @JvmField val bindInterface: String?,
        /** DEGRADED reason; null when binding. */
        @JvmField val degradedReason: String?
    ) {
        fun isBind(): Boolean {
            return bindIp != null
        }

        companion object {
            @JvmStatic
            fun bind(ip: String, ifName: String?): Decision {
                return Decision(ip, ifName, null)
            }

            @JvmStatic
            fun degraded(reason: String): Decision {
                return Decision(null, null, reason)
            }
        }
    }
}
