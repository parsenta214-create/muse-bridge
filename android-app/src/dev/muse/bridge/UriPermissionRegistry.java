package dev.muse.bridge;

import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.content.UriPermission;
import android.net.Uri;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

/**
 * SPEC-DRIVE-001: registry of which in-app feature uses which
 * persistable URI permission. One line per entry:
 * "feature<TAB>uri" (UTF-8). The file lives in the app's
 * private files dir and is written atomically
 * (BridgeLog.atomicWriteText).
 *
 * The registry has no "uninitialized" state: a missing file is
 * an empty registry (zero entries), and the first registration
 * creates the file. A corrupt file (a line that does not match
 * the format, or unreadable UTF-8) is an error: every method
 * that needs the registry refuses, and existing permissions are
 * kept. Recovery is a user data-clear / reinstall; the corrupt
 * file is never auto-deleted.
 *
 * NEW-P2-03: the common lock REGISTRY_LOCK is private to this
 * class. All register / unregister / read / release-decision
 * operations run through the synchronized methods below; other
 * classes never touch the lock directly, so a future feature
 * cannot bypass the exclusion by accident.
 */
public class UriPermissionRegistry {

    public static final String FEATURE_LOG_FOLDER = "log_folder";

    private static final String REGISTRY_NAME = "uri_permission_registry";

    private static final Object REGISTRY_LOCK = new Object();

    private UriPermissionRegistry() {
    }

    static File registryFile(Context ctx) {
        return new File(ctx.getFilesDir(), REGISTRY_NAME);
    }

    /**
     * Register that a feature uses a URI. Returns false when the
     * registration could not be persisted — the caller must not
     * enable the feature for that URI (the permission is kept).
     * Idempotent: registering the same feature+URI twice keeps
     * one entry.
     */
    public static boolean register(Context ctx, String feature, String uri) {
        if (ctx == null || feature == null || uri == null) {
            return false;
        }
        synchronized (REGISTRY_LOCK) {
            List<String[]> entries = readLocked(ctx);
            if (entries == null) {
                return false;
            }
            for (String[] e : entries) {
                if (e[0].equals(feature) && e[1].equals(uri)) {
                    return true;
                }
            }
            entries.add(new String[]{feature, uri});
            return writeLocked(ctx, entries);
        }
    }

    /**
     * Remove a feature's registration for a URI. Returns false
     * when the removal could not be persisted — the caller must
     * treat the usage as uncertain and keep the permission.
     * Removing a registration that does not exist succeeds.
     */
    public static boolean unregister(Context ctx, String feature, String uri) {
        if (ctx == null || feature == null || uri == null) {
            return false;
        }
        synchronized (REGISTRY_LOCK) {
            List<String[]> entries = readLocked(ctx);
            if (entries == null) {
                return false;
            }
            boolean changed = false;
            Iterator<String[]> it = entries.iterator();
            while (it.hasNext()) {
                String[] e = it.next();
                if (e[0].equals(feature) && e[1].equals(uri)) {
                    it.remove();
                    changed = true;
                }
            }
            if (!changed) {
                return true;
            }
            return writeLocked(ctx, entries);
        }
    }

    /**
     * True when another feature uses the URI, or when the
     * registry state cannot be confirmed (fail safe: the caller
     * must not release the permission then).
     */
    public static boolean isUsedByOther(Context ctx, String feature, String uri) {
        if (ctx == null || feature == null || uri == null) {
            return true;
        }
        synchronized (REGISTRY_LOCK) {
            List<String[]> entries = readLocked(ctx);
            if (entries == null) {
                return true;
            }
            for (String[] e : entries) {
                if (e[1].equals(uri) && !e[0].equals(feature)) {
                    return true;
                }
            }
            return false;
        }
    }

    /**
     * Check, release, and unregister as one atomic section under
     * REGISTRY_LOCK, so no other feature's registration can slip
     * in between the check and the release. Returns true when
     * the permission is gone (released now, or already absent).
     * Returns false when the permission must be kept (another
     * feature uses it, the registry is unreadable, or the
     * release itself failed). When the permission was released
     * but the registry update afterwards failed, the stale entry
     * is reconciled at next launch (reconcile()).
     */
    public static boolean releaseIfUnused(Context ctx, String feature, String uri) {
        if (ctx == null || feature == null || uri == null) {
            return false;
        }
        synchronized (REGISTRY_LOCK) {
            List<String[]> entries = readLocked(ctx);
            if (entries == null) {
                return false;
            }
            for (String[] e : entries) {
                if (e[1].equals(uri) && !e[0].equals(feature)) {
                    return false;
                }
            }
            ContentResolver resolver = ctx.getContentResolver();
            boolean held = false;
            for (UriPermission p : resolver.getPersistedUriPermissions()) {
                if (p.getUri().toString().equals(uri)
                        && p.isWritePermission()) {
                    held = true;
                    break;
                }
            }
            if (held) {
                try {
                    resolver.releasePersistableUriPermission(Uri.parse(uri),
                            Intent.FLAG_GRANT_READ_URI_PERMISSION
                                    | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
                } catch (Exception ignored) {
                    return false;
                }
            }
            Iterator<String[]> it = entries.iterator();
            while (it.hasNext()) {
                String[] e = it.next();
                if (e[0].equals(feature) && e[1].equals(uri)) {
                    it.remove();
                }
            }
            // The permission is already gone; a failed registry
            // write leaves a stale entry that reconcile() drops
            // at next launch, so this still counts as released.
            writeLocked(ctx, entries);
            return true;
        }
    }

    /**
     * Next-launch reconciliation: drop entries whose permission
     * no longer exists. A corrupt registry is left alone.
     */
    public static void reconcile(Context ctx) {
        if (ctx == null) {
            return;
        }
        synchronized (REGISTRY_LOCK) {
            List<String[]> entries = readLocked(ctx);
            if (entries == null) {
                return;
            }
            ContentResolver resolver = ctx.getContentResolver();
            List<String> held = new ArrayList<>();
            for (UriPermission p : resolver.getPersistedUriPermissions()) {
                held.add(p.getUri().toString());
            }
            boolean changed = false;
            Iterator<String[]> it = entries.iterator();
            while (it.hasNext()) {
                if (!held.contains(it.next()[1])) {
                    it.remove();
                    changed = true;
                }
            }
            if (changed) {
                writeLocked(ctx, entries);
            }
        }
    }

    /**
     * Read the registry. A missing file (ENOENT) is an empty
     * registry. Returns null only when the file is corrupt or
     * unreadable (a line without exactly one tab, an empty
     * field, or a read/UTF-8 failure). Existence is checked
     * with Os.stat, never File.exists() alone — an EACCES/EIO
     * must not be mistaken for absence. Callers must hold
     * REGISTRY_LOCK.
     */
    private static List<String[]> readLocked(Context ctx) {
        File f = registryFile(ctx);
        try {
            Os.stat(f.getAbsolutePath());
        } catch (ErrnoException e) {
            if (e.errno == OsConstants.ENOENT) {
                return new ArrayList<>();
            }
            return null;
        } catch (Exception ignored) {
            return null;
        }
        try {
            List<String> lines = Files.readAllLines(f.toPath(),
                    StandardCharsets.UTF_8);
            List<String[]> entries = new ArrayList<>();
            for (String line : lines) {
                if (line.isEmpty()) {
                    continue;
                }
                String[] parts = line.split("\t", -1);
                if (parts.length != 2 || parts[0].isEmpty()
                        || parts[1].isEmpty()) {
                    return null;
                }
                entries.add(parts);
            }
            return entries;
        } catch (Exception ignored) {
            return null;
        }
    }

    private static boolean writeLocked(Context ctx, List<String[]> entries) {
        StringBuilder sb = new StringBuilder();
        for (String[] e : entries) {
            sb.append(e[0]).append('\t').append(e[1]).append('\n');
        }
        return BridgeLog.atomicWriteText(ctx.getFilesDir(), REGISTRY_NAME,
                sb.toString());
    }
}
