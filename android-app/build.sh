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
export PATH="$JAVA_HOME/bin:$PATH"  # d8/apksigner wrappers need java on PATH
BT="$ANDROID_HOME/build-tools/34.0.0"
ANDROID_JAR="$ANDROID_HOME/platforms/android-34/android.jar"
BUILD="$HERE/build"
# File naming rule (2026-11-06): underscores, never spaces, and the
# extension always matches the content. The APK is versioned so a
# download is recognizable: Muse_Bridge_v1.9.apk.
VERSION_CODE=12
VERSION_NAME="1.9.2"
OUT="$HERE/Muse_Bridge_v${VERSION_NAME}.apk"

for f in "$ANDROID_JAR" "$BT/aapt2" "$BT/d8" "$BT/zipalign" "$BT/apksigner" "$JAVA_HOME/bin/javac"; do
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

echo "== javac =="
find "$HERE/src" "$BUILD/gen" -name '*.java' > "$BUILD/sources.txt"
"$JAVA_HOME/bin/javac" -J-Xmx256m -encoding UTF-8 -cp "$ANDROID_JAR" \
    -d "$BUILD/classes" @"$BUILD/sources.txt"

echo "== d8 =="
# d8 in build-tools 34 rejects a bare class directory ("Unsupported source
# file type"), so jar the classes first.
"$JAVA_HOME/bin/jar" --create --file "$BUILD/classes.jar" -C "$BUILD/classes" .
"$BT/d8" -JXmx384m --min-api 30 --output "$BUILD/dex" "$BUILD/classes.jar"

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
