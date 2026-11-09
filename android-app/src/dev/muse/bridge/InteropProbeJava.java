// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-09
package dev.muse.bridge;

import android.content.Context;

/**
 * Bidirectional Java/Kotlin interop fixture (SPEC-KOTLIN-MIGRATION-001
 * D3, pilot). Inert at runtime: never called from app code. Kept as
 * a permanent compile-time regression check that Java can call Kotlin.
 */
public final class InteropProbeJava {
    private InteropProbeJava() {
    }

    public static String javaGreeting() {
        return "java";
    }

    /**
     * Java -> Kotlin: reads the version through AppVersion (now Kotlin)
     * and calls InteropProbe (Kotlin object) static members.
     */
    public static String probeFromJava(Context ctx) {
        String version = AppVersion.name(ctx);
        String kotlinSide = InteropProbe.kotlinGreeting();
        InteropProbe.setFlag(true);
        boolean flag = InteropProbe.getFlag();
        return version + "/" + kotlinSide + "/" + flag;
    }
}
