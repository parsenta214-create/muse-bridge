// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12
package dev.muse.bridge

import android.content.Context
import android.content.Intent
import android.content.UriPermission
import android.net.Uri
import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import java.io.File
import java.nio.charset.StandardCharsets
import java.nio.file.Files

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
object UriPermissionRegistry {

    const val FEATURE_LOG_FOLDER = "log_folder"

    private const val REGISTRY_NAME = "uri_permission_registry"

    private val REGISTRY_LOCK = Any()

    // NOTE (Kotlin): Java's registryFile was package-private.
    // Kotlin has no package-private; internal would mangle the
    // name to registryFile$main, which the Java caller
    // (LogActivity) cannot call. Public is the closest working
    // equivalent — same package, same single caller.
    @JvmStatic
    fun registryFile(ctx: Context): File {
        return File(ctx.filesDir, REGISTRY_NAME)
    }

    /**
     * Register that a feature uses a URI. Returns false when the
     * registration could not be persisted — the caller must not
     * enable the feature for that URI (the permission is kept).
     * Idempotent: registering the same feature+URI twice keeps
     * one entry.
     */
    @JvmStatic
    fun register(ctx: Context?, feature: String?, uri: String?): Boolean {
        if (ctx == null || feature == null || uri == null) {
            return false
        }
        return synchronized(REGISTRY_LOCK) {
            val entries = readLocked(ctx)
            if (entries == null) {
                return@synchronized false
            }
            for (e in entries) {
                if (e[0] == feature && e[1] == uri) {
                    return@synchronized true
                }
            }
            entries.add(arrayOf(feature, uri))
            writeLocked(ctx, entries)
        }
    }

    /**
     * Remove a feature's registration for a URI. Returns false
     * when the removal could not be persisted — the caller must
     * treat the usage as uncertain and keep the permission.
     * Removing a registration that does not exist succeeds.
     */
    @JvmStatic
    fun unregister(ctx: Context?, feature: String?, uri: String?): Boolean {
        if (ctx == null || feature == null || uri == null) {
            return false
        }
        return synchronized(REGISTRY_LOCK) {
            val entries = readLocked(ctx)
            if (entries == null) {
                return@synchronized false
            }
            var changed = false
            val it = entries.iterator()
            while (it.hasNext()) {
                val e = it.next()
                if (e[0] == feature && e[1] == uri) {
                    it.remove()
                    changed = true
                }
            }
            if (!changed) {
                return@synchronized true
            }
            writeLocked(ctx, entries)
        }
    }

    /**
     * True when another feature uses the URI, or when the
     * registry state cannot be confirmed (fail safe: the caller
     * must not release the permission then).
     */
    @JvmStatic
    fun isUsedByOther(ctx: Context?, feature: String?, uri: String?): Boolean {
        if (ctx == null || feature == null || uri == null) {
            return true
        }
        return synchronized(REGISTRY_LOCK) {
            val entries = readLocked(ctx)
            if (entries == null) {
                return@synchronized true
            }
            for (e in entries) {
                if (e[1] == uri && e[0] != feature) {
                    return@synchronized true
                }
            }
            false
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
    @JvmStatic
    fun releaseIfUnused(ctx: Context?, feature: String?, uri: String?): Boolean {
        if (ctx == null || feature == null || uri == null) {
            return false
        }
        return synchronized(REGISTRY_LOCK) {
            val entries = readLocked(ctx)
            if (entries == null) {
                return@synchronized false
            }
            for (e in entries) {
                if (e[1] == uri && e[0] != feature) {
                    return@synchronized false
                }
            }
            val resolver = ctx.contentResolver
            var held = false
            for (p in resolver.getPersistedUriPermissions()) {
                if (p.uri.toString() == uri
                        && p.isWritePermission) {
                    held = true
                    break
                }
            }
            if (held) {
                try {
                    resolver.releasePersistableUriPermission(Uri.parse(uri),
                            Intent.FLAG_GRANT_READ_URI_PERMISSION
                                    or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                } catch (ignored: Exception) {
                    return@synchronized false
                }
            }
            val it = entries.iterator()
            while (it.hasNext()) {
                val e = it.next()
                if (e[0] == feature && e[1] == uri) {
                    it.remove()
                }
            }
            // The permission is already gone; a failed registry
            // write leaves a stale entry that reconcile() drops
            // at next launch, so this still counts as released.
            writeLocked(ctx, entries)
            true
        }
    }

    /**
     * Next-launch reconciliation: drop entries whose permission
     * no longer exists. A corrupt registry is left alone.
     */
    @JvmStatic
    fun reconcile(ctx: Context?) {
        if (ctx == null) {
            return
        }
        synchronized(REGISTRY_LOCK) {
            val entries = readLocked(ctx)
            if (entries == null) {
                return@synchronized
            }
            val resolver = ctx.contentResolver
            val held = mutableListOf<String>()
            for (p in resolver.getPersistedUriPermissions()) {
                held.add(p.uri.toString())
            }
            var changed = false
            val it = entries.iterator()
            while (it.hasNext()) {
                if (!held.contains(it.next()[1])) {
                    it.remove()
                    changed = true
                }
            }
            if (changed) {
                writeLocked(ctx, entries)
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
    private fun readLocked(ctx: Context): MutableList<Array<String>>? {
        val f = registryFile(ctx)
        try {
            Os.stat(f.absolutePath)
        } catch (e: ErrnoException) {
            if (e.errno == OsConstants.ENOENT) {
                return mutableListOf()
            }
            return null
        } catch (ignored: Exception) {
            return null
        }
        try {
            val lines = Files.readAllLines(f.toPath(),
                    StandardCharsets.UTF_8)
            val entries = mutableListOf<Array<String>>()
            for (line in lines) {
                if (line.isEmpty()) {
                    continue
                }
                // NOTE (Kotlin): String.split rejects a negative
                // limit, so Java's line.split with a -1 limit
                // cannot be transliterated directly. Exactly one
                // tab is equivalent: split with limit -1 yields
                // exactly two parts iff the line holds exactly one
                // tab, with the parts either side of it.
                val tab = line.indexOf('\t')
                if (tab < 0) {
                    return null
                }
                val first = line.substring(0, tab)
                val second = line.substring(tab + 1)
                if (first.isEmpty() || second.isEmpty()
                        || second.contains('\t')) {
                    return null
                }
                entries.add(arrayOf(first, second))
            }
            return entries
        } catch (ignored: Exception) {
            return null
        }
    }

    private fun writeLocked(ctx: Context, entries: List<Array<String>>): Boolean {
        val sb = StringBuilder()
        for (e in entries) {
            sb.append(e[0]).append('\t').append(e[1]).append('\n')
        }
        return BridgeLog.atomicWriteText(ctx.filesDir, REGISTRY_NAME,
                sb.toString())
    }
}
