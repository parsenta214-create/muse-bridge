package dev.muse.bridge;

import android.content.Context;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.FileReader;
import java.io.FileWriter;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Date;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

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
public class BridgeLog {

    private static final int MAX_LINES = 200;
    private static final int MAX_PREVIOUS_LINES = 100;
    private static final long MAX_FILE_BYTES = 64 * 1024;
    private static final String FILE_NAME = "bridge.log";
    private static final String BACKUP_NAME = "bridge.log.1";
    // SPEC-LOG-001 LOG-01 (P1-H): the run start time is persisted
    // as separate metadata, NOT in the log ring — a marker line in
    // the log body falls out of the 200-line tail, so the start
    // time would become "unknown" even for a healthy log. The
    // file holds two lines: the previous run's start time and
    // the current run's start time (or "unknown" when absent).
    private static final String RUN_META_NAME = "run-meta";
    private static final String RUN_META_TMP_NAME = "run-meta.tmp";
    private static final String UNKNOWN_START = "unknown";
    private static final long THROTTLE_MS = 10_000;
    private static final int MAX_EVENT_LEN = 64;
    private static final int MAX_DETAIL_LEN = 200;
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
    private static final java.util.regex.Pattern IPV4_PATTERN =
            java.util.regex.Pattern.compile(
                    "\\b(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})\\b");
    private static final java.util.regex.Pattern LONG_HEX_PATTERN =
            java.util.regex.Pattern.compile("\\b[0-9a-fA-F]{32,}\\b");

    private static final Deque<String> lines = new ArrayDeque<>();
    private static final List<String> previousLines = new ArrayList<>();
    private static File logFile;
    private static File runMetaFile;
    private static String currentStartTime = UNKNOWN_START;
    private static String previousStartTime = UNKNOWN_START;
    private static final Map<String, Long> lastLogged = new HashMap<>();
    private static final Map<String, Integer> suppressed = new HashMap<>();
    private static boolean initialized = false;

    /** Reads the previous run's tail (if any) and opens the file. */
    public static synchronized void init(Context ctx) {
        if (initialized) {
            return;
        }
        initialized = true;
        try {
            logFile = new File(ctx.getFilesDir(), FILE_NAME);
            if (logFile.exists()) {
                previousLines.addAll(readTail(logFile, MAX_PREVIOUS_LINES));
                if (logFile.length() > MAX_FILE_BYTES) {
                    rotate();
                }
            }
        } catch (Exception ignored) {
            // Logging must never break the server; the ring buffer
            // keeps working even when the file is unavailable.
            logFile = null;
        }
        // SPEC-LOG-001 LOG-01 (P1-H): persist the run start times
        // as separate metadata. Startup procedure: (1) read the
        // previous run's currentStartTime from run-meta and fix it
        // as this run's previousStartTime, (2) take the new
        // currentStartTime, (3) save both, (4) only then start
        // logging. A corrupt, missing, or old-format file yields
        // "unknown" and must never stop logging or the server.
        try {
            runMetaFile = new File(ctx.getFilesDir(), RUN_META_NAME);
            String prev = UNKNOWN_START;
            if (runMetaFile.exists()) {
                List<String> meta = readTail(runMetaFile, 2);
                // Line 2 of the old file is the previous run's
                // current start time; line 1 is its previous.
                if (meta.size() >= 2 && isStartTime(meta.get(1))) {
                    prev = meta.get(1);
                } else if (meta.size() >= 1 && isStartTime(meta.get(0))) {
                    prev = meta.get(0);
                }
            }
            previousStartTime = prev;
            currentStartTime = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss",
                    Locale.US).format(new Date());
            saveRunMeta(previousStartTime, currentStartTime);
        } catch (Exception ignored) {
            previousStartTime = UNKNOWN_START;
            currentStartTime = UNKNOWN_START;
        }
    }

    private static boolean isStartTime(String s) {
        return s != null && s.matches("\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}");
    }

    /** Crash-resistant save: write to a temp file in the same
     * directory, sync it, then atomically rename over run-meta.
     * A process killed mid-write leaves either the old file or
     * the new one — never a half-written file. */
    private static void saveRunMeta(String previous, String current) {
        if (runMetaFile == null) {
            return;
        }
        try {
            File tmp = new File(runMetaFile.getParentFile(), RUN_META_TMP_NAME);
            FileOutputStream fos = new FileOutputStream(tmp);
            try {
                String content = previous + "\n" + current + "\n";
                fos.write(content.getBytes(StandardCharsets.UTF_8));
                fos.getFD().sync();
            } finally {
                fos.close();
            }
            tmp.renameTo(runMetaFile);
        } catch (Exception ignored) {
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
     */
    static boolean atomicWriteText(File dir, String fileName, String content) {
        if (dir == null || fileName == null || content == null) {
            return false;
        }
        File tmp = new File(dir, fileName + ".tmp");
        File target = new File(dir, fileName);
        FileOutputStream fos = null;
        try {
            fos = new FileOutputStream(tmp);
            fos.write(content.getBytes(StandardCharsets.UTF_8));
            fos.getFD().sync();
            fos.close();
            fos = null;
            Os.rename(tmp.getAbsolutePath(), target.getAbsolutePath());
            return fsyncDirectory(dir);
        } catch (Exception ignored) {
            return false;
        } finally {
            if (fos != null) {
                try {
                    fos.close();
                } catch (Exception ignored) {
                }
            }
        }
    }

    /**
     * SPEC-DRIVE-001: sync a directory's metadata so a rename or
     * delete in it survives a crash. Best effort: returns false
     * when the directory cannot be opened or synced, and the
     * caller treats the persistence as unconfirmed.
     */
    static boolean fsyncDirectory(File dir) {
        if (dir == null) {
            return false;
        }
        FileDescriptor fd = null;
        try {
            fd = Os.open(dir.getAbsolutePath(), OsConstants.O_RDONLY, 0);
            Os.fsync(fd);
            return true;
        } catch (Exception ignored) {
            return false;
        } finally {
            if (fd != null) {
                try {
                    Os.close(fd);
                } catch (ErrnoException ignored) {
                }
            }
        }
    }

    /** SPEC-DRIVE-001: the run-meta file, for the first-init
     * "no other app-owned state" check. Package-private. */
    static File runMetaFile(Context ctx) {
        return new File(ctx.getFilesDir(), RUN_META_NAME);
    }

    /** One log line: "[MM-dd HH:mm:ss] EVENT detail". Returns
     * true when the line reached the log file (SPEC-LOG-001
     * P1-C: the server's stop() needs to know whether the STOPPED
     * record was written). The ring buffer always keeps the line
     * even when the file write fails. */
    public static synchronized boolean log(String event, String detail) {
        String line = format(event, detail);
        lines.addLast(line);
        while (lines.size() > MAX_LINES) {
            lines.removeFirst();
        }
        return appendToFile(line);
    }

    /**
     * Like log(), but a repeated key fires at most once per
     * THROTTLE_MS; skipped repeats are counted and reported as
     * "(+N suppressed)" when the key fires again. For floods and
     * bursts that would otherwise fill the ring with one line.
     */
    public static synchronized void logThrottled(String key, String event,
            String detail) {
        long now = android.os.SystemClock.elapsedRealtime();
        Long last = lastLogged.get(key);
        if (last != null && now - last < THROTTLE_MS) {
            Integer n = suppressed.get(key);
            suppressed.put(key, (n == null ? 1 : n + 1));
            return;
        }
        lastLogged.put(key, now);
        Integer n = suppressed.remove(key);
        String d = detail == null ? "" : detail;
        if (n != null && n > 0) {
            d = d + " (+" + n + " suppressed)";
        }
        log(event, d);
    }

    /**
     * SPEC-LOG-001 LOG-04 (P1-D): final flush at stop time. If
     * repeats of this key were suppressed, emit one line with the
     * count and the "final flush" marker, then clear the count.
     * No-op when nothing was suppressed.
     */
    public static synchronized void flushThrottled(String key, String event) {
        Integer n = suppressed.remove(key);
        if (n != null && n > 0) {
            log(event, "(+" + n + " suppressed, final flush)");
        }
    }

    /** Copy of the current run's lines, oldest first. */
    public static synchronized List<String> snapshot() {
        return new ArrayList<>(lines);
    }

    /** Copy of the previous run's tail, oldest first. */
    public static synchronized List<String> previousSnapshot() {
        return new ArrayList<>(previousLines);
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
     */
    public static String renderText(String appVersion, int appVersionCode) {
        StringBuilder out = new StringBuilder();
        String now = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
                .format(new Date());
        out.append("Muse Bridge log\n");
        out.append("app ").append(appVersion)
                .append(" (").append(appVersionCode).append(")\n");
        out.append("exported ").append(now).append("\n\n");
        List<String> prev = previousSnapshot();
        if (!prev.isEmpty()) {
            out.append("--- previous run (started ")
                    .append(previousStartTime).append(") ---\n");
            for (String line : prev) {
                out.append(line).append("\n");
            }
            out.append("\n");
        }
        out.append("--- current run (started ")
                .append(currentStartTime).append(") ---\n");
        List<String> current = snapshot();
        for (String line : current) {
            out.append(line).append("\n");
        }
        return out.toString();
    }

    private static String format(String event, String detail) {
        String ts = new SimpleDateFormat("MM-dd HH:mm:ss", Locale.US)
                .format(new Date());
        return "[" + ts + "] " + sanitize(event, MAX_EVENT_LEN)
                + " " + sanitize(detail, MAX_DETAIL_LEN);
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
    private static String sanitize(String s, int maxLen) {
        if (s == null) {
            return "";
        }
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < s.length() && out.length() < maxLen; i++) {
            char c = s.charAt(i);
            out.append((c < 0x20 || c == 0x7f) ? ' ' : c);
        }
        String cleaned = out.toString().trim();
        cleaned = IPV4_PATTERN.matcher(cleaned).replaceAll("$1.$2.*.*");
        cleaned = LONG_HEX_PATTERN.matcher(cleaned).replaceAll("[token]");
        return cleaned;
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
    public static String maskIp(String ip) {
        if (ip == null) {
            return "";
        }
        if (ip.startsWith("::ffff:")) {
            return maskIpv4(ip.substring(7));
        }
        if (ip.contains(":")) {
            String[] groups = ip.split(":", -1);
            List<String> first = new ArrayList<>();
            for (String g : groups) {
                if (!g.isEmpty()) {
                    first.add(g);
                }
                if (first.size() == 2) {
                    break;
                }
            }
            if (first.size() == 2) {
                return first.get(0) + ":" + first.get(1) + ":*.*";
            }
            return "*.*";
        }
        return maskIpv4(ip);
    }

    private static String maskIpv4(String ip) {
        String[] parts = ip.split("\\.");
        if (parts.length == 4) {
            return parts[0] + "." + parts[1] + ".*.*";
        }
        return ip;
    }

    private static List<String> readTail(File f, int maxLines) {
        ArrayDeque<String> tail = new ArrayDeque<>();
        try {
            BufferedReader r = new BufferedReader(new FileReader(f));
            String line;
            while ((line = r.readLine()) != null) {
                tail.addLast(line);
                while (tail.size() > maxLines) {
                    tail.removeFirst();
                }
            }
            r.close();
        } catch (Exception ignored) {
        }
        return new ArrayList<>(tail);
    }

    private static void rotate() {
        try {
            File backup = new File(logFile.getParentFile(), BACKUP_NAME);
            if (backup.exists()) {
                backup.delete();
            }
            logFile.renameTo(backup);
        } catch (Exception ignored) {
        }
    }

    private static boolean appendToFile(String line) {
        if (logFile == null) {
            return false;
        }
        try {
            if (logFile.exists() && logFile.length() > MAX_FILE_BYTES) {
                rotate();
            }
            FileWriter w = new FileWriter(logFile, true);
            w.write(line);
            w.write("\n");
            w.close();
            return true;
        } catch (Exception ignored) {
            return false;
        }
    }
}
