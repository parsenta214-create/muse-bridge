// Implemented by MUSE-Koyuki (Muse Spark) — SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-09
package dev.muse.bridge

import android.content.Context

/**
 * Bidirectional Java/Kotlin interop fixture (SPEC-KOTLIN-MIGRATION-001
 * D3, pilot). Inert at runtime: never called from app code. Kept as
 * a permanent compile-time regression check that Kotlin and Java can
 * call each other, that @Volatile / @Synchronized survive the
 * conversion, and that Java-visible static members are generated.
 */
object InteropProbe {
    @Volatile
    private var probeFlag: Boolean = false

    /**
     * Kotlin -> Java: reads the version through AppVersion (Kotlin)
     * and calls InteropProbeJava (Java). Also exercises an Android
     * framework type (Context).
     */
    @JvmStatic
    fun probeFromKotlin(ctx: Context): String {
        return AppVersion.name(ctx) + "/" + InteropProbeJava.javaGreeting()
    }

    /** Java-visible static member, synchronized like a Java method. */
    @JvmStatic
    @Synchronized
    fun setFlag(value: Boolean) {
        probeFlag = value
    }

    @JvmStatic
    fun getFlag(): Boolean {
        return probeFlag
    }

    /** Called from Java (InteropProbeJava.probeFromJava). */
    @JvmStatic
    fun kotlinGreeting(): String {
        return "kotlin"
    }
}
