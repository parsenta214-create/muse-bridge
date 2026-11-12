// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12
package dev.muse.bridge

import android.os.SystemClock
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.ConnectException
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.TimeUnit
import java.util.regex.Matcher
import java.util.regex.Pattern

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
 * UI thread before touching views.
 *
 * NOTE (Kotlin): Java locked the Class object — static
 * synchronized methods plus a synchronized class block.
 * Kotlin has no static methods on an object, and a Synchronized
 * annotation on a @JvmStatic function would lock the Class while
 * synchronized(PingProber) locks the singleton instance — two
 * different monitors, which would break the probe/throttle
 * exclusion. So all three critical sections use one explicit
 * lock object, PROBE_LOCK (the batch D pattern). No code
 * outside this file locks on the class object (verified
 * by grep), so the monitor change is safe. */
object PingProber {

    /** One probe result. Immutable. rttMs is -1 when nothing was
     * measured; on a refused TCP connect it still holds the time
     * until the refusal (see the "refused" error).
     *
     * NOTE (Kotlin): the fields are @JvmField so the Java callers
     * (HttpBridgeServer, MainActivity) keep their field access
     * (r.target, r.rttMs, r.error, ...) — plain Kotlin properties
     * would expose only getters. */
    class Result(
        @JvmField val target: String?,
        @JvmField val method: String?, // "icmp" or "tcp-connect"
        @JvmField val rttMs: Long,
        @JvmField val sent: Int,
        @JvmField val received: Int,
        @JvmField val measuredAtMs: Long, // System.currentTimeMillis(), for /state
        @JvmField val measuredAtElapsedMs: Long, // SystemClock.elapsedRealtime(), for age
        @JvmField val error: String? // null on success
    ) {
        fun ok(): Boolean {
            return error == null
        }
    }

    fun interface Listener {
        fun onResult(result: Result)
    }

    private const val MIN_INTERVAL_MS = 30_000L
    private const val PING_COUNT = 3
    private const val PING_TIMEOUT_S = 2
    private const val PING_WAIT_TIMEOUT_S = 10
    private const val TCP_TIMEOUT_MS = 3000
    private const val TCP_PORT = 22

    @Volatile
    private var lastClientIp: String? = null
    @Volatile
    private var lastResult: Result? = null
    /** CONS-053 NEW-02: the throttle uses elapsedRealtime
     * (monotonic), not the wall clock. */
    @Volatile
    private var lastProbeStartedMs: Long = 0
    @Volatile
    private var probing: Boolean = false

    /** The one monitor for all probe-state critical sections
     * (see the class KDoc). */
    private val PROBE_LOCK = Any()

    /** CONS-052 PING-10: when the target changes, the old result
     * belongs to another client — drop it so the display never
     * shows a stale target's numbers. CONS-053 NEW-01: a new
     * target may be probed at once — the old target's throttle
     * must not block it. */
    @JvmStatic
    fun setLastClientIp(ip: String?) {
        if (ip == null) {
            return
        }
        synchronized(PROBE_LOCK) {
            if (ip != lastClientIp) {
                lastClientIp = ip
                lastResult = null
                lastProbeStartedMs = 0
            }
        }
    }

    @JvmStatic
    fun getLastClientIp(): String? {
        return lastClientIp
    }

    @JvmStatic
    fun getLastResult(): Result? {
        return lastResult
    }

    /** CONS-052 PING-03/PING-06: true while a probe is running. */
    @JvmStatic
    fun isProbing(): Boolean {
        return probing
    }

    /** Start a probe on a background thread. At most one probe runs
     * at a time, and probes start at most once per 30 seconds; a
     * throttled call hands back the cached result when there is one. */
    @JvmStatic
    fun probe(listener: Listener?) {
        synchronized(PROBE_LOCK) {
            val target = lastClientIp
            if (target == null || target.isEmpty()) {
                listener?.onResult(Result(null, null, -1, 0, 0,
                        System.currentTimeMillis(),
                        SystemClock.elapsedRealtime(), "no-client-yet"))
                return
            }
            val now = SystemClock.elapsedRealtime()
            if (probing || now - lastProbeStartedMs < MIN_INTERVAL_MS) {
                val cached = lastResult
                if (listener != null && cached != null) {
                    listener.onResult(cached)
                }
                return
            }
            lastProbeStartedMs = now
            probing = true
            Thread({
                val r: Result? = try {
                    runProbe(target)
                } catch (e: Exception) {
                    null
                }
                var stored = false
                var targetChanged = false
                synchronized(PROBE_LOCK) {
                    probing = false
                    // CONS-053 PING-10: only keep the result if the
                    // target is still current; a result for an old
                    // target must not revive after a target change.
                    // CONS-054 NEW-04: whether the target changed is
                    // decided apart from the result — a failed probe
                    // for the old target must not stop the re-probe
                    // for the new one.
                    targetChanged = !target.equals(lastClientIp)
                    if (!targetChanged && r != null) {
                        lastResult = r
                        stored = true
                    }
                }
                if (stored) {
                    // stored is set only when r != null.
                    listener?.onResult(r!!)
                } else if (targetChanged && lastClientIp != null) {
                    // Target changed mid-probe: start a fresh probe for
                    // the new target (the throttle was reset by
                    // setLastClientIp). One re-probe per completed
                    // probe — a new target change during it starts
                    // its own single re-probe, so this cannot loop
                    // without further target changes.
                    probe(null)
                }
            }, "ping-prober").start()
        }
    }

    private fun runProbe(target: String): Result {
        val icmp = runIcmpPing(target)
        if (icmp != null) {
            return icmp
        }
        return runTcpConnect(target)
    }

    /** ICMP ping via the system binary. Returns null when the binary
     * is missing, fails, or no reply arrives (caller falls back).
     * CONS-052 PING-01: stderr is merged into stdout, the exit code
     * is checked, and the process is killed after a timeout.
     * CONS-053 PING-01: the output is drained on its own thread, so
     * a hung process cannot block the timeout — readLine() on the
     * probe thread would never reach waitFor(). */
    private fun runIcmpPing(target: String): Result? {
        try {
            val p = ProcessBuilder("/system/bin/ping",
                    "-c", PING_COUNT.toString(),
                    "-W", PING_TIMEOUT_S.toString(), target)
                    .redirectErrorStream(true)
                    .start()
            val pingIn = p.inputStream
            val out = StringBuilder()
            val reader = Thread({
                try {
                    BufferedReader(InputStreamReader(pingIn)).use { r ->
                        var line = r.readLine()
                        while (line != null) {
                            out.append(line).append('\n')
                            line = r.readLine()
                        }
                    }
                } catch (ignored: Exception) {
                }
            }, "ping-reader")
            reader.isDaemon = true
            reader.start()
            val finished = p.waitFor(PING_WAIT_TIMEOUT_S.toLong(), TimeUnit.SECONDS)
            if (!finished) {
                p.destroyForcibly()
                // CONS-055 NEW-05 / CONS-056 NEW-06: waitFor/join
                // return even when they time out — confirm the
                // exit explicitly instead of assuming it.
                val processExited = p.waitFor(2, TimeUnit.SECONDS)
                reader.join(2000)
                if (!processExited) {
                    p.destroyForcibly()
                    // CONS-056 NEW-06: confirm the second kill
                    // too, then give the reader a final bounded
                    // chance to drain.
                    p.waitFor(2, TimeUnit.SECONDS)
                    reader.join(2000)
                }
                if (reader.isAlive) {
                    // The reader may be stuck in a blocking read;
                    // closing the stream unblocks it. The timeout
                    // path never parses the output anyway.
                    try {
                        pingIn.close()
                    } catch (ignored: Exception) {
                    }
                    reader.join(500)
                }
                return null
            }
            reader.join(2000)
            // CONS-055 NEW-05: do not parse a possibly partial
            // output — a reader still alive after the bounded
            // join means the drain did not finish.
            if (reader.isAlive) {
                return null
            }
            if (p.exitValue() != 0) {
                return null
            }
            val packets: Matcher = Pattern.compile(
                    """(\d+) packets transmitted, (\d+) received""")
                    .matcher(out)
            var sent = PING_COUNT
            var received = 0
            if (packets.find()) {
                sent = Integer.parseInt(packets.group(1))
                received = Integer.parseInt(packets.group(2))
            }
            val rtt: Matcher = Pattern.compile(
                    """rtt min/avg/max/mdev = [0-9.]+/([0-9.]+)/""")
                    .matcher(out)
            if (rtt.find() && received > 0) {
                val avg = Math.round(rtt.group(1).toDouble())
                return Result(target, "icmp", avg, sent, received,
                        System.currentTimeMillis(),
                        SystemClock.elapsedRealtime(), null)
            }
            return null
        } catch (e: Exception) {
            return null
        }
    }

    /** TCP connect timing. CONS-052 PING-02: a refused connection is
     * a failure ("refused"), not a success — but the time until the
     * refusal is kept in rttMs, so the display can still show how
     * long the round trip took. */
    private fun runTcpConnect(target: String): Result {
        val t0 = SystemClock.elapsedRealtime()
        try {
            Socket().use { s ->
                s.connect(InetSocketAddress(target, TCP_PORT), TCP_TIMEOUT_MS)
                val rtt = SystemClock.elapsedRealtime() - t0
                return Result(target, "tcp-connect", rtt, 1, 1,
                        System.currentTimeMillis(),
                        SystemClock.elapsedRealtime(), null)
            }
        } catch (e: ConnectException) {
            val rtt = SystemClock.elapsedRealtime() - t0
            return Result(target, "tcp-connect", rtt, 1, 0,
                    System.currentTimeMillis(),
                    SystemClock.elapsedRealtime(), "refused")
        } catch (e: Exception) {
            return Result(target, "tcp-connect", -1, 1, 0,
                    System.currentTimeMillis(),
                    SystemClock.elapsedRealtime(), "unreachable")
        }
    }
}
