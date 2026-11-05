package dev.muse.bridge;

import android.content.Context;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.FileWriter;
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
 * error messages. Never request bodies, node text, or tokens. The
 * log() entry point sanitizes every line (single line, bounded),
 * so a careless caller cannot leak them.
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
    private static final long THROTTLE_MS = 10_000;
    private static final int MAX_EVENT_LEN = 64;
    private static final int MAX_DETAIL_LEN = 200;

    private static final Deque<String> lines = new ArrayDeque<>();
    private static final List<String> previousLines = new ArrayList<>();
    private static File logFile;
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
    }

    /** One log line: "[MM-dd HH:mm:ss] EVENT detail". */
    public static synchronized void log(String event, String detail) {
        String line = format(event, detail);
        lines.addLast(line);
        while (lines.size() > MAX_LINES) {
            lines.removeFirst();
        }
        appendToFile(line);
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
     * time), the current run's lines, then the previous run's tail.
     * Shared by the on-phone viewer (LogActivity) and the .txt
     * export, so both show exactly what GET /logs returns.
     */
    public static String renderText(String appVersion, int appVersionCode) {
        StringBuilder out = new StringBuilder();
        String now = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
                .format(new Date());
        out.append("Muse Bridge log\n");
        out.append("app ").append(appVersion)
                .append(" (").append(appVersionCode).append(")\n");
        out.append("exported ").append(now).append("\n\n");
        List<String> current = snapshot();
        for (String line : current) {
            out.append(line).append("\n");
        }
        List<String> prev = previousSnapshot();
        if (!prev.isEmpty()) {
            out.append("\n--- previous run ---\n");
            for (String line : prev) {
                out.append(line).append("\n");
            }
        }
        return out.toString();
    }

    private static String format(String event, String detail) {
        String ts = new SimpleDateFormat("MM-dd HH:mm:ss", Locale.US)
                .format(new Date());
        return "[" + ts + "] " + sanitize(event, MAX_EVENT_LEN)
                + " " + sanitize(detail, MAX_DETAIL_LEN);
    }

    /** Single line, bounded, no control characters, no token-shaped
     * values: a detail is an event description, never a payload. */
    private static String sanitize(String s, int maxLen) {
        if (s == null) {
            return "";
        }
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < s.length() && out.length() < maxLen; i++) {
            char c = s.charAt(i);
            out.append((c < 0x20 || c == 0x7f) ? ' ' : c);
        }
        return out.toString().trim();
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

    private static void appendToFile(String line) {
        if (logFile == null) {
            return;
        }
        try {
            if (logFile.exists() && logFile.length() > MAX_FILE_BYTES) {
                rotate();
            }
            FileWriter w = new FileWriter(logFile, true);
            w.write(line);
            w.write("\n");
            w.close();
        } catch (Exception ignored) {
        }
    }
}
