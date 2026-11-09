// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-09
package dev.muse.bridge

import android.content.Context
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.os.Build

/**
 * Installed app version, read once from PackageManager so /state,
 * /health, and the MainActivity label always report the version that
 * is actually installed. The single source of truth is the
 * versionCode/versionName set at build time (build.sh passes them to
 * aapt2 link); nothing is hardcoded here. Falls back to "unknown" /
 * -1 when the package info cannot be read.
 *
 * Converted from AppVersion.java in the SPEC-KOTLIN-MIGRATION-001
 * pilot (v2.2). Java callers are unchanged: name() and code() are
 * @JvmStatic, so HttpBridgeServer, LogActivity and MainActivity
 * keep calling AppVersion.name(ctx) / AppVersion.code(ctx).
 */
object AppVersion {
    @Volatile
    private var cachedName: String? = null

    @Volatile
    private var cachedCode: Int = -2 // -2 = not yet read

    @JvmStatic
    fun name(ctx: Context): String {
        load(ctx)
        return cachedName ?: "unknown"
    }

    @JvmStatic
    fun code(ctx: Context): Int {
        load(ctx)
        return cachedCode
    }

    @JvmStatic
    @Synchronized
    private fun load(ctx: Context) {
        if (cachedCode != -2) {
            return
        }
        cachedName = "unknown"
        cachedCode = -1
        try {
            val pm = ctx.packageManager
            val info: PackageInfo? = if (Build.VERSION.SDK_INT >= 33) {
                pm.getPackageInfo(ctx.packageName, PackageManager.PackageInfoFlags.of(0))
            } else {
                pm.getPackageInfo(ctx.packageName, 0)
            }
            if (info != null) {
                cachedName = info.versionName ?: "unknown"
                cachedCode = info.longVersionCode.toInt()
            }
        } catch (_: Exception) {
            // Fallbacks above stay; the version just reads "unknown".
        }
    }
}
