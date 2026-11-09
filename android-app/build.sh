#!/bin/bash
# Build the Muse Bridge APK with the manual aapt2 / javac / d8 pipeline.
# No Gradle: the VM has ~400 MiB free RAM and Gradle's daemon does not fit.
# No Java networking is used: the SDK was installed by downloading the
# official zips with curl (see README.md), because Java's HttpURLConnection
# cannot tunnel through this VM's egress proxy.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ANDROID_HOME="${ANDROID_HOME:-$HOME/workspace/tools/android-sdk}"
JAVA_HOME="${JAVA_HOME:-$HOME/workspace/tools/jdk17}"
export JAVA_HOME
export PATH="$JAVA_HOME/bin:$PATH"  # d8/apksigner wrappers need java on PATH
# kotlinc (SPEC-KOTLIN-MIGRATION-001 D2): standalone compiler 2.4.21.
# KOTLIN_HOME is overridable for CI; the jar path below is the contract
# (D2: exactly one kotlin-stdlib.jar, same version as the compiler,
# kotlin-reflect excluded).
KOTLIN_HOME="${KOTLIN_HOME:-$HOME/workspace/tools/kotlin/kotlinc}"
KOTLINC="$KOTLIN_HOME/bin/kotlinc"
KOTLIN_STDLIB="$KOTLIN_HOME/lib/kotlin-stdlib.jar"
BT="$ANDROID_HOME/build-tools/34.0.0"
ANDROID_JAR="$ANDROID_HOME/platforms/android-34/android.jar"
BUILD="$HERE/build"
# File naming rule (2026-11-06): underscores, never spaces, and the
# extension always matches the content. The APK is versioned so a
# download is recognizable: Muse_Bridge_v1.9.apk.
VERSION_CODE=63
VERSION_NAME="2.9.10"
OUT="$HERE/Muse_Bridge_v${VERSION_NAME}.apk"

for f in "$ANDROID_JAR" "$BT/aapt2" "$BT/d8" "$BT/zipalign" "$BT/apksigner" "$JAVA_HOME/bin/javac" "$KOTLINC" "$KOTLIN_STDLIB"; do
    if [ ! -e "$f" ]; then
        echo "missing: $f" >&2
        echo "Set ANDROID_HOME/JAVA_HOME or re-run the SDK install in README.md." >&2
        exit 1
    fi
done

rm -rf "$BUILD"
mkdir -p "$BUILD/gen" "$BUILD/classes" "$BUILD/dex"

echo "== aapt2 compile =="
"$BT/aapt2" compile --dir "$HERE/res" -o "$BUILD/compiled.zip"

echo "== aapt2 link =="
"$BT/aapt2" link -o "$BUILD/app.unaligned.apk" \
    -I "$ANDROID_JAR" \
    --manifest "$HERE/AndroidManifest.xml" \
    --version-code "$VERSION_CODE" --version-name "$VERSION_NAME" \
    --java "$BUILD/gen" \
    --auto-add-overlay \
    "$BUILD/compiled.zip"

echo "== kotlinc (Kotlin -> .class) =="
# SPEC-KOTLIN-MIGRATION-001 D2, two-compiler pipeline, step 1:
# kotlinc compiles all .kt sources. The .java sources are passed
# only for symbol resolution — kotlinc does NOT generate Java
# .class files (that is javac's job, step 2). Heap starts at
# 256m (D2 memory rule); do NOT auto-escalate to 384m.
# -jvm-target 17 matches javac's class-file version (61).
# kotlin-stdlib is on kotlinc's compile classpath automatically;
# -include-runtime is NOT used (stdlib is not bundled into the
# kotlinc output; d8 gets it as a separate input instead).
"$KOTLINC" -version
"$KOTLINC" -J-Xmx256m -jvm-target 17 -cp "$ANDROID_JAR" \
    -d "$BUILD/classes" \
    $(find "$HERE/src" "$BUILD/gen" -name '*.kt') \
    $(find "$HERE/src" "$BUILD/gen" -name '*.java')

echo "== javac =="
# SPEC-KOTLIN-MIGRATION-001 D2, step 2 (a NORMAL mandatory step,
# not a fallback): javac compiles all .java sources (incl. generated
# R.java) into the SAME output dir. $BUILD/classes is on the
# classpath so Java can call the Kotlin classes kotlinc just
# produced; $KOTLIN_STDLIB is on it because generated Kotlin
# classes may reference stdlib types/annotations.
find "$HERE/src" "$BUILD/gen" -name '*.java' > "$BUILD/sources.txt"
"$JAVA_HOME/bin/javac" -J-Xmx256m -encoding UTF-8 -source 17 -target 17 \
    -cp "$ANDROID_JAR:$BUILD/classes:$KOTLIN_STDLIB" \
    -d "$BUILD/classes" @"$BUILD/sources.txt"

echo "== d8 =="
# d8 in build-tools 34 rejects a bare class directory ("Unsupported source
# file type"), so jar the classes first.
"$JAVA_HOME/bin/jar" --create --file "$BUILD/classes.jar" -C "$BUILD/classes" .
# SPEC-KOTLIN-MIGRATION-001 D2: d8 receives the combined classes
# jar AND exactly one kotlin-stdlib.jar (no duplicates, no reflect).
"$BT/d8" -JXmx384m --min-api 30 --output "$BUILD/dex" "$BUILD/classes.jar" "$KOTLIN_STDLIB"

echo "== package dex + align =="
# aapt2 2.19 has no `add` subcommand; jar (zip format) adds classes.dex.
"$JAVA_HOME/bin/jar" --update --file "$BUILD/app.unaligned.apk" \
    -C "$BUILD/dex" classes.dex
"$BT/zipalign" -f 4 "$BUILD/app.unaligned.apk" "$BUILD/app.aligned.apk"

echo "== sign (debug key, sideloading only) =="
KEYSTORE="$HERE/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
    "$JAVA_HOME/bin/keytool" -genkeypair \
        -keystore "$KEYSTORE" -alias muse-bridge-debug \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -storepass android -keypass android \
        -dname "CN=Muse Bridge Debug, OU=dev, O=muse"
fi
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android \
    --key-pass pass:android --out "$OUT" "$BUILD/app.aligned.apk"

echo "== verify =="
"$BT/apksigner" verify --verbose "$OUT" | tail -n 3
"$BT/aapt2" dump badging "$OUT" | grep -E "package:|sdkVersion|targetSdkVersion|launchable-activity" || true
ls -la "$OUT"
echo "BUILD OK: $OUT"
