package dev.muse.bridge;

import java.util.ArrayList;
import java.util.List;

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
 */
public final class VpnBindSelector {

    /** No VPN network, or no 100.64/10 candidate in the one VPN. */
    public static final String DEGRADED_NO_VPN = "tailscale-interface-unavailable";
    /** More than one VPN network, or more than one 100.64/10 address. */
    public static final String DEGRADED_AMBIGUOUS = "tailscale-network-ambiguous";

    private VpnBindSelector() {
    }

    /** One VPN network as seen by the ConnectivityManager callback. */
    public static final class VpnNetwork {
        /** Interface name from LinkProperties; may be null. */
        public final String interfaceName;
        /** IPv4 addresses (dotted quads) from LinkProperties. */
        public final List<String> ipv4Addresses;

        public VpnNetwork(String interfaceName, List<String> ipv4Addresses) {
            this.interfaceName = interfaceName;
            this.ipv4Addresses = ipv4Addresses == null
                    ? new ArrayList<String>() : ipv4Addresses;
        }
    }

    /** The decision: bind to one address, or DEGRADED with a reason. */
    public static final class Decision {
        /** Address to bind; null when DEGRADED. */
        public final String bindIp;
        /** Interface of the bind address; null when DEGRADED. */
        public final String bindInterface;
        /** DEGRADED reason; null when binding. */
        public final String degradedReason;

        private Decision(String bindIp, String bindInterface, String degradedReason) {
            this.bindIp = bindIp;
            this.bindInterface = bindInterface;
            this.degradedReason = degradedReason;
        }

        public boolean isBind() {
            return bindIp != null;
        }

        public static Decision bind(String ip, String ifName) {
            return new Decision(ip, ifName, null);
        }

        public static Decision degraded(String reason) {
            return new Decision(null, null, reason);
        }
    }

    /** 100.64.0.0/10 (Tailscale range; also carrier CGNAT). */
    public static boolean isTailscaleRange(String ip) {
        if (ip == null) {
            return false;
        }
        String[] parts = ip.split("\\.");
        if (parts.length != 4) {
            return false;
        }
        try {
            int first = Integer.parseInt(parts[0]);
            int second = Integer.parseInt(parts[1]);
            return first == 100 && second >= 64 && second <= 127;
        } catch (NumberFormatException e) {
            return false;
        }
    }

    /**
     * Network-first, address-second. Count the VPN networks first:
     * 0 or 2+ is DEGRADED. With exactly one, evaluate its IPv4
     * addresses: 0 or 2+ 100.64/10 candidates is DEGRADED; exactly
     * one is bound.
     */
    public static Decision select(List<VpnNetwork> networks) {
        if (networks == null || networks.isEmpty()) {
            return Decision.degraded(DEGRADED_NO_VPN);
        }
        if (networks.size() > 1) {
            return Decision.degraded(DEGRADED_AMBIGUOUS);
        }
        VpnNetwork net = networks.get(0);
        List<String> candidates = new ArrayList<>();
        for (String ip : net.ipv4Addresses) {
            if (isTailscaleRange(ip)) {
                candidates.add(ip);
            }
        }
        if (candidates.isEmpty()) {
            return Decision.degraded(DEGRADED_NO_VPN);
        }
        if (candidates.size() > 1) {
            return Decision.degraded(DEGRADED_AMBIGUOUS);
        }
        return Decision.bind(candidates.get(0), net.interfaceName);
    }
}
