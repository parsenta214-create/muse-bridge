package dev.muse.bridge;

import android.os.SystemClock;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.ConnectException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** SPEC-PING-001: probe the Muse client from this phone and keep the
 * latest result, so a slow or broken link can be debugged from both
 * directions (the client already measures its side in the health
 * check; this is the phone's side).
 *
 * Target: the remote IP of the most recent authenticated client
 * connection to this server (the Muse client's Tailscale address).
 * Loopback / link-local / multicast addresses are ignored.
 *
 * Method: ICMP ping first ("/system/bin/ping"); if the binary is
 * missing or fails, fall back to a TCP connect timing. A refused
 * connection is NOT a success (CONS-052 PING-02): it is reported
 * as a failure, but the measured time is kept so the display can
 * still show how long the round trip took.
 *
 * Probes always run on a background thread, at most one at a time
 * (CONS-052 PING-03/PING-09), and are throttled to at most once
 * per 30 seconds. Listeners run on the probe thread — post to the
 * UI thread before touching views. */
public class PingProber {

    /** One probe result. Immutable. rttMs is -1 when nothing was
     * measured; on a refused TCP connect it still holds the time
     * until the refusal (see the "refused" error). */
    public static class Result {
        public final String target;
        public final String method; // "icmp" or "tcp-connect"
        public final long rttMs;
        public final int sent;
        public final int received;
        public final long measuredAtMs; // System.currentTimeMillis(), for /state
        public final long measuredAtElapsedMs; // SystemClock.elapsedRealtime(), for age
        public final String error; // null on success

        public Result(String target, String method, long rttMs,
                      int sent, int received, long measuredAtMs,
                      long measuredAtElapsedMs, String error) {
            this.target = target;
            this.method = method;
            this.rttMs = rttMs;
            this.sent = sent;
            this.received = received;
            this.measuredAtMs = measuredAtMs;
            this.measuredAtElapsedMs = measuredAtElapsedMs;
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
    private static final int PING_WAIT_TIMEOUT_S = 10;
    private static final int TCP_TIMEOUT_MS = 3000;
    private static final int TCP_PORT = 22;

    private static volatile String lastClientIp = null;
    private static volatile Result lastResult = null;
    private static volatile long lastProbeStartedMs = 0;
    private static volatile boolean probing = false;

    /** CONS-052 PING-10: when the target changes, the old result
     * belongs to another client — drop it so the display never
     * shows a stale target's numbers. */
    public static synchronized void setLastClientIp(String ip) {
        if (ip == null) {
            return;
        }
        if (!ip.equals(lastClientIp)) {
            lastClientIp = ip;
            lastResult = null;
        }
    }

    public static String getLastClientIp() {
        return lastClientIp;
    }

    public static Result getLastResult() {
        return lastResult;
    }

    /** CONS-052 PING-03/PING-06: true while a probe is running. */
    public static boolean isProbing() {
        return probing;
    }

    /** Start a probe on a background thread. At most one probe runs
     * at a time, and probes start at most once per 30 seconds; a
     * throttled call hands back the cached result when there is one. */
    public static synchronized void probe(Listener listener) {
        String target = lastClientIp;
        if (target == null || target.isEmpty()) {
            if (listener != null) {
                listener.onResult(new Result(null, null, -1, 0, 0,
                        System.currentTimeMillis(),
                        SystemClock.elapsedRealtime(), "no-client-yet"));
            }
            return;
        }
        long now = System.currentTimeMillis();
        if (probing || now - lastProbeStartedMs < MIN_INTERVAL_MS) {
            if (listener != null && lastResult != null) {
                listener.onResult(lastResult);
            }
            return;
        }
        lastProbeStartedMs = now;
        probing = true;
        new Thread(() -> {
            try {
                Result r = runProbe(target);
                lastResult = r;
                if (listener != null) {
                    listener.onResult(r);
                }
            } finally {
                probing = false;
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
     * is missing, fails, or no reply arrives (caller falls back).
     * CONS-052 PING-01: stderr is merged into stdout, the exit code
     * is checked, and the process is killed after a timeout. */
    private static Result runIcmpPing(String target) {
        try {
            Process p = new ProcessBuilder("/system/bin/ping",
                    "-c", String.valueOf(PING_COUNT),
                    "-W", String.valueOf(PING_TIMEOUT_S), target)
                    .redirectErrorStream(true)
                    .start();
            StringBuilder out = new StringBuilder();
            try (BufferedReader r = new BufferedReader(
                    new InputStreamReader(p.getInputStream()))) {
                String line;
                while ((line = r.readLine()) != null) {
                    out.append(line).append('\n');
                }
            }
            boolean finished = p.waitFor(PING_WAIT_TIMEOUT_S, TimeUnit.SECONDS);
            if (!finished) {
                p.destroyForcibly();
                return null;
            }
            if (p.exitValue() != 0) {
                return null;
            }
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
                        System.currentTimeMillis(),
                        SystemClock.elapsedRealtime(), null);
            }
            return null;
        } catch (Exception e) {
            return null;
        }
    }

    /** TCP connect timing. CONS-052 PING-02: a refused connection is
     * a failure ("refused"), not a success — but the time until the
     * refusal is kept in rttMs, so the display can still show how
     * long the round trip took. */
    private static Result runTcpConnect(String target) {
        long t0 = SystemClock.elapsedRealtime();
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress(target, TCP_PORT), TCP_TIMEOUT_MS);
            long rtt = SystemClock.elapsedRealtime() - t0;
            return new Result(target, "tcp-connect", rtt, 1, 1,
                    System.currentTimeMillis(),
                    SystemClock.elapsedRealtime(), null);
        } catch (ConnectException e) {
            long rtt = SystemClock.elapsedRealtime() - t0;
            return new Result(target, "tcp-connect", rtt, 1, 0,
                    System.currentTimeMillis(),
                    SystemClock.elapsedRealtime(), "refused");
        } catch (Exception e) {
            return new Result(target, "tcp-connect", -1, 1, 0,
                    System.currentTimeMillis(),
                    SystemClock.elapsedRealtime(), "unreachable");
        }
    }
}
