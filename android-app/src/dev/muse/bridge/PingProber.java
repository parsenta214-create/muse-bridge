package dev.muse.bridge;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.ConnectException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** SPEC-PING-001: probe the Muse client from this phone and keep the
 * latest result, so a slow or broken link can be debugged from both
 * directions (the client already measures its side in the health
 * check; this is the phone's side).
 *
 * Target: the remote IP of the most recent client connection to this
 * server (the Muse client's Tailscale address). Loopback is ignored
 * — local dashboard / Termux connections are 127.0.0.1.
 *
 * Method: ICMP ping first ("/system/bin/ping"); if the binary is
 * missing or fails, fall back to a TCP connect timing (the time
 * until the connection succeeds or is refused is roughly the RTT).
 *
 * Probes always run on a background thread and are throttled to at
 * most once per 30 seconds. Listeners run on the probe thread — post
 * to the UI thread before touching views. */
public class PingProber {

    /** One probe result. Immutable; rttMs is -1 when the probe failed. */
    public static class Result {
        public final String target;
        public final String method; // "icmp" or "tcp-connect"
        public final long rttMs;
        public final int sent;
        public final int received;
        public final long measuredAtMs; // System.currentTimeMillis()
        public final String error; // null on success

        public Result(String target, String method, long rttMs,
                      int sent, int received, long measuredAtMs, String error) {
            this.target = target;
            this.method = method;
            this.rttMs = rttMs;
            this.sent = sent;
            this.received = received;
            this.measuredAtMs = measuredAtMs;
            this.error = error;
        }

        public boolean ok() {
            return error == null;
        }
    }

    public interface Listener {
        void onResult(Result result);
    }

    private static final long MIN_INTERVAL_MS = 30_000;
    private static final int PING_COUNT = 3;
    private static final int PING_TIMEOUT_S = 2;
    private static final int TCP_TIMEOUT_MS = 3000;
    private static final int TCP_PORT = 22;

    private static volatile String lastClientIp = null;
    private static volatile Result lastResult = null;
    private static volatile long lastProbeStartedMs = 0;

    public static void setLastClientIp(String ip) {
        lastClientIp = ip;
    }

    public static String getLastClientIp() {
        return lastClientIp;
    }

    public static Result getLastResult() {
        return lastResult;
    }

    /** Start a probe on a background thread. Throttled: if a probe
     * ran less than 30 seconds ago, the cached result is handed back
     * instead of starting a new probe. */
    public static synchronized void probe(Listener listener) {
        String target = lastClientIp;
        if (target == null || target.isEmpty()) {
            if (listener != null) {
                listener.onResult(new Result(null, null, -1, 0, 0,
                        System.currentTimeMillis(), "no-client-yet"));
            }
            return;
        }
        long now = System.currentTimeMillis();
        if (lastResult != null && now - lastProbeStartedMs < MIN_INTERVAL_MS) {
            if (listener != null) {
                listener.onResult(lastResult);
            }
            return;
        }
        lastProbeStartedMs = now;
        new Thread(() -> {
            Result r = runProbe(target);
            lastResult = r;
            if (listener != null) {
                listener.onResult(r);
            }
        }, "ping-prober").start();
    }

    private static Result runProbe(String target) {
        Result icmp = runIcmpPing(target);
        if (icmp != null) {
            return icmp;
        }
        return runTcpConnect(target);
    }

    /** ICMP ping via the system binary. Returns null when the binary
     * is missing, fails, or no reply arrives (caller falls back). */
    private static Result runIcmpPing(String target) {
        try {
            Process p = Runtime.getRuntime().exec(new String[]{
                    "/system/bin/ping", "-c", String.valueOf(PING_COUNT),
                    "-W", String.valueOf(PING_TIMEOUT_S), target});
            StringBuilder out = new StringBuilder();
            try (BufferedReader r = new BufferedReader(
                    new InputStreamReader(p.getInputStream()))) {
                String line;
                while ((line = r.readLine()) != null) {
                    out.append(line).append('\n');
                }
            }
            p.waitFor();
            Matcher packets = Pattern.compile(
                    "(\\d+) packets transmitted, (\\d+) received")
                    .matcher(out);
            int sent = PING_COUNT;
            int received = 0;
            if (packets.find()) {
                sent = Integer.parseInt(packets.group(1));
                received = Integer.parseInt(packets.group(2));
            }
            Matcher rtt = Pattern.compile(
                    "rtt min/avg/max/mdev = [0-9.]+/([0-9.]+)/")
                    .matcher(out);
            if (rtt.find() && received > 0) {
                long avg = Math.round(Double.parseDouble(rtt.group(1)));
                return new Result(target, "icmp", avg, sent, received,
                        System.currentTimeMillis(), null);
            }
            return null;
        } catch (Exception e) {
            return null;
        }
    }

    /** TCP connect timing. A fast refusal still measures the round
     * trip; only a timeout (or another failure) counts as an error. */
    private static Result runTcpConnect(String target) {
        long t0 = System.currentTimeMillis();
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress(target, TCP_PORT), TCP_TIMEOUT_MS);
            long rtt = System.currentTimeMillis() - t0;
            return new Result(target, "tcp-connect", rtt, 1, 1,
                    System.currentTimeMillis(), null);
        } catch (ConnectException e) {
            long rtt = System.currentTimeMillis() - t0;
            return new Result(target, "tcp-connect", rtt, 1, 1,
                    System.currentTimeMillis(), null);
        } catch (Exception e) {
            return new Result(target, "tcp-connect", -1, 1, 0,
                    System.currentTimeMillis(), "unreachable");
        }
    }
}
