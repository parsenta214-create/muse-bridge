package dev.muse.bridge;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.os.Build;

/**
 * Installed app version, read once from PackageManager so /state,
 * /health, and the MainActivity label always report the version that
 * is actually installed. The single source of truth is the
 * versionCode/versionName set at build time (build.sh passes them to
 * aapt2 link); nothing is hardcoded here. Falls back to "unknown" /
 * -1 when the package info cannot be read.
 */
public final class AppVersion {
    private static volatile String cachedName;
    private static volatile int cachedCode = -2; // -2 = not yet read

    private AppVersion() {
    }

    public static String name(Context ctx) {
        load(ctx);
        return cachedName != null ? cachedName : "unknown";
    }

    public static int code(Context ctx) {
        load(ctx);
        return cachedCode;
    }

    private static synchronized void load(Context ctx) {
        if (cachedCode != -2) {
            return;
        }
        cachedName = "unknown";
        cachedCode = -1;
        try {
            PackageManager pm = ctx.getPackageManager();
            PackageInfo info;
            if (Build.VERSION.SDK_INT >= 33) {
                info = pm.getPackageInfo(ctx.getPackageName(),
                        PackageManager.PackageInfoFlags.of(0));
            } else {
                info = pm.getPackageInfo(ctx.getPackageName(), 0);
            }
            if (info != null) {
                cachedName = info.versionName != null ? info.versionName : "unknown";
                cachedCode = (int) info.getLongVersionCode();
            }
        } catch (Exception ignored) {
            // Fallbacks above stay; the version just reads "unknown".
        }
    }
}
