"""Read-only verification pass for the Android bridge (Phase 4, Step 2/3).

Captures a screenshot from the phone, sends it to Gemini
(gemini-3.5-flash-lite) for a text-only read of the screen, then captures a
second screenshot and compares the two to confirm the phone state was not
changed. No tap, click, type, or other input is performed anywhere in this
pass; actions stay disabled.

The Gemini key is supplied at run time as a surrogate from the registered
custom.gemini connection (same pattern as run_trial.py). The real key is
never written to a file or a log.

Usage:
  python3 android_readonly_pass.py [--out-dir logs]
      [--prompt "describe this phone screen in detail"]
Prints the model's screen read and a JSON summary. Exits 0 only when both
screenshots were captured and the comparison was made.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")

from android_bridge import AndroidAdapter, TransportError  # noqa: E402
from dynamic_credentials import dynamic_credential_entry  # noqa: E402

DEFAULT_PROMPT = (
    "Describe this Android phone screenshot in detail, in Japanese: "
    "what app or screen is shown, what text is visible, what time and "
    "battery level the status bar shows, and what UI elements are present. "
    "Do not guess at anything you cannot see. Text only, no tools."
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def model_screen_read(png: bytes, prompt: str) -> str:
    """Ask Gemini for a text read of the screenshot (vision, text only)."""
    from google import genai
    from google.genai import types

    # httpx cannot parse the bracketed IPv6 entries in this VM's NO_PROXY
    # list, so trim it to plain hostnames (same fix as run_trial.py).
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"
    entry = dynamic_credential_entry("custom.gemini")
    client = genai.Client(api_key=entry["surrogate"])
    image_part = types.Part.from_bytes(data=png, mime_type="image/png")
    # Transient 503s happen (high demand); retry with the Phase 2 policy.
    from retry import call_with_retry

    def _call():
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=[prompt, image_part],
        )
        return (response.text or "").strip()

    def _on_retry(attempt, error, delay, status):
        print(f"Gemini retry {attempt}/5 after {status}: sleeping "
              f"{delay:.1f}s ({str(error)[:120]})")

    return call_with_retry(_call, on_retry=_on_retry)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Android read-only verification pass")
    parser.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "logs"))
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = parser.parse_args(argv)

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    shot1_path = os.path.join(out_dir, f"android-{stamp}-shot1.png")
    shot2_path = os.path.join(out_dir, f"android-{stamp}-shot2.png")
    log_path = os.path.join(out_dir, f"android-{stamp}-readonly.log")

    adapter = AndroidAdapter(actions_enabled=False)
    summary: dict = {
        "timestamp_utc": stamp,
        "transport": "app",
        "model": "gemini-3.5-flash-lite",
        "actions_performed": [],
    }
    log_lines: list = []

    def note(line: str):
        log_lines.append(line)
        print(line, flush=True)

    try:
        # Full-res PNG on purpose: the byte comparison only proves
        # "unchanged" when both shots use the same lossless encoding.
        note("Capturing screenshot 1 (full PNG)...")
        png1 = adapter.capture_screen(scale=1.0, format="png")
        with open(shot1_path, "wb") as f:
            f.write(png1)
        summary["shot1"] = {"path": shot1_path, "bytes": len(png1), "sha256": sha256(png1)}
        note(f"shot1: {len(png1)} bytes, sha256:{sha256(png1)[:16]}")

        note("Asking Gemini to read the screen (text only)...")
        read = model_screen_read(png1, args.prompt)
        summary["model_read"] = read
        note("--- model screen read ---")
        note(read)
        note("--- end model read ---")

        note("Capturing screenshot 2 to confirm state unchanged...")
        time.sleep(2)
        png2 = adapter.capture_screen(scale=1.0, format="png")
        with open(shot2_path, "wb") as f:
            f.write(png2)
        summary["shot2"] = {"path": shot2_path, "bytes": len(png2), "sha256": sha256(png2)}
        summary["unchanged"] = png1 == png2
        note(f"shot2: {len(png2)} bytes, sha256:{sha256(png2)[:16]}")
        note(f"state unchanged (identical PNG bytes): {summary['unchanged']}")
        if not summary["unchanged"]:
            note("Note: PNG bytes differ; this can be caused by the clock or "
                 "status-bar changes alone, not by an action. Compare the two "
                 "screenshots visually to confirm nothing moved.")
    except TransportError as exc:
        summary["error"] = f"transport error: {exc}"
        note(f"TRANSPORT ERROR: {exc}")
        ok = False
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        summary["error"] = f"{type(exc).__name__}: {exc}"
        note(f"ERROR: {exc}")
        ok = False
    else:
        ok = True

    summary["log_path"] = log_path
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))
        f.write("\n\n--- summary ---\n")
        f.write(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"log: {log_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
