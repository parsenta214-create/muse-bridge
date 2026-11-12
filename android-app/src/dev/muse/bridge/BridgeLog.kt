// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-13
package dev.muse.bridge

import android.content.Context
import android.os.SystemClock
import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import java.io.BufferedReader
import java.io.File
import java.io.FileDescriptor
import java.io.FileOutputStream
import java.io.FileReader
import java.io.FileWriter
import java.nio.charset.StandardCharsets
import java.text.SimpleDateFormat
import java.util.ArrayDeque
import java.util.ArrayList
import java.util.Date
import java.util.HashMap
import java.util.Locale
import java.util.regex.Pattern

/**
 * Debug log for the bridge (v1.9.3). The VM pulls it with GET /logs;
 * the phone never pushes anything anywhere, and /logs is behind the
 * same token auth as every other endpoint.
 *
 * Content rule (allowlist): only event names, counts, and short
 * error messages. Never request bodies, node text, or tokens.
 * P1-03 (CONS-014): IP addresses are masked to the first two
 * octets by sanitize(); exception messages, package names,
 * URLs, and file names are dropped at the call sites instead
 * of being logged — the log must never carry them. The
 * log() entry point sanitizes every line (single line, bounded,
 * masked), so a careless caller cannot leak them.
 *
 * Only key events are recorded (server start/stop, bind results,
 * accept failures, connection-cap sheds, overload 503s, HTTP error
 * statuses by code, batch outcomes) — never every request.
 *
 * Lines live in a bounded in-memory ring (200) and are appended to
 * a rotating file (64 KB) in the app's private files dir, so the
 * last lines survive a restart: /logs returns the current run's
 * lines plus the previous run's tail.
 */
object BridgeLog {

    private const val MAX_LINES = 200
    private const val MAX_PREVIOUS_LINES = 100
    private const val MAX_FILE_BYTES = 64 * 1024L
    private const val FILE_NAME = "bridge.log"
    private const val BACKUP_NAME = "bridge.log.1"
    // SPEC-LOG-001 LOG-01 (P1-H): the run start time is persisted
    // as separate metadata, NOT in the log ring — a marker line in
    // the log body falls out of the 200-line tail, so the start
    // time would become "unknown" even for a healthy log. The
    // file holds two lines: the previous run's start time and
    // the current run's start time (or "unknown" when absent).
    private const val RUN_META_NAME = "run-meta"
    private const val RUN_META_TMP_NAME = "run-meta.tmp"
    private const val UNKNOWN_START = "unknown"
    private const val THROTTLE_MS = 10_000L
    private const val MAX_EVENT_LEN = 64
    private const val MAX_DETAIL_LEN = 200
    // P1-03 (CONS-014): central PII masking for every log line.
    // An IPv4 address is masked to its first two octets
    // (100.111.74.42 -> 100.111.*.*); a run of 32+ hex chars
    // (a token-shaped value) is replaced outright. Callers must
    // still not pass user data, but a careless caller can no
    // longer leak a full IP or a token through this choke point.
    // SPEC-LOG-001 §7 (P1-F): the app has exactly one token
    // format — the bridge token (32+ hex, generated in
    // MainActivity.getOrCreateToken) — and LONG_HEX_PATTERN
    // covers it. No other token format exists in the app.
    private val IPV4_PATTERN: Pattern = Pattern.compile(
        "\\b(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})\\b"
    )
    private val LONG_HEX_PATTERN: Pattern =
        Pattern.compile("\\b[0-9a-fA-F]{32,}\\b")

    // One explicit lock for every critical section (batch E
    // lesson): @Synchronized on a @JvmStatic function locks the
    // Class object, while synchronized() on another monitor
    // would split the exclusion. All six former
    // static-synchronized methods take LOG_LOCK instead.
    // No external code locks on this object (verified by grep).
    private val LOG_LOCK = Any()

    private val lines = ArrayDeque<String>()
    private val previousLines = ArrayList<String>()
    private var logFile: File? = null
    // Renamed from runMetaFile: a property and a function cannot
    // share a name in Kotlin, and runMetaFile(Context) below is
    // the kept API name. Private field, invisible externally.
    private var runMeta: File? = null
    private var currentStartTime: String = UNKNOWN_START
    private var previousStartTime: String = UNKNOWN_START
    private val lastLogged = HashMap<String?, Long>()
    private val suppressed = HashMap<String?, Int>()
    private var initialized = false

    /** Reads the previous run's tail (if any) and opens the file.
     *
     * CONS-151 P2-01 (D4 nullability): the Java original
     * tolerated a null Context — the NullPointerException from
     * ctx.getFilesDir() was caught by the try blocks below, so
     * init(null) left logFile null and the start times
     * "unknown" while the ring kept working. The Kotlin
     * parameter is therefore nullable too, and ctx!! throws
     * KotlinNullPointerException (a NullPointerException),
     * caught by the same try blocks, for the same outcome. */
    @JvmStatic
    fun init(ctx: Context?) {
        synchronized(LOG_LOCK) {
            if (initialized) {
                return
            }
            initialized = true
            try {
                val file = File(ctx!!.filesDir, FILE_NAME)
                logFile = file
                if (file.exists()) {
                    previousLines.addAll(readTail(file, MAX_PREVIOUS_LINES))
                    if (file.length() > MAX_FILE_BYTES) {
                        rotate()
                    }
                }
            } catch (_: Exception) {
                // Logging must never break the server; the ring buffer
                // keeps working even when the file is unavailable.
                logFile = null
            }
            // SPEC-LOG-001 LOG-01 (P1-H): persist the run start times
            // as separate metadata. Startup procedure: (1) read the
            // previous run's currentStartTime from run-meta and fix it
            // as this run's previousStartTime, (2) take the new
            // currentStartTime, (3) save both, (4) only then start
            // logging. A corrupt, missing, or old-format file yields
            // "unknown" and must never stop logging or the server.
            try {
                val metaFile = File(ctx!!.filesDir, RUN_META_NAME)
                runMeta = metaFile
                var prev = UNKNOWN_START
                if (metaFile.exists()) {
                    val meta = readTail(metaFile, 2)
                    // Line 2 of the old file is the previous run's
                    // current start time; line 1 is its previous.
                    if (meta.size >= 2 && isStartTime(meta[1])) {
                        prev = meta[1]
                    } else if (meta.size >= 1 && isStartTime(meta[0])) {
                        prev = meta[0]
                    }
                }
                previousStartTime = prev
                currentStartTime = SimpleDateFormat(
                    "yyyy-MM-dd HH:mm:ss", Locale.US
                ).format(Date())
                saveRunMeta(previousStartTime, currentStartTime)
            } catch (_: Exception) {
                previousStartTime = UNKNOWN_START
                currentStartTime = UNKNOWN_START
            }
        }
    }

    private fun isStartTime(s: String?): Boolean {
        return s != null && s.matches(
            "\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}".toRegex()
        )
    }

    /** Crash-resistant save: write to a temp file in the same
     * directory, sync it, then atomically rename over run-meta.
     * A process killed mid-write leaves either the old file or
     * the new one — never a half-written file. */
    private fun saveRunMeta(previous: String, current: String) {
        val meta = runMeta ?: return
        try {
            val tmp = File(meta.parentFile, RUN_META_TMP_NAME)
            FileOutputStream(tmp).use { fos ->
                val content = previous + "\n" + current + "\n"
                fos.write(content.toByteArray(StandardCharsets.UTF_8))
                fos.fd.sync()
            }
            tmp.renameTo(meta)
        } catch (_: Exception) {
            // Metadata loss is acceptable; the log still works.
        }
    }

    /**
     * SPEC-DRIVE-001: crash-resistant atomic write shared by the
     * log-folder feature (folder URI, request-code counter, init
     * marker, URI registry). Write to a temp file in the same
     * directory, sync it, rename over the target with Os.rename,
     * then sync the parent directory so the rename itself
     * survives a crash. Returns true only when every step
     * (including the directory sync) succeeded. A false result
     * means the caller must assume the old file is still the
     * stored one — or that the state is uncertain and must be
     * re-read before deciding.
     *
     * Public (was Java package-private): LogActivity.java and
     * UriPermissionRegistry.kt call it, and Kotlin internal
     * would mangle the name (atomicWriteText$main), so javac
     * could not link the Java callers. Batch D precedent
     * (registryFile made public @JvmStatic).
     */
    @JvmStatic
    fun atomicWriteText(dir: File?, fileName: String?, content: String?): Boolean {
        if (dir == null || fileName == null || content == null) {
            return false
        }
        val tmp = File(dir, fileName + ".tmp")
        val target = File(dir, fileName)
        var fos: FileOutputStream? = null
        try {
            fos = FileOutputStream(tmp)
            fos.write(content.toByteArray(StandardCharsets.UTF_8))
            fos.fd.sync()
            fos.close()
            fos = null
            Os.rename(tmp.absolutePath, target.absolutePath)
            return fsyncDirectory(dir)
        } catch (_: Exception) {
            return false
        } finally {
            if (fos != null) {
                try {
                    fos.close()
                } catch (_: Exception) {
                }
            }
        }
    }

    /**
     * SPEC-DRIVE-001: sync a directory's metadata so a rename or
     * delete in it survives a crash. Best effort: returns false
     * when the directory cannot be opened or synced, and the
     * caller treats the persistence as unconfirmed.
     *
     * Public for the same reason as atomicWriteText above
     * (LogActivity.java calls it).
     */
    @JvmStatic
    fun fsyncDirectory(dir: File?): Boolean {
        if (dir == null) {
            return false
        }
        var fd: FileDescriptor? = null
        try {
            fd = Os.open(dir.absolutePath, OsConstants.O_RDONLY, 0)
            Os.fsync(fd)
            return true
        } catch (_: Exception) {
            return false
        } finally {
            if (fd != null) {
                try {
                    Os.close(fd)
                } catch (_: ErrnoException) {
                }
            }
        }
    }

    /** SPEC-DRIVE-001: the run-meta file, for the first-init
     * "no other app-owned state" check. No callers in the current
     * code (verified by grep); internal, since Java cannot call
     * a Kotlin internal function by its plain name. The
     * parameter is nullable like the Java original (a null
     * Context threw NullPointerException from getFilesDir()). */
    internal fun runMetaFile(ctx: Context?): File {
        return File(ctx!!.filesDir, RUN_META_NAME)
    }

    /** One log line: "[MM-dd HH:mm:ss] EVENT detail". Returns
     * true when the line reached the log file (SPEC-LOG-001
     * P1-C: the server's stop() needs to know whether the STOPPED
     * record was written). The ring buffer always keeps the line
     * even when the file write fails.
     *
     * CONS-151 P2-01 (D4 nullability): the Java original passed
     * null event/detail through to sanitize(), which converted
     * them to empty strings. The parameters are nullable here
     * for the same behavior (sanitize already takes String?). */
    @JvmStatic
    fun log(event: String?, detail: String?): Boolean {
        synchronized(LOG_LOCK) {
            val line = format(event, detail)
            lines.addLast(line)
            while (lines.size > MAX_LINES) {
                lines.removeFirst()
            }
            return appendToFile(line)
        }
    }

    /**
     * Like log(), but a repeated key fires at most once per
     * THROTTLE_MS; skipped repeats are counted and reported as
     * "(+N suppressed)" when the key fires again. For floods and
     * bursts that would otherwise fill the ring with one line.
     *
     * CONS-151 P2-01 (D4 nullability): the Java original
     * normalized a null detail to "" and accepted a null key
     * (Java HashMap allows null keys). The parameters and the
     * maps are nullable here for the same behavior. */
    @JvmStatic
    fun logThrottled(key: String?, event: String?, detail: String?) {
        synchronized(LOG_LOCK) {
            val now = SystemClock.elapsedRealtime()
            val last = lastLogged[key]
            if (last != null && now - last < THROTTLE_MS) {
                val n = suppressed[key]
                suppressed[key] = (n ?: 0) + 1
                return
            }
            lastLogged[key] = now
            val n = suppressed.remove(key)
            var d = detail ?: ""
            if (n != null && n > 0) {
                d = d + " (+" + n + " suppressed)"
            }
            log(event, d)
        }
    }

    /**
     * SPEC-LOG-001 LOG-04 (P1-D): final flush at stop time. If
     * repeats of this key were suppressed, emit one line with the
     * count and the "final flush" marker, then clear the count.
     * No-op when nothing was suppressed.
     *
     * CONS-151 P2-01 (D4 nullability): nullable like
     * logThrottled — a null key removes the null-key entry,
     * a null event is sanitized to "" by log(). */
    @JvmStatic
    fun flushThrottled(key: String?, event: String?) {
        synchronized(LOG_LOCK) {
            val n = suppressed.remove(key)
            if (n != null && n > 0) {
                log(event, "(+" + n + " suppressed, final flush)")
            }
        }
    }

    /** Copy of the current run's lines, oldest first. */
    @JvmStatic
    fun snapshot(): List<String> {
        synchronized(LOG_LOCK) {
            return ArrayList(lines)
        }
    }

    /** Copy of the previous run's tail, oldest first. */
    @JvmStatic
    fun previousSnapshot(): List<String> {
        synchronized(LOG_LOCK) {
            return ArrayList(previousLines)
        }
    }

    /**
     * The full log as one text: a header (app version + export
     * time), the previous run's tail first, then the current
     * run's lines — the whole export is chronological
     * (oldest first). SPEC-LOG-001 LOG-01: the old order put the
     * current run first, so the file jumped backwards in time.
     * Each section carries its run start time (from the run-meta
     * file, not from the log body). Shared by the on-phone viewer
     * (LogActivity) and the .txt export, so both show exactly
     * what GET /logs returns.
     *
     * Not synchronized, as in Java: the snapshots are taken
     * under LOG_LOCK, and the start-time reads are plain reads
     * (same visibility caveat as the Java original, where the
     * fields are not volatile either).
     *
     * CONS-151 P2-01 (D4 nullability): the version parameter is
     * nullable — Java StringBuilder.append(null) appends
     * "null", and Kotlin's append(String?) does the same.
     * CONS-151 P2-02 (inherited, not fixed here): the
     * start-time reads are outside the lock, as in Java — a
     * future cleanup can take both times and both snapshots in
     * one synchronized(LOG_LOCK) section. */
    @JvmStatic
    fun renderText(appVersion: String?, appVersionCode: Int): String {
        val out = StringBuilder()
        val now = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
            .format(Date())
        out.append("Muse Bridge log\n")
        out.append("app ").append(appVersion)
            .append(" (").append(appVersionCode).append(")\n")
        out.append("exported ").append(now).append("\n\n")
        val prev = previousSnapshot()
        if (prev.isNotEmpty()) {
            out.append("--- previous run (started ")
                .append(previousStartTime).append(") ---\n")
            for (line in prev) {
                out.append(line).append("\n")
            }
            out.append("\n")
        }
        out.append("--- current run (started ")
            .append(currentStartTime).append(") ---\n")
        val current = snapshot()
        for (line in current) {
            out.append(line).append("\n")
        }
        return out.toString()
    }

    private fun format(event: String?, detail: String?): String {
        val ts = SimpleDateFormat("MM-dd HH:mm:ss", Locale.US)
            .format(Date())
        return "[" + ts + "] " + sanitize(event, MAX_EVENT_LEN) +
            " " + sanitize(detail, MAX_DETAIL_LEN)
    }

    /** Single line, bounded, no control characters, no full IP
     * addresses, no token-shaped values: a detail is an event
     * description, never a payload. P1-03 (CONS-014): sanitize
     * used to only replace control characters, so a full IP
     * (listen/bind-failed) or an exception message carrying user
     * data could reach the log file. The masking here is the
     * choke point; call sites additionally drop the PII they
     * used to pass (exception messages, package names, URLs,
     * file names). */
    private fun sanitize(s: String?, maxLen: Int): String {
        if (s == null) {
            return ""
        }
        val out = StringBuilder()
        for (i in 0 until s.length) {
            if (out.length >= maxLen) {
                break
            }
            val c = s[i]
            out.append(if (c < ' ' || c == '\u007f') ' ' else c)
        }
        var cleaned = out.toString().trim()
        cleaned = IPV4_PATTERN.matcher(cleaned).replaceAll("$1.$2.*.*")
        cleaned = LONG_HEX_PATTERN.matcher(cleaned).replaceAll("[token]")
        return cleaned
    }

    /**
     * SPEC-LOG-001 §7: mask one IP address for a log line. IPv4 is
     * masked to its first two octets; an IPv4-mapped IPv6
     * (::ffff:100.111.74.42) is normalized to IPv4 first; a full
     * IPv6 address is masked to its first two groups. This is used
     * at the call sites that log an address (listen, client-first,
     * interface list); sanitize() above remains the backstop for
     * IPv4 and token-shaped values in free text. A generic IPv6
     * regex is deliberately NOT added to sanitize(): it would also
     * match the HH:mm:ss timestamps and corrupt them.
     */
    @JvmStatic
    fun maskIp(ip: String?): String {
        if (ip == null) {
            return ""
        }
        if (ip.startsWith("::ffff:")) {
            return maskIpv4(ip.substring(7))
        }
        if (ip.contains(":")) {
            // Kotlin split(':') keeps trailing empties, exactly
            // like Java split(":", -1) (verified 2026-11-13).
            val groups = ip.split(':')
            val first = ArrayList<String>()
            for (g in groups) {
                if (g.isNotEmpty()) {
                    first.add(g)
                }
                if (first.size == 2) {
                    break
                }
            }
            if (first.size == 2) {
                return first[0] + ":" + first[1] + ":*.*"
            }
            return "*.*"
        }
        return maskIpv4(ip)
    }

    private fun maskIpv4(ip: String): String {
        // Java split("\\.") drops trailing empties; Kotlin
        // Regex.split does NOT (verified 2026-11-13 with
        // kotlinc -script: "1.2.3.".split("\\.".toRegex()) keeps
        // the empty tail). split('.') + dropLastWhile replicates
        // the Java semantics exactly.
        val parts = ip.split('.').dropLastWhile { it.isEmpty() }
        if (parts.size == 4) {
            return parts[0] + "." + parts[1] + ".*.*"
        }
        return ip
    }

    private fun readTail(f: File, maxLines: Int): List<String> {
        val tail = ArrayDeque<String>()
        try {
            BufferedReader(FileReader(f)).use { r ->
                var line: String?
                while (r.readLine().also { line = it } != null) {
                    tail.addLast(line!!)
                    while (tail.size > maxLines) {
                        tail.removeFirst()
                    }
                }
            }
        } catch (_: Exception) {
        }
        return ArrayList(tail)
    }

    private fun rotate() {
        try {
            val file = logFile ?: return
            val backup = File(file.parentFile, BACKUP_NAME)
            if (backup.exists()) {
                backup.delete()
            }
            file.renameTo(backup)
        } catch (_: Exception) {
        }
    }

    private fun appendToFile(line: String): Boolean {
        val file = logFile ?: return false
        try {
            if (file.exists() && file.length() > MAX_FILE_BYTES) {
                rotate()
            }
            FileWriter(file, true).use { w ->
                w.write(line)
                w.write("\n")
            }
            return true
        } catch (_: Exception) {
            return false
        }
    }
}
