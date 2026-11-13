"""Tests for the bridge-app ("app") transport of android_bridge.py.

A local mock HTTP server stands in for the phone's Muse Bridge app: it
checks the X-Bridge-Token header and answers the same endpoints. No phone
is involved, and no real token is used (the mock token is a test-only
constant, never a real credential).
"""
import json
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import android_bridge as ab

MOCK_TOKEN = "test-token-only-for-this-test"


def _tiny_png() -> bytes:
    """A minimal valid 1x1 transparent PNG."""
    def chunk(kind, data):
        out = kind + data
        return struct.pack(">I", len(data)) + out + struct.pack(">I", zlib.crc32(out) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    raw = b"\x00" + b"\x00\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _a11y_off_body(path: str) -> dict:
    """P0-1 (v2.0): the structured a11y-off 503 body the real
    server sends when the AccessibilityService is off."""
    return {"ok": False, "status": "unavailable",
            "reason": "ACCESSIBILITY_DISABLED",
            "required_mode": "FULL_ACCESS",
            "detail": f"accessibility service is not enabled; {path} needs FULL_ACCESS",
            "error": "accessibility service not enabled"}


# P1-07 (CONS-014): the mock's mirror of the server's
# /node_action arg validation (nodeActionArgs). Returns an
# error message, or None when the args are valid. The client
# validates the same rules before sending, so the mock only
# sees these from a raw request.
_NODE_ACTION_ARG_KEYS = {
    "set_selection": ("start", "end"),
    "set_progress": ("value",),
    "scroll_to_position": ("row",),
    "press_and_hold": ("duration_ms",),
    "set_text": ("text",),
}


def _validate_node_action_args(action: str, args: dict):
    """Mirror of the server's integer/range checks. Bools are
    not integers; a fractional float (1.8) is rejected instead
    of truncating; an integral float (2.0) is accepted."""
    def int_arg(key, minimum=None, maximum=None):
        v = args[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return f"{action} needs integer {key}"
        if isinstance(v, float):
            if not v.is_integer():
                return f"{action} needs integer {key} (got {v!r})"
            v = int(v)
        # P1-07 fix round 2 (CONS-033): the 32-bit range matches
        # the Java server exactly at both edges (-2**31 .. 2**31-1).
        if v < -(2**31) or v > 2**31 - 1:
            return f"{action} needs integer {key} (got {v!r})"
        if minimum is not None and v < minimum:
            return f"{action} needs {key} >= {minimum}"
        if maximum is not None and v > maximum:
            return f"{action} needs {key} <= {maximum}"
        return None

    if action == "set_selection":
        for key in ("start", "end"):
            err = int_arg(key, minimum=0)
            if err is not None:
                return err
    elif action == "set_progress":
        v = args.get("value")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "set_progress needs numeric value"
        if not 0.0 <= v <= 1.0:
            return "set_progress value must be between 0.0 and 1.0"
    elif action == "scroll_to_position":
        err = int_arg("row", minimum=0)
        if err is not None:
            return err
    elif action == "press_and_hold":
        if "duration_ms" in args:
            err = int_arg("duration_ms", minimum=1, maximum=60000)
            if err is not None:
                return err
    elif action == "set_text":
        if not isinstance(args.get("text"), str):
            return "set_text needs text"
    return None


def _health_body(accessibility: bool, banking_safe: bool = False,
                 can_install_apk: bool = True,
                 tailscale_stale: bool = False) -> dict:
    """P0-3 (v2.0): the extended /health body the real v2.0.0
    server sends. a11y on -> mode FULL_ACCESS, screen caps on;
    a11y off -> mode MINIMAL (or BANKING_SAFE when the user
    picked it), screen caps off, overall HEALTHY (a11y off is
    normal in MINIMAL).
    P1-3 (CONS-022, DEGRADED by CONS-029): tailscale_ips is the
    CURRENT address list and tailscale_ips_bound the
    bound-at-start list; when tailscale_stale is True the two
    differ (bound is the old IP), so the real server reports
    modules.tailscale DEGRADED and overall_status DEGRADED —
    the VM cannot connect on the current IP.
    P1-4 (CONS-029 split): install_apk is always True
    (endpoint available, P0 spec A5); install_apk_permitted
    follows can_install_apk (the user's 'Install unknown apps'
    setting)."""
    a11y = bool(accessibility)
    if a11y:
        mode = "FULL_ACCESS"
    elif banking_safe:
        mode = "BANKING_SAFE"
    else:
        mode = "MINIMAL"
    current = ["100.85.138.44"]
    bound = ["100.111.74.42"] if tailscale_stale else list(current)
    return {"ok": True, "service": "muse-bridge",
            "version": 1, "accessibility": a11y,
            "notification_listener": True,
            "app_version": "1.9.5",
            "app_version_code": 15,
            "mode": mode,
            "overall_status": "DEGRADED" if tailscale_stale else "HEALTHY",
            "modules": {"http": "HEALTHY",
                        "tailscale": "DEGRADED" if tailscale_stale else "HEALTHY",
                        "accessibility": "ENABLED" if a11y else "DISABLED",
                        "notification_access": "GRANTED",
                        "termux_ssh": "UNKNOWN"},
            "capabilities": {"screenshot": a11y, "tap": a11y,
                             "swipe": a11y, "text_input": a11y,
                             "notification": True, "notification_post": True,
                             "http_api": True, "launch_app": True,
                             "launch_app_guaranteed": False,
                             "foreground_verification": a11y,
                             "install_apk": True,
                             "install_apk_permitted": can_install_apk},
            "tailscale_ips": current,
            "tailscale_ips_bound": bound}


class MockAppHandler(BaseHTTPRequestHandler):
    requests = []
    # Test hook for the retry policy: {(method, path): N} — answer
    # the next N calls to that endpoint with 503, then behave
    # normally. A (status, N) tuple answers with that status instead.
    # Reset by the fixture.
    fail_next = {}
    # Step 4 (CONS-020 P0-04): when set, /batch answers with this
    # batch_id instead of the one in the request, so the adapter's
    # batch_id check can be tested. Reset by the fixture.
    batch_id_override = None
    # C-01 (SPEC §2.10): when set to "unknown", /batch answers with
    # the UNKNOWN shape (no results, no payload_hash), so the
    # adapter's unknown handling can be tested. Reset by the fixture.
    batch_status_override = None
    # A-3 (v1.9): how /state answers, per test. "normal" always returns
    # a stable snapshot; "unstable_then_same" returns an unstable
    # snapshot first, then the same tree hash stable; "unstable_then_
    # changed" returns an unstable snapshot first, then a changed tree
    # hash stable. "text_after" is like "normal" but adds the
    # decision-61 target nodes (n3 "MUSE用", n4 "押せない", n5 with
    # desc "MUSE用" and empty text) once state_calls > text_after.
    # Reset by the fixture.
    state_mode = "normal"
    state_calls = 0
    text_after = 0
    # P0-1 (v2.0): when False, the mock behaves as if the
    # AccessibilityService is off on the phone — /health still
    # answers 200 (with accessibility: false) and the
    # a11y-dependent endpoints answer the structured 503 body,
    # while the available endpoints keep answering 200.
    # Reset by the fixture.
    accessibility = True
    # P0-2 (v2.0): when True (and accessibility is False), the
    # mock's /health reports mode BANKING_SAFE. Reset by the
    # fixture.
    banking_safe = False
    # P1-4 (CONS-022, split by CONS-029): when False, the mock's
    # /health reports install_apk_permitted False (the user's
    # "Install unknown apps" setting is off); install_apk stays
    # True (endpoint available, P0 spec A5). Reset by the fixture.
    can_install_apk = True
    # P1-3 (CONS-022, DEGRADED by CONS-029): when True, the
    # mock's /health reports a stale bound-at-start IP
    # (100.111.74.42) while the current tailscale_ips is
    # 100.85.138.44, with modules.tailscale DEGRADED and
    # overall_status DEGRADED. Reset by the fixture.
    tailscale_stale = False
    # P0-4 (v2.0): what /launch_app answers. "launched" is the
    # confirmed-foreground report (a11y on); "unconfirmed" is
    # the verified-but-not-confirmed report
    # (FOREGROUND_NOT_CONFIRMED). With accessibility False the
    # mock always answers VERIFICATION_UNAVAILABLE, like the
    # real server. Reset by the fixture.
    launch_outcome = "launched"
    # P0-4 deny list (System Development Lead decision 2026-11-06 20:16,
    # README decision 91): mirrors the Java LAUNCH_DENY_LIST —
    # the mock refuses these packages with 403 before any launch
    # attempt and before the operation notification.
    LAUNCH_DENY_LIST = frozenset({
        "com.kasikorn.retail.mbanking.wap",
        "com.bbl.mobilebanking",
        "com.scb.phone",
        "com.krungsri.kma",
    })
    # P0-4 fix (CONS-027 P1 #3): per-package launch outcomes,
    # so two simultaneous requests for different packages can
    # be told apart ("launched", "unconfirmed",
    # "foreground_left" — the target left before the post-request
    # poll — or "no_window" — getRootInActiveWindow() null).
    # Values answer the same wire shape as "unconfirmed" where
    # the server-side cause differs; the name documents the
    # scenario. Reset by the fixture.
    launch_outcomes = {}
    # Counters proving what a request did: launch_attempts counts
    # launches actually attempted, operation_notifications counts
    # "操作中" notifications posted. A refused (deny-listed)
    # launch increments neither. Guarded by a lock because the
    # mock serves concurrent requests. Reset by the fixture.
    counter_lock = threading.Lock()
    launch_attempts = 0
    operation_notifications = 0
    # P0-4 fix (CONS-027 P1 #3): when True, the service "goes
    # away" mid-verification — the handler answers as the real
    # server does when its reads start failing (unconfirmed /
    # FOREGROUND_NOT_CONFIRMED, never launched). Reset by the
    # fixture.
    disconnect_during_launch = False
    # P0-4 fix (CONS-027 P1 #1): artificial delay (seconds)
    # before the /launch_app answer, modelling a slow
    # verification window. Reset by the fixture.
    launch_delay_s = 0.0
    # C-04 (SPEC-FOREGROUND-GUARD-001 §2.1): when set to a dict,
    # the next failing POST (fail_next tuple hook) answers with
    # this body instead of the generic simulated body — so a test
    # can make the mock return a real SPEC error id (e.g. 409
    # {"ok": false, "error": "session_mismatch"}). Reset by the
    # fixture.
    fail_body = None
    # C-04: the server-issued session id the mock returns on
    # guarded responses, mirroring the real server (which returns
    # session_id on every guarded success). Reset by the fixture.
    mock_session_id = "mock-session-1"

    def log_message(self, *args):  # keep test output quiet
        pass

    def _check_token(self):
        return self.headers.get("X-Bridge-Token") == MOCK_TOKEN

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_GET(self):
        MockAppHandler.requests.append(("GET", self.path, None))
        if not self._check_token():
            return self._send_json(401, {"ok": False, "error": "unauthorized"})
        key = ("GET", self.path.split("?")[0])
        fail = MockAppHandler.fail_next.get(key, 0)
        if isinstance(fail, tuple):
            status, n = fail
            if n > 0:
                MockAppHandler.fail_next[key] = (status, n - 1)
                return self._send_json(status, {"ok": False, "error": "simulated (test hook)"})
        elif fail > 0:
            MockAppHandler.fail_next[key] -= 1
            return self._send_json(503, {"ok": False, "error": "overloaded (test hook)"})
        # P0-1 (v2.0): a11y-off mode. /health still answers 200
        # (accessibility: false); the a11y-dependent GETs answer
        # the structured 503 body; the available GETs (/logs,
        # /notifications, /clipboard) fall through to normal handling.
        if not MockAppHandler.accessibility:
            base = self.path.split("?")[0]
            if base == "/health":
                return self._send_json(200, _health_body(
                    False, MockAppHandler.banking_safe,
                    can_install_apk=MockAppHandler.can_install_apk,
                    tailscale_stale=MockAppHandler.tailscale_stale))
            if base in ("/state", "/capture_screen", "/events"):
                return self._send_json(503, _a11y_off_body(base))
        if self.path == "/health":
            return self._send_json(200, _health_body(
                True,
                can_install_apk=MockAppHandler.can_install_apk,
                tailscale_stale=MockAppHandler.tailscale_stale))
        if self.path.startswith("/logs"):
            # v1.9.3: debug log (current run + previous run's tail).
            # P1-03 (CONS-014): the listen line carries a MASKED
            # IP (100.111.*.*), mirroring the real server's
            # BridgeLog.sanitize — a full IP must never appear
            # in a log line. The bind-failed / accept-failed lines
            # below mirror the sanitized call sites too: masked IP
            # + exception class name only, no message, no package,
            # no URL, no file name.
            return self._send_json(200, {"ok": True,
                                         "app_version": "1.9.5",
                                         "app_version_code": 15,
                                         "lines": ["[11-06 03:55:00] listen 100.111.*.*:8080",
                                                   "[11-06 03:55:00] server RUNNING",
                                                   "[11-06 03:55:01] batch completed 1/1",
                                                   "[11-06 03:55:02] bind-failed 100.111.*.*: BindException",
                                                   "[11-06 03:55:03] accept-failed SocketException"],
                                         "previous_lines": ["[11-06 03:50:00] server STOPPED"]})
        if self.path.startswith("/state?"):
            # A-1 (v1.9): simulated bridge input-validation rejections.
            import urllib.parse
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            err = query.get("err", [None])[0]
            if err in ("400", "401", "408", "413", "431", "503"):
                return self._send_json(int(err), {"ok": False, "error": "simulated"})
        if self.path == "/capture_screen" or self.path.startswith("/capture_screen?"):
            import urllib.parse
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("bad") == ["1"]:
                body = b"not-an-image"
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if query.get("format") == ["jpeg"]:
                body = b"\xff\xd8\xff\xe0" + b"fake-jpeg-bytes"
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            png = _tiny_png()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            return self.wfile.write(png)
        if self.path == "/state":
            MockAppHandler.state_calls += 1
            mode = MockAppHandler.state_mode
            if mode == "unstable_then_changed":
                tree_hash = 111 if MockAppHandler.state_calls == 1 else 222
                stable = MockAppHandler.state_calls != 1
                snap = 7 if MockAppHandler.state_calls == 1 else 8
            elif mode == "unstable_then_same":
                tree_hash = 111
                stable = MockAppHandler.state_calls != 1
                snap = 7 if MockAppHandler.state_calls == 1 else 8
            else:
                tree_hash = 123456
                stable = True
                snap = 7
            nodes = [
                                             {"id": "n0", "parent": None, "class": "android.widget.FrameLayout",
                                              "text": "", "desc": "", "bounds": {"left": 0, "top": 0, "right": 1080, "bottom": 2340},
                                              "clickable": False, "focusable": False, "focused": False,
                                              "editable": False, "scrollable": False, "enabled": True,
                                              "selected": False, "password": False, "input_type": 0,
                                              "resource_id": "", "actions": []},
                                             {"id": "n1", "parent": "n0", "class": "android.widget.EditText",
                                              "text": "hello", "desc": "", "bounds": {"left": 100, "top": 200, "right": 980, "bottom": 300},
                                              "clickable": True, "focusable": True, "focused": True,
                                              "editable": True, "scrollable": False, "enabled": True,
                                              "selected": False, "password": False, "input_type": 1,
                                              "resource_id": "com.example:id/search", "actions": ["focus", "set_text"]},
                                             {"id": "n2", "parent": "n0", "class": "android.widget.EditText",
                                              "text": "[REDACTED]", "desc": "", "bounds": {"left": 100, "top": 400, "right": 980, "bottom": 500},
                                              "clickable": True, "focusable": True, "focused": False,
                                              "editable": True, "scrollable": False, "enabled": True,
                                              "selected": False, "password": True, "input_type": 129,
                                              "resource_id": "", "actions": ["focus", "set_text"]},
            ]
            if mode == "text_after" and MockAppHandler.state_calls > MockAppHandler.text_after:
                # Decision 61 targets: n4 present but not clickable,
                # n5 matches only by desc (never by text), n3 is the
                # clickable text match. n5 precedes n3 on purpose.
                nodes += [
                    {"id": "n4", "parent": "n0", "class": "android.widget.TextView",
                     "text": "押せない", "desc": "", "bounds": {"left": 100, "top": 600, "right": 980, "bottom": 700},
                     "clickable": False, "focusable": False, "focused": False,
                     "editable": False, "scrollable": False, "enabled": True,
                     "selected": False, "password": False, "input_type": 0,
                     "resource_id": "", "actions": []},
                    {"id": "n5", "parent": "n0", "class": "android.widget.TextView",
                     "text": "", "desc": "MUSE用", "bounds": {"left": 100, "top": 800, "right": 980, "bottom": 900},
                     "clickable": False, "focusable": False, "focused": False,
                     "editable": False, "scrollable": False, "enabled": True,
                     "selected": False, "password": False, "input_type": 0,
                     "resource_id": "", "actions": []},
                    {"id": "n3", "parent": "n0", "class": "android.widget.TextView",
                     "text": "MUSE用", "desc": "", "bounds": {"left": 100, "top": 1000, "right": 980, "bottom": 1100},
                     "clickable": True, "focusable": False, "focused": False,
                     "editable": False, "scrollable": False, "enabled": True,
                     "selected": False, "password": False, "input_type": 0,
                     "resource_id": "", "actions": ["click"]},
                ]
            return self._send_json(200, {"ok": True, "width": 1080, "height": 2340,
                                         "package": "com.example", "texts": ["Hi"],
                                         "revision": 7, "tree_hash": tree_hash,
                                         "snapshot_id": snap,
                                         "snapshot_valid": True,
                                         "snapshot_stable": stable,
                                         "event_seq": 42,
                                         "device": {"battery_pct": 87, "charging": True,
                                                    "network": "wifi", "interactive": True},
                                         "app_version": "1.9.5",
                                         "app_version_code": 15,
"nodes": nodes,
                                         "focused": "n1", "ime_visible": True, "truncated": False})
        if self.path == "/events" or self.path.startswith("/events?"):
            import urllib.parse
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            since = int(query.get("since", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            all_events = [
                {"seq": 1, "t": 1000, "type": "TYPE_WINDOW_STATE_CHANGED",
                 "package": "com.example", "class": "android.widget.FrameLayout",
                 "text": "", "desc": "",
                 "bounds": {"left": -1, "top": -1, "right": -1, "bottom": -1},
                 "coalesced": 0},
                {"seq": 2, "t": 1500, "type": "TYPE_VIEW_TEXT_CHANGED",
                 "package": "com.example", "class": "android.widget.EditText",
                 "text": "[REDACTED]", "desc": "",
                 "bounds": {"left": 100, "top": 400, "right": 980, "bottom": 500},
                 "coalesced": 3},
            ]
            shown = [e for e in all_events if e["seq"] > since][:limit]
            return self._send_json(200, {"ok": True, "events": shown,
                                         "latest_seq": 2, "dropped": False,
                                         "tree_hash": 123456, "revision": 7})
        if self.path == "/notifications" or self.path.startswith("/notifications?"):
            # Decision 63 (v1.9.7): the notification event stream.
            # Canned data: a post, an update of the same key, an
            # ongoing post from another package, and a tombstone
            # removal of the first key.
            import urllib.parse
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            since = int(query.get("since", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            pkg = query.get("package", [None])[0]
            all_notifs = [
                {"seq": 1, "type": "posted",
                 "key": "0|com.example.chat|1|null|10001",
                 "package": "com.example.chat", "id": 1, "tag": None,
                 "title": "Aya", "text": "こんにちは", "t": 1700000000000,
                 "ongoing": False, "group_summary": False},
                {"seq": 2, "type": "updated",
                 "key": "0|com.example.chat|1|null|10001",
                 "package": "com.example.chat", "id": 1, "tag": None,
                 "title": "Aya", "text": "こんにちは！元気？",
                 "t": 1700000005000, "ongoing": False, "group_summary": False},
                {"seq": 3, "type": "posted",
                 "key": "0|com.example.music|2|null|10002",
                 "package": "com.example.music", "id": 2, "tag": None,
                 "title": "Now playing", "text": "Song — Artist",
                 "t": 1700000008000, "ongoing": True, "group_summary": False},
                {"seq": 4, "type": "removed",
                 "key": "0|com.example.chat|1|null|10001",
                 "package": "com.example.chat", "id": 1, "tag": None,
                 "title": "", "text": "", "t": 1700000000000,
                 "ongoing": False, "group_summary": False,
                 "removed_t": 1700000010000},
            ]
            shown = [e for e in all_notifs if e["seq"] > since
                     and (pkg is None or e["package"] == pkg)][:limit]
            return self._send_json(200, {"ok": True, "events": shown,
                                         "latest_seq": 4, "dropped": False})
        if self.path == "/install_status":
            # Decision 75 (v1.9.16): the install-apk job state.
            return self._send_json(200, {"ok": True, "status": "idle",
                                         "detail": ""})
        if self.path == "/clipboard":
            # Decision 76 (v1.9.17): the clipboard content.
            return self._send_json(200, {"ok": True, "text": "hello-clipboard",
                                         "truncated": False})
        return self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        payload = self._read_json()
        MockAppHandler.requests.append(("POST", self.path, payload))
        if not self._check_token():
            return self._send_json(401, {"ok": False, "error": "unauthorized"})
        key = ("POST", self.path.split("?")[0])
        fail = MockAppHandler.fail_next.get(key, 0)
        if isinstance(fail, tuple):
            status, n = fail
            if n > 0:
                MockAppHandler.fail_next[key] = (status, n - 1)
                # C-04: a test-set fail_body replaces the generic
                # simulated body, so the client sees a real SPEC
                # error id (e.g. "session_mismatch").
                if MockAppHandler.fail_body is not None:
                    return self._send_json(status, MockAppHandler.fail_body)
                return self._send_json(status, {"ok": False, "error": "simulated (test hook)"})
        elif fail > 0:
            MockAppHandler.fail_next[key] -= 1
            return self._send_json(503, {"ok": False, "error": "overloaded (test hook)"})
        # P0-1 (v2.0): a11y-off mode. The a11y-dependent POSTs
        # answer the structured 503 body; the available POSTs
        # (/launch_app, /open, /install_apk, /notify,
        # /clipboard, /operation_end) fall through to normal handling.
        # C-04 (SPEC §2.2): /notification_action is a guarded
        # ordinary screen operation — it needs the service, so it
        # 503s here too (it was listed as available before C-04).
        if not MockAppHandler.accessibility:
            base = self.path.split("?")[0]
            if base in ("/batch", "/click", "/swipe", "/type_text",
                        "/press_key", "/click_node", "/type_node",
                        "/scroll_node", "/node_action",
                        "/notification_action"):
                return self._send_json(503, _a11y_off_body(base))
        if self.path == "/batch":
            # C-01 (SPEC §2.10): the UNKNOWN shape — no results
            # array, no payload_hash, just the status and a message.
            if MockAppHandler.batch_status_override == "unknown":
                return self._send_json(200, {
                    "ok": True, "batch_id": payload.get("batch_id"),
                    "status": "unknown", "replayed": False,
                    "session_id": MockAppHandler.mock_session_id,
                    "message": "batch outcome is unknown — do not "
                    "re-send this batch_id; check the phone and "
                    "confirm manually"})
            # A-4 (v1.9): a realistic batch response, with the status,
            # payload hash, and completed/failed action ids. Step 4:
            # the payload_hash is the real content hash (computed the
            # same way the server does), so the client's registry can
            # verify it against what it sent.
            acts = payload.get("actions", [])
            payload_hash = ab.bridge_protocol.BatchRegistry.compute_content_hash(
                payload.get("protocol_version", 1),
                payload.get("tree_hash", 0),
                acts,
            )
            # Step 4 (CONS-020 P0-04): the override answers with a
            # different batch_id than the request carried.
            answered_id = MockAppHandler.batch_id_override
            if answered_id is None:
                answered_id = payload.get("batch_id")
            return self._send_json(200, {
                "ok": True, "batch_id": answered_id,
                "snapshot_id": 7, "status": "completed",
                "session_id": MockAppHandler.mock_session_id,
                "payload_hash": payload_hash, "replayed": False,
                "results": [{"action_id": a.get("action_id"),
                             "status": "executed", "error": None,
                             "hash_before": 123456, "hash_after": 123456}
                            for a in acts],
                "completed_action_ids": [a.get("action_id") for a in acts],
                "failed_action_id": None,
                "stopped_at": -1, "stop_reason": None})
        if self.path == "/operation_end":
            # v2.1: explicit end of the operation session. The
            # stateless mock always answers was_active false — the
            # test pins the field's presence, not server state.
            return self._send_json(200, {"ok": True, "was_active": False})
        if self.path == "/launch_app":
            # P0-4 (v2.0): the structured launch report. a11y on
            # + outcome "launched" -> the target was observed in
            # the foreground; outcome "unconfirmed" -> verified
            # but not confirmed; a11y off -> verification is
            # unavailable (UsageStats is P1).
            pkg = payload.get("package", "")
            # P0-4 deny list: refused before any launch attempt
            # and before the operation notification — no launch
            # is attempted and no "操作中" is posted.
            if pkg in MockAppHandler.LAUNCH_DENY_LIST:
                return self._send_json(403, {
                    "ok": False,
                    "error": f"launch denied: {pkg} is on the deny list"})
            # P0-4 fix (CONS-027 P1 #1): model a slow
            # verification window (the full ~3 s window
            # elapsing) — the answer still arrives, no hang.
            if MockAppHandler.launch_delay_s:
                time.sleep(MockAppHandler.launch_delay_s)
            # P0-4 fix (CONS-027 P1 #3): the service goes away
            # mid-verification. The request started with a11y
            # on, so the reads failing surface as missed polls
            # (unconfirmed / FOREGROUND_NOT_CONFIRMED), not as
            # VERIFICATION_UNAVAILABLE — exactly like the real
            # server, whose captured service reference goes
            # stale. Never launched.
            if MockAppHandler.disconnect_during_launch:
                MockAppHandler.accessibility = False
                with MockAppHandler.counter_lock:
                    MockAppHandler.operation_notifications += 1
                    MockAppHandler.launch_attempts += 1
                return self._send_json(200, {
                    "ok": False, "status": "unconfirmed",
                    "reason": "FOREGROUND_NOT_CONFIRMED",
                    "accepted": True, "package": pkg,
                    "launch_requested": True,
                    "foreground_confirmed": False,
                    "verification": "accessibility",
                    "session_id": MockAppHandler.mock_session_id})
            # The operation notification is posted only once the
            # request is accepted (deny check passed).
            with MockAppHandler.counter_lock:
                MockAppHandler.operation_notifications += 1
                MockAppHandler.launch_attempts += 1
            if not MockAppHandler.accessibility:
                return self._send_json(200, {
                    "ok": False, "status": "unconfirmed",
                    "reason": "VERIFICATION_UNAVAILABLE",
                    "accepted": True, "package": pkg,
                    "launch_requested": True,
                    "foreground_confirmed": None, "verification": "none",
                    "session_id": MockAppHandler.mock_session_id})
            outcome = MockAppHandler.launch_outcomes.get(
                pkg, MockAppHandler.launch_outcome)
            if outcome in ("unconfirmed", "foreground_left", "no_window"):
                return self._send_json(200, {
                    "ok": False, "status": "unconfirmed",
                    "reason": "FOREGROUND_NOT_CONFIRMED",
                    "accepted": True, "package": pkg,
                    "launch_requested": True,
                    "foreground_confirmed": False,
                    "verification": "accessibility",
                    "session_id": MockAppHandler.mock_session_id})
            return self._send_json(200, {
                "ok": True, "status": "launched", "accepted": True,
                "package": pkg, "launch_requested": True,
                "foreground_confirmed": True,
                "verification": "accessibility",
                "session_id": MockAppHandler.mock_session_id})
        if self.path == "/repair_termux":
            # Decision 95 (v2.5.0): the mock always answers a
            # repaired report — the client test pins the
            # request shape and the parsing, not the phone.
            return self._send_json(200, {
                "ok": True, "repaired": True, "reason": "REPAIRED",
                "termux_sshd_local": {
                    "reachable": True,
                    "banner": "SSH-2.0-OpenSSH_10.2",
                    "latency_ms": 12, "error": ""},
                "steps": ["termux-wake-lock", "sshd"]})
        if self.path == "/scroll_node":
            # P1-04 (CONS-014): direction is required — a missing
            # direction is a 400, not a silent scroll forward.
            # The value itself is still validated.
            if "direction" not in payload:
                return self._send_json(400, {"ok": False,
                                             "error": "missing direction"})
            if payload.get("direction") not in ("forward", "backward"):
                return self._send_json(400, {
                    "ok": False,
                    "error": f"unknown direction: {payload.get('direction')}"})
            return self._send_json(200, {"ok": True, "snapshot_id": 7})
        if self.path == "/node_action":
            # P1-07 (CONS-014): mirror the server's arg
            # validation — non-object args, unknown keys,
            # non-integer numbers, and out-of-range values are
            # all 400. The client validates the same rules
            # before sending, so these paths are only reachable
            # by a raw request (which is what the tests post).
            args = payload.get("args", None)
            if "args" in payload and not isinstance(args, dict):
                return self._send_json(400, {"ok": False,
                                             "error": "args must be an object"})
            args = args or {}
            action = payload.get("action", "")
            allowed = _NODE_ACTION_ARG_KEYS.get(action, ())
            for key in args:
                if key not in allowed:
                    return self._send_json(400, {
                        "ok": False,
                        "error": f"unknown argument for {action}: {key}"})
            err = _validate_node_action_args(action, args)
            if err is not None:
                return self._send_json(400, {"ok": False, "error": err})
            return self._send_json(200, {"ok": True, "snapshot_id": 7})
        # C-04: the guarded endpoints return the server-issued
        # session id on success (the mock's fixed id); the unguarded
        # ones (/notify, /install_apk, /clipboard) do not.
        if self.path.split("?")[0] in ("/click", "/swipe", "/type_text",
                                       "/press_key", "/notification_action",
                                       "/open"):
            return self._send_json(200, {
                "ok": True, "echo": payload,
                "session_id": MockAppHandler.mock_session_id})
        return self._send_json(200, {"ok": True, "echo": payload})


@pytest.fixture()
def mock_app(monkeypatch, tmp_path):
    MockAppHandler.requests.clear()
    MockAppHandler.fail_next.clear()
    MockAppHandler.batch_id_override = None
    MockAppHandler.batch_status_override = None
    MockAppHandler.state_mode = "normal"
    MockAppHandler.state_calls = 0
    MockAppHandler.text_after = 0
    MockAppHandler.accessibility = True
    MockAppHandler.banking_safe = False
    MockAppHandler.can_install_apk = True
    MockAppHandler.tailscale_stale = False
    MockAppHandler.launch_outcome = "launched"
    MockAppHandler.launch_outcomes = {}
    MockAppHandler.launch_attempts = 0
    MockAppHandler.operation_notifications = 0
    MockAppHandler.disconnect_during_launch = False
    MockAppHandler.launch_delay_s = 0.0
    MockAppHandler.fail_body = None
    # C-01 (SPEC-BATCH-PERSIST-001 §2.9): every test gets a fresh,
    # initialized outbox in a temp dir — never the real
    # ~/.muse-bridge. batch() requires an initialized outbox, and
    # the durability gate ships ON (CONS-152 P1-02), so the
    # fixture also records the durability verification.
    outbox_dir = tmp_path / "outbox"
    monkeypatch.setenv("MUSE_BRIDGE_OUTBOX_DIR", str(outbox_dir))
    outbox = ab.bridge_protocol.BatchOutbox(str(outbox_dir))
    outbox.initialize("test fixture — fresh outbox")
    outbox.record_durability_verified(
        "file_atomic_v1", "test fixture — crash-durability check passed")
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockAppHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(ab, "APP_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(ab, "APP_TOKEN", MOCK_TOKEN)
    yield server
    server.shutdown()
    server.server_close()


def test_app_screenshot_default_is_half_scale_jpeg(mock_app):
    """v1.2: the default routine shot is a half-scale JPEG."""
    adapter = ab.AndroidAdapter()
    data = adapter.capture_screen()
    assert data.startswith(b"\xff\xd8\xff")
    gets = [r for r in MockAppHandler.requests if r[0] == "GET"]
    assert len(gets) == 1
    path = gets[0][1]
    assert path.startswith("/capture_screen?")
    assert "scale=0.5" in path and "format=jpeg" in path and "quality=80" in path


def test_app_screenshot_full_png_on_request(mock_app):
    adapter = ab.AndroidAdapter()
    data = adapter.capture_screen(scale=1.0, format="png")
    assert data.startswith(ab.PNG_MAGIC)
    gets = [r for r in MockAppHandler.requests if r[0] == "GET"]
    assert "scale=1.0" in gets[0][1] and "format=png" in gets[0][1]


def test_app_screenshot_rejects_non_image(mock_app):
    # Ask the mock for its test-only garbage body, then check the
    # client's image validation rejects it.
    adapter = ab.AndroidAdapter()
    with pytest.raises(ab.TransportError):
        adapter._app_request("GET", "/capture_screen?bad=1", expect_image=True)


def test_app_state(mock_app):
    adapter = ab.AndroidAdapter()
    state = adapter.get_current_state()
    assert state["width"] == 1080
    assert state["height"] == 2340
    assert state["transport"] == "app"


def test_app_state_tree_v14(mock_app):
    """Phase 1 (v1.4): /state carries the node tree, focus, IME state,
    and revision; password values arrive already redacted."""
    adapter = ab.AndroidAdapter()
    state = adapter.get_current_state()
    assert state["revision"] == 7
    assert state["tree_hash"] == 123456
    assert state["focused"] == "n1"
    assert state["ime_visible"] is True
    assert state["truncated"] is False
    nodes = {n["id"]: n for n in state["nodes"]}
    edit = nodes["n1"]
    assert edit["bounds"] == {"left": 100, "top": 200, "right": 980, "bottom": 300}
    assert edit["editable"] is True and edit["focused"] is True
    assert edit["input_type"] == 1
    assert edit["resource_id"] == "com.example:id/search"
    assert "set_text" in edit["actions"]
    pw = nodes["n2"]
    assert pw["password"] is True
    assert pw["text"] == "[REDACTED]"


def test_app_actions_send_json(mock_app):
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.click(500, 800)["ok"] is True
    assert adapter.swipe(100, 200, 300, 400)["ok"] is True
    assert adapter.type_text("hello")["ok"] is True
    assert adapter.press_key("HOME")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    # C-04: every guarded POST carries the operation_context of a
    # fresh /state (mock package com.example, revision 7). The
    # first action bootstraps (no session_id); later actions carry
    # the session id the mock returned (retention).
    assert (("POST", "/click", {"x": 500, "y": 800,
                                "operation_context": {"expected_package": "com.example", "tree_hash": 123456, "generation": 7}}) in posts)
    assert (("POST", "/press_key", {"key": "HOME",
                                   "operation_context": {"expected_package": "com.example", "tree_hash": 123456, "generation": 7, "session_id": "mock-session-1"}}) in posts)


def test_app_actions_refused_without_flag(mock_app):
    adapter = ab.AndroidAdapter()
    with pytest.raises(PermissionError):
        adapter.click(1, 1)
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_wrong_token_is_transport_error(mock_app, monkeypatch):
    monkeypatch.setattr(ab, "APP_TOKEN", "wrong")
    adapter = ab.AndroidAdapter()
    with pytest.raises(ab.TransportError):
        adapter.capture_screen()


def test_app_missing_token_is_transport_error(mock_app, monkeypatch):
    monkeypatch.setattr(ab, "APP_TOKEN", "")
    adapter = ab.AndroidAdapter()
    with pytest.raises(ab.TransportError):
        adapter.get_current_state()


def test_app_notify_posts_title_and_text(mock_app):
    """v1.3: notify posts to /notify without the --allow-actions gate
    (it only shows a notification; it cannot change phone state)."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    result = adapter.notify("Muse Bridge の更新", "Muse_Bridge_v1.3.apk が届いたよ。")
    assert result["ok"] is True
    posts = MockAppHandler.requests
    assert ("POST", "/notify",
            {"title": "Muse Bridge の更新", "text": "Muse_Bridge_v1.3.apk が届いたよ。"}) in posts


def test_app_notify_error_surfaces(mock_app, monkeypatch):
    """A 422 (notifications not permitted) must reach the caller."""
    adapter = ab.AndroidAdapter(actions_enabled=False)

    def fake_post(path, payload):
        raise ab.TransportError("HTTP 422: notifications not permitted")
    monkeypatch.setattr(adapter, "_app_post", fake_post)
    with pytest.raises(ab.TransportError, match="notifications not permitted"):
        adapter.notify("t", "x")


def test_app_click_node_and_type_node_send_json(mock_app):
    """Phase 2 (v1.5): node actions post node_id + tree_hash."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.click_node("n5", 123456)["ok"] is True
    assert adapter.type_node("n1", 123456, "hello")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/click_node", {"node_id": "n5", "tree_hash": 123456}) in posts
    assert ("POST", "/type_node",
            {"node_id": "n1", "tree_hash": 123456, "text": "hello"}) in posts


def test_app_batch_posts_actions(mock_app):
    """Phase 2 (v1.5): batch posts batch_id + tree_hash + actions."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    actions = [
        {"action_id": "a1", "type": "type_node", "node_id": "n1", "text": "MUSEテスト"},
        {"action_id": "a2", "type": "type_node", "node_id": "n1", "text": ""},
    ]
    result = adapter.batch("b-001", 123456, actions)
    assert result["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    # C-04: the batch carries the operation_context of the snapshot
    # it acts on (first action — bootstrap, no session_id).
    assert ("POST", "/batch",
            {"batch_id": "b-001", "protocol_version": 1,
             "tree_hash": 123456, "actions": actions,
             "operation_context": {"expected_package": "com.example", "tree_hash": 123456, "generation": 7}}) in posts


def test_app_batch_response_fields_v19(mock_app):
    """A-4 (v1.9): the batch response carries status, payload_hash,
    completed_action_ids, and failed_action_id. Step 4: the mock's
    payload_hash is the real content hash, so it must match the
    hash the client computed for the same payload."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    actions = [{"action_id": "a1", "type": "tap", "x": 1, "y": 2}]
    result = adapter.batch("b-002", 123456, actions)
    expected_hash = ab.bridge_protocol.BatchRegistry.compute_content_hash(
        1, 123456, actions)
    assert result["status"] == "completed"
    assert result["payload_hash"] == expected_hash
    assert result["completed_action_ids"] == ["a1"]
    assert result["failed_action_id"] is None
    assert result["replayed"] is False


def test_step3_invalid_inputs_rejected_before_request(mock_app):
    """Step 3: the protocol builders validate client-side, so a bad
    input raises ProtocolError and NO request is sent to the phone."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    ProtocolError = ab.bridge_protocol.ProtocolError
    cases = [
        lambda: adapter.click(-1, 0),
        lambda: adapter.click(0, -5),
        lambda: adapter.swipe(0, 0, -1, 0),
        lambda: adapter.swipe(0, 0, 10, 10, duration_ms=-1),
        lambda: adapter.type_text(123),
        lambda: adapter.press_key("VOLUME_UP"),
        lambda: adapter.press_key(""),
        lambda: adapter.click_node("", 123456),
        lambda: adapter.type_node("n1", 123456, 123),
        lambda: adapter.batch("b-1", 123456, []),
        lambda: adapter.batch("b-1", 123456,
                              [{"action_id": "a1", "type": "double_tap",
                                "x": 1, "y": 2}]),
        lambda: adapter.batch("b-1", 123456,
                              [{"action_id": f"a{i}", "type": "wait",
                                "wait_ms": 1} for i in range(21)]),
        lambda: adapter.notify(123, "text"),
    ]
    for call in cases:
        with pytest.raises(ProtocolError):
            call()
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_step3_wired_paths_post_validated_bodies(mock_app):
    """Step 3: the wired paths send the protocol builders' output —
    the same payloads as before, now validated client-side."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    adapter.click(100, 200)
    adapter.swipe(0, 0, 10, 20, duration_ms=150)
    adapter.type_text("hi")
    adapter.press_key("DPAD_DOWN")
    adapter.notify("Title", "Body")
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    # C-04: guarded POSTs carry the operation_context (click
    # bootstraps; the rest carry the retained session id).
    assert ("POST", "/click", {"x": 100, "y": 200,
                               "operation_context": {"expected_package": "com.example", "tree_hash": 123456, "generation": 7}}) in posts
    assert ("POST", "/swipe", {"x1": 0, "y1": 0, "x2": 10, "y2": 20,
                               "duration_ms": 150,
                               "operation_context": {"expected_package": "com.example", "tree_hash": 123456, "generation": 7, "session_id": "mock-session-1"}}) in posts
    assert ("POST", "/type_text", {"text": "hi",
                                   "operation_context": {"expected_package": "com.example", "tree_hash": 123456, "generation": 7, "session_id": "mock-session-1"}}) in posts
    assert ("POST", "/press_key", {"key": "DPAD_DOWN",
                                   "operation_context": {"expected_package": "com.example", "tree_hash": 123456, "generation": 7, "session_id": "mock-session-1"}}) in posts
    assert ("POST", "/notify", {"title": "Title", "text": "Body"}) in posts


def test_step3_query_validation_rejects_before_request(mock_app):
    """Step 3 (P1-03, CONS-019): the GET query builders validate
    client-side, so a bad query raises ProtocolError and NO request
    is sent. A good query sends the exact string the server expects."""
    adapter = ab.AndroidAdapter()
    ProtocolError = ab.bridge_protocol.ProtocolError
    with pytest.raises(ProtocolError):
        adapter.events(since=-1)
    with pytest.raises(ProtocolError):
        adapter.events(limit=201)
    with pytest.raises(ProtocolError):
        adapter.capture_screen(scale=0.05)
    with pytest.raises(ProtocolError):
        adapter.capture_screen(format="gif")
    assert MockAppHandler.requests == []
    adapter.events()
    adapter.capture_screen()
    gets = [r for r in MockAppHandler.requests if r[0] == "GET"]
    assert ("GET", "/events?since=0&limit=100", None) in gets
    assert ("GET", "/capture_screen?scale=0.5&format=jpeg&quality=80",
            None) in gets


def test_step4_batch_registry_enforced_by_adapter(mock_app):
    """Step 4 (v1.2.0): the adapter's batch() goes through the
    registry. A second call with the same batch_id and different
    content raises STALE_REVISION and sends NO request. C-01
    (SPEC-BATCH-PERSIST-001 §2.9): a second call with identical
    content is ALSO refused — the VM never reuses a batch_id
    (BATCH_ID_REUSED, no request sent). The old behaviour (an
    identical re-send answered from the server cache) is superseded
    by the SPEC."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    ProtocolError = ab.bridge_protocol.ProtocolError
    actions = [{"action_id": "a1", "type": "wait", "wait_ms": 500}]
    result = adapter.batch("b-reg", 123456, actions)
    assert result["status"] == "completed"
    record = adapter._batch_registry.get("b-reg")
    assert record["status"] == "completed"
    assert record["payload_hash"] == result["payload_hash"]
    assert record["completed_action_ids"] == ["a1"]
    posts_before = len([r for r in MockAppHandler.requests
                        if r[0] == "POST" and r[1] == "/batch"])
    with pytest.raises(ProtocolError) as ctx:
        adapter.batch("b-reg", 123456,
                      [{"action_id": "a1", "type": "wait", "wait_ms": 100}])
    assert ctx.value.code == "STALE_REVISION"
    posts_after = len([r for r in MockAppHandler.requests
                       if r[0] == "POST" and r[1] == "/batch"])
    assert posts_after == posts_before  # nothing was sent
    # An identical re-send is refused too (C-01: the VM never
    # reuses a batch_id), and nothing is sent.
    with pytest.raises(ProtocolError) as ctx:
        adapter.batch("b-reg", 123456, actions)
    assert ctx.value.code == "BATCH_ID_REUSED"
    posts_after2 = len([r for r in MockAppHandler.requests
                        if r[0] == "POST" and r[1] == "/batch"])
    assert posts_after2 == posts_before  # nothing was sent
    # The outbox still holds the first (completed) outcome.
    outbox_record = adapter._batch_outbox().get_by_batch_id("b-reg")
    assert outbox_record is not None
    assert outbox_record["state"] == "result_received"


def test_step4_batch_id_mismatch_rejected(mock_app):
    """Step 4 (CONS-020 P0-04): when the server answers with a
    batch_id different from the one that was asked for, batch()
    raises BATCH_ID_MISMATCH and does NOT record the result."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    ProtocolError = ab.bridge_protocol.ProtocolError
    MockAppHandler.batch_id_override = "some-other-batch"
    actions = [{"action_id": "a1", "type": "wait", "wait_ms": 500}]
    with pytest.raises(ProtocolError) as ctx:
        adapter.batch("b-mismatch", 123456, actions)
    assert ctx.value.code == "BATCH_ID_MISMATCH"
    # Nothing was recorded for the requested id (the outcome
    # belongs to a different batch).
    record = adapter._batch_registry.get("b-mismatch")
    assert record["status"] is None
    assert record["payload_hash"] is None
    # C-01 (SPEC §2.9): the response is unusable, so our batch's
    # outcome is unknown — the outbox records it as undetermined.
    outbox_record = adapter._batch_outbox().get_by_batch_id("b-mismatch")
    assert outbox_record is not None
    assert outbox_record["state"] == "outcome_unknown"


def test_app_batch_server_unknown_is_never_resent(mock_app):
    """C-01 (SPEC §2.10): when the server answers status "unknown",
    batch() raises BATCH_OUTCOME_UNKNOWN, the outbox records the
    logical batch as undetermined, and nothing is re-sent. The
    in-memory registry records the unknown outcome (no payload
    hash)."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    ProtocolError = ab.bridge_protocol.ProtocolError
    MockAppHandler.batch_status_override = "unknown"
    actions = [{"action_id": "a1", "type": "wait", "wait_ms": 500}]
    with pytest.raises(ProtocolError) as ctx:
        adapter.batch("b-unknown", 123456, actions)
    assert ctx.value.code == "BATCH_OUTCOME_UNKNOWN"
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/batch"]
    assert len(posts) == 1  # sent once, never re-sent
    record = adapter._batch_registry.get("b-unknown")
    assert record["status"] == "unknown"
    assert record["payload_hash"] is None
    outbox_record = adapter._batch_outbox().get_by_batch_id("b-unknown")
    assert outbox_record is not None
    assert outbox_record["state"] == "outcome_unknown"


def test_app_phase2_actions_refused_without_flag(mock_app):
    """Node actions and batch need --allow-actions, like other actions."""
    adapter = ab.AndroidAdapter()
    with pytest.raises(PermissionError):
        adapter.click_node("n5", 123456)
    with pytest.raises(PermissionError):
        adapter.type_node("n1", 123456, "x")
    with pytest.raises(PermissionError):
        adapter.batch("b-001", 123456, [])
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_no_transport_argument():
    """Decision 60: the transport parameter is gone — passing it is
    a TypeError, not a ValueError for an unknown value."""
    with pytest.raises(TypeError):
        ab.AndroidAdapter(transport="app")


def test_app_state_event_seq_v16(mock_app):
    """Phase 3 (v1.6): /state carries the latest event seq, so the
    client can tell from /state alone whether anything happened
    since its last /events poll."""
    adapter = ab.AndroidAdapter()
    state = adapter.get_current_state()
    assert state["event_seq"] == 42


def test_app_state_app_version_v195(mock_app):
    """v1.9: /state and /health carry the installed app version, so
    the VM can tell which build is on the phone without guessing
    from behaviour. The version comes from the APK itself (aapt2
    --version-name/--version-code), never a hardcoded constant."""
    adapter = ab.AndroidAdapter()
    state = adapter.get_current_state()
    assert state["app_version"] == "1.9.5"
    assert state["app_version_code"] == 15
    import urllib.request
    req = urllib.request.Request(
        f"{ab.APP_URL}/health", headers={"X-Bridge-Token": MOCK_TOKEN})
    with urllib.request.urlopen(req, timeout=10) as resp:
        health = json.loads(resp.read().decode("utf-8"))
    assert health["app_version"] == "1.9.5"
    assert health["app_version_code"] == 15
    # The old protocol version field is untouched.
    assert health["version"] == 1


@pytest.mark.parametrize("code", ["400", "408", "413", "431"])
def test_app_http_input_errors_classified_v19(mock_app, code):
    """A-1 (v1.9): bridge input-validation rejections (400/408/413/
    431) reach the caller as TransportError with the input-validation
    hint, never blamed on the token."""
    adapter = ab.AndroidAdapter()
    with pytest.raises(ab.TransportError) as excinfo:
        adapter._app_request("GET", f"/state?err={code}")
    msg = str(excinfo.value)
    assert f"HTTP {code}" in msg
    assert "input validation" in msg
    assert "wrong token" not in msg


def test_app_503_hint_mentions_overload_v19(mock_app):
    """A-1 (v1.9): a 503 names the accessibility service or the
    overload, not the token."""
    adapter = ab.AndroidAdapter()
    with pytest.raises(ab.TransportError) as excinfo:
        adapter._app_request("GET", "/state?err=503")
    msg = str(excinfo.value)
    assert "HTTP 503" in msg
    assert "overloaded" in msg
    assert "wrong token" not in msg


def test_app_error_message_carries_protocol_code(mock_app):
    """MERGE_PLAN_A Step 2: the TransportError message carries the
    protocol error code from bridge_protocol.error_code_for_status,
    so callers can classify without re-parsing the status. The hints
    are unchanged (input validation / token / overload)."""
    adapter = ab.AndroidAdapter()
    cases = [
        (400, "[INVALID_REQUEST]", "input validation"),
        (401, "[UNAUTHORIZED]", "wrong token"),
        (408, "[TIMEOUT]", "input validation"),
        (413, "[TOO_LARGE]", "input validation"),
        (431, "[HEADER_TOO_LARGE]", "input validation"),
        (503, "[UNAVAILABLE]", "overloaded"),
    ]
    for code, expected_code, expected_hint in cases:
        with pytest.raises(ab.TransportError) as excinfo:
            adapter._app_request("GET", f"/state?err={code}")
        msg = str(excinfo.value)
        assert f"HTTP {code}" in msg
        assert expected_code in msg
        assert expected_hint in msg


def test_app_state_snapshot_fields_v19(mock_app):
    """A-3 (v1.9): /state carries snapshot_id/snapshot_valid/
    snapshot_stable, so the client can tell whether the snapshot it
    holds is trustworthy before acting on its node ids."""
    adapter = ab.AndroidAdapter()
    state = adapter.get_current_state()
    assert state["snapshot_id"] == 7
    assert state["snapshot_valid"] is True
    assert state["snapshot_stable"] is True


def test_app_stable_snapshot_no_refetch_v19(mock_app):
    """A-3: a stable snapshot needs no refetch before a node action."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    state = adapter.get_current_state()
    assert adapter.click_node("n5", state["tree_hash"])["ok"] is True
    gets = [r for r in MockAppHandler.requests
            if r[0] == "GET" and r[1] == "/state"]
    assert len(gets) == 1


def test_app_unstable_snapshot_refetches_same_hash_v19(mock_app):
    """A-3: an unstable snapshot is refetched before the action; when
    the tree hash is unchanged the action proceeds (with the refetched
    hash)."""
    MockAppHandler.state_mode = "unstable_then_same"
    adapter = ab.AndroidAdapter(actions_enabled=True)
    state = adapter.get_current_state()
    assert state["snapshot_stable"] is False
    assert adapter.click_node("n5", state["tree_hash"])["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/click_node", {"node_id": "n5", "tree_hash": 111}) in posts
    gets = [r for r in MockAppHandler.requests
            if r[0] == "GET" and r[1] == "/state"]
    assert len(gets) == 2


def test_app_unstable_snapshot_changed_raises_v19(mock_app):
    """A-3: an unstable snapshot whose refetch shows a changed tree
    hash raises instead of acting on stale ids (and posts nothing)."""
    MockAppHandler.state_mode = "unstable_then_changed"
    adapter = ab.AndroidAdapter(actions_enabled=True)
    state = adapter.get_current_state()
    assert state["snapshot_stable"] is False
    with pytest.raises(ab.TransportError, match="stale snapshot"):
        adapter.click_node("n5", state["tree_hash"])
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_get_retries_transient_503(mock_app, monkeypatch):
    """Client improvement 1: a GET that fails twice with 503 succeeds
    on the third attempt (reads are idempotent)."""
    monkeypatch.setattr(ab, "APP_RETRY_BASE_DELAY_S", 0.01)
    MockAppHandler.fail_next[("GET", "/state")] = 2
    adapter = ab.AndroidAdapter()
    state = adapter.get_current_state()
    assert state["tree_hash"] == 123456
    gets = [r for r in MockAppHandler.requests
            if r[0] == "GET" and r[1] == "/state"]
    assert len(gets) == 3


def test_app_batch_never_retries_transient_503(mock_app, monkeypatch):
    """C-01 (SPEC-BATCH-PERSIST-001 §2.9): POST /batch is NEVER
    retried. A transient 503 means the outcome is undetermined —
    the batch is sent exactly once, the outbox records
    "outcome_unknown", and batch() raises BATCH_OUTCOME_UNKNOWN
    (never re-sending the same batch_id, never re-issuing the
    logical batch under a new ID). The old A-4 retry behaviour is
    superseded by the SPEC."""
    monkeypatch.setattr(ab, "APP_RETRY_BASE_DELAY_S", 0.01)
    MockAppHandler.fail_next[("POST", "/batch")] = 2
    adapter = ab.AndroidAdapter(actions_enabled=True)
    ProtocolError = ab.bridge_protocol.ProtocolError
    with pytest.raises(ProtocolError) as ctx:
        adapter.batch("retry-1", 123456,
                      [{"action_id": "a1", "type": "wait", "wait_ms": 100}])
    assert ctx.value.code == "BATCH_OUTCOME_UNKNOWN"
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/batch"]
    assert len(posts) == 1  # exactly once — no retry
    assert all(p[2]["batch_id"] == "retry-1" for p in posts)
    # The outbox recorded the undetermined outcome.
    record = adapter._batch_outbox().get_by_batch_id("retry-1")
    assert record is not None
    assert record["state"] == "outcome_unknown"


def test_app_click_does_not_retry(mock_app, monkeypatch):
    """Client improvement 1: a non-idempotent POST (click) runs
    exactly once — a retry could double-tap."""
    monkeypatch.setattr(ab, "APP_RETRY_BASE_DELAY_S", 0.01)
    MockAppHandler.fail_next[("POST", "/click")] = 1
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.TransportError):
        adapter.click(100, 100)
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/click"]
    assert len(posts) == 1


def test_app_get_fails_fast_on_400(mock_app, monkeypatch):
    """Client improvement 1: a 4xx is not retried — it cannot heal."""
    monkeypatch.setattr(ab, "APP_RETRY_BASE_DELAY_S", 0.01)
    adapter = ab.AndroidAdapter()
    with pytest.raises(ab.TransportError):
        adapter._app_request("GET", "/state?err=400")
    gets = [r for r in MockAppHandler.requests
            if r[0] == "GET" and r[1].startswith("/state?err=400")]
    assert len(gets) == 1


def test_bridge_error_classifier():
    """Client improvement 1: the retry classifier (unit test)."""
    assert ab._is_retryable_bridge_error(ab.TransportError(
        "app returned HTTP 503 for GET /health: {} (overloaded)"))
    assert ab._is_retryable_bridge_error(ab.TransportError(
        "app returned HTTP 500 for GET /state: {} (unexpected bridge error)"))
    assert not ab._is_retryable_bridge_error(ab.TransportError(
        "app returned HTTP 409 for POST /batch: {} (unexpected bridge error)"))
    assert not ab._is_retryable_bridge_error(ab.TransportError(
        "app returned HTTP 401 for GET /state: {} (wrong token)"))
    assert not ab._is_retryable_bridge_error(ab.TransportError(
        "app returned HTTP 422 for POST /click_node: {} (unexpected bridge error)"))
    reset = ab.TransportError("cannot reach the app at http://127.0.0.1:8080")
    reset.__cause__ = ConnectionResetError(104, "Connection reset by peer")
    assert ab._is_retryable_bridge_error(reset)
    refused = ab.TransportError("cannot reach the app at http://127.0.0.1:8080")
    refused.__cause__ = ConnectionRefusedError(111, "Connection refused")
    assert ab._is_retryable_bridge_error(refused)
    bad = ab.TransportError("cannot reach the app at http://127.0.0.1:8080")
    bad.__cause__ = ValueError("bad value")
    assert not ab._is_retryable_bridge_error(bad)


def test_bridge_error_classifier_new_format():
    """Step 2 (P1-02): the retry classifier also reads the NEW
    message format with the protocol error code in brackets."""
    assert ab._is_retryable_bridge_error(ab.TransportError(
        "app returned HTTP 503 [UNAVAILABLE] for GET /health: {} "
        "(accessibility service not enabled, or the bridge is overloaded)"))
    assert ab._is_retryable_bridge_error(ab.TransportError(
        "app returned HTTP 500 [INTERNAL_ERROR] for GET /state: {} "
        "(unexpected bridge error)"))
    assert not ab._is_retryable_bridge_error(ab.TransportError(
        "app returned HTTP 400 [INVALID_REQUEST] for GET /state: {} "
        "(request rejected by the bridge input validation "
        "(bad request, timeout, or too large))"))
    assert not ab._is_retryable_bridge_error(ab.TransportError(
        "app returned HTTP 401 [UNAUTHORIZED] for GET /state: {} "
        "(wrong token, or the token changed on the phone)"))
    # P0-4 deny list: a 403 (package on the deny list) is a
    # policy refusal, never retried.
    assert not ab._is_retryable_bridge_error(ab.TransportError(
        "app returned HTTP 403 [FORBIDDEN] for POST /launch_app: "
        '{"ok": false, "error": "launch denied: com.kasikorn.'
        'retail.mbanking.wap is on the deny list"} (forbidden — '
        "the bridge refused the request (e.g. a launch "
        "deny-listed package))"))


def test_app_last_call_records_success(mock_app):
    """Decision 59: after a call, last_call holds the measurement."""
    adapter = ab.AndroidAdapter()
    assert adapter.last_call is None
    state = adapter.get_current_state()
    assert state["tree_hash"] == 123456
    call = adapter.last_call
    assert call["method"] == "GET" and call["path"] == "/state"
    assert call["ok"] is True and call["status"] == 200
    assert call["attempts"] == 1 and call["error"] is None
    assert isinstance(call["seconds"], float) and call["seconds"] >= 0


def test_app_last_call_records_retry(mock_app, monkeypatch):
    """Decision 59: last_call counts the real attempts (incl. retries)."""
    monkeypatch.setattr(ab, "APP_RETRY_BASE_DELAY_S", 0.01)
    MockAppHandler.fail_next[("GET", "/state")] = 2
    adapter = ab.AndroidAdapter()
    adapter.get_current_state()
    call = adapter.last_call
    assert call["attempts"] == 3 and call["ok"] is True


def test_app_last_call_records_failure(mock_app):
    """Decision 59: a failed call still records attempts + status."""
    adapter = ab.AndroidAdapter()
    with pytest.raises(ab.TransportError):
        adapter._app_request("GET", "/state?err=400")
    call = adapter.last_call
    assert call["ok"] is False and call["status"] == 400
    assert call["attempts"] == 1 and "400" in call["error"]


def test_app_timeout_env_validation(monkeypatch):
    """Decision 59: env overrides are validated, never silently defaulted."""
    monkeypatch.setenv("ANDROID_APP_TIMEOUT_READ_S", "45")
    assert ab._timeout_env("ANDROID_APP_TIMEOUT_READ_S", 30.0) == 45.0
    monkeypatch.setenv("ANDROID_APP_TIMEOUT_READ_S", "300")
    assert ab._timeout_env("ANDROID_APP_TIMEOUT_READ_S", 30.0) == 300.0
    monkeypatch.delenv("ANDROID_APP_TIMEOUT_READ_S", raising=False)
    assert ab._timeout_env("ANDROID_APP_TIMEOUT_READ_S", 30.0) == 30.0
    for bad in ("abc", "0", "-5", "301", "1e6"):
        monkeypatch.setenv("ANDROID_APP_TIMEOUT_READ_S", bad)
        with pytest.raises(ValueError):
            ab._timeout_env("ANDROID_APP_TIMEOUT_READ_S", 30.0)


def test_app_batch_uses_60s_timeout(mock_app, monkeypatch):
    """Decision 59: POST /batch gets the 60 s timeout, reads get 30 s."""
    seen = []
    real_opener = ab._app_opener()

    class RecOpener:
        def open(self, req, timeout=None):
            seen.append((req.get_method(), req.full_url, timeout))
            return real_opener.open(req, timeout=timeout)

    monkeypatch.setattr(ab, "_app_opener", lambda: RecOpener())
    adapter = ab.AndroidAdapter(actions_enabled=True)
    adapter.get_current_state()
    adapter.batch("t-1", 123456, [{"action_id": "a1", "type": "wait", "wait_ms": 10}])
    get_timeout = [t for (m, u, t) in seen if m == "GET" and "/state" in u]
    post_timeout = [t for (m, u, t) in seen if m == "POST" and "/batch" in u]
    assert get_timeout == [30.0]
    assert post_timeout == [60.0]


def test_app_slow_call_note(mock_app, monkeypatch, capsys):
    """Decision 59: a single attempt over the slow threshold is noted."""
    monkeypatch.setattr(ab, "APP_SLOW_CALL_S", 0.0)
    adapter = ab.AndroidAdapter()
    adapter.get_current_state()
    assert "slow call GET /state" in capsys.readouterr().err


def test_app_logs_returns_lines_v193(mock_app):
    """v1.9.3: /logs returns the current run's lines plus the
    previous run's tail, through the adapter."""
    adapter = ab.AndroidAdapter()
    result = adapter.logs()
    assert result["lines"][1] == "[11-06 03:55:00] server RUNNING"
    assert result["previous_lines"] == ["[11-06 03:50:00] server STOPPED"]
    gets = [r for r in MockAppHandler.requests
            if r[0] == "GET" and r[1].startswith("/logs")]
    assert len(gets) == 1


def test_app_logs_never_carry_full_ips_or_packages(mock_app):
    """P1-03 (CONS-014): no log line may carry a full IPv4
    address, a package name, a URL, or a file name — IPs are
    masked to two octets by BridgeLog.sanitize, and package
    names / URLs / file names / exception messages are dropped
    at the call sites. The listen and bind-failed lines carry
    the masked form; accept-failed carries an exception class
    name only.

    HONEST LIMIT (CONS-032): this test verifies the CLIENT-
    VISIBLE contract against the mock server's fixed lines —
    it proves the client accepts the sanitized shape and that
    the shape itself contains no PII. It does NOT verify the
    Java sanitize behaviour (BridgeLog.sanitize, the call-site
    drops, or the raw android.util.Log calls in
    HttpBridgeServer.kt) and it does NOT read actual Logcat
    output. Those are verified by direct code inspection (the
    CONS-032 fix round audited every raw Log call) and by the
    Java build; a live-phone Logcat capture is still the only
    way to observe the real Logcat output.
    """
    import re
    adapter = ab.AndroidAdapter()
    result = adapter.logs()
    lines = result["lines"] + result["previous_lines"]
    assert lines[0] == "[11-06 03:55:00] listen 100.111.*.*:8080"
    assert lines[3] == "[11-06 03:55:02] bind-failed 100.111.*.*: BindException"
    assert lines[4] == "[11-06 03:55:03] accept-failed SocketException"
    ipv4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
    for line in lines:
        assert not ipv4.search(line), line
        assert "com." not in line, line
        assert "http://" not in line and "https://" not in line, line
        assert ".apk" not in line, line


def test_app_events_returns_history(mock_app):
    """Phase 3 (v1.6): /events returns the recorded history with
    seq, type, package, text, bounds, and the coalesced count;
    password text arrives already redacted."""
    adapter = ab.AndroidAdapter()
    result = adapter.events()
    assert result["latest_seq"] == 2
    assert result["dropped"] is False
    assert result["tree_hash"] == 123456
    assert result["transport"] == "app"
    events = result["events"]
    assert len(events) == 2
    assert events[0]["type"] == "TYPE_WINDOW_STATE_CHANGED"
    assert events[0]["seq"] == 1
    pw = events[1]
    assert pw["type"] == "TYPE_VIEW_TEXT_CHANGED"
    assert pw["text"] == "[REDACTED]"
    assert pw["coalesced"] == 3
    assert pw["bounds"] == {"left": 100, "top": 400, "right": 980, "bottom": 500}
    gets = [r for r in MockAppHandler.requests if r[0] == "GET"]
    assert "/events?since=0&limit=100" in gets[0][1]


def test_app_events_since_filters(mock_app):
    adapter = ab.AndroidAdapter()
    result = adapter.events(since=1, limit=1)
    assert [e["seq"] for e in result["events"]] == [2]
    gets = [r for r in MockAppHandler.requests if r[0] == "GET"]
    assert "since=1" in gets[0][1] and "limit=1" in gets[0][1]


def test_app_events_is_read_only(mock_app):
    """Events need no --allow-actions gate: they only report what
    happened on the phone."""
    adapter = ab.AndroidAdapter()
    assert adapter.events()["ok"] is True




# --------------------------------------------------------------------------
# Decision 61: wait_for_text / click_text.
# --------------------------------------------------------------------------

def test_wait_for_text_finds_node_on_first_poll(mock_app):
    """Decision 61: the node is there — return it with its tree_hash."""
    MockAppHandler.state_mode = "text_after"
    MockAppHandler.text_after = 0
    adapter = ab.AndroidAdapter()
    node = adapter.wait_for_text("MUSE用")
    assert node["id"] == "n3" and node["tree_hash"] == 123456
    # The node dict is a copy: the cached state is not polluted.
    assert "tree_hash" not in adapter._last_state["nodes"][3]


def test_wait_for_text_waits_for_node(mock_app):
    """Decision 61: the node appears on the 3rd /state — wait for it."""
    MockAppHandler.state_mode = "text_after"
    MockAppHandler.text_after = 2
    adapter = ab.AndroidAdapter()
    node = adapter.wait_for_text("MUSE用", timeout_s=15.0, poll_s=1.0)
    assert node["id"] == "n3"
    assert MockAppHandler.state_calls == 3


def test_wait_for_text_contains_and_desc_not_matched(mock_app):
    """Decision 61: contains matches substrings; desc is never matched
    (n5 has desc "MUSE用" and empty text, and precedes n3)."""
    MockAppHandler.state_mode = "text_after"
    MockAppHandler.text_after = 0
    adapter = ab.AndroidAdapter()
    assert adapter.wait_for_text("MUSE", match="contains")["id"] == "n3"
    assert adapter.wait_for_text("MUSE用", match="exact")["id"] == "n3"


def test_wait_for_text_timeout(mock_app):
    """Decision 61: nothing appears — TimeoutError, not TransportError."""
    MockAppHandler.state_mode = "text_after"
    MockAppHandler.text_after = 0
    adapter = ab.AndroidAdapter()
    with pytest.raises(TimeoutError) as excinfo:
        adapter.wait_for_text("ないもの", timeout_s=1.0, poll_s=1.0)
    assert "ないもの" in str(excinfo.value) and "1 polls" in str(excinfo.value)


def test_wait_for_text_invalid_args(mock_app):
    """Decision 61: out-of-range args fail fast, before any polling."""
    adapter = ab.AndroidAdapter()
    for kwargs in ({"timeout_s": 0}, {"timeout_s": 61}, {"timeout_s": -1},
                   {"poll_s": 0.5}, {"match": "regex"}):
        with pytest.raises(ValueError):
            adapter.wait_for_text("MUSE用", **kwargs)
    assert MockAppHandler.state_calls == 0


def test_click_text_success(mock_app):
    """Decision 61: find by text, click once, return node_id + attempts."""
    MockAppHandler.state_mode = "text_after"
    MockAppHandler.text_after = 0
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.click_text("MUSE用")
    assert result["ok"] is True
    assert result["node_id"] == "n3" and result["attempts"] == 1
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert posts == [("POST", "/click_node",
                      {"node_id": "n3", "tree_hash": 123456})]


def test_click_text_is_gated(mock_app):
    """Decision 61: click_text needs the actions gate, like click_node."""
    MockAppHandler.state_mode = "text_after"
    MockAppHandler.text_after = 0
    adapter = ab.AndroidAdapter(actions_enabled=False)
    with pytest.raises(PermissionError):
        adapter.click_text("MUSE用")


def test_click_text_409_then_success(mock_app):
    """Decision 61: a 409 means the server refused before acting —
    re-find by text and click again (attempts == 2)."""
    MockAppHandler.state_mode = "text_after"
    MockAppHandler.text_after = 0
    MockAppHandler.fail_next[("POST", "/click_node")] = (409, 1)
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.click_text("MUSE用")
    assert result["ok"] is True and result["attempts"] == 2
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/click_node"]
    assert len(posts) == 2


def test_click_text_422_race_then_success(mock_app):
    """Decision 61: a 422 on a node that IS clickable + enabled in a
    fresh state was a race — re-find and click again."""
    MockAppHandler.state_mode = "text_after"
    MockAppHandler.text_after = 0
    MockAppHandler.fail_next[("POST", "/click_node")] = (422, 1)
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.click_text("MUSE用")
    assert result["ok"] is True and result["attempts"] == 2


def test_click_text_422_node_not_clickable_raises(mock_app):
    """Decision 61: a 422 on a node that is present but has no
    clickable + enabled node or ancestor is a real failure — raise
    at once, and do NOT re-send the click."""
    MockAppHandler.state_mode = "text_after"
    MockAppHandler.text_after = 0
    MockAppHandler.fail_next[("POST", "/click_node")] = (422, 1)
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.TransportError) as excinfo:
        adapter.click_text("押せない")
    assert "cannot be clicked" in str(excinfo.value)
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/click_node"]
    assert len(posts) == 1


# --------------------------------------------------------------------------
# Decision 62: device state in /state (v1.9.6).
# --------------------------------------------------------------------------

def test_app_state_device_object(mock_app):
    """Decision 62: /state carries the nested device object with the
    four frozen fields (battery_pct, charging, network, interactive)."""
    adapter = ab.AndroidAdapter()
    state = adapter.get_current_state()
    device = state["device"]
    assert device == {"battery_pct": 87, "charging": True,
                      "network": "wifi", "interactive": True}
    # No new client method: the dict flows through get_current_state().
    assert not hasattr(adapter, "device")


# --------------------------------------------------------------------------
# Decision 63: notification reading (v1.9.7).
# --------------------------------------------------------------------------

def test_app_notifications_fetch(mock_app):
    """Decision 63: /notifications returns the event stream: a post,
    an update of the same key, an ongoing post, and a tombstone
    removal (title/text empty, removed_t set)."""
    adapter = ab.AndroidAdapter()
    result = adapter.notifications()
    assert result["ok"] is True
    assert result["latest_seq"] == 4
    assert result["dropped"] is False
    events = result["events"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert [e["type"] for e in events] == ["posted", "updated",
                                           "posted", "removed"]
    assert events[1]["key"] == events[0]["key"]  # update, same key
    assert events[2]["ongoing"] is True
    tombstone = events[3]
    assert tombstone["key"] == events[0]["key"]
    assert tombstone["title"] == "" and tombstone["text"] == ""
    assert tombstone["removed_t"] == 1700000010000


def test_app_notifications_since_sees_tombstone(mock_app):
    """Decision 63: a client that already fetched seq=3 still learns
    of the removal via since=3 — the tombstone has its own seq."""
    adapter = ab.AndroidAdapter()
    result = adapter.notifications(since=3)
    assert [e["seq"] for e in result["events"]] == [4]
    assert result["events"][0]["type"] == "removed"


def test_app_notifications_package_filter(mock_app):
    """Decision 63: ?package= filters client-side; the server still
    returns every package when no filter is given."""
    adapter = ab.AndroidAdapter()
    result = adapter.notifications(package="com.example.music")
    assert [e["seq"] for e in result["events"]] == [3]
    assert all(e["package"] == "com.example.music"
               for e in result["events"])


def test_app_notifications_limit(mock_app):
    """Decision 63: limit caps the returned events."""
    adapter = ab.AndroidAdapter()
    result = adapter.notifications(limit=2)
    assert [e["seq"] for e in result["events"]] == [1, 2]
    assert result["latest_seq"] == 4


def test_app_notifications_is_read_only(mock_app):
    """Decision 63: notifications() needs no --allow-actions gate,
    like events()."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    assert adapter.notifications()["ok"] is True


def test_app_health_notification_listener_flag(mock_app):
    """Decision 63: /health carries notification_listener (the grant,
    not the connection state)."""
    assert ab.check_app()["notification_listener"] is True


# --------------------------------------------------------------------------
# Decision 66: DPAD keys in press_key (v1.9.9).
# --------------------------------------------------------------------------

def test_app_press_key_dpad(mock_app):
    """Decision 66: press_key accepts the DPAD keys (they move focus;
    scrolling is a side effect in lists, app-dependent). The mock
    echoes any POST, so this pins the client-side pass-through."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    for key in ("DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
                "DPAD_CENTER"):
        assert adapter.press_key(key)["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    # C-04: DPAD_DOWN is the 2nd press — the 1st bootstrapped the
    # session, so this one carries the retained session id.
    assert ("POST", "/press_key", {"key": "DPAD_DOWN",
                                   "operation_context": {"expected_package": "com.example", "tree_hash": 123456, "generation": 7, "session_id": "mock-session-1"}}) in posts


# --------------------------------------------------------------------------
# Decision 67: scroll_node (v1.9.10).
# --------------------------------------------------------------------------

def test_app_scroll_node_forward(mock_app):
    """Decision 67: scroll_node posts node_id + tree_hash + direction
    and returns ok; the mock echoes any POST, so this pins the
    client-side request shape."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.scroll_node("n3", 123456, "forward")
    assert result["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/scroll_node",
            {"node_id": "n3", "tree_hash": 123456,
             "direction": "forward"}) in posts


def test_app_scroll_node_backward_default(mock_app):
    """Decision 67: direction defaults to forward; backward passes
    through as given."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.scroll_node("n3", 123456)["ok"] is True
    assert adapter.scroll_node("n3", 123456, "backward")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/scroll_node",
            {"node_id": "n3", "tree_hash": 123456,
             "direction": "backward"}) in posts


def test_app_scroll_node_bad_direction(mock_app):
    """Decision 67: an unknown direction raises ValueError before any
    request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="unknown direction"):
        adapter.scroll_node("n3", 123456, "sideways")
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_scroll_node_needs_gate(mock_app):
    """Decision 67: scroll_node is an action (it moves the screen),
    so it needs --allow-actions like click and swipe."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    with pytest.raises(PermissionError):
        adapter.scroll_node("n3", 123456)


def test_app_scroll_node_missing_direction_is_400(mock_app):
    """P1-04 (CONS-014): a missing direction is a 400, not a
    silent scroll forward. The client always sends a direction,
    so this posts raw (bypassing the client method)."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.TransportError, match="missing direction"):
        adapter._app_post("/scroll_node",
                          {"node_id": "n3", "tree_hash": 123456})


def test_app_scroll_node_bad_direction_raw_is_400(mock_app):
    """P1-04 (CONS-014): the value validation is unchanged — a
    present but unknown direction is still a 400."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.TransportError, match="unknown direction"):
        adapter._app_post("/scroll_node",
                          {"node_id": "n3", "tree_hash": 123456,
                           "direction": "sideways"})


def test_app_scroll_node_with_direction_raw(mock_app):
    """P1-04 (CONS-014): a present, valid direction scrolls."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter._app_post("/scroll_node",
                             {"node_id": "n3", "tree_hash": 123456,
                              "direction": "backward"})["ok"] is True


# --------------------------------------------------------------------------
# Decision 68: generic node_action (v1.9.11).
# --------------------------------------------------------------------------

def test_app_node_action_no_arg(mock_app):
    """Decision 68: node_action posts node_id + tree_hash + action +
    args for a no-arg action (long_click)."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.node_action("n3", 123456, "long_click")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/node_action",
            {"node_id": "n3", "tree_hash": 123456,
             "action": "long_click", "args": {}}) in posts


def test_app_node_action_directional_scroll(mock_app):
    """Decision 68: scroll_down / page_down pass through as actions."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.node_action("n3", 123456, "scroll_down")["ok"] is True
    assert adapter.node_action("n3", 123456, "page_down")["ok"] is True


def test_app_node_action_with_args(mock_app):
    """Decision 68: set_selection and set_progress carry their args."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.node_action(
        "n3", 123456, "set_selection",
        {"start": 2, "end": 5})["ok"] is True
    assert adapter.node_action(
        "n3", 123456, "set_progress", {"value": 0.5})["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/node_action",
            {"node_id": "n3", "tree_hash": 123456,
             "action": "set_selection",
             "args": {"start": 2, "end": 5}}) in posts
    assert ("POST", "/node_action",
            {"node_id": "n3", "tree_hash": 123456,
             "action": "set_progress", "args": {"value": 0.5}}) in posts


def test_app_node_action_unknown(mock_app):
    """Decision 68: an unknown action name raises ValueError before
    any request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="unknown action"):
        adapter.node_action("n3", 123456, "explode")
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_node_action_missing_arg(mock_app):
    """Decision 68: a missing required arg raises ValueError before
    any request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="needs argument"):
        adapter.node_action("n3", 123456, "set_selection", {"start": 2})
    with pytest.raises(ValueError, match="needs argument"):
        adapter.node_action("n3", 123456, "set_progress")
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_node_action_needs_gate(mock_app):
    """Decision 68: node_action is an action (it changes the screen),
    so it needs --allow-actions."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    with pytest.raises(PermissionError):
        adapter.node_action("n3", 123456, "focus")


def test_app_node_action_all_names_known(mock_app):
    """Decision 68: every name in NODE_ACTIONS is accepted by the
    client (the server maps them; the mock echoes any POST)."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    for name in sorted(ab.AndroidAdapter.NODE_ACTIONS):
        args = {}
        if name == "set_selection":
            args = {"start": 0, "end": 1}
        elif name == "set_progress":
            args = {"value": 0.5}
        elif name == "scroll_to_position":
            args = {"row": 0}
        elif name == "set_text":
            args = {"text": "x"}
        assert adapter.node_action("n3", 123456, name, args)["ok"] is True
    assert len(ab.AndroidAdapter.NODE_ACTIONS) == 32


def test_app_node_action_fractional_arg_rejected(mock_app):
    """P1-07 (CONS-014): a fractional number (1.8) is rejected
    instead of truncating — before any request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="needs integer"):
        adapter.node_action("n3", 123456, "set_selection",
                            {"start": 1.8, "end": 5})
    with pytest.raises(ValueError, match="needs integer"):
        adapter.node_action("n3", 123456, "scroll_to_position",
                            {"row": 2.5})
    with pytest.raises(ValueError, match="needs integer"):
        adapter.node_action("n3", 123456, "press_and_hold",
                            {"duration_ms": 500.5})
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_node_action_integral_float_accepted(mock_app):
    """P1-07 (CONS-014): an integral float (2.0) IS an integer —
    it is accepted and normalised to int on the wire."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.node_action("n3", 123456, "set_selection",
                               {"start": 2.0, "end": 5})["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/node_action",
            {"node_id": "n3", "tree_hash": 123456,
             "action": "set_selection",
             "args": {"start": 2, "end": 5}}) in posts


def test_app_node_action_range_checks(mock_app):
    """P1-07 (CONS-014): out-of-range args fail before any
    request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        adapter.node_action("n3", 123456, "set_progress", {"value": 1.5})
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        adapter.node_action("n3", 123456, "set_progress", {"value": -0.1})
    with pytest.raises(ValueError, match=">= 0"):
        adapter.node_action("n3", 123456, "scroll_to_position", {"row": -1})
    with pytest.raises(ValueError, match=">= 0"):
        adapter.node_action("n3", 123456, "set_selection",
                            {"start": -1, "end": 5})
    with pytest.raises(ValueError, match="needs duration_ms"):
        adapter.node_action("n3", 123456, "press_and_hold",
                            {"duration_ms": 0})
    with pytest.raises(ValueError, match="needs duration_ms"):
        adapter.node_action("n3", 123456, "press_and_hold",
                            {"duration_ms": 60001})
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_node_action_unknown_arg_rejected(mock_app):
    """P1-07 (CONS-014): an unknown argument key is rejected
    before any request is sent — including on a no-arg action."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="unknown argument"):
        adapter.node_action("n3", 123456, "set_progress",
                            {"value": 0.5, "speed": 2})
    with pytest.raises(ValueError, match="unknown argument"):
        adapter.node_action("n3", 123456, "click", {"force": True})
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_node_action_non_object_args_rejected(mock_app):
    """P1-07 (CONS-014): non-object args are rejected before
    any request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="args must be an object"):
        adapter.node_action("n3", 123456, "click", ["force"])
    with pytest.raises(ValueError, match="args must be an object"):
        adapter.node_action("n3", 123456, "click", "fast")
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_node_action_raw_bad_args_are_400(mock_app):
    """P1-07 (CONS-014): the server's validation — a raw request
    with non-object args, an unknown key, a fractional number,
    or an out-of-range value is a 400."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    base = {"node_id": "n3", "tree_hash": 123456}
    with pytest.raises(ab.TransportError, match="args must be an object"):
        adapter._app_post("/node_action", {**base, "action": "click",
                                           "args": "fast"})
    with pytest.raises(ab.TransportError, match="unknown argument"):
        adapter._app_post("/node_action", {**base, "action": "click",
                                           "args": {"force": True}})
    with pytest.raises(ab.TransportError, match="needs integer"):
        adapter._app_post("/node_action",
                          {**base, "action": "set_selection",
                           "args": {"start": 1.8, "end": 5}})
    with pytest.raises(ab.TransportError, match="between 0.0 and 1.0"):
        adapter._app_post("/node_action",
                          {**base, "action": "set_progress",
                           "args": {"value": 2.0}})


def test_app_node_action_large_int_arg_rejected():
    """P1-07 fix round (CONS-032) + fix round 2 (CONS-033): the
    32-bit range check must apply to Python int values too — a
    Python int is arbitrary precision, so 2**40 used to pass the
    client while the Java server's requireIntArg rejects it. The
    range matches the server exactly at both edges: -2**31
    (Integer.MIN_VALUE) is accepted, -(2**31) - 1 is rejected;
    2**31 - 1 is the largest accepted int, 2**31 is rejected.
    (No mock_app needed: the client raises before any request.)"""
    require = ab.AndroidAdapter._require_int_arg
    with pytest.raises(ValueError, match="needs integer"):
        require("scroll_to_position", "row", 2**40)
    with pytest.raises(ValueError, match="needs integer"):
        require("scroll_to_position", "row", 2**31)
    assert require("scroll_to_position", "row", 2**31 - 1) == 2**31 - 1
    with pytest.raises(ValueError, match="needs integer"):
        require("scroll_to_position", "row", -(2**31) - 1)
    assert require("scroll_to_position", "row", -(2**31)) == -(2**31)
    assert require("scroll_to_position", "row", 0) == 0


# --------------------------------------------------------------------------
# P1-01 / P1-06 source-level regression guards (CONS-032).
#
# HONEST LIMIT: these two fixes have NO Python-observable surface —
# node lifecycle (P1-01) and accept-loop threading (P1-06) are
# Java-internal, and the mock server has no equivalent. There is no
# behavioural Python test for them, and inventing one would fake a
# verification that does not exist. What these tests DO verify is
# that the fix mechanism is still present in the Java source: if
# someone removes the recycle() call or the generation check, the
# suite fails. Behavioural verification is by compilation, dex
# inspection (collectNodes contains recycle() invokes;
# acceptGeneration strings present), and code inspection; a
# live-phone test is still needed to observe them behaviourally
# (e.g. a stop -> restart cycle for P1-06).
# --------------------------------------------------------------------------

def _java_source(name):
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    return (root / "android-app" / "src" / "dev" / "muse" / "bridge"
            / name).read_text(encoding="utf-8")


def _manifest_source():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    return (root / "android-app" / "AndroidManifest.xml").read_text(
        encoding="utf-8")


def _res_text(rel):
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    return (root / "android-app" / "res" / rel).read_text(encoding="utf-8")


def test_p1_01_collect_nodes_early_returns_recycle():
    """P1-01 (CONS-014): both collectNodes early returns (depth
    over MAX_DEPTH, out at MAX_NODES) must recycle the node —
    it was obtained via getChild() and never reached the live
    map, so nobody else will recycle it. Source-level guard:
    the region from the method start to the first Node
    allocation (i.e. both early-return branches) must contain
    exactly two node.recycle() calls."""
    src = _kotlin_source("BridgeAccessibilityService.kt")
    start = src.index("private fun collectNodes(")
    end = src.index("val n = Node()", start)
    region = src[start:end]
    assert region.count("node.recycle()") == 2, region


def test_p1_06_accept_loop_checks_generation():
    """P1-06 (CONS-014): the accept loop must stop when its
    generation no longer matches the server lifecycle, so a
    stale thread that wakes after stop/restart exits instead
    of spinning on its closed socket. Source-level guard: the
    loop condition must contain the generation equality check."""
    src = _kotlin_source("HttpBridgeServer.kt")
    assert "acceptGeneration == serverLifecycleGeneration" in src


def test_p1_03_request_log_never_carries_arbitrary_path():
    """P1-03 fix round 2 (CONS-033): the per-request Logcat line
    in the finally block must never carry the raw request target
    — only known endpoints are logged by name, anything else
    logs as "UNKNOWN". Three source-level guards:
    (1) the finally-block Log.i call interpolates
    loggablePath(path), not the raw path;
    (2) the KNOWN_PATHS set contains exactly the endpoints in
    the handler's if/else chain — a new endpoint that is added
    to the chain but not to the set fails the suite (fail-closed
    in the safe direction: it would log as UNKNOWN);
    (3) loggablePath returns the path for known endpoints and
    "UNKNOWN" otherwise.

    HONEST LIMIT (CONS-033): no Python test can read Logcat, so
    this does not observe the real Logcat output — it proves the
    sanitising mechanism is present in the Java source. The
    mechanism is verified by compilation, dex inspection (the
    "UNKNOWN" string and loggablePath are present), and code
    inspection; a live-phone Logcat capture is still the only
    way to observe the real Logcat line.
    """
    import re
    src = _kotlin_source("HttpBridgeServer.kt")
    # (1) the request log goes through loggablePath, never raw path.
    assert 'Log.i(TAG, method + " " + loggablePath(path) + " -> " + status)' in src
    assert 'Log.i(TAG, method + " " + path + " -> " + status)' not in src
    # (2) KNOWN_PATHS matches the handler chain exactly.
    chain = set(re.findall(r'"(/[^"]*)" == path', src))
    start = src.index("private val KNOWN_PATHS = setOf(")
    end = src.index("\n\n        /** Loggable form", start)
    known = set(re.findall(r'"(/[^"]*)"', src[start:end]))
    assert chain, "no endpoints found in the handler chain"
    assert known == chain, (sorted(known - chain), sorted(chain - known))
    # (3) loggablePath maps unknown paths to "UNKNOWN".
    helper = src[src.index("fun loggablePath(path: String): String {"):]
    helper = helper[:helper.index("}")]
    assert 'if (KNOWN_PATHS.contains(path)) path else "UNKNOWN"' in helper


def test_p1_03_handler_chain_has_no_duplicate_branch():
    """Stage 3 batch 4 (self-found P0 during packet prep): two
    handlers (/click, /launch_app) were written with a duplicated
    empty else-if branch — the first (empty) branch matched and
    the real handler body in the second branch never ran, so POST
    /click and POST /launch_app would have answered nothing.
    kotlinc accepts it (syntactically valid) and the mock tests
    cannot see it (they run against the mock, not this source).
    Guard: every endpoint path occurs exactly once as `"..." ==
    path` in the chain — except /clipboard, which legitimately has
    one branch per method (GET + POST)."""
    import re
    from collections import Counter
    src = _kotlin_source("HttpBridgeServer.kt")
    counts = Counter(re.findall(r'"(/[a-z_/]+)" == path', src))
    assert counts, "no endpoints found in the handler chain"
    for path, n in counts.items():
        expected = 2 if path == "/clipboard" else 1
        assert n == expected, (path, n)


def test_p2_01_launch_baseline_after_pre_read():
    """P2-01 (CONS-028, step 3; fix round after CONS-035): the
    /launch_app verification baseline (requestTimeMs) must be
    captured just before startActivity() — AFTER the
    pre-foreground read AND after the Intent fetch inside
    launchApp() — so neither the up-to-500 ms pre-read nor the
    PackageManager lookup eats into the ~3 s post-launch
    verification window. Source-level guard: (1) the handler
    reads the foreground first and takes the baseline from
    launchApp()'s return value, capturing no timestamp itself;
    (2) inside launchApp(), the Intent fetch precedes the
    baseline capture, which precedes startActivity()."""
    src = _kotlin_source("HttpBridgeServer.kt")
    # (1) handler: pre-read first, baseline from launchApp().
    start = src.index('"/launch_app" == path')
    end = src.index('} else if ("/open" == path', start)
    region = src[start:end]
    pre = region.index("foregroundPackageSafe(svc,")
    call = region.index("val requestTimeMs = launchApp(pkg)")
    assert pre < call, (pre, call)
    assert "elapsedRealtime()" not in region
    assert "if (requestTimeMs < 0)" in region
    # (2) launchApp(): Intent fetch < baseline < startActivity.
    body = src[src.index("private fun launchApp(pkg: String?): Long {"):]
    body = body[:body.index("\n    }\n")]
    fetch = body.index("getLaunchIntentForPackage(pkg)")
    base = body.index(
        "val requestTimeMs = SystemClock.elapsedRealtime()")
    start_activity = body.index("context.startActivity(intent)")
    assert fetch < base < start_activity, (fetch, base, start_activity)
    assert "return requestTimeMs" in body
    assert "return -1" in body


def test_p2_03_launch_state_is_request_local():
    """P2-03 (CONS-028, step 3; fix round after CONS-035): the
    launch flow's per-request state must be request-local,
    never shared instance state — that is the property that
    makes concurrent launches and a mid-verification disconnect
    safe (each request's verification is tied to its own
    timestamp + package). Source-level guard: (1) requestTimeMs
    and wasForeground are declared as locals inside the
    /launch_app handler, and the timestamp itself is a local
    inside launchApp(); (2) neither name is an instance field
    — the field check matches declarations with AND without
    initializers (e.g. `private long requestTimeMs = 0;`), in
    any modifier order; (3) verifyLaunchForeground receives
    all per-request state as parameters; (4)
    foregroundPackageSafe tolerates a dead service (any
    exception -> null, a missed poll, never a crash or false
    success). Since the fix round, "not issued" is folded into
    the timestamp (requestTimeMs < 0), so there is no separate
    `issued` local to guard.

    HONEST LIMIT: there is no Java test harness in this
    project (manual aapt2/javac/d8 build, no Gradle, Android
    APIs are unavailable on the JVM), so no Java unit or
    Instrumentation test can run here. This guard proves the
    structural property in the source; it is regex-based, not
    an AST — it only sees class-level (4-space) field
    declarations, so a field hidden inside an inner class or
    split across lines would escape it. Actual concurrent
    behaviour is covered by the Python mock tests
    (test_app_launch_app_concurrent,
    test_app_launch_app_disconnect_during_verification) and
    still needs live-phone disconnect testing.
    """
    import re
    src = _kotlin_source("HttpBridgeServer.kt")
    # (1) locals inside the handler, timestamp local in launchApp.
    start = src.index('"/launch_app" == path')
    end = src.index('} else if ("/open" == path', start)
    region = src[start:end]
    assert "val requestTimeMs = launchApp(pkg)" in region
    assert "val wasForeground = svc != null" in region
    assert "if (requestTimeMs < 0)" in region
    launch = src[src.index("private fun launchApp(pkg: String?): Long {"):]
    launch = launch[:launch.index("\n    }\n")]
    assert ("val requestTimeMs = SystemClock.elapsedRealtime()"
            in launch)
    # (2) neither name is an instance field. Kotlin fields are
    # `val`/`var` declarations at class level (exactly 4-space
    # indent); method locals are indented deeper, so anchoring on
    # 4 spaces keeps locals out. Modifiers may appear in any order.
    for name in ("requestTimeMs", "wasForeground"):
        assert not re.search(
            rf"^    [^\n]*\b(?:val|var)\s+{name}\s*:",
            src, re.MULTILINE), name
    # (3) verifyLaunchForeground takes the state as parameters.
    sig = src[src.index("private fun verifyLaunchForeground("):]
    sig = sig[:sig.index("{")]
    assert "pkg: String" in sig
    assert "svc: BridgeAccessibilityService?" in sig
    assert "requestTimeMs: Long" in sig
    assert "wasForeground: Boolean" in sig
    # (4) a dead service degrades to a missed poll, not a throw.
    fg = src[src.index("private fun foregroundPackageSafe("):]
    fg = fg[:fg.index("private fun verifyLaunchForeground(")]
    assert "if (svc == null) {\n            return null\n        }" in fg
    assert "} catch (e: Exception) {" in fg
    assert "return null" in fg


def test_p2_service_unavailable_detail_names_endpoint():
    """CONS-022 P2 (step 3): the shared ServiceUnavailable catch
    must name the endpoint in its detail — a caller can then
    tell WHICH endpoint was unavailable, like the three inline
    checks (/capture_screen, /state, /events) already do. The
    real server used to send the bare generic detail
    ("accessibility service is not enabled") from the catch
    while the mock already modelled the per-endpoint detail.
    Source-level guard: the catch's unavailableJson call
    interpolates loggablePath(path) and the bare generic detail
    is gone from the catch. The client side is covered by the
    mock test test_app_a11y_off_structured_503, which asserts
    the endpoint path appears in the raised
    AccessibilityUnavailableError."""
    src = _kotlin_source("HttpBridgeServer.kt")
    start = src.index("} catch (e: ServiceUnavailable) {")
    end = src.index(
        "} catch (e: BridgeAccessibilityService.ServiceTimeoutException) {",
        start)
    region = src[start:end]
    assert "loggablePath(path)" in region
    assert ('unavailableJson("accessibility service is not enabled; "'
            in region)
    assert '" needs FULL_ACCESS"))' in region
    assert ('unavailableJson("accessibility service is not enabled")'
            not in region)


# --------------------------------------------------------------------------
# Home-screen widget (v2.6.0, SPEC-WIDGET.md, W1–W7).
# --------------------------------------------------------------------------
#
# HONEST LIMIT: the Python suite cannot run Java widget code —
# there is no Java test harness in this project (manual
# aapt2/javac/d8 build, no Gradle, Android APIs unavailable on
# the JVM), same as P1-01/P1-06 and P2-03. These guards prove
# structure in the source only: if someone removes the receiver
# declaration, an update hook, or the token-free provider, the
# suite fails. Actual widget behaviour (placing it, tapping
# Start/Stop, a11y/mode refresh) needs the §6 live verification
# on the phone; the aapt2 link step validates the widget XML
# resources at build time.
#
# CONS-037 fix round: the toggle control path moved out of the
# exported provider into the non-exported
# BridgeWidgetActionReceiver (P1), and the W6 async start-failure
# check was added (P2). The guards below pin that separation.

def test_widget_manifest_declares_receiver():
    """W3: the manifest must declare the widget receiver with
    APPWIDGET_UPDATE + the a11y-state action and the
    appwidget-provider metadata, so the system can deliver
    widget updates. The receiver must be exported (the system
    sends APPWIDGET_UPDATE). Source-level guard (see the
    section header for the honest limit)."""
    src = _manifest_source()
    assert 'android:name=".BridgeWidgetProvider"' in src
    start = src.index('android:name=".BridgeWidgetProvider"')
    region = src[start:start + 800]
    assert 'android:exported="true"' in region
    assert 'android.appwidget.action.APPWIDGET_UPDATE' in region
    assert 'dev.muse.bridge.ACTION_A11Y_STATE_CHANGED' in region
    assert 'android:name="android.appwidget.provider"' in region
    assert 'android:resource="@xml/bridge_widget_info"' in region


def test_widget_info_update_period_zero():
    """W3/W7: bridge_widget_info.xml must exist with
    updatePeriodMillis="0" (event-driven updates only, no
    periodic polling), the widget layout, home-screen
    category, and the 2x1 minimum size."""
    src = _res_text("xml/bridge_widget_info.xml")
    assert 'android:updatePeriodMillis="0"' in src
    assert 'android:initialLayout="@layout/widget_bridge"' in src
    assert 'android:widgetCategory="home_screen"' in src
    assert 'android:minWidth="180dp"' in src
    assert 'android:minHeight="110dp"' in src


def test_widget_layout_has_required_ids():
    """W1/W2/W7: widget_bridge.xml must carry the status, mode,
    a11y and last-updated lines plus the Start/Stop and Open
    buttons."""
    src = _res_text("layout/widget_bridge.xml")
    for view_id in ("widget_root", "widget_status_text",
                    "widget_mode_text", "widget_a11y_text",
                    "widget_updated_text", "widget_toggle_button",
                    "widget_open_button"):
        assert f'android:id="@+id/{view_id}"' in src


def test_widget_provider_never_touches_token():
    """W5: the widget never touches the HTTP API or the token —
    it talks to the service via Intents only. Neither the
    provider nor the action receiver source may reference the
    token at all."""
    for name in ("BridgeWidgetProvider.java",
                 "BridgeWidgetActionReceiver.java"):
        src = _java_source(name)
        assert "getOrCreateToken" not in src
        assert "currentToken" not in src
        assert "KEY_TOKEN" not in src


def test_widget_provider_handles_a11y_only():
    """W2/W3 + CONS-037 P1: the EXPORTED provider must handle
    ONLY the a11y-state redraw (plus the system widget updates
    via super.onReceive). It must NOT handle the toggle action —
    an exported receiver handling a control action would let any
    app toggle the service with an explicit broadcast. The
    toggle lives in BridgeWidgetActionReceiver (see the next
    test). Source-level guard (see the section header for the
    honest limit)."""
    src = _java_source("BridgeWidgetProvider.java")
    # NOTE (2026-11-08, v2.8.6): the blanket check
    # `"ACTION_WIDGET_TOGGLE" not in src` can never pass — the
    # provider MUST reference the constant to build the toggle
    # PendingIntent (see test_widget_toggle_targets_action_receiver,
    # which requires
    # `BridgeWidgetActionReceiver.ACTION_WIDGET_TOGGLE` here).
    # The guard is that the provider never DISPATCHES on the action:
    assert "ACTION_WIDGET_TOGGLE.equals" not in src
    assert "toggleService" not in src
    assert "startForegroundService(" not in src
    assert "stopService(" not in src
    assert "ACTION_A11Y_STATE_CHANGED.equals(action)" in src
    assert "R.layout.widget_bridge" in src
    assert "updateAllWidgets(context)" in src


def test_widget_action_receiver_is_not_exported():
    """CONS-037 P1: the manifest must declare
    .BridgeWidgetActionReceiver with exported=false and NO
    intent-filter — only explicit intents from our own app (the
    widget's PendingIntent, same UID) may reach it."""
    src = _manifest_source()
    assert 'android:name=".BridgeWidgetActionReceiver"' in src
    start = src.index('android:name=".BridgeWidgetActionReceiver"')
    region = src[start:start + 300]
    assert 'android:exported="false"' in region
    assert "intent-filter" not in region


def test_widget_toggle_targets_action_receiver():
    """CONS-037 P1: the widget buttons' toggle PendingIntent must
    target the non-exported action receiver, never the exported
    provider."""
    src = _java_source("BridgeWidgetProvider.java")
    assert ("new Intent(context, BridgeWidgetActionReceiver.class)"
            in src)
    assert ("BridgeWidgetActionReceiver.ACTION_WIDGET_TOGGLE" in src)
    assert ("new Intent(context, BridgeWidgetProvider.class)" not in src)


def test_widget_action_receiver_handles_toggle_and_async_failure():
    """W2/W3/W6 + CONS-037 P1/P2: the action receiver must handle
    the toggle action (start/stop the service via
    startForegroundService/stopService), fall back to MainActivity
    on a sync start failure, refresh now + after the ~600 ms
    settle delay, and — start path only — run a delayed check
    that opens MainActivity when the service is still not running
    (async bind failure). Source-level guard (see the section
    header for the honest limit); whether the delayed check fires
    correctly on a slow phone needs the §6 live verification."""
    src = _java_source("BridgeWidgetActionReceiver.java")
    assert "ACTION_WIDGET_TOGGLE.equals(action)" in src
    assert "startForegroundService(" in src
    assert "stopService(" in src
    assert "TOGGLE_SETTLE_DELAY_MS" in src
    assert "START_FAILURE_CHECK_DELAY_MS" in src
    assert "if (!BridgeService.isRunning())" in src
    assert "openMainActivity(appContext)" in src
    # The async check must be start-path only: a stop tap leaves
    # the service correctly not running, so it must not open the
    # app. The guard is the `started` flag gating the check.
    assert "final boolean started = !wasRunning;" in src
    assert "if (started) {" in src


def test_widget_updates_pushed_from_service_and_mode_toggle():
    """W3: updateAllWidgets must be pushed from
    BridgeService.onCreate/onDestroy (service start/stop) and
    from the MainActivity mode-toggle path."""
    svc = _kotlin_source("BridgeService.kt")
    start = svc.index("override fun onCreate()")
    end = svc.index("override fun onStartCommand(", start)
    assert "BridgeWidgetProvider.updateAllWidgets(this)" in svc[start:end]
    start = svc.index("override fun onDestroy()")
    end = svc.index("override fun onBind(", start)
    assert "BridgeWidgetProvider.updateAllWidgets(this)" in svc[start:end]
    main = _kotlin_source("MainActivity.kt")
    start = main.index("modeButton.setOnClickListener")
    # NOTE (2026-11-08, v2.8.6): the end marker was the pre-redesign
    # `Button notif = findViewById`; the current code declares the
    # field as `notifButton` and assigns it with
    # `notifButton = findViewById(...)`. Same region, same intent.
    end = main.index("notifButton = findViewById", start)
    assert ("BridgeWidgetProvider.updateAllWidgets(this)"
            in main[start:end])


def test_widget_a11y_broadcast_is_explicit():
    """W3: the a11y-state broadcast must reach the manifest
    widget receiver — since API 26 a manifest receiver cannot
    receive an implicit broadcast, so the send must be explicit
    (setPackage). Runtime receivers in this app are unaffected."""
    src = _kotlin_source("BridgeAccessibilityService.kt")
    start = src.index("private fun broadcastA11yStateChanged()")
    end = src.index("override fun onServiceConnected()", start)
    region = src[start:end]
    assert ".setPackage(packageName)" in region


# --------------------------------------------------------------------------
# Decision 69: launch_app (v1.9.12).
# --------------------------------------------------------------------------

def test_app_launch_app(mock_app):
    """Decision 69: launch_app posts the package name. P0-4: the
    answer is the structured launch report — a confirmed launch
    (a11y on, foreground observed) carries status "launched",
    foreground_confirmed True, verification "accessibility"."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.launch_app("com.twitter.android")
    assert result["ok"] is True
    assert result["status"] == "launched"
    assert result["accepted"] is True
    assert result["package"] == "com.twitter.android"
    assert result["launch_requested"] is True
    assert result["foreground_confirmed"] is True
    assert result["verification"] == "accessibility"
    assert result["transport"] == "app"
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/launch_app",
            {"package": "com.twitter.android"}) in posts


def test_app_launch_app_unconfirmed_foreground(mock_app):
    """P0-4: with a11y on, a launch that was issued but never
    observed in the foreground answers "unconfirmed" /
    FOREGROUND_NOT_CONFIRMED with ok False — never the old
    bare ok=true false positive."""
    MockAppHandler.launch_outcome = "unconfirmed"
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.launch_app("com.twitter.android")
    assert result["ok"] is False
    assert result["status"] == "unconfirmed"
    assert result["reason"] == "FOREGROUND_NOT_CONFIRMED"
    assert result["accepted"] is True
    assert result["launch_requested"] is True
    assert result["foreground_confirmed"] is False
    assert result["verification"] == "accessibility"


def test_app_launch_app_empty_package(mock_app):
    """Decision 69: an empty package raises ValueError before any
    request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="must not be empty"):
        adapter.launch_app("")
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_launch_app_needs_gate(mock_app):
    """Decision 69: launch_app changes the screen, so it needs
    --allow-actions."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    with pytest.raises(PermissionError):
        adapter.launch_app("com.twitter.android")


# --------------------------------------------------------------------------
# Decision 95: repair_termux (v2.5.0, TARMUX_KICKASS step 2).
# --------------------------------------------------------------------------

def test_app_repair_termux(mock_app):
    """Decision 95: repair_termux posts the fixed action only —
    no command, path or argument is ever sent — and the
    repaired report parses."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.repair_termux()
    assert result["ok"] is True
    assert result["repaired"] is True
    assert result["reason"] == "REPAIRED"
    assert result["termux_sshd_local"]["reachable"] is True
    assert result["steps"] == ["termux-wake-lock", "sshd"]
    assert result["transport"] == "app"
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/repair_termux", {"action": "sshd"}) in posts


def test_app_repair_termux_needs_gate(mock_app):
    """Decision 95: repair_termux restarts sshd on the phone,
    so it needs --allow-actions like any other action."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    with pytest.raises(PermissionError):
        adapter.repair_termux()


# --------------------------------------------------------------------------
# P0-1 (v2.0): HTTP / Accessibility dependency separation.
# --------------------------------------------------------------------------

def test_app_launch_app_without_a11y(mock_app):
    """P0-1: /launch_app works with the AccessibilityService off
    (it needs only startActivity from the server's context).
    P0-4: with a11y off the launch is still processed (HTTP
    200), but the answer is "unconfirmed" /
    VERIFICATION_UNAVAILABLE with foreground_confirmed null —
    the server cannot verify the foreground (UsageStats is P1),
    so it must not claim success."""
    MockAppHandler.accessibility = False
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.launch_app("com.twitter.android")
    assert result["ok"] is False
    assert result["status"] == "unconfirmed"
    assert result["reason"] == "VERIFICATION_UNAVAILABLE"
    assert result["accepted"] is True
    assert result["launch_requested"] is True
    assert result["foreground_confirmed"] is None
    assert result["verification"] == "none"


@pytest.mark.parametrize("package", [
    "com.kasikorn.retail.mbanking.wap",  # K PLUS (KBank)
    "com.bbl.mobilebanking",  # Bualuang mBanking
    "com.scb.phone",  # SCB EASY
    "com.krungsri.kma",  # KMA (Krungsri)
])
def test_app_launch_app_deny_list(mock_app, package):
    """P0-4 deny list (System Development Lead decision 2026-11-06 20:16,
    README decision 91): a deny-listed package is refused with
    HTTP 403 before any launch attempt and before the operation
    notification. The 403 surfaces as a TransportError, is never
    retried (last_call attempts == 1), and no launch was
    attempted and no operation notification was posted. A
    non-denied package is processed normally afterwards."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.TransportError) as excinfo:
        adapter.launch_app(package)
    assert "403" in str(excinfo.value)
    assert "deny list" in str(excinfo.value)
    assert MockAppHandler.launch_attempts == 0
    assert MockAppHandler.operation_notifications == 0
    assert adapter.last_call is not None
    assert adapter.last_call["attempts"] == 1
    assert adapter.last_call["status"] == 403
    # A non-denied package is processed normally.
    result = adapter.launch_app("com.twitter.android")
    assert result["status"] == "launched"
    assert MockAppHandler.launch_attempts == 1
    assert MockAppHandler.operation_notifications == 1


def test_app_launch_app_concurrent(mock_app):
    """P0-4 fix (CONS-027 P1 #3): two simultaneous /launch_app
    requests — each request's verification is tied to its own
    timestamp + package, so the answers must not cross: the
    package whose foreground is observed answers launched, the
    other answers unconfirmed, and neither answer is swapped.
    (The mock serves both on separate threads, like the real
    server's worker pool.)"""
    MockAppHandler.launch_outcomes = {
        "com.twitter.android": "launched",
        "com.example.other": "unconfirmed",
    }
    results = {}
    errors = []

    def run(package):
        try:
            adapter = ab.AndroidAdapter(actions_enabled=True)
            results[package] = adapter.launch_app(package)
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=("com.twitter.android",)),
        threading.Thread(target=run, args=("com.example.other",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert results["com.twitter.android"]["status"] == "launched"
    assert results["com.twitter.android"]["foreground_confirmed"] is True
    assert results["com.example.other"]["status"] == "unconfirmed"
    assert results["com.example.other"]["reason"] == "FOREGROUND_NOT_CONFIRMED"
    assert results["com.example.other"]["foreground_confirmed"] is False
    assert MockAppHandler.launch_attempts == 2
    assert MockAppHandler.operation_notifications == 2


def test_app_launch_app_disconnect_during_verification(mock_app):
    """P0-4 fix (CONS-027 P1 #3): the accessibility service
    goes away mid-verification — the reads start failing, so
    the answer must be unconfirmed, never a false launched.
    The Java-side ordering (a stale service reference makes
    the polls miss, reported as FOREGROUND_NOT_CONFIRMED) is
    covered by code inspection + live verification; this pins
    the client-visible contract for that outcome."""
    MockAppHandler.disconnect_during_launch = True
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.launch_app("com.twitter.android")
    assert result["ok"] is False
    assert result["status"] == "unconfirmed"
    assert result["reason"] == "FOREGROUND_NOT_CONFIRMED"
    assert result["foreground_confirmed"] is False
    assert result["verification"] == "accessibility"


def test_app_launch_app_foreground_left_before_poll(mock_app):
    """P0-4 fix (CONS-027 P1 #3): the target was already in
    front before the request but left before the post-request
    poll — a pre-request observation alone must NOT produce
    "launched". The server answers unconfirmed and the client
    accepts it as unconfirmed. The Java-side rule (only
    post-request observations count) is covered by code
    inspection + live verification; this pins the wire contract
    for that outcome."""
    MockAppHandler.launch_outcomes = {
        "com.twitter.android": "foreground_left",
    }
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.launch_app("com.twitter.android")
    assert result["ok"] is False
    assert result["status"] == "unconfirmed"
    assert result["reason"] == "FOREGROUND_NOT_CONFIRMED"
    assert result["foreground_confirmed"] is False


def test_app_launch_app_no_active_window(mock_app):
    """P0-4 fix (CONS-027 P1 #3): getRootInActiveWindow()
    returns null persistently (no active window) — every poll
    misses, so the answer is unconfirmed, not an error and not
    a retry. The client accepts the unconfirmed report as-is."""
    MockAppHandler.launch_outcomes = {
        "com.twitter.android": "no_window",
    }
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.launch_app("com.twitter.android")
    assert result["ok"] is False
    assert result["status"] == "unconfirmed"
    assert result["reason"] == "FOREGROUND_NOT_CONFIRMED"
    assert result["foreground_confirmed"] is False
    assert adapter.last_call is not None
    assert adapter.last_call["attempts"] == 1


def test_app_launch_app_slow_verification_does_not_hang(mock_app):
    """P0-4 fix (CONS-027 P1 #1): a slow verification window
    (the mock waits ~3.5 s, modelling the full ~3 s window
    elapsing) must still answer — the HTTP response does not
    hang and the client does not time out. The Java-side
    deadline (each read bounded by the remaining budget) is
    covered by code inspection + live verification; this pins
    the client side: a ~3–4 s verification round trip is
    accepted, not treated as a timeout or retried."""
    MockAppHandler.launch_delay_s = 3.5
    MockAppHandler.launch_outcome = "unconfirmed"
    adapter = ab.AndroidAdapter(actions_enabled=True)
    start = time.monotonic()
    result = adapter.launch_app("com.twitter.android")
    elapsed = time.monotonic() - start
    assert result["status"] == "unconfirmed"
    assert result["reason"] == "FOREGROUND_NOT_CONFIRMED"
    assert elapsed >= 3.5
    assert elapsed < 10.0  # well under the client's 30 s read timeout
    assert adapter.last_call is not None
    assert adapter.last_call["attempts"] == 1


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/state", None),
    ("GET", "/capture_screen", None),
    ("GET", "/events", None),
    ("POST", "/click", {"x": 500, "y": 800}),
    ("POST", "/swipe", {"x1": 100, "y1": 200, "x2": 300, "y2": 400, "duration_ms": 300}),
    ("POST", "/type_text", {"text": "hello"}),
    ("POST", "/press_key", {"key": "HOME"}),
    ("POST", "/click_node", {"node_id": "n1", "tree_hash": 123456}),
    ("POST", "/type_node", {"node_id": "n1", "tree_hash": 123456, "text": "hi"}),
    ("POST", "/scroll_node", {"node_id": "n1", "tree_hash": 123456, "direction": "forward"}),
    ("POST", "/node_action", {"node_id": "n1", "tree_hash": 123456, "action": "focus"}),
    ("POST", "/notification_action", {"key": "key-1", "action": "open"}),
    ("POST", "/batch", {"batch_id": "b-1", "protocol_version": 1,
                        "tree_hash": 123456, "actions": []}),
])
def test_app_a11y_off_structured_503(mock_app, method, path, payload):
    """P0-1: with a11y off, every a11y-dependent endpoint answers
    the structured 503 body, and the client raises
    AccessibilityUnavailableError carrying reason/required_mode."""
    MockAppHandler.accessibility = False
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.AccessibilityUnavailableError) as excinfo:
        adapter._app_request(method, path, payload)
    assert excinfo.value.reason == "ACCESSIBILITY_DISABLED"
    assert excinfo.value.required_mode == "FULL_ACCESS"
    assert path in str(excinfo.value)
    # It is still a TransportError, so existing handlers catch it.
    assert isinstance(excinfo.value, ab.TransportError)


def test_app_a11y_off_no_retry(mock_app):
    """P0-1: a structured a11y-off 503 is not retried — enabling
    the service is a manual user action, so retrying only burns
    attempts and backoff time. GET /state is sent once per call
    (get_current_state once, and the batch's C-04 guard fetch
    once); POST /batch is never sent — the guard fetch fails
    first, so the batch is refused before any action is posted."""
    MockAppHandler.accessibility = False
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.AccessibilityUnavailableError):
        adapter.get_current_state()
    with pytest.raises(ab.AccessibilityUnavailableError):
        adapter.batch("b-1", 123456,
                      [{"action_id": "a1", "type": "tap", "x": 1, "y": 2}])
    gets = [r for r in MockAppHandler.requests
            if r[0] == "GET" and r[1] == "/state"]
    assert len(gets) == 2
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/batch"]
    assert len(posts) == 0


def test_app_a11y_off_available_endpoints(mock_app):
    """P0-1: with a11y off, the endpoints that do not need the
    service still answer 200 — /health, /logs, /notifications,
    /open, /install_apk, GET/POST /clipboard, /launch_app,
    /notify. C-04 (SPEC §2.2): /notification_action is NOT in
    this list anymore — it is a guarded ordinary screen operation
    and needs the service (see test_app_a11y_off_structured_503,
    which now covers it)."""
    MockAppHandler.accessibility = False
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert ab.check_app()["ok"] is None  # reachable, a11y off
    assert adapter.logs()["ok"] is True
    assert adapter.notifications()["ok"] is True
    assert adapter.open("https://example.com")["ok"] is True
    assert adapter.install_apk(url="https://example.com/app.apk")["ok"] is True
    assert adapter.get_clipboard()["ok"] is True
    assert adapter.set_clipboard("MUSEテスト")["ok"] is True
    # P0-4: /launch_app is processed with a11y off (HTTP 200),
    # but the report is unconfirmed, never a success claim.
    launch = adapter.launch_app("com.twitter.android")
    assert launch["ok"] is False
    assert launch["status"] == "unconfirmed"
    assert launch["reason"] == "VERIFICATION_UNAVAILABLE"
    assert adapter.notify("Title", "Body")["ok"] is True
    # C-04: /notification_action needs the service now — with a11y
    # off it raises AccessibilityUnavailableError (the /state fetch
    # inside notification_action() fails first, same error class).
    with pytest.raises(ab.AccessibilityUnavailableError):
        adapter.notification_action("key-1", "open")


def test_app_launch_blocked_not_misclassified(mock_app):
    """P0-1: /launch_app's own "launch blocked by the system" 503
    (a bare body, no reason field) must raise plain TransportError,
    not AccessibilityUnavailableError — it is a different failure."""
    MockAppHandler.fail_next[("POST", "/launch_app")] = (503, 1)
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.TransportError) as excinfo:
        adapter.launch_app("com.twitter.android")
    assert not isinstance(excinfo.value, ab.AccessibilityUnavailableError)
    assert "ACCESSIBILITY_DISABLED" not in str(excinfo.value)


# --------------------------------------------------------------------------
# P0-3 (v2.0): per-module Health API.
# --------------------------------------------------------------------------

def test_app_check_app_carries_health_detail(mock_app):
    """P0-3: check_app() returns the same ok/detail as before
    AND carries the new /health fields (mode, overall_status,
    modules, capabilities, tailscale_ips). P1-3: it also
    carries tailscale_ips_bound (equal to tailscale_ips when
    the bound list is fresh)."""
    result = ab.check_app()
    assert result["ok"] is True
    assert result["detail"] == "app reachable, accessibility service enabled"
    assert result["mode"] == "FULL_ACCESS"
    assert result["overall_status"] == "HEALTHY"
    assert result["modules"]["accessibility"] == "ENABLED"
    assert result["modules"]["termux_ssh"] == "UNKNOWN"
    assert result["capabilities"]["screenshot"] is True
    assert result["capabilities"]["launch_app"] is True
    assert result["capabilities"]["launch_app_guaranteed"] is False
    assert result["capabilities"]["foreground_verification"] is True
    assert result["tailscale_ips"] == ["100.85.138.44"]
    assert result["tailscale_ips_bound"] == ["100.85.138.44"]


def test_app_check_app_a11y_off_carries_minimal(mock_app):
    """P0-3: with a11y off, check_app() returns ok=None (as
    before) and mode MINIMAL with the screen capabilities off
    — a11y off is normal in MINIMAL, so overall stays HEALTHY."""
    MockAppHandler.accessibility = False
    result = ab.check_app()
    assert result["ok"] is None
    assert result["mode"] == "MINIMAL"
    assert result["overall_status"] == "HEALTHY"
    assert result["modules"]["accessibility"] == "DISABLED"
    assert result["capabilities"]["screenshot"] is False
    assert result["capabilities"]["tap"] is False
    assert result["capabilities"]["launch_app"] is True
    assert result["capabilities"]["launch_app_guaranteed"] is False
    assert result["capabilities"]["foreground_verification"] is False
    assert result["capabilities"]["http_api"] is True


def test_app_get_health_detail(mock_app):
    """P0-3: get_health_detail() returns the full parsed /health
    dict (plus seconds). P1-3: it also carries
    tailscale_ips_bound, equal to tailscale_ips when the bound
    list is fresh."""
    detail = ab.get_health_detail()
    assert detail["ok"] is True
    assert detail["mode"] == "FULL_ACCESS"
    assert detail["overall_status"] == "HEALTHY"
    assert detail["modules"]["tailscale"] == "HEALTHY"
    assert detail["capabilities"]["install_apk"] is True
    assert detail["tailscale_ips"] == ["100.85.138.44"]
    assert detail["tailscale_ips_bound"] == ["100.85.138.44"]
    assert isinstance(detail["seconds"], float)


def test_app_old_server_health_still_works(mock_app, monkeypatch):
    """P0-3: an old server (no new /health fields) still works —
    check_app() returns the same ok/detail, and the new keys
    are None."""
    import test_android_app_client as tac
    monkeypatch.setattr(tac, "_health_body",
                        lambda a11y, banking_safe=False, **kw: {
        "ok": True, "service": "muse-bridge", "version": 1,
        "accessibility": True, "notification_listener": True,
        "app_version": "1.9.5", "app_version_code": 15})
    result = ab.check_app()
    assert result["ok"] is True
    assert result["mode"] is None
    assert result["overall_status"] is None
    assert result["modules"] is None
    assert result["capabilities"] is None
    assert result["tailscale_ips"] is None
    assert result["tailscale_ips_bound"] is None


# --------------------------------------------------------------------------
# P0-2 (v2.0): MINIMAL as the standard startup state.
# --------------------------------------------------------------------------

def test_app_check_app_banking_safe_mode(mock_app):
    """P0-2: with a11y off and banking-safe picked, /health
    reports mode BANKING_SAFE (and the screen capabilities
    stay off). The mode is a persisted preference; the client
    just reports it."""
    MockAppHandler.accessibility = False
    MockAppHandler.banking_safe = True
    result = ab.check_app()
    assert result["ok"] is None
    assert result["mode"] == "BANKING_SAFE"
    assert result["overall_status"] == "HEALTHY"
    assert result["capabilities"]["screenshot"] is False
    assert result["capabilities"]["launch_app"] is True
    assert result["capabilities"]["launch_app_guaranteed"] is False
    assert result["capabilities"]["foreground_verification"] is False


# --------------------------------------------------------------------------
# P1-3 / P1-4 (CONS-022): tailscale IP staleness signalling and
# install_apk reflecting the "Install unknown apps" setting.
# --------------------------------------------------------------------------

def test_app_health_tailscale_stale_visible(mock_app):
    """P1-3 (CONS-029 fix): when the bound IP has gone stale,
    /health reports the current tailscale_ips and the old bound
    list separately, AND reports modules.tailscale DEGRADED
    with overall_status DEGRADED — the VM cannot connect on
    the current IP, so it must not be reported HEALTHY."""
    MockAppHandler.tailscale_stale = True
    detail = ab.get_health_detail()
    assert detail["tailscale_ips"] == ["100.85.138.44"]
    assert detail["tailscale_ips_bound"] == ["100.111.74.42"]
    assert detail["tailscale_ips"] != detail["tailscale_ips_bound"]
    assert detail["modules"]["tailscale"] == "DEGRADED"
    assert detail["overall_status"] == "DEGRADED"
    result = ab.check_app()
    assert result["tailscale_ips"] == ["100.85.138.44"]
    assert result["tailscale_ips_bound"] == ["100.111.74.42"]
    assert result["modules"]["tailscale"] == "DEGRADED"
    assert result["overall_status"] == "DEGRADED"


def test_app_health_install_apk_false_when_not_permitted(mock_app):
    """P1-4 (CONS-029 split): install_apk stays True (endpoint
    available, P0 spec A5) when installs are not permitted;
    only install_apk_permitted goes False ('Install unknown
    apps' off for this app)."""
    MockAppHandler.can_install_apk = False
    detail = ab.get_health_detail()
    assert detail["capabilities"]["install_apk"] is True
    assert detail["capabilities"]["install_apk_permitted"] is False
    result = ab.check_app()
    assert result["capabilities"]["install_apk"] is True
    assert result["capabilities"]["install_apk_permitted"] is False


def test_app_health_install_apk_true_by_default(mock_app):
    """P1-4 (CONS-029 split): install_apk and
    install_apk_permitted are both true when installs are
    permitted (the default)."""
    detail = ab.get_health_detail()
    assert detail["capabilities"]["install_apk"] is True
    assert detail["capabilities"]["install_apk_permitted"] is True
    result = ab.check_app()
    assert result["capabilities"]["install_apk"] is True
    assert result["capabilities"]["install_apk_permitted"] is True


# --------------------------------------------------------------------------
# Decision 74: open (v1.9.15, Phase D-1).
# --------------------------------------------------------------------------

def test_app_open_uri(mock_app):
    """Decision 74: open posts the uri."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.open("https://x.com/search?q=Muse")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/open",
            {"uri": "https://x.com/search?q=Muse"}) in posts


def test_app_open_empty_uri(mock_app):
    """Decision 74: an empty uri raises ValueError before any
    request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="must not be empty"):
        adapter.open("")
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_open_needs_gate(mock_app):
    """Decision 74: open changes the screen, so it needs
    --allow-actions."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    with pytest.raises(PermissionError):
        adapter.open("https://example.com")


# --------------------------------------------------------------------------
# Decision 75: install_apk / install_status (v1.9.16).
# --------------------------------------------------------------------------

def test_app_install_apk_url(mock_app):
    """Decision 75: install_apk posts the https url."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.install_apk(
        url="https://f-droid.org/repo/com.termux_1002.apk")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/install_apk",
            {"url": "https://f-droid.org/repo/com.termux_1002.apk"}) in posts


def test_app_install_apk_path(mock_app):
    """Decision 75: install_apk posts a file already on the phone."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.install_apk(path="/sdcard/Download/x.apk")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/install_apk",
            {"path": "/sdcard/Download/x.apk"}) in posts


def test_app_install_apk_neither_or_both(mock_app):
    """Decision 75: exactly one of url, path — anything else
    raises ValueError before any request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="exactly one"):
        adapter.install_apk()
    with pytest.raises(ValueError, match="exactly one"):
        adapter.install_apk(url="https://example.com/x.apk",
                            path="/sdcard/x.apk")
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_install_apk_http_rejected(mock_app):
    """Decision 75: only https downloads are allowed."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="must be https"):
        adapter.install_apk(url="http://example.com/x.apk")
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_install_apk_needs_gate(mock_app):
    """Decision 75: installing an app changes the device, so it
    needs --allow-actions."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    with pytest.raises(PermissionError):
        adapter.install_apk(url="https://example.com/x.apk")


def test_app_install_status(mock_app):
    """Decision 75: install_status reads the job state."""
    adapter = ab.AndroidAdapter()
    assert adapter.install_status() == {"ok": True, "status": "idle",
                                        "detail": ""}


# --------------------------------------------------------------------------
# Decision 76: clipboard read/write (v1.9.17).
# --------------------------------------------------------------------------

def test_app_get_clipboard(mock_app):
    """Decision 76: get_clipboard reads the clipboard text."""
    adapter = ab.AndroidAdapter()
    assert adapter.get_clipboard() == {"ok": True, "text": "hello-clipboard",
                                       "truncated": False}


def test_app_set_clipboard(mock_app):
    """Decision 76: set_clipboard posts the text."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.set_clipboard("MUSEテスト")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/clipboard", {"text": "MUSEテスト"}) in posts


def test_app_set_clipboard_empty_clears(mock_app):
    """Decision 76: an empty string clears the clipboard — it is
    allowed, unlike open() where empty means a missing uri."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.set_clipboard("")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/clipboard", {"text": ""}) in posts


def test_app_set_clipboard_bad_type(mock_app):
    """Decision 76: a non-string raises ValueError before any
    request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="must be a string"):
        adapter.set_clipboard(None)
    with pytest.raises(ValueError, match="must be a string"):
        adapter.set_clipboard(42)
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_set_clipboard_needs_gate(mock_app):
    """Decision 76: writing the clipboard changes device state,
    so it needs --allow-actions; reading needs no gate."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    with pytest.raises(PermissionError):
        adapter.set_clipboard("x")
    assert adapter.get_clipboard()["ok"] is True


# --------------------------------------------------------------------------
# Decision 77: notification open/reply (v1.9.18).
# --------------------------------------------------------------------------

NOTIF_KEY = "0|com.example.chat|1|null|10001"


def test_app_notification_open(mock_app):
    """Decision 77: notification_action open posts key + action."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.notification_action(NOTIF_KEY, "open")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    # C-04: guarded — operation_context of a fresh snapshot
    # (first action, bootstrap, no session_id).
    assert ("POST", "/notification_action",
            {"key": NOTIF_KEY, "action": "open",
             "operation_context": {"expected_package": "com.example", "tree_hash": 123456, "generation": 7}}) in posts


def test_app_notification_reply(mock_app):
    """Decision 77: notification_action reply posts key + action +
    the exact text."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.notification_action(
        NOTIF_KEY, "reply", text="はい")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/notification_action",
            {"key": NOTIF_KEY, "action": "reply", "text": "はい",
             "operation_context": {"expected_package": "com.example", "tree_hash": 123456, "generation": 7}}) in posts


def test_app_notification_action_bad_args(mock_app):
    """Decision 77: a bad key, action, or missing reply text
    raises ValueError before any request is sent."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="must not be empty"):
        adapter.notification_action("", "open")
    with pytest.raises(ValueError, match="must be 'open' or 'reply'"):
        adapter.notification_action(NOTIF_KEY, "dismiss")
    with pytest.raises(ValueError, match="text is required"):
        adapter.notification_action(NOTIF_KEY, "reply")
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_notification_action_needs_gate(mock_app):
    """Decision 77: opening a notification changes the screen
    and replying sends a message, so both need
    --allow-actions."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    with pytest.raises(PermissionError):
        adapter.notification_action(NOTIF_KEY, "open")
    with pytest.raises(PermissionError):
        adapter.notification_action(NOTIF_KEY, "reply", text="x")


# --------------------------------------------------------------------------
# v2.1: operation notifications ("操作中" / "操作終了").
# --------------------------------------------------------------------------

def test_operation_end_posts(mock_app):
    """v2.1: operation_end() sends ("POST", "/operation_end", {})
    and returns ok with was_active present."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    result = adapter.operation_end()
    assert result["ok"] is True
    assert "was_active" in result
    assert ("POST", "/operation_end", {}) in MockAppHandler.requests


def test_operation_end_not_gated(mock_app):
    """v2.1: operation_end() is not gated by --allow-actions — it
    only dismisses a notification, like notify()."""
    adapter = ab.AndroidAdapter(actions_enabled=False)
    assert adapter.operation_end()["ok"] is True


def test_operation_end_not_retried(mock_app, monkeypatch):
    """v2.1: POST /operation_end runs exactly once — the server
    auto-end is the fallback for a lost end signal."""
    monkeypatch.setattr(ab, "APP_RETRY_BASE_DELAY_S", 0.01)
    MockAppHandler.fail_next[("POST", "/operation_end")] = (503, 1)
    adapter = ab.AndroidAdapter(actions_enabled=False)
    with pytest.raises(ab.TransportError):
        adapter.operation_end()
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/operation_end"]
    assert len(posts) == 1


def test_operation_end_with_a11y_off(mock_app):
    """v2.1: /operation_end works with the AccessibilityService
    off — ending a session must work in MINIMAL."""
    MockAppHandler.accessibility = False
    adapter = ab.AndroidAdapter(actions_enabled=False)
    assert adapter.operation_end()["ok"] is True


def test_operation_session_sends_end_on_exit(mock_app):
    """v2.1: operation_session() sends /operation_end when the
    block exits, after the actions inside it."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with adapter.operation_session():
        adapter.click(1, 2)
    paths = [r[1] for r in MockAppHandler.requests if r[0] == "POST"]
    assert paths.index("/click") < paths.index("/operation_end")
    assert paths[-1] == "/operation_end"


def test_operation_session_sends_end_on_exception(mock_app):
    """v2.1: operation_session() still sends /operation_end when
    the body raises (finally)."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ValueError, match="boom"):
        with adapter.operation_session():
            raise ValueError("boom")
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/operation_end", {}) in posts


def test_operation_session_swallows_end_failure(mock_app, monkeypatch):
    """v2.1: a failed end signal must not mask the task's own
    exception — and must not raise on a clean exit either."""
    monkeypatch.setattr(ab, "APP_RETRY_BASE_DELAY_S", 0.01)
    adapter = ab.AndroidAdapter(actions_enabled=True)
    # Clean exit: the end fails, but exiting must not raise.
    MockAppHandler.fail_next[("POST", "/operation_end")] = (503, 1)
    with adapter.operation_session():
        pass
    # Body exception: it still propagates, not the end failure.
    MockAppHandler.fail_next[("POST", "/operation_end")] = (503, 1)
    with pytest.raises(ValueError, match="boom"):
        with adapter.operation_session():
            raise ValueError("boom")


def test_plain_actions_do_not_send_operation_end(mock_app):
    """v2.1: bare actions send no /operation_end — the session
    boundary belongs at the task level, not per action."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    adapter.click(1, 2)
    adapter.swipe(1, 2, 3, 4)
    adapter.launch_app("com.twitter.android")
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert not [r for r in posts if r[1] == "/operation_end"]


# ----------------------------------------------------------------------
# SPEC-DRIVE-001 (v2.9.0): log export to a user-chosen folder (SAF).
# Source-level guards. Honest limit (as with the widget/ping guards):
# these check that the code contains the specified mechanism; the
# actual Drive behaviour (D-01..D-10) is verified live on the phone.
# ----------------------------------------------------------------------

def _drive_log():
    return _kotlin_source("LogActivity.kt")


def _drive_registry():
    return _kotlin_source("UriPermissionRegistry.kt")


def test_drive001_t01_folder_pick_uses_document_tree():
    """T-01: the folder pick uses ACTION_OPEN_DOCUMENT_TREE and
    takePersistableUriPermission."""
    src = _drive_log()
    assert "ACTION_OPEN_DOCUMENT_TREE" in src
    assert "takePersistableUriPermission" in src


def test_drive001_t02_auto_save_creates_document():
    """T-02: a saved folder writes via createDocument."""
    src = _drive_log()
    assert "DocumentsContract.createDocument" in src
    assert "buildDocumentUriUsingTree" in src


def test_drive001_t03_no_folder_falls_back_to_picker():
    """T-03: without a saved folder the export falls back to
    ACTION_CREATE_DOCUMENT."""
    src = _drive_log()
    assert "ACTION_CREATE_DOCUMENT" in src
    assert "KIND_FALLBACK_EXPORT" in src


def test_drive001_t04_failure_classification():
    """T-04: auto-save failures are classified (DRIVE-06) using
    the persisted-permission check and a two-query parent check."""
    src = _drive_log()
    assert "classifyAutoSaveFailure" in src
    assert "persistedUriPermissions" in src
    assert src.count("COLUMN_DOCUMENT_ID") >= 1


def test_drive001_t05_clear_releases_permission():
    """T-05: clearing the folder releases the permission through
    the registry (releasePersistableUriPermission)."""
    reg = _drive_registry()
    assert "releasePersistableUriPermission" in reg
    assert "releaseIfUnused" in reg
    src = _drive_log()
    assert "releaseIfUnused" in src


def test_drive001_t06_no_drive_api_or_oauth():
    """T-06: no Drive API, OAuth, or Google Sign-In anywhere in
    the folder feature."""
    for src in (_drive_log(), _drive_registry()):
        # The words appear in explanatory comments; the guard is
        # that no Drive/OAuth API is actually used.
        assert "drive.googleapis" not in src
        assert "GoogleSignIn" not in src
        assert "DriveService" not in src
        assert "GoogleAccountCredential" not in src
        assert "com.google.api" not in src
        assert "import com.google" not in src


def test_drive001_t07_folder_uri_in_dedicated_atomic_file():
    """T-07: the folder URI lives in its own file, written
    atomically; no SharedPreferences for it."""
    src = _drive_log()
    assert 'FOLDER_URI_NAME = "log_folder_uri"' in src
    assert "atomicWriteText" in src
    assert "SharedPreferences" not in src


def test_drive001_t08_granted_flags_checked():
    """T-08: the folder-pick result checks the granted READ,
    WRITE and PERSISTABLE flags from the result intent."""
    src = _drive_log()
    assert "data.flags" in src
    assert "FLAG_GRANT_READ_URI_PERMISSION" in src
    assert "FLAG_GRANT_WRITE_URI_PERMISSION" in src
    assert "FLAG_GRANT_PERSISTABLE_URI_PERMISSION" in src


def test_drive001_t09_parent_uri_built_from_tree():
    """T-09: the parent document URI is built with
    buildDocumentUriUsingTree(treeUri, getTreeDocumentId(treeUri))."""
    src = _drive_log()
    assert "getTreeDocumentId" in src
    assert "buildDocumentUriUsingTree" in src


def test_drive001_t10_same_uri_reselect_keeps_permission():
    """T-10: re-picking the same folder never releases its
    permission (same-URI guard before releaseIfUnused)."""
    src = _drive_log()
    assert "oldUri != uriString" in src


def test_drive001_t11_write_failure_keeps_old_folder():
    """T-11: when the settings write fails, the old folder and
    its permission stay (release happens only after a
    successful atomic write)."""
    src = _drive_log()
    assert "handleFolderWriteFailed" in src
    start = src.index("handleFolderWriteFailed")
    region = src[start:start + 2500]
    assert "unregister" in region
    # The success path releases the old permission only after
    # atomicWriteText returned true.
    ok = src.index('atomicWriteText(filesDir, FOLDER_URI_NAME,')
    after = src[ok:ok + 800]
    assert "releaseIfUnused" in after


def test_drive001_t12_ioexception_keeps_folder():
    """T-12: an IOException during the failure classification
    keeps the folder (classify returns false on any exception)."""
    src = _drive_log()
    start = src.index("private fun classifyAutoSaveFailure(treeUri: Uri): Boolean")
    region = src[start:start + 2600]
    assert "catch (_: Exception)" in region
    assert "return false" in region


def test_drive001_t13_open_output_stream_null_is_failure():
    """T-13: a null openOutputStream is a write failure."""
    src = _drive_log()
    assert "openOutputStream(writeTarget)" in src
    assert "openOutputStream returned null" in src


def test_drive001_t14_incomplete_file_deleted():
    """T-14: a failed write deletes the half-written document
    (deleteDocument, best-effort, never hiding the error)."""
    src = _drive_log()
    assert "deleteCreatedBestEffort" in src
    assert "DocumentsContract.deleteDocument" in src


def test_drive001_t15_permission_checked_before_release():
    """T-15: releaseIfUnused checks the persisted permissions
    before releasing anything."""
    reg = _drive_registry()
    assert "getPersistedUriPermissions" in reg
    assert "isWritePermission" in reg


def test_drive001_t18_persistable_flag_missing_refuses():
    """T-18: a folder pick without the PERSISTABLE grant is
    refused (toast, old folder kept)."""
    src = _drive_log()
    start = src.index("private fun handleFolderPicked(data: Intent, opId: Long)")
    region = src[start:start + 1200]
    assert "persistOk" in region
    assert "toast_log_folder_no_write" in region


def test_drive001_t19_folder_write_failure_keeps_old_permission():
    """T-19: when the folder-file write fails, the old
    permission is not released (only the new registration is
    removed, and only when that removal succeeds)."""
    src = _drive_log()
    start = src.index("handleFolderWriteFailed")
    region = src[start:start + 1800]
    assert "releasePermissionBestEffort" in region
    assert "unregister" in region


def test_drive001_t20_clear_persistence_failure_keeps_permission():
    """T-20: when the clear marker cannot be persisted, the
    clear aborts before deleting anything (permission kept)."""
    src = _drive_log()
    start = src.index("private fun doClearFolder()")
    region = src[start:start + 1200]
    assert "toast_log_folder_clear_failed" in region
    assert "atomicWriteText(filesDir, CLEAR_PENDING_NAME" in region


def test_drive001_t21_security_exception_keeps_folder():
    """T-21: a SecurityException alone never clears the folder
    (the classification catch returns keep)."""
    src = _drive_log()
    start = src.index("private fun classifyAutoSaveFailure(treeUri: Uri): Boolean")
    region = src[start:start + 2600]
    assert "catch (_: Exception)" in region
    assert "return false" in region


def test_drive001_t22_file_not_found_keeps_folder():
    """T-22: a FileNotFoundException alone never clears the
    folder (same classification catch as T-21)."""
    src = _drive_log()
    start = src.index("private fun classifyAutoSaveFailure(treeUri: Uri): Boolean")
    region = src[start:start + 2600]
    assert "catch (_: Exception)" in region
    assert "return false" in region


def test_drive001_t23_double_tap_guarded():
    """T-23: a second tap while a save is running is ignored
    (beginOperation returns -1 outside IDLE). Source-level
    guard only — true JVM concurrency cannot be exercised
    from this harness."""
    src = _drive_log()
    assert "beginOperation(OpState.IDLE, OpState.WRITING)" in src
    assert "if (opId < 0)" in src


def test_drive001_t24_save_possible_after_failure():
    """T-24: after a failure the operation is always released
    (endOperation in finally), so the next save can start."""
    src = _drive_log()
    start = src.index("private fun writeLogToUriInBackground(")
    region = src[start:start + 2600]
    assert "finally" in region
    assert "endOperation(opId)" in region


def test_drive001_t25_no_stale_view_ops_after_destroy():
    """T-25: completion notifications check isFinishing /
    isDestroyed and go through a WeakReference."""
    src = _drive_log()
    assert "private var resumedActivity: WeakReference<LogActivity>?" in src
    assert "isFinishing" in src
    assert "isDestroyed" in src


def test_drive001_t26_delete_attempted_on_null_stream():
    """T-26: when openOutputStream returns null, the created
    document is still deleted (the outer catch runs
    deleteCreatedBestEffort)."""
    src = _drive_log()
    start = src.index("private fun writeLogToUriInBackground(")
    region = src[start:start + 2600]
    assert "deleteCreatedBestEffort" in region
    assert "openOutputStream returned null" in region


def test_drive001_t27_parent_absence_vs_transient_failure():
    """T-27: parent absence is confirmed by two independent
    queries (with a pause between them); a single failure
    keeps the folder."""
    src = _drive_log()
    start = src.index("private fun classifyAutoSaveFailure(treeUri: Uri): Boolean")
    region = src[start:start + 2600]
    assert "for (i in 0 until 2)" in region
    assert "Thread.sleep(500)" in region
    assert "emptyCount == 2" in region


def test_drive001_t28_memory_and_disk_uri_agree():
    """T-28: the saved URI is always re-read from disk
    (readFolderUri), never trusted from a stale in-memory copy."""
    src = _drive_log()
    assert "readFolderUri()" in src
    assert "private fun readFolderUriString(): String?" in src


def test_drive001_t29_fallback_cancel_releases_state():
    """T-29: cancelling the fallback picker releases the
    operation (endOperation + UI sync)."""
    src = _drive_log()
    start = src.index("override fun onActivityResult(")
    region = src[start:start + 2600]
    assert "User cancelled" in region
    assert "endOperation(req.opId)" in region


def test_drive001_t30_state_is_static_and_shared():
    """T-30: the operation state is static, so a recreated
    activity sees the same state."""
    src = _drive_log()
    assert "private var state = OpState.IDLE" in src
    assert "private var currentOpId = 0L" in src
    assert "private val requestCodeToOpId: MutableMap<Int, PickerRequest>" in src


def test_drive001_t31_change_clear_save_share_exclusion():
    """T-31: change, clear and save all go through the same
    beginOperation / SAVE_LOCK exclusion."""
    src = _drive_log()
    assert "beginOperation(OpState.IDLE, OpState.PICKER_PENDING)" in src
    assert "beginOperation(OpState.IDLE, OpState.WRITING)" in src
    assert src.count("beginOperation(OpState.IDLE") >= 3


def test_drive001_t32_same_uri_save_failure_keeps_permission():
    """T-32: when the new URI equals the old one, a write
    failure never releases the permission."""
    src = _drive_log()
    start = src.index("handleFolderWriteFailed")
    region = src[start:start + 1800]
    assert "if (newUri != oldUri) {" in region


def test_drive001_t33_atomic_write_pattern():
    """T-33: atomic write is temp + fsync + Os.rename + parent
    dir fsync (BridgeLog.atomicWriteText). Batch G: the source
    is Kotlin now (fos.fd.sync() is the Kotlin spelling of
    Java's fos.getFD().sync())."""
    src = _kotlin_source("BridgeLog.kt")
    start = src.index("fun atomicWriteText")
    region = src[start:start + 1600]
    assert 'fileName + ".tmp"' in region
    assert "fd.sync()" in region
    assert "Os.rename(" in region
    assert "fsyncDirectory(dir)" in region


def test_drive001_t34_three_way_folder_read():
    """T-34: the saved-folder read distinguishes OK / MISSING /
    ERROR (FolderKind)."""
    src = _drive_log()
    assert "private enum class FolderKind" in src
    assert "OK, MISSING, ERROR" in src


def test_drive001_t35_recreation_never_stuck():
    """T-35: a recreated activity cannot stay stuck in
    PICKER_PENDING/WRITING — the timeout and the finally
    release bound every operation."""
    src = _drive_log()
    assert "checkPickerTimeout" in src
    assert "finally" in src
    assert "PICKER_TIMEOUT_MS" in src


def test_drive001_t36_dir_fsync_failure_keeps_old_permission():
    """T-36: a directory-sync failure makes the atomic write
    fail, so the old permission is not released."""
    src = _drive_log()
    assert "atomicWriteText" in src
    bridge = _kotlin_source("BridgeLog.kt")
    start = bridge.index("fun atomicWriteText")
    region = bridge[start:start + 1600]
    assert "return fsyncDirectory(dir)" in region


def test_drive001_t37_state_machine_with_op_id():
    """T-37: the state machine carries an op id; a stale
    callback cannot clear a newer operation."""
    src = _drive_log()
    assert "r.opId != currentOpId" in src
    assert "private fun transition(expected: OpState, opId: Long," in src


def test_drive001_t38_lock_not_held_during_async_work():
    """T-38: SAVE_LOCK is never held during the background
    write — the write thread only calls endOperation /
    notifySaveFinished (which take the lock briefly)."""
    src = _drive_log()
    start = src.index("Thread {")
    region = src[start:start + 2600]
    assert "synchronized(SAVE_LOCK)" not in region
    assert "synchronized(SAVE_LOCK)" not in region


def test_drive001_t39_strict_absence_check():
    """T-39: absence is decided by Os.stat + ENOENT, never by
    File.exists() alone."""
    src = _drive_log()
    assert "Os.stat(f.absolutePath)" in src
    assert "OsConstants.ENOENT" in src
    # No File.exists() decision for the folder/counter/marker
    # (the phrase appears in one explanatory comment, stripped
    # here before checking).
    code = "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith(("*", "//")))
    assert ".exists()" not in code
    reg = _drive_registry()
    assert "Os.stat(" in reg
    assert "OsConstants.ENOENT" in reg
    reg_code = "\n".join(
        line for line in reg.splitlines()
        if not line.strip().startswith(("*", "//")))
    assert ".exists()" not in reg_code


def test_drive001_t40_ui_sync_on_create_and_resume():
    """T-40: the UI is resynced from the static state in both
    onCreate and onResume."""
    src = _drive_log()
    assert "syncUiFromState()" in src
    assert src.count("syncUiFromState()") >= 3


def test_drive001_t41_rename_ok_dir_fsync_fail_rereads():
    """T-41: after a rename-OK / dir-fsync-fail, the stored
    file is re-read instead of assuming the old file."""
    src = _drive_log()
    start = src.index("handleFolderWriteFailed")
    region = src[start:start + 1800]
    assert "readFolderUriString()" in region
    assert "stored == oldUri" in region


def test_drive001_t42_fsync_failure_keeps_both_permissions():
    """T-42: on a sync failure both the old and the new
    permission are kept until the state is certain."""
    src = _drive_log()
    start = src.index("handleFolderWriteFailed")
    region = src[start:start + 1800]
    assert "keep BOTH" in region
    assert "keep the new permission (uncertain)" in region


def test_drive001_t43_late_result_no_effect():
    """T-43: a late picker result cannot affect a newer
    operation (op id + map checks first)."""
    src = _drive_log()
    start = src.index("override fun onActivityResult(")
    region = src[start:start + 2600]
    assert "r.opId != currentOpId" in region
    assert "requestCodeToOpId.remove(requestCode)" in region


def test_drive001_t44_mapping_survives_recreation():
    """T-44: the requestCode -> op id map is static, so it
    survives activity recreation."""
    src = _drive_log()
    assert "private val requestCodeToOpId: MutableMap<Int, PickerRequest>" in src
    assert "private var currentPickerRequestCode = -1" in src


def test_drive001_t45_transition_under_exclusion():
    """T-45: the PICKER_PENDING -> WRITING transition runs
    inside the same SAVE_LOCK section as the checks."""
    src = _drive_log()
    start = src.index("override fun onActivityResult(")
    region = src[start:start + 2600]
    assert "synchronized(SAVE_LOCK)" in region
    assert "transition(OpState.PICKER_PENDING, r.opId," in region
    assert "OpState.WRITING" in region


def test_drive001_t46_absence_vs_io_error():
    """T-46: file absence (ENOENT) and I/O errors (EACCES/EIO)
    are distinct FileState values."""
    src = _drive_log()
    assert "private enum class FileState" in src
    assert "EXISTS, NOT_EXISTS, UNKNOWN" in src


def test_drive001_t47_recreated_activity_syncs_on_finish():
    """T-47: the completion notification resyncs the resumed
    activity from the static state (not from the notification)."""
    src = _drive_log()
    start = src.index("private fun notifySaveFinished(ok: Boolean)")
    region = src[start:start + 1200]
    assert "syncUiFromState()" in region
    assert "resumedActivity" in region


def test_drive001_t48_process_death_no_stale_state():
    """T-48: after a process death the static state is fresh,
    so an unknown requestCode is ignored in onActivityResult."""
    src = _drive_log()
    start = src.index("override fun onActivityResult(")
    region = src[start:start + 1200]
    assert "?: return // unknown or already invalidated" in region
    assert "unknown or already invalidated" in region


def test_drive001_t49_clear_order_marker_then_delete_then_release():
    """T-49: clear writes the marker, deletes the settings
    file, syncs the dir, then releases (doClearFolder order)."""
    src = _drive_log()
    start = src.index("private fun doClearFolder()")
    region = src[start:start + 2400]
    marker = region.index("CLEAR_PENDING_NAME")
    delete = region.index("settings.delete()")
    release = region.index("releaseIfUnused")
    assert marker < delete < release


def test_drive001_t50_result_handling_one_lock_section():
    """T-50: the whole result-handling check sequence runs in
    one SAVE_LOCK section (map lookup, op id, state, deadline,
    removal, transition)."""
    src = _drive_log()
    start = src.index("override fun onActivityResult(")
    region = src[start:start + 2600]
    assert region.count("synchronized(SAVE_LOCK)") == 1
    assert "requestCodeToOpId[requestCode]" in region
    assert "transition(OpState.PICKER_PENDING" in region


def test_drive001_t51_map_entry_removed_on_all_exits():
    """T-51: the map entry is removed on every exit path
    (normal, cancel, launch failure, timeout, abnormal)."""
    src = _drive_log()
    assert src.count("requestCodeToOpId.remove(") >= 4
    assert src.count("removePickerRequest(") >= 2


def test_drive001_t52_begin_transition_separation():
    """T-52: beginOperation (new op) and transition (existing
    op) are separate methods with distinct roles."""
    src = _drive_log()
    assert "private fun beginOperation(expected: OpState, initial: OpState): Long" in src
    assert "private fun transition(expected: OpState, opId: Long," in src
    assert "private fun endOperation(opId: Long)" in src


def test_drive001_t53_picker_timeout_ten_minutes():
    """T-53: PICKER_TIMEOUT_MS is 10 minutes; a timeout is
    recovery, and a late result is ignored."""
    src = _drive_log()
    assert "PICKER_TIMEOUT_MS = 10 * 60 * 1000" in src
    assert "toast_picker_timeout" in src


def test_drive001_t54_late_result_refused_idle():
    """T-54: a result arriving after the 10-minute deadline is
    refused and the state returns to IDLE."""
    src = _drive_log()
    start = src.index("override fun onActivityResult(")
    region = src[start:start + 2600]
    assert "PICKER_TIMEOUT_MS" in region
    assert "endOperation(req.opId)" in region


def test_drive001_t55_timeout_vs_callback_single_winner():
    """T-55: the timeout path and the result callback share one
    SAVE_LOCK section, so only one of them wins."""
    src = _drive_log()
    start = src.index("override fun onActivityResult(")
    region = src[start:start + 2600]
    assert region.count("synchronized(SAVE_LOCK)") == 1
    start2 = src.index("private fun checkPickerTimeout()")
    region2 = src[start2:start2 + 1200]
    assert "synchronized(SAVE_LOCK)" in region2


def test_drive001_t56_op_id_mismatch_keeps_current_op():
    """T-56: on an op id mismatch only that map entry is
    removed; the current operation is untouched."""
    src = _drive_log()
    start = src.index("override fun onActivityResult(")
    region = src[start:start + 1200]
    assert "r.opId != currentOpId" in region
    assert "Stale result" in region


def test_drive001_t57_request_code_exhaustion_refuses():
    """T-57: requestCode exhaustion (beyond 65535) refuses the
    launch and leaves the state IDLE."""
    src = _drive_log()
    start = src.index("allocateRequestCode")
    region = src[start:start + 1400]
    assert "REQUEST_CODE_MAX" in region
    assert "return@synchronized -1" in region


def test_drive001_t58_next_launch_releases_only_orphans():
    """T-58: the next-launch check releases only orphaned
    permissions (registry reconcile + clear-marker handling),
    never a bulk release."""
    src = _drive_log()
    assert "startupConsistencyCheck" in src
    assert "UriPermissionRegistry.reconcile" in src
    assert "persistedUriPermissions" in src
    reg = _drive_registry()
    assert "fun reconcile(" in reg


def test_drive001_t59_allowed_transition_combinations():
    """T-59: beginOperation only from IDLE; transition only
    PICKER_PENDING -> WRITING; endOperation only to IDLE."""
    src = _drive_log()
    assert "if (state != expected)" in src
    assert "transition(OpState.PICKER_PENDING, r.opId," in src
    start = src.index("private fun endOperation(opId: Long)")
    region = src[start:start + 400]
    assert "state = OpState.IDLE" in region


def test_drive001_t60_old_process_code_no_effect():
    """T-60: a requestCode issued by an old process has no
    effect after a restart (unknown code ignored; the
    persistent counter never reuses a code)."""
    src = _drive_log()
    assert "?: return // unknown or already invalidated" in src
    assert 'COUNTER_NAME = "request_code_counter"' in src


def test_drive001_t61_no_reuse_after_restart():
    """T-61: the counter is persistent and the reservation is
    written before the map registration, so no code is reused
    across restarts."""
    src = _drive_log()
    start = src.index("allocateRequestCode")
    region = src[start:start + 1400]
    write = region.index("atomicWriteText")
    put = region.index("requestCodeToOpId.put")
    assert write < put


def test_drive001_t62_mismatch_checked_before_deadline():
    """T-62: the op id / state check runs before the deadline
    check, so a mismatched result cannot time out the current
    operation."""
    src = _drive_log()
    start = src.index("override fun onActivityResult(")
    region = src[start:start + 2600]
    mismatch = region.index("r.opId != currentOpId")
    deadline = region.index("PICKER_TIMEOUT_MS")
    assert mismatch < deadline


def test_drive001_t63_marker_before_settings_delete():
    """T-63: the clear marker is created (and synced) before
    the settings file is deleted."""
    src = _drive_log()
    start = src.index("private fun doClearFolder()")
    region = src[start:start + 2400]
    marker = region.index("CLEAR_PENDING_NAME")
    delete = region.index("settings.delete()")
    assert marker < delete


def test_drive001_t64_eacces_not_mistaken_for_absence():
    """T-64: EACCES/EIO is never mistaken for absence (statFile
    returns UNKNOWN, never NOT_EXISTS, for other errnos)."""
    src = _drive_log()
    start = src.index("private fun statFile(f: File): FileState")
    region = src[start:start + 600]
    assert "OsConstants.ENOENT" in region
    assert "UNKNOWN" in region


def test_drive001_t65_corrupt_counter_refuses_picker():
    """T-65: a corrupt counter refuses the picker launch
    (allocate returns -1; the init gate requires OK)."""
    src = _drive_log()
    assert "ReadKind.CORRUPT" in src
    assert "c.kind != ReadKind.OK" in src
    assert "marker.kind == ReadKind.OK && counter.kind == ReadKind.OK" in src


def test_drive001_t66_counter_missing_with_state_refuses():
    """T-66: a missing counter while a folder/marker exists
    refuses the picker (abnormal state, no auto-reset)."""
    src = _drive_log()
    start = src.index("fun ensureDriveInit(ctx: Context)")
    region = src[start:start + 3600]
    assert "never auto-resets to 1000" in region
    assert "checkDriveInitOk" in src


def test_drive001_t67_first_init_random_epoch():
    """T-67: the first init starts the counter from a random
    epoch (SecureRandom), not from a fixed 1000."""
    src = _drive_log()
    assert "SecureRandom" in src
    assert "EPOCH_MAX" in src
    assert "REQUEST_CODE_MIN + SecureRandom()" in src


def test_drive001_t68_persist_first_then_register_then_launch():
    """T-68: the code is reserved persistently first, then
    registered in the map, then the picker launches — a failed
    launch never reuses the code."""
    src = _drive_log()
    start = src.index("allocateRequestCode")
    region = src[start:start + 1400]
    write = region.index("atomicWriteText")
    put = region.index("requestCodeToOpId.put")
    assert write < put
    # Launch failures remove the map entry but the persisted
    # counter is never rolled back.
    assert "removePickerRequest(code)" in src


def test_drive001_t69_marker_failure_aborts_clear():
    """T-69: when the clear marker cannot be persisted, the
    clear aborts before deleting anything (permission kept)."""
    src = _drive_log()
    start = src.index("private fun doClearFolder()")
    region = src[start:start + 1200]
    assert "toast_log_folder_clear_failed" in region


def test_drive001_t70_release_failure_keeps_marker():
    """T-70: when the release fails at the startup check, the
    marker is kept and retried at the next launch."""
    src = _drive_log()
    start = src.index("private fun startupConsistencyCheck()")
    region = src[start:start + 2400]
    assert "if (released)" in region
    assert "deleteClearMarker()" in region


def test_drive001_t71_marker_present_counter_missing_refuses():
    """T-71: marker present + counter missing is abnormal and
    refuses the picker (the marker alone is not proof)."""
    src = _drive_log()
    start = src.index("fun ensureDriveInit(ctx: Context)")
    region = src[start:start + 3600]
    assert "marker.kind == ReadKind.MISSING" in region
    assert "counter.kind == ReadKind.OK" in region
    assert "checkDriveInitOk" in src


def test_drive001_t72_counter_lost_after_fallback_no_reuse():
    """T-72: a counter lost after a fallback launch cannot
    reuse the old code — the re-init starts from a fresh
    random epoch (see T-67 / T-86)."""
    src = _drive_log()
    assert "SecureRandom" in src
    assert "EPOCH_MAX" in src


def test_drive001_t73_clear_release_failure_keeps_marker():
    """T-73: when the release fails on the clear path, the
    marker is kept for the next-launch reconciliation."""
    src = _drive_log()
    start = src.index("private fun doClearFolder()")
    region = src[start:start + 2400]
    assert "if (released)" in region
    assert "keep the marker for next-launch reconciliation" in region


def test_drive001_t74_shared_uri_keeps_other_permission():
    """T-74: releasing a URI used by another feature is
    refused (isUsedByOther / the other-feature check)."""
    reg = _drive_registry()
    assert "isUsedByOther" in reg
    assert "e[0] != feature" in reg


def test_drive001_t75_both_files_lost_no_fixed_restart():
    """T-75: when both the marker and the counter are lost,
    the re-init does NOT deterministically return to 1000 —
    the fresh start comes from a SecureRandom epoch (see the
    failure model in ensureDriveInit). The probabilistic bound
    is checked in T-86; a literal 'refuse' is not what the
    adopted design does, because a genuine fresh install must
    still be able to initialise."""
    src = _drive_log()
    assert "SecureRandom" in src
    assert "REQUEST_CODE_MIN + SecureRandom()" in src


def test_drive001_t76_folder_absent_history_lost_refuses():
    """T-76: when the folder URI file is absent and the
    counter history is lost, the picker refuses (the init
    gate requires a readable counter)."""
    src = _drive_log()
    assert "checkDriveInitOk" in src
    start = src.index("private fun checkDriveInitOk(ctx: Context): Boolean")
    region = src[start:start + 800]
    assert "counter.kind == ReadKind.OK" in region


def test_drive001_t77_marker_absent_counter_corrupt_refuses():
    """T-77: marker absent + counter corrupt refuses the
    picker (both must be OK)."""
    src = _drive_log()
    start = src.index("private fun checkDriveInitOk(ctx: Context): Boolean")
    region = src[start:start + 800]
    assert "marker.kind == ReadKind.OK" in region
    assert "counter.kind == ReadKind.OK" in region


def test_drive001_t78_registry_write_failure_disables_feature():
    """T-78: when the registry write fails after the grant,
    the feature is not enabled for that URI (the permission
    is kept)."""
    src = _drive_log()
    start = src.index("private fun handleFolderPicked(data: Intent, opId: Long)")
    region = src[start:start + 2600]
    assert "UriPermissionRegistry.register" in region
    assert "toast_log_folder_unreadable" in region


def test_drive001_t79_registry_read_failure_no_release():
    """T-79: when the registry cannot be read, no permission
    is released (isUsedByOther / releaseIfUnused fail safe)."""
    reg = _drive_registry()
    assert "entries == null" in reg
    start = reg.index("fun isUsedByOther(")
    region = reg[start:start + 900]
    assert "return true" in region


def test_drive001_t80_registry_ops_serialized_under_one_lock():
    """T-80: register / unregister / read / release all run
    under the one private REGISTRY_LOCK."""
    reg = _drive_registry()
    assert "private val REGISTRY_LOCK = Any()" in reg
    assert reg.count("synchronized(REGISTRY_LOCK)") >= 5


def test_drive001_t81_release_registry_failure_reconciled():
    """T-81: a registry-update failure after a release leaves
    a stale entry that reconcile() drops at next launch."""
    reg = _drive_registry()
    assert "fun reconcile(" in reg
    start = reg.index("fun reconcile(")
    region = reg[start:start + 1200]
    assert "getPersistedUriPermissions" in region
    assert "writeLocked(ctx, entries)" in region


def test_drive001_t82_kind_a_marker_deletable():
    """T-82: Kind A (file present + old URI readable) may
    delete the marker."""
    src = _drive_log()
    start = src.index("private fun doClearFolder()")
    region = src[start:start + 2400]
    assert "kind A" in region
    assert "deleteClearMarker() // kind A" in region


def test_drive001_t83_kind_b_marker_kept():
    """T-83: Kind B (state unconfirmable) keeps the marker."""
    src = _drive_log()
    start = src.index("private fun doClearFolder()")
    region = src[start:start + 2400]
    assert "kind B" in region
    assert "keep the permission and the marker" in region


def test_drive001_t84_kind_c_release_then_marker():
    """T-84: Kind C (ENOENT confirmed) proceeds to release,
    and the marker stays until the release succeeds."""
    src = _drive_log()
    start = src.index("private fun doClearFolder()")
    region = src[start:start + 2400]
    assert "kind C" in region
    assert "if (released)" in region


def test_drive001_t85_first_init_random_start_recorded():
    """T-85: the first init picks a random start in
    1000..60000 and records it in the counter file."""
    src = _drive_log()
    assert "EPOCH_MAX = 60000" in src
    assert "atomicWriteText(ctx.filesDir, COUNTER_NAME," in src


def test_drive001_t86_reinit_collision_bound_documented():
    """T-86: after a data loss + re-init the collision bound
    (at most N/59000 per new allocation) is stated in the
    failure model comment of ensureDriveInit."""
    src = _drive_log()
    assert "N/59000" in src


def test_drive001_t87_registry_rollback_before_release():
    """T-87: on a settings-write failure the registry entry is
    removed first (under REGISTRY_LOCK); the permission is
    released only when that removal succeeds."""
    src = _drive_log()
    start = src.index("handleFolderWriteFailed")
    region = src[start:start + 1800]
    unreg = region.index("UriPermissionRegistry.unregister")
    release = region.index("releasePermissionBestEffort")
    assert unreg < release


def test_drive001_t88_classification_is_final():
    """T-88: the deletion classification (A/B/C) is the final
    determination; a dir-sync failure alone never decides the
    marker, and Kind A's condition is explicit."""
    src = _drive_log()
    assert "classification (A/B/C) is the FINAL determination" in src
    start = src.index("private fun doClearFolder()")
    region = src[start:start + 2400]
    assert "stored.trim() == uriString" in region


def test_drive001_t89_registry_lock_private_and_single_entry():
    """T-89: REGISTRY_LOCK is private to UriPermissionRegistry
    and every operation goes through its synchronized methods."""
    reg = _drive_registry()
    assert "private val REGISTRY_LOCK = Any()" in reg
    assert "fun register(" in reg
    assert "fun unregister(" in reg
    assert "fun isUsedByOther(" in reg
    assert "fun releaseIfUnused(" in reg
    assert "fun reconcile(" in reg
    # No other file reaches the lock directly.
    assert "REGISTRY_LOCK" not in _drive_log()


def test_drive001_t90_verification_inputs_defined():
    """T-90: the verification inputs are defined (marker
    content, counter content, PackageManager install times,
    other app-owned state existence)."""
    src = _drive_log()
    assert "readDecimalFile(ctx, INIT_MARKER_NAME)" in src
    assert "readDecimalFile(ctx, COUNTER_NAME)" in src
    assert "firstInstallTimes(ctx)" in src


def test_drive001_t91_verification_unavailable_only_on_io_error():
    """T-91: verification info becomes unavailable only via a
    file-read error or a PackageManager failure — absence is
    an ENOENT-verifiable fact, not 'unavailable'."""
    src = _drive_log()
    assert "ReadKind.UNREADABLE" in src
    assert "return null" in src  # firstInstallTimes: NameNotFound
    start = src.index("fun ensureDriveInit(ctx: Context)")
    region = src[start:start + 400]
    assert "verification impossible" in region


def test_drive001_t92_corrupt_registry_refuses_and_keeps():
    """T-92: a corrupt registry file refuses every
    registry-dependent operation, keeps existing permissions,
    and is never auto-deleted."""
    reg = _drive_registry()
    assert "return null" in reg
    start = reg.index("private fun readLocked(")
    region = reg[start:start + 1400]
    assert "second.contains('\\t')" in region
    assert "never auto-deleted" in reg


def test_drive001_t93_update_install_can_initialise():
    """T-93 (v2.9.1): an UPDATE install can initialise. The
    v2.9.0 first init required firstInstallTime ==
    lastUpdateTime, which is false on every updated install,
    so the picker could never open after an update (live
    defect 2026-11-08). The init must not compare the two
    install times."""
    src = _drive_log()
    start = src.index("fun ensureDriveInit(ctx: Context)")
    region = src[start:start + 3200]
    assert "neverUpdated" not in region
    assert "times[0] == times[1]" not in region


def test_drive001_t94_run_meta_does_not_block_init():
    """T-94 (v2.9.1): the LOG-001 run-meta file does not block
    the first init. Run-meta exists on any install that ran
    v2.8.6+, so requiring its absence blocked every update
    install (live defect 2026-11-08). Only genuine DRIVE-001
    state (folder file, clear marker, registry, persisted
    URI permission) may block init."""
    src = _drive_log()
    start = src.index("fun ensureDriveInit(ctx: Context)")
    region = src[start:start + 3200]
    assert "runMetaFile" not in region
    assert "FOLDER_URI_NAME" in region
    assert "CLEAR_PENDING_NAME" in region
    assert "registryFile" in region
    assert "persistedUriPermissions" in region


def test_drive001_layout_has_folder_controls():
    """Layout: the log screen carries the folder pick button,
    the folder display, and the clear button."""
    src = _res_text("layout/activity_log.xml")
    for view_id in ("log_folder_button", "log_folder_text",
                    "log_folder_clear_button"):
        assert f'android:id="@+id/{view_id}"' in src
    assert '@string/btn_log_folder' in src
    assert '@string/btn_log_folder_clear' in src


def test_drive001_strings_present():
    """Strings: every DRIVE-001 label and toast exists."""
    src = _res_text("values/strings.xml")
    for name in ("btn_log_folder", "btn_log_folder_clear",
                 "log_folder_unselected", "log_folder_name_unknown",
                 "toast_log_folder_no_write",
                 "toast_log_folder_not_creatable",
                 "toast_log_folder_unreadable",
                 "toast_log_folder_change_failed",
                 "toast_log_folder_clear_failed",
                 "toast_log_folder_no_permission",
                 "toast_picker_timeout"):
        assert f'name="{name}"' in src


def test_locale_tile_version_2_9_25():
    """AX-12 (superseded by SPEC-A11Y-OFF-002): the build now
    carries v2.9.25 / code 78 (was v2.9.23 / code 76 at Stage 3 batch 2)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    build = (root / "android-app" / "build.sh").read_text(
        encoding="utf-8")
    assert 'VERSION_NAME="2.9.25"' in build
    assert "VERSION_CODE=78" in build


def test_drive001_main_activity_calls_ensure_init():
    """First-init hooks: MainActivity, BootReceiver and the
    server constructor all run ensureDriveInit before any
    app-owned state file is created."""
    main = _kotlin_source("MainActivity.kt")
    assert "LogActivity.ensureDriveInit(this)" in main
    boot = _kotlin_source("BootReceiver.kt")
    assert "LogActivity.ensureDriveInit(" in boot
    assert boot.index("ensureDriveInit") < boot.index("BridgeLog.init")
    server = _kotlin_source("HttpBridgeServer.kt")
    assert "LogActivity.ensureDriveInit(" in server
    assert (server.index("ensureDriveInit")
            < server.index("BridgeLog.init(this.context)"))

# ----------------------------------------------------------------------
# SPEC-OVERRIDE-001 (v2.9.2): live "Override - x/y" operation
# notification. Source-level guards (OV-01..OV-15); the actual
# notification behaviour (OV-L1..OV-L7) is verified live on the phone.
# ----------------------------------------------------------------------

def _override_server():
    return _kotlin_source("HttpBridgeServer.kt")


def test_override_01_click_label_carries_coordinates():
    """OV-01: /click posts "Override - タップ x/y" (coordinates are
    the operation's own metadata)."""
    src = _override_server()
    assert '"タップ " + x + "/" + y' in src
    assert '"Override - " + label' in src


def test_override_02_swipe_label_carries_coordinates():
    """OV-02: /swipe posts "Override - スワイプ x1/y1 → x2/y2"."""
    src = _override_server()
    assert '"スワイプ " + x1 + "/" + y1 + " → " + x2 + "/" + y2' in src


def test_override_03_type_text_label_has_no_typed_text():
    """OV-03: /type_text keeps its fixed label (typed text never
    goes into the notification); the "Override - " prefix is added
    centrally in noteOperation."""
    src = _override_server()
    assert 'noteOperation(lifecycleGeneration, "文字を入力中")' in src
    assert '"Override - " + label' in src


def test_override_04_other_endpoints_keep_labels_with_prefix():
    """OV-04: every other endpoint keeps its label; the prefix is
    added centrally, so no call site can forget it."""
    src = _override_server()
    assert '"Override - " + label' in src
    for label in ('"通知を開いています"', '"スクロール中"',
                  '"アプリを起動中："', '"リンクを開いています"',
                  '"複数の操作を実行中"'):
        assert label in src


def test_override_05_dedicated_icon_in_running_only():
    """OV-05: the dedicated ic_override drawable is used in
    postOperationRunning only; "操作終了" keeps ic_dialog_info."""
    src = _override_server()
    assert src.count("R.drawable.ic_override") == 1
    assert src.count("android.R.drawable.ic_dialog_info") == 1
    icon = _res_text("drawable/ic_override.xml")
    assert 'android:viewportWidth="24"' in icon
    assert 'android:viewportHeight="24"' in icon
    assert "@android:color/white" in icon


def test_override_06_throttle_first_post_never_throttled():
    """OV-06: (a) 500 ms throttle with a first-post exemption.
    (b) The live timing is verified on the phone (OV-L3)."""
    src = _override_server()
    assert "OPERATION_NOTIFY_THROTTLE_MS = 500" in src
    assert "if (!hasPostedInSession)" in src
    assert src.count("hasPostedInSession = false") == 2
    assert "now - operationLastNotifyMs >= OPERATION_NOTIFY_THROTTLE_MS" in src
    assert "operationLastNotifyMs + OPERATION_NOTIFY_THROTTLE_MS - now" in src


def test_override_07_coordinates_are_operation_metadata():
    """OV-07: coordinates come from getInt() (integer type, not a
    range check) and are posted as-is; no typed text or URI ever
    enters a label."""
    src = _override_server()
    assert '"タップ " + x + "/" + y' in src
    assert 'noteOperation(lifecycleGeneration, "リンクを開いています", openSessionId)' in src
    assert "j.getInt(\"x\")" in src
    assert "j.getInt(\"y\")" in src


def test_override_08_title_stays_operation_in_progress():
    """OV-08: the notification title stays "操作中"; only the
    content text changes."""
    src = _override_server()
    assert '.setContentTitle("操作中")' in src


def test_override_09_batch_stays_generic():
    """OV-09: /batch notes once with a generic label; no per-step
    coordinates."""
    src = _override_server()
    # CONS-167 P1-02: the hook moved into the onFinalGuardPassed
    # callback — it runs only after the final guard passes, not
    # before runBatch. The label is still generic, used exactly once.
    # CONS-168 P1-01: the generation is an AtomicLong now (set in the
    # callback, copied in the finally around runBatch).
    assert 'batchOpGeneration.set(noteOperation(' in src
    assert src.count('"複数の操作を実行中"') == 1


def test_override_10_node_and_key_labels_covered():
    """OV-10: /type_node, /scroll_node, /click_node,
    /notification_action and /press_key labels all flow through
    the central prefix."""
    src = _override_server()
    assert '"Override - " + label' in src
    for label in ('"タップ中"', '"文字を入力中"', '"スクロール中"',
                  '"通知を開いています"', '"キー操作中（" + key + "）"',
                  '"操作を実行中（" + actionName + "）"'):
        assert label in src


def test_override_11_deferred_post_exists_with_guards():
    """OV-11: (a) a deferred post is scheduled and re-checks
    session-active + generation + token + pending label before
    posting. (b) The live timing is verified on the phone (OV-L3)."""
    src = _override_server()
    assert "runDeferredPost(capturedGeneration, capturedToken)" in src
    assert "val capturedGeneration = operationGeneration" in src
    assert "val capturedToken = pendingToken" in src
    assert "capturedGeneration != operationGeneration" in src
    assert "capturedToken != pendingToken" in src
    # Kotlin: the nullable scheduler is captured into a local and
    # null-checked before scheduling (Java called the field
    # directly — a null there would NPE).
    assert "val scheduler = operationScheduler" in src
    assert "scheduler.schedule(" in src


def test_override_12_new_session_posts_immediately():
    """OV-12: (a) a new session resets hasPostedInSession, so its
    first post is immediate regardless of the previous post time."""
    src = _override_server()
    assert src.count("hasPostedInSession = false") == 2
    assert "hasPostedInSession = true" in src


def test_override_13_session_end_discards_pending_label():
    """OV-13: (a) endOperationLocked fully invalidates the pending
    reservation, so no deferred post runs after the session ends.
    (b) Verified live as OV-L4."""
    src = _override_server()
    start = src.index("private fun endOperationLocked(): Boolean {")
    seg = src[start:start + 1500]
    assert "invalidatePendingPostLocked()" in seg


def test_override_14_old_task_never_touches_new_reservation():
    """OV-14: (a) an old deferred task checks generation + token
    and touches none of the pending fields on mismatch; an
    immediate post fully invalidates the old reservation
    (future/label/token/generation), and a later operation creates
    a fresh reservation with a new token. (b) The A/B and B/C/D
    races are confirmed by code review (live reproduction is not
    required)."""
    src = _override_server()
    assert "pendingToken = ++pendingTokenCounter" in src
    assert "pendingPostFuture = null" in src
    assert "pendingToken = -1" in src
    assert "pendingGeneration = -1" in src
    assert src.count("invalidatePendingPostLocked()") >= 4
    start = src.index("private fun runDeferredPost(")
    seg = src[start:start + 1500]
    assert "touch none of the pending fields" in seg
    assert "pendingLabel = null" in seg
    assert "pendingPostFuture = null" in seg


def test_override_15_throttle_boundary_constants():
    """OV-15: (a) the 500 ms boundary is a single named constant
    with a >= comparison. (b) The 499/500/501 ms switching is
    verified live (OV-L3)."""
    src = _override_server()
    assert "OPERATION_NOTIFY_THROTTLE_MS = 500" in src
    assert src.count("OPERATION_NOTIFY_THROTTLE_MS") >= 3
    assert "now - operationLastNotifyMs >= OPERATION_NOTIFY_THROTTLE_MS" in src


def test_override_p202_drive_init_check_failure_fails_closed():
    """CONS-078 P2-02: a failed persisted-URI-permission check in
    ensureDriveInit is NOT "absent" — init is refused. Kotlin form
    (Stage 3 batch 2): the check is a try expression whose catch
    branch yields false, so a failed check never counts as
    "no persisted permissions"."""
    log = _drive_log()
    start = log.index("fun ensureDriveInit(ctx: Context)")
    region = log[start:start + 2500]
    assert "val noPersistedPermissions = try {" in region
    assert "persistedUriPermissions.isEmpty()" in region
    assert "} catch (_: Exception) {" in region
    assert "noPersistedPermissions" in log


def test_override_p202_classify_failure_keeps_folder():
    """CONS-078 P2-02: a failed persisted-URI-permission check in
    classifyAutoSaveFailure keeps the folder (failure is not
    "permission absent")."""
    log = _drive_log()
    start = log.index("private fun classifyAutoSaveFailure(treeUri: Uri): Boolean")
    seg = log[start:start + 2000]
    assert "persistedUriPermissions" in seg
    assert "catch (_: Exception)" in seg
    assert "return false" in seg



# ----------------------------------------------------------------------
# SPEC-A11Y-OFF-002 (v2.9.7): source-structure guards (section 4).
# The 001 shortcut path is DELETED (D3): no PinRequestManager, no
# token, no shortcut intent, no backup-rules files. The widget's
# third button and the second QS tile call the shared headless
# A11yOffExecutor instead. Runtime behaviour is covered by live
# verification W-L1..L3, T-L1..T-L5, X-L1, L4..L8, not here.
# ----------------------------------------------------------------------

def _executor():
    return _kotlin_source("A11yOffExecutor.kt")


def _a11y_tile():
    return _kotlin_source("A11yOffTileService.kt")


def _a11y_main():
    return _kotlin_source("MainActivity.kt")


def _a11y_tile_block():
    """The A11yOffTileService <service> block of the manifest."""
    man = _manifest_source()
    start = man.index('android:name=".A11yOffTileService"')
    start = man.rindex("<service", 0, start)
    end = man.index("</service>", start) + len("</service>")
    return man[start:end]


def test_a11yoff002_d3_shortcut_button_gone():
    """D3: the in-app shortcut button is gone from the layout and
    from MainActivity."""
    layout = _res_text("layout/activity_main.xml")
    assert "a11y_off_shortcut_button" not in layout
    main = _a11y_main()
    assert "a11y_off_shortcut_button" not in main
    assert "pinButton" not in main


def test_a11yoff002_d3_pin_manager_gone():
    """D3: PinRequestManager.java is deleted; no reference remains."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    assert not (root / "android-app" / "src" / "dev" / "muse" / "bridge"
                / "PinRequestManager.java").exists()
    main = _a11y_main()
    assert "PinRequestManager" not in main
    assert "requestPinShortcut" not in main


def test_a11yoff002_d3_token_gone():
    """D3: the 001 token is gone — no prefs file, no create/match/
    read helpers, no token in the executor."""
    main = _a11y_main()
    for sym in ("a11y_off_token_prefs", "getOrCreateA11yOffToken",
                "isA11yOffTokenMatch", "readA11yOffToken",
                "A11Y_OFF_TOKEN_PREFS", "KEY_A11Y_OFF_TOKEN"):
        assert sym not in main
    ex = _executor()
    for sym in ("a11y_off_token_prefs", "getOrCreateA11yOffToken",
                "isA11yOffTokenMatch", "readA11yOffToken"):
        assert sym not in ex


def test_a11yoff002_d3_backup_rules_gone():
    """D3: backup_rules.xml and data_extraction_rules.xml are
    deleted; the manifest drops both attributes but keeps
    allowBackup=false."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    xml = root / "android-app" / "res" / "xml"
    assert not (xml / "backup_rules.xml").exists()
    assert not (xml / "data_extraction_rules.xml").exists()
    man = _manifest_source()
    assert 'android:allowBackup="false"' in man
    assert "fullBackupContent" not in man
    assert "dataExtractionRules" not in man
    assert "backup_rules" not in man
    assert "data_extraction_rules" not in man


def test_a11yoff002_d3_mainactivity_intent_handling_gone():
    """D3: MainActivity no longer handles the disable intent and no
    longer owns the disable flow (executor owns it now)."""
    main = _a11y_main()
    for sym in ("handleDisableIntent", "handleDisableA11y",
                "ACTION_DISABLE_A11Y", "EXTRA_DISABLE_TOKEN",
                "onNewIntent", "disableInProgress", "disableGeneration",
                "MAIN_HANDLER", "scheduleDisableCheck", "finishDisable",
                "onA11yBroadcastForDisable", "refreshPinButton",
                "getAccessibilityState", "A11yState"):
        assert sym not in main, sym
    # the a11y-state receiver still refreshes the UI, nothing else
    assert "a11yStateReceiver" in main


def test_a11yoff002_d3_shortcut_strings_gone():
    """D3: shortcut-only strings are deleted; the 001 toasts are
    reused, and the new tile/widget strings exist."""
    strings = _res_text("values/strings.xml")
    for name in ("btn_a11y_off_shortcut", "a11y_off_short_label",
                 "a11y_off_long_label", "toast_pin_failed",
                 "toast_pin_unresolved"):
        assert f'name="{name}"' not in strings, name
    for name in ("toast_a11y_off_done", "toast_a11y_already_off",
                 "toast_a11y_not_connected", "toast_a11y_off_unconfirmed",
                 "toast_a11y_status_unknown", "a11y_off_tile_label",
                 "widget_a11y_off_button", "toast_a11y_off_failed"):
        assert f'name="{name}"' in strings, name
    assert "補助機能オフ" in strings


def test_a11yoff002_d3_no_new_drawable():
    """D6: the a11y-off tile reuses the main tile's icon; no new
    drawable file was added (ic_locale is the locale tile's)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    names = sorted(p.name for p in
                   (root / "android-app" / "res" / "drawable").iterdir())
    assert names == ["card_background.xml", "ic_locale.xml",
                     "ic_override.xml", "ic_tile.xml"]


def test_a11yoff002_d5_widget_pending_intent():
    """D5: the widget's third button uses an explicit intent to the
    non-exported receiver, a dedicated action and request code, and
    FLAG_UPDATE_CURRENT | FLAG_IMMUTABLE."""
    prov = _java_source("BridgeWidgetProvider.java")
    assert "REQUEST_A11Y_OFF = 3" in prov
    assert "REQUEST_TOGGLE = 1" in prov and "REQUEST_OPEN = 2" in prov
    seg = prov[prov.index("Intent a11yOff = new Intent"):]
    assert "new Intent(context, BridgeWidgetActionReceiver.class)" in seg
    assert ".setAction(BridgeWidgetActionReceiver.ACTION_WIDGET_A11Y_OFF)" in seg
    assert "PendingIntent.getBroadcast(" in seg
    assert "REQUEST_A11Y_OFF" in seg
    assert ("PendingIntent.FLAG_UPDATE_CURRENT | "
            "PendingIntent.FLAG_IMMUTABLE") in seg
    assert "setOnClickPendingIntent(R.id.widget_a11y_off_button" in seg
    recv = _java_source("BridgeWidgetActionReceiver.java")
    assert ('ACTION_WIDGET_A11Y_OFF =\n            "dev.muse.bridge.'
            'ACTION_WIDGET_A11Y_OFF"') in recv
    assert "A11yOffExecutor.execute(context);" in recv


def test_a11yoff002_d5_widget_third_button_layout():
    """D5: the widget layout has the third button, and the provider
    labels it from the new string."""
    layout = _res_text("layout/widget_bridge.xml")
    assert "widget_a11y_off_button" in layout
    assert "@string/widget_a11y_off_button" in layout
    prov = _java_source("BridgeWidgetProvider.java")
    assert "R.id.widget_a11y_off_button" in prov
    assert "R.string.widget_a11y_off_button" in prov


def test_a11yoff002_d5_action_receiver_not_exported():
    """D5: the widget action receiver stays exported=false with no
    intent-filter, so only our own PendingIntent reaches it."""
    man = _manifest_source()
    start = man.index('android:name=".BridgeWidgetActionReceiver"')
    start = man.rindex("<receiver", 0, start)
    end = man.index("/>", start) + len("/>")
    block = man[start:end]
    assert 'android:exported="false"' in block
    assert "intent-filter" not in block


def test_a11yoff002_d6_tile_manifest():
    """D6: the manifest declares the second tile (exported=true,
    BIND_QUICK_SETTINGS_TILE, QS_TILE filter, label + icon), once."""
    man = _manifest_source()
    assert man.count('android:name=".A11yOffTileService"') == 1
    block = _a11y_tile_block()
    assert 'android:exported="true"' in block
    assert ('android:permission="android.permission.'
            'BIND_QUICK_SETTINGS_TILE"') in block
    assert ('android.service.quicksettings.action.QS_TILE') in block
    assert 'android:label="@string/a11y_off_tile_label"' in block
    assert 'android:icon="@drawable/ic_tile"' in block
    # the first tile and the locale tile are unchanged
    assert man.count('android:name=".BridgeTileService"') == 1
    assert man.count('android:name=".LocaleTileService"') == 1
    # no new permission was added
    import re
    perms = re.findall(r'<uses-permission android:name="([^"]+)"', man)
    assert sorted(perms) == sorted([
        "android.permission.INTERNET",
        "android.permission.ACCESS_NETWORK_STATE",
        "android.permission.FOREGROUND_SERVICE",
        "android.permission.FOREGROUND_SERVICE_DATA_SYNC",
        "android.permission.POST_NOTIFICATIONS",
        "android.permission.RECEIVE_BOOT_COMPLETED",
        "android.permission.REQUEST_INSTALL_PACKAGES",
        "com.termux.permission.RUN_COMMAND"])


def test_ui003_a11y_tile_state_inverted():
    """UI-003 D1 replaces A11Y-OFF-002 D6's mapping: DISABLED ->
    ACTIVE, ENABLED -> INACTIVE, UNKNOWN -> UNAVAILABLE. The tile
    reads as a mode name — lit means the off state holds. UNKNOWN is
    never misclassified."""
    tile = _a11y_tile()
    assert "A11yOffExecutor.getAccessibilityState(this)" in tile
    assert "Tile.STATE_ACTIVE" in tile
    assert "Tile.STATE_INACTIVE" in tile
    assert "Tile.STATE_UNAVAILABLE" in tile
    # the state read feeds the switch, not a cached value
    seg = tile[tile.index("private fun refreshTile()"):]
    assert "val state = A11yOffExecutor.getAccessibilityState(this)" in seg
    assert "when (state)" in seg
    assert "A11yOffExecutor.A11yState.ENABLED ->" in seg
    assert "A11yOffExecutor.A11yState.DISABLED ->" in seg
    assert "else ->" in seg
    # order-sensitive: ENABLED must be followed by INACTIVE and
    # DISABLED by ACTIVE — mere presence of both constants would not
    # prove the inversion.
    enabled = seg[seg.index("A11yOffExecutor.A11yState.ENABLED ->"):seg.index("A11yOffExecutor.A11yState.DISABLED ->")]
    assert "Tile.STATE_INACTIVE" in enabled
    disabled = seg[seg.index("A11yOffExecutor.A11yState.DISABLED ->"):seg.index("else ->")]
    assert "Tile.STATE_ACTIVE" in disabled
    after_default = seg[seg.index("else ->"):]
    assert "Tile.STATE_UNAVAILABLE" in after_default


def test_ui003_three_tile_semantics_table():
    """UI-003 D2: the three-tile semantics table, pinned at source
    level. Main tile ACTIVE = server running; a11y-off tile uses
    the inverted mapping; locale tile has no ACTIVE state."""
    # Muse Bridge tile: refreshTile reads BridgeService.isRunning()
    main = _kotlin_source("BridgeTileService.kt")
    ref = main[main.index("private fun refreshTile()"):]
    assert "BridgeService.isRunning()" in ref
    assert "Tile.STATE_ACTIVE" in ref
    assert "Tile.STATE_INACTIVE" in ref
    assert ref.index("BridgeService.isRunning()") < ref.index(
        "Tile.STATE_ACTIVE")
    # A11y-off tile: inverted mapping (details pinned by
    # test_ui003_a11y_tile_state_inverted; here only the switch
    # correspondence is re-checked).
    tile = _a11y_tile()
    seg = tile[tile.index("private fun refreshTile()"):]
    enabled = seg[seg.index("A11yOffExecutor.A11yState.ENABLED ->"):seg.index("A11yOffExecutor.A11yState.DISABLED ->")]
    assert "Tile.STATE_INACTIVE" in enabled
    disabled = seg[seg.index("A11yOffExecutor.A11yState.DISABLED ->"):seg.index("else ->")]
    assert "Tile.STATE_ACTIVE" in disabled
    # Locale tile: no ACTIVE state, always INACTIVE, subtitle shows
    # the current language (details pinned by
    # test_locale_tile_state_inactive_and_subtitle).
    loc = _locale()
    assert "Tile.STATE_ACTIVE" not in loc
    lref = loc[loc.index("private fun refreshTile()"):]
    assert "tile.state = Tile.STATE_INACTIVE" in lref
    assert "Locale.getDefault().getDisplayLanguage()" in lref


def test_ui004_longpress_manifest_mainactivity():
    """UI-004 D1: MainActivity declares exactly one
    QS_TILE_PREFERENCES intent-filter (with DEFAULT), separate
    from the MAIN/LAUNCHER filter. MainActivity stays exported
    and carries no BIND_QUICK_SETTINGS_TILE permission."""
    man = _manifest_source()
    start = man.index('android:name=".MainActivity"')
    block = man[start:man.index("</activity>", start)]
    assert 'android:exported="true"' in block
    assert "android.permission.BIND_QUICK_SETTINGS_TILE" not in block
    assert block.count(
        "android.service.quicksettings.action.QS_TILE_PREFERENCES") == 1
    # two <intent-filter> elements: MAIN/LAUNCHER, then the QS one
    filters = block.split("<intent-filter>")
    assert len(filters) == 3
    main_f, qs_f = filters[1], filters[2]
    assert "android.intent.action.MAIN" in main_f
    assert "android.intent.category.LAUNCHER" in main_f
    assert "android.service.quicksettings.action.QS_TILE_PREFERENCES" in qs_f
    assert "android.intent.category.DEFAULT" in qs_f
    assert "android.intent.action.MAIN" not in qs_f
    assert "android.intent.category.LAUNCHER" not in qs_f


def test_ui004_longpress_manifest_only():
    """UI-004 D1/D3: QS_TILE_PREFERENCES is declared only in
    AndroidManifest.xml, only on MainActivity. No Java/Kotlin
    source mentions it; the three TileServices keep their
    QS_TILE-only declarations."""
    man = _manifest_source()
    assert man.count(
        "android.service.quicksettings.action.QS_TILE_PREFERENCES") == 1
    # no Java/Kotlin source mentions it
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    hits = []
    for path in (root / "android-app" / "src").rglob("*"):
        if path.is_file() and "QS_TILE_PREFERENCES" in path.read_text(
                encoding="utf-8", errors="ignore"):
            hits.append(str(path))
    assert hits == []
    # each TileService still declares only the QS_TILE filter
    for name in ("BridgeTileService", "LocaleTileService",
                 "A11yOffTileService"):
        start = man.index(f'android:name=".{name}"')
        seg = man[start:man.index("</service>", start)]
        assert seg.count("android.service.quicksettings.action.QS_TILE") == 1
        assert "QS_TILE_PREFERENCES" not in seg
    # LogActivity (the only other activity; self-closing element)
    # declares nothing of it
    start = man.index('android:name=".LogActivity"')
    seg = man[start:man.index("/>", start)]
    assert "QS_TILE_PREFERENCES" not in seg


def test_a11yoff002_d6_tile_refresh_helper_three_sites():
    """D6 (Argon P1-01): refreshTile() updates the tile directly
    (getQsTile().setState + updateTile) and is called from all three
    sites — onStartListening, the runtime receiver, and onClick."""
    tile = _a11y_tile()
    assert "private fun refreshTile()" in tile
    helper = tile[tile.index("private fun refreshTile()"):]
    assert "qsTile" in helper
    assert "tile.state = " in helper
    assert "tile.updateTile()" in helper
    assert "requestListeningState" not in helper
    # onStartListening calls it
    start = tile[tile.index("override fun onStartListening()"):]
    start = start[:start.index("override fun onStopListening()")]
    assert "refreshTile()" in start
    # the runtime receiver calls it
    assert tile.count("refreshTile()") >= 3
    # onClick calls the executor, then refreshes directly
    click = tile[tile.index("override fun onClick()"):]
    click = click[:click.index("private fun refreshTile()")]
    assert "A11yOffExecutor.execute(this)" in click
    assert click.index("A11yOffExecutor.execute(this)") < click.index(
        "refreshTile()")


def test_a11yoff002_d6_tile_receiver_not_exported_idempotent():
    """D6 (Argon P2-01 / GPT P2-02): the tile's runtime receiver is
    registered with RECEIVER_NOT_EXPORTED, idempotently, and
    unregistered only if registered (double-unregister guarded)."""
    tile = _a11y_tile()
    assert "Context.RECEIVER_NOT_EXPORTED" in tile
    assert "private var receiverRegistered = false" in tile
    start = tile[tile.index("override fun onStartListening()"):]
    start = start[:start.index("override fun onStopListening()")]
    assert "if (!receiverRegistered)" in start
    assert "receiverRegistered = true" in start
    stop = tile[tile.index("override fun onStopListening()"):]
    stop = stop[:stop.index("override fun onClick()")]
    assert "if (receiverRegistered)" in stop
    assert "unregisterReceiver(a11yStateReceiver)" in stop
    assert "receiverRegistered = false" in stop
    assert "catch (ignored: IllegalArgumentException)" in stop


def test_a11yoff002_d7_three_valued_state():
    """D7: the executor's three-valued state read — null means
    DISABLED, an exception means UNKNOWN."""
    ex = _executor()
    assert "enum class A11yState { ENABLED, DISABLED, UNKNOWN }" in ex
    st = ex[ex.index("fun getAccessibilityState("):]
    st = st[:st.index("fun execute(")]
    assert "return A11yState.DISABLED" in st  # null string
    assert "catch (e: Exception)" in st and "return A11yState.UNKNOWN" in st


def test_a11yoff002_d7_entry_state_first():
    """D7: the outcome is decided by STATE first. UNKNOWN and
    DISABLED apply regardless of the instance; only ENABLED
    consults the instance (null -> not-connected toast)."""
    ex = _executor()
    body = ex[ex.index("fun execute("):]
    body = body[:body.index("// Stage A")]
    # state is read before the instance is consulted for the
    # not-connected branch
    assert body.index("val state = getAccessibilityState(app)") < \
        body.index("if (service == null)")
    assert "R.string.toast_a11y_status_unknown" in body
    assert "R.string.toast_a11y_already_off" in body
    assert "R.string.toast_a11y_not_connected" in body
    # no disableSelf in the early-return region
    early = body[:body.index("disableInProgress = true")]
    assert "disableSelf" not in early


def test_a11yoff002_d11_entry_ordering():
    """D11b: the in-progress check comes FIRST (silent ignore, no
    toast); early returns keep the guard false; the guard and the
    generation are set only when ENABLED and the instance is
    non-null, in the same synchronized block."""
    ex = _executor()
    body = ex[ex.index("fun execute("):]
    body = body[:body.index("// Stage A")]
    assert "synchronized(LOCK)" in body
    assert body.index("if (disableInProgress)") < body.index(
        "val state = getAccessibilityState(app)")
    assert 'Log.i(TAG, "ignored-during-execution")' in body
    # the ignore path has no toast
    ign = body[body.index("if (disableInProgress)"):]
    ign = ign[:ign.index("service = BridgeAccessibilityService.getInstance()")]
    assert "Toast" not in ign
    # guard + generation in the same synchronized block, after the
    # early returns
    assert "disableInProgress = true" in body
    assert "opGen = ++disableGeneration" in body
    assert body.index("disableInProgress = true") > body.index(
        "if (service == null)")


def test_a11yoff002_d11_atomic_completion():
    """D11c: completion ownership is acquired atomically — the guard
    is flipped and the receiver detached in the same synchronized
    block, and only the owning caller unregisters and toasts."""
    ex = _executor()
    fin = ex[ex.index("private fun finishDisable("):]
    fin = fin[:fin.index("private class DisableBroadcastReceiver")]
    sync = fin[fin.index("synchronized(LOCK)"):]
    sync = sync[:sync.index("}\n        if (toUnregister != null)")]
    assert "if (!disableInProgress || opGen != disableGeneration)" in sync
    assert "return" in sync
    assert "disableInProgress = false" in sync
    assert "currentReceiverGen == opGen" in sync
    assert "toUnregister = currentReceiver" in sync
    assert "currentReceiver = null" in sync
    assert "currentReceiverGen = 0" in sync
    # unregister + toast happen after the lock is released, once
    assert fin.count("unregisterReceiver(toUnregister)") == 1
    assert fin.count("Toast.makeText(app, toastRes, Toast.LENGTH_LONG)") == 1
    # completion notifies the open tile and the widget
    assert "TileService.requestListeningState(app," in fin
    assert "A11yOffTileService::class.java" in fin
    assert "ACTION_A11Y_STATE_CHANGED" in fin
    assert ".setPackage(app.packageName)" in fin
    assert "BridgeWidgetProvider.updateAllWidgets(app)" in fin
    # the completion trigger is logged
    assert '" via $trigger"' in fin


def test_a11yoff002_d11_per_generation_receiver():
    """D11c (GPT re-review 2 P1-01): the receiver is owned per
    operation — generation and receiver are stored and cleared
    together, and finishDisable unregisters only its own
    generation's receiver."""
    ex = _executor()
    assert "private var currentReceiver: BroadcastReceiver? = null" in ex
    assert "private var currentReceiverGen: Int = 0" in ex
    body = ex[ex.index("fun execute("):]
    body = body[:body.index("// Stage A")]
    assert "currentReceiver = receiver" in body
    assert "currentReceiverGen = opGen" in body
    fin = ex[ex.index("private fun finishDisable("):]
    fin = fin[:fin.index("private class DisableBroadcastReceiver")]
    assert "if (currentReceiverGen == opGen)" in fin
    # the receiver reference is captured, not re-read, for unregister
    assert "toUnregister = currentReceiver" in fin


def test_a11yoff002_d11_executor_receiver_not_exported():
    """D11 (Argon P2-01): the executor registers its receiver with
    RECEIVER_NOT_EXPORTED (targetSdk 34), and the unregister is
    double-unregister guarded."""
    ex = _executor()
    assert "Context.RECEIVER_NOT_EXPORTED" in ex
    assert "catch (ignored: IllegalArgumentException)" in ex


def test_a11yoff002_d11_stage_exceptions():
    """D11b (GPT re-review 2 P2-02, CONS-110 P1-01): stage-based
    exception handling. Stage A (register) and stage B (disableSelf)
    roll back; stage C (polling registration) does NOT roll back.
    A false return from postDelayed() is a registration failure
    too — not just an exception."""
    ex = _executor()
    # stage A
    assert "rollback(app, opGen, false)" in ex
    # stage B
    assert "rollback(app, opGen, true)" in ex
    assert "service!!.disableSelf()" in ex
    # stage C: the polling registration's boolean return is checked
    stage_c = ex[ex.index("// Stage C"):]
    stage_c = stage_c[:stage_c.index("private fun stageCFallback(")]
    assert "if (!scheduleDisableCheck(app, opGen, 1))" in stage_c
    assert "stageCFallback(app, opGen)" in stage_c
    assert "rollback(" not in stage_c
    # scheduleDisableCheck returns the postDelayed() result
    assert "private fun scheduleDisableCheck(" in ex
    assert "return MAIN_HANDLER.postDelayed(" in ex
    # a failed re-registration (attempt 2/3) also moves to fallback
    poll = ex[ex.index("private fun scheduleDisableCheck("):]
    poll = poll[:poll.index("private fun finishDisable(")]
    assert "if (!scheduleDisableCheck(app, opGen, attempt + 1))" in poll
    assert "stageCFallback(app, opGen)" in poll
    # rollback releases the guard and clears the receiver
    rb = ex[ex.index("private fun rollback("):]
    rb = rb[:rb.index("private fun scheduleDisableCheck(")]
    assert "disableInProgress = false" in rb
    assert "currentReceiver = null" in rb
    assert "currentReceiverGen = 0" in rb


def test_a11yoff002_d11_stage_c_fallback():
    """D11b (GPT re-review 3 P1-01, CONS-110 P1-01/P2-01): stage C has
    a guaranteed fallback completion independent of the failed
    polling — a one-shot delayed re-check at the 6 s mark,
    generation-checked; if the fallback registration itself fails
    (exception OR a false return from postDelayed), it completes
    immediately with a final state check. The state is logged
    distinctly (DISABLED / ENABLED / UNKNOWN)."""
    ex = _executor()
    assert "private fun stageCFallback(" in ex
    fb = ex[ex.index("private fun stageCFallback("):]
    fb = fb[:fb.index("private fun rollback(")]
    # immediate re-check first, with the state logged distinctly
    assert 'Log.i(TAG, "stage C fallback state=$now")' in fb
    assert "if (now == A11yState.DISABLED)" in fb
    # one-shot delayed re-check at the 6 s mark
    assert "DISABLE_CHECK_INTERVAL_MS * DISABLE_CHECK_MAX" in fb
    assert "posted = MAIN_HANDLER.postDelayed(" in fb
    # a false return is handled, not just an exception
    assert "if (!posted)" in fb
    assert "stage C fallback not posted" in fb
    # fallback registration failure -> immediate completion
    assert "stage C fallback registration failed" in fb
    # the fallback completes with success or timeout, never silently
    assert "R.string.toast_a11y_off_done" in fb
    assert "R.string.toast_a11y_off_unconfirmed" in fb
    # the fallback logs the state distinctly (P2-01)
    assert 'Log.i(TAG, "fallback state=$state")' in fb
    assert 'Log.i(TAG, "fallback immediate state=$state")' in fb


def test_a11yoff002_d11_no_token_no_server_stop():
    """D4/D7: the executor uses no token and never stops the server."""
    ex = _executor()
    assert "stopService" not in ex
    assert "BridgeService" not in ex
    assert "server.stop" not in ex
    assert "A11yState.DISABLED" in ex


def test_a11yoff002_d11_polling_and_broadcast():
    """D7/D11: 2 s x 3 polling, generation-checked; the broadcast is
    a fast path that completes only on DISABLED. A failed polling
    registration (false return) moves to the stage C fallback."""
    ex = _executor()
    assert "DISABLE_CHECK_INTERVAL_MS = 2000L" in ex
    assert "DISABLE_CHECK_MAX = 3" in ex
    poll = ex[ex.index("private fun scheduleDisableCheck("):]
    poll = poll[:poll.index("private fun finishDisable(")]
    assert "if (!disableInProgress || opGen != disableGeneration)" in poll
    assert "A11yState.DISABLED" in poll
    assert "attempt < DISABLE_CHECK_MAX" in poll
    assert "if (!scheduleDisableCheck(app, opGen, attempt + 1))" in poll
    bc = ex[ex.index("private class DisableBroadcastReceiver"):]
    assert "if (getAccessibilityState(app) == A11yState.DISABLED)" in bc
    assert 'finishDisable(app, opGen, R.string.toast_a11y_off_done, "broadcast")' in bc


def test_a11yoff002_d11_logs():
    """D11d: the executor logs request / already-off / UNKNOWN /
    ignored-during-execution / success / timeout, with the
    completion trigger type."""
    ex = _executor()
    assert 'private const val TAG = "A11yOff"' in ex
    assert 'Log.i(TAG, "request")' in ex
    assert 'Log.i(TAG, "already-off")' in ex
    assert 'Log.i(TAG, "UNKNOWN")' in ex
    assert 'Log.i(TAG, "ignored-during-execution")' in ex
    assert 'Log.i(TAG, "not-connected")' in ex
    assert '(if (success) "success" else "timeout") + " via $trigger"' in ex


def test_a11yoff002_version_2_9_25():
    """The build now carries v2.9.25 / code 78 (was v2.9.23 / code 76
    at Stage 3 batch 2)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    build = (root / "android-app" / "build.sh").read_text(
        encoding="utf-8")
    assert 'VERSION_NAME="2.9.25"' in build
    assert "VERSION_CODE=78" in build


def test_a11yoff002_attribution_first_line():
    """Kotlin migration: the two .kt files carry the migration
    attribution as line 1 (the SPEC-A11Y-OFF-002 v6 reference
    stays in the class KDoc, as in batch B)."""
    for src in (_executor(), _a11y_tile()):
        first = src.splitlines()[0]
        assert first == ("// Implemented by MUSE-Koyuki (Muse Spark) "
                         "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12")
        assert "SPEC-A11Y-OFF-002 v6" in src

def _locale():
    return _kotlin_source("LocaleTileService.kt")


def _locale_service_block():
    """The LocaleTileService <service> block of the manifest."""
    man = _manifest_source()
    start = man.index('android:name=".LocaleTileService"')
    # Back up to the opening <service> tag.
    start = man.rindex("<service", 0, start)
    end = man.index("</service>", start) + len("</service>")
    return man[start:end]


def _brace_block(src, start):
    """The {...} block starting at the '{' at src[start], by brace
    counting. Used to prove that a statement sits inside (or
    outside) a particular if-block, not merely before or after it
    in the file."""
    assert src[start] == "{"
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced braces")


def test_locale_tile_manifest_declares_service_once():
    """SPEC-LOCALE-TILE-001 L1/L10: the manifest declares the new tile
    service (BIND_QUICK_SETTINGS_TILE, QS_TILE, ic_locale,
    locale_tile_label), exactly once; the BridgeTileService
    declaration is unchanged."""
    man = _manifest_source()
    assert man.count('android:name=".LocaleTileService"') == 1
    assert man.count('android:name=".BridgeTileService"') == 1
    block = _locale_service_block()
    assert ('android:permission="android.permission.'
            'BIND_QUICK_SETTINGS_TILE"') in block
    assert ('android:name="android.service.quicksettings.action'
            '.QS_TILE"') in block
    assert 'android:icon="@drawable/ic_locale"' in block
    assert 'android:label="@string/locale_tile_label"' in block
    bridge = man[man.index('android:name=".BridgeTileService"'):]
    bridge = bridge[:bridge.index("</service>")]
    assert 'android:icon="@drawable/ic_tile"' in bridge
    assert 'android:label="@string/tile_label"' in bridge


def test_locale_tile_opens_locale_settings():
    """SPEC-LOCALE-TILE-001 v4 L3/L9: onClick launches the language
    settings intent with plain startActivity, implicit (no
    resolveActivity, no setComponent, no startActivityAndCollapse —
    the Intent version of startActivityAndCollapse is deprecated in
    API 34 and throws UnsupportedOperationException at targetSdk 34,
    and the v3 explicit-ification is the suspect for the silent
    failure). A launch that returns without throwing shows the
    launch-confirmation toast (v4 diagnostic, temporary). All APIs
    are minSdk 30 compatible."""
    src = _locale()
    assert "Settings.ACTION_LOCALE_SETTINGS" in src
    assert "Intent.FLAG_ACTIVITY_NEW_TASK" in src
    assert "import android.provider.Settings" in src
    # No explicit-ification and no startActivityAndCollapse in the
    # class body (the class Javadoc legitimately names them as things
    # v4 dropped, so scope to the body).
    body = src[src.index("class LocaleTileService"):]
    assert "resolveActivity" not in body
    assert "setComponent" not in body
    assert "resolveExplicit" not in body
    assert "startActivityAndCollapse" not in body
    assert "PackageManager" not in body
    assert "ResolveInfo" not in body
    assert "ComponentName" not in body
    # tryLaunch uses plain startActivity and shows the
    # launch-confirmation toast when it returns without throwing.
    launch = src[src.index("private fun tryLaunch("):]
    launch = launch[:launch.index("private fun refreshTile()")]
    assert "startActivity(intent)" in launch
    assert "R.string.toast_locale_opened" in launch
    assert "Toast.makeText" in launch
    # The confirmation toast is shown only after startActivity
    # returns (not before the launch attempt).
    assert (launch.index("startActivity(intent)")
            < launch.index("R.string.toast_locale_opened"))


def test_locale_tile_settings_fallback():
    """SPEC-LOCALE-TILE-001 v4 L3: when the locale launch throws,
    the main Settings page is tried with startActivity (implicit).
    The fallback runs only after the locale launch fails, and never
    after a successful locale launch (early return)."""
    src = _locale()
    assert "Settings.ACTION_SETTINGS" in src
    click = src[src.index("override fun onClick()"):]
    click = click[:click.index("private fun tryLaunch(")]
    # (1) Early return on a successful locale launch: the
    # if (tryLaunch(locale, "locale")) branch returns before the
    # Settings fallback is reached.
    succ = click[click.index('if (tryLaunch(locale, "locale")) {'):]
    succ = succ[:succ.index("val settings = ")]
    assert "\n            return\n" in succ
    # (2) The Settings fallback is a fresh implicit intent launched
    # with startActivity via tryLaunch (no resolve, no explicit
    # component, no startActivityAndCollapse).
    assert "val settings = Intent(Settings.ACTION_SETTINGS)" \
        in click
    assert 'tryLaunch(settings, "settings")' in click
    assert click.index('tryLaunch(locale, "locale")') < \
        click.index('tryLaunch(settings, "settings")')
    assert (click.index("val settings = ")
            < click.index('tryLaunch(settings, "settings")'))
    # (3) The failure toast comes after the Settings fallback.
    assert (click.index('tryLaunch(settings, "settings")')
            < click.index("R.string.toast_locale_open_failed"))


def test_locale_tile_logs_failed_stages():
    """SPEC-LOCALE-TILE-001 v4 L4 (CONS-105 P2-02, kept): each failed
    stage and its exception class are logged concisely via
    android.util.Log, with no persistent state. There is no
    resolveExplicit anymore, so no 'did not resolve' log and no
    startActivityAndCollapse log."""
    src = _locale()
    assert "import android.util.Log" in src
    assert 'private const val TAG = "LocaleTile"' in src
    assert 'e.javaClass.simpleName' in src
    assert 'Log.w(TAG, "startActivity $label failed: ' in src
    assert "intent did not resolve" not in src
    # The class body only: the class Javadoc legitimately names
    # startActivityAndCollapse (as a thing v4 dropped).
    body = src[src.index("class LocaleTileService"):]
    assert "startActivityAndCollapse" not in body
    # No persistent state for the diagnostics.
    assert "SharedPreferences" not in src
    assert "getSharedPreferences" not in src


def test_locale_tile_toast_only_after_all_fail():
    """SPEC-LOCALE-TILE-001 v4 L4: the failure toast is shown only
    after every launch attempt has failed (locale startActivity,
    then Settings startActivity). The launch-confirmation toast
    (toast_locale_opened) is a different toast: it is shown inside
    tryLaunch when a launch returns without throwing, never on the
    failure path. There is still no MainActivity fallback."""
    src = _locale()
    click = src[src.index("override fun onClick()"):]
    click = click[:click.index("private fun tryLaunch(")]
    assert "R.string.toast_locale_open_failed" in click
    assert "Toast.makeText" in click
    # The failure toast is the last step in onClick, after both
    # launch attempts.
    assert (click.index('tryLaunch(settings, "settings")')
            < click.index("R.string.toast_locale_open_failed"))
    # The launch-confirmation toast is NOT on the failure path: it
    # lives in tryLaunch (success), not in onClick after the
    # fallbacks.
    assert "R.string.toast_locale_opened" not in click
    launch = src[src.index("private fun tryLaunch("):]
    launch = launch[:launch.index("private fun refreshTile()")]
    assert "R.string.toast_locale_opened" in launch
    # The class body only: the header comment legitimately names
    # MainActivity (as a thing the tile must NOT fall back to).
    body = src[src.index("class LocaleTileService"):]
    assert "MainActivity" not in body


def test_locale_tile_state_inactive_and_subtitle():
    """SPEC-LOCALE-TILE-001 L2/L6: always INACTIVE; onStartListening
    refreshes the subtitle from the current language and calls
    updateTile(); an empty language name shows locale_unknown."""
    src = _locale()
    assert "Tile.STATE_INACTIVE" in src
    assert "Tile.STATE_ACTIVE" not in src
    on = src[src.index("override fun onStartListening()"):]
    on = on[:on.index("override fun onClick()")]
    assert "refreshTile()" in on
    ref = src[src.index("private fun refreshTile()"):]
    assert "tile.state = Tile.STATE_INACTIVE" in ref
    assert "Locale.getDefault().getDisplayLanguage()" in ref
    assert "tile.setSubtitle(language)" in ref
    assert "tile.updateTile()" in ref
    assert ref.index("tile.setSubtitle(language)") < ref.index(
        "tile.updateTile()")
    assert "R.string.locale_unknown" in ref
    assert "language.isEmpty()" in ref
    # L-104-01 (CONS-104 P2): negative guards — no cached locale, no
    # background polling, no scheduled updates, no persistent state.
    for bad in ("private Locale", "static Locale", "cachedLanguage",
                "cachedLocale", "Handler", "postDelayed",
                "ScheduledExecutor", "AlarmManager", "WorkManager",
                "Timer", "Thread", "SharedPreferences",
                "getSharedPreferences"):
        assert bad not in src


def test_locale_tile_no_mainline_reference():
    """SPEC-LOCALE-TILE-001 v4 L4/L5/L10: no MainActivity, token,
    executor, server, or settings write anywhere in the tile
    class. (The failure toast's placement — only after every
    launch attempt has failed — is covered by
    test_locale_tile_toast_only_after_all_fail.)"""
    src = _locale()
    # The class body only: the header comment legitimately names
    # MainActivity (as a thing the tile must NOT fall back to).
    src = src[src.index("class LocaleTileService"):]
    for bad in ("MainActivity", "token", "Token", "Executor",
                "executor", "BridgeService", "stopService",
                "disableSelf", "Settings.Secure", "putString"):
        assert bad not in src
    # The launch failure is caught (not silently swallowed):
    # tryLaunch catches the single startActivity attempt. (v3 had
    # two catches, one per launch method; v4 has one.)
    assert src.count("catch (e: Exception)") == 1


def test_locale_tile_strings_and_icon():
    """SPEC-LOCALE-TILE-001 L1/L6/L7 (v4 L4/L10): the strings exist
    with the frozen wording, including the v4 diagnostic
    launch-confirmation toast (toast_locale_opened, removed in v5),
    and ic_locale.xml exists as a stroke-drawn vector that is not
    a copy of ic_tile."""
    strings = _res_text("values/strings.xml")
    assert 'name="locale_tile_label">言語設定</string>' in strings
    assert ('name="toast_locale_open_failed">言語設定を開けませんで'
            'した</string>') in strings
    assert 'name="toast_locale_opened">言語設定を開きました</string>' \
        in strings
    assert 'name="locale_unknown">不明</string>' in strings
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    icon = (root / "android-app" / "res" / "drawable"
            / "ic_locale.xml").read_text(encoding="utf-8")
    assert "<vector" in icon
    assert 'strokeColor="#FFFFFF"' in icon
    assert "pathData" in icon
    tile = (root / "android-app" / "res" / "drawable"
            / "ic_tile.xml").read_text(encoding="utf-8")
    import re
    assert (sorted(re.findall(r'pathData="([^"]+)"', icon))
            != sorted(re.findall(r'pathData="([^"]+)"', tile)))


def test_locale_tile_attribution_first_line():
    """New Kotlin file carries the required attribution as line 1."""
    first = _locale().splitlines()[0]
    assert first == ("// Implemented by MUSE-Koyuki (Muse Spark) "
                     "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12")
    # The SPEC-LOCALE-TILE-001 design note stays in the class KDoc.
    assert "SPEC-LOCALE-TILE-001 v4" in _locale()


# ----------------------------------------------------------------------
# SPEC-KOTLIN-MIGRATION-001 v2.2, pilot (AppVersion + interop fixture).
# Source-level guards (T-M1 / D6): the compiled JVM properties
# (volatile, synchronized, static members, signatures) are verified
# by javap -v on the real build output, not here; these tests guard
# that the Kotlin sources keep the annotations and call sites the
# JVM checks depend on, and that the two-compiler pipeline is wired
# into build.sh. Behavioural verification is by compilation and dex
# inspection; a live-phone check (T-M3) still needs the user install.
# ----------------------------------------------------------------------

_KOTLIN_PILOT_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-09")


def _kotlin_source(name):
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    return (root / "android-app" / "src" / "dev" / "muse" / "bridge"
            / name).read_text(encoding="utf-8")


def _kotlin_source_exists(name):
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    return (root / "android-app" / "src" / "dev" / "muse" / "bridge"
            / name).exists()


def _java_source_exists(name):
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    return (root / "android-app" / "src" / "dev" / "muse" / "bridge"
            / name).exists()


def _build_sh():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    return (root / "android-app" / "build.sh").read_text(
        encoding="utf-8")


def test_kotlin_pilot_appversion_is_kotlin():
    """Pilot: AppVersion.java is gone, AppVersion.kt is the source."""
    assert not _java_source_exists("AppVersion.java")
    assert _kotlin_source_exists("AppVersion.kt")
    assert "object AppVersion" in _kotlin_source("AppVersion.kt")


def test_kotlin_pilot_appversion_thread_safety():
    """Pilot (D4 checklist): @Volatile on both cached fields,
    @Synchronized on load(), @JvmStatic on the Java-visible API
    (name/code/load) so the Java callers keep compiling."""
    src = _kotlin_source("AppVersion.kt")
    # Exact annotated declarations (not raw counts: the docstring
    # above mentions the same annotation names).
    assert "    @Volatile\n    private var cachedName" in src
    assert "    @Volatile\n    private var cachedCode" in src
    assert "    @JvmStatic\n    fun name(" in src
    assert "    @JvmStatic\n    fun code(" in src
    assert "    @JvmStatic\n    @Synchronized\n    private fun load(" in src


def test_kotlin_pilot_appversion_java_callers_unchanged():
    """Pilot: the Java callers were NOT modified — they still call
    AppVersion.name(ctx) / AppVersion.code(ctx) as statics, which
    only compiles because the Kotlin members are @JvmStatic.
    (Stage 3 batch 1: MainActivity is Kotlin now and calls them
    the same way. Stage 3 batch 2: LogActivity is Kotlin now too.
    Stage 3 batch 4: HttpBridgeServer is Kotlin now and calls them
    the same way.)"""
    for name in ("HttpBridgeServer.kt",):
        src = _kotlin_source(name)
        assert "AppVersion.name(" in src, name
        assert "AppVersion.code(" in src, name
    for name in ("MainActivity.kt", "LogActivity.kt"):
        src = _kotlin_source(name)
        assert "AppVersion.name(" in src, name
        assert "AppVersion.code(" in src, name


def test_kotlin_pilot_interop_fixture_bidirectional():
    """Pilot (D3): the fixture proves both directions. Kotlin ->
    Java: InteropProbe.kt calls InteropProbeJava and AppVersion.
    Java -> Kotlin: InteropProbeJava.java calls InteropProbe and
    AppVersion. Both files are inert (never called from app code)."""
    assert _kotlin_source_exists("InteropProbe.kt")
    assert _java_source_exists("InteropProbeJava.java")
    kt = _kotlin_source("InteropProbe.kt")
    assert "InteropProbeJava.javaGreeting()" in kt
    assert "AppVersion.name(ctx)" in kt
    assert "@Volatile" in kt
    assert "@Synchronized" in kt
    assert "@JvmStatic" in kt
    java = _java_source("InteropProbeJava.java")
    assert "InteropProbe.kotlinGreeting()" in java
    assert "AppVersion.name(ctx)" in java
    assert "InteropProbe.setFlag(true)" in java
    assert "probeFromJava" in java
    assert "probeFromKotlin" in kt


def test_kotlin_pilot_attribution_first_line():
    """New Kotlin/Java files carry the required attribution as line 1."""
    for name in ("AppVersion.kt", "InteropProbe.kt"):
        first = _kotlin_source(name).splitlines()[0]
        assert first == _KOTLIN_PILOT_ATTRIBUTION, name
    first = _java_source("InteropProbeJava.java").splitlines()[0]
    assert first == _KOTLIN_PILOT_ATTRIBUTION


# ----------------------------------------------------------------------
# SPEC-KOTLIN-MIGRATION-001 v2.2, Stage 1 batch B (BridgeTileService,
# LocaleTileService, BridgeService). Same D6 guards as the pilot:
# the compiled JVM properties are verified by javap -v on the real
# build output (T-M1), not here; these tests guard that the Kotlin
# sources keep the annotations and call sites the JVM checks depend
# on. Behavioural verification is by compilation and dex inspection;
# a live-phone check (T-M3) still needs the user install.
# ----------------------------------------------------------------------

_KOTLIN_BATCH_B_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12")


def test_kotlin_batch_b_services_are_kotlin():
    """Batch B: the three .java files are gone, the .kt files are
    the source, and each carries the migration attribution as
    line 1."""
    for name in ("BridgeTileService", "LocaleTileService",
                 "BridgeService"):
        assert not _java_source_exists(name + ".java"), name
        assert _kotlin_source_exists(name + ".kt"), name
        first = _kotlin_source(name + ".kt").splitlines()[0]
        assert first == _KOTLIN_BATCH_B_ATTRIBUTION, name


def test_kotlin_batch_b_bridgeservice_thread_safety():
    """Batch B (D4 checklist): @Volatile on the running flag and on
    pendingStopReason, @JvmField on ACTION_STOP and
    pendingStopReason, @JvmStatic on isRunning() so the Java
    callers keep compiling and the LOG-03 stop-reason handoff
    keeps working."""
    src = _kotlin_source("BridgeService.kt")
    assert "        @Volatile\n        private var running" in src
    assert ("        @JvmField\n        @Volatile\n"
            "        var pendingStopReason") in src
    assert "        @JvmField\n        val ACTION_STOP" in src
    assert "        @JvmStatic\n        fun isRunning()" in src


def test_kotlin_batch_b_java_callers_unchanged():
    """Batch B: the Java callers were NOT modified — they still call
    BridgeService.isRunning() and set BridgeService.pendingStopReason
    as before, which only compiles because the Kotlin members are
    @JvmStatic / @JvmField. (Stage 3 batch 1: MainActivity is Kotlin
    now and calls them the same way.)"""
    for name in ("BridgeWidgetActionReceiver.java",
                 "BridgeWidgetProvider.java"):
        src = _java_source(name)
        assert "BridgeService.isRunning()" in src, name
    for name in ("BridgeWidgetActionReceiver.java",):
        src = _java_source(name)
        assert "BridgeService.pendingStopReason" in src, name
    main = _kotlin_source("MainActivity.kt")
    assert "BridgeService.isRunning()" in main
    assert "BridgeService.pendingStopReason" in main


_KOTLIN_BATCH_C_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12")


def test_kotlin_batch_c_a11yoff_are_kotlin():
    """Batch C: the two .java files are gone, the .kt files are
    the source, and each carries the migration attribution as
    line 1."""
    for name in ("A11yOffExecutor", "A11yOffTileService"):
        assert not _java_source_exists(name + ".java"), name
        assert _kotlin_source_exists(name + ".kt"), name
        first = _kotlin_source(name + ".kt").splitlines()[0]
        assert first == _KOTLIN_BATCH_C_ATTRIBUTION, name


def test_kotlin_batch_c_executor_thread_safety():
    """Batch C (D4 checklist): @JvmStatic on execute() and
    getAccessibilityState() so the Java widget caller keeps
    compiling; the LOCK guard and the synchronized entry/completion
    are preserved (no volatile fields — the guard fields are
    LOCK-guarded, not volatile)."""
    src = _kotlin_source("A11yOffExecutor.kt")
    assert "    @JvmStatic\n    fun execute(" in src
    assert "    @JvmStatic\n    fun getAccessibilityState(" in src
    assert "private val LOCK = Any()" in src
    assert "synchronized(LOCK)" in src
    assert "private var disableInProgress = false" in src
    assert "private var disableGeneration = 0" in src


def test_kotlin_batch_c_java_callers_unchanged():
    """Batch C: the Java caller was NOT modified — the widget
    receiver still calls A11yOffExecutor.execute(context), which
    only compiles because the Kotlin member is @JvmStatic."""
    src = _java_source("BridgeWidgetActionReceiver.java")
    assert "A11yOffExecutor.execute(context);" in src


_KOTLIN_BATCH_D_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12")


def test_kotlin_batch_d_registry_is_kotlin():
    """Batch D: the .java file is gone, the .kt file is the
    source, and it carries the migration attribution as line 1."""
    assert not _java_source_exists("UriPermissionRegistry.java")
    assert _kotlin_source_exists("UriPermissionRegistry.kt")
    first = _kotlin_source("UriPermissionRegistry.kt").splitlines()[0]
    assert first == _KOTLIN_BATCH_D_ATTRIBUTION


def test_kotlin_batch_d_registry_interop():
    """Batch D (D4 checklist): @JvmStatic on the five public
    methods so the Java LogActivity caller keeps compiling;
    registryFile stays a plain static method (public, because
    Kotlin has no package-private and internal would mangle the
    name to registryFile$main, unusable from Java);
    FEATURE_LOG_FOLDER stays a static constant; the lock stays
    a private Any() guarded by synchronized blocks."""
    src = _kotlin_source("UriPermissionRegistry.kt")
    assert "object UriPermissionRegistry" in src
    assert "const val FEATURE_LOG_FOLDER" in src
    assert "private val REGISTRY_LOCK = Any()" in src
    for name in ("register(", "unregister(", "isUsedByOther(",
                 "releaseIfUnused(", "reconcile(", "registryFile("):
        assert "    @JvmStatic\n    fun " + name in src, name
    assert src.count("synchronized(REGISTRY_LOCK)") >= 5


def test_kotlin_batch_d_registry_split_equivalence():
    """Batch D: Kotlin's String.split rejects a negative limit,
    so the corrupt-line check is an indexOf parse, not a split.
    It must be exactly equivalent to Java's split("\\t", -1) +
    length-2 check: exactly one tab, both fields non-empty."""
    src = _kotlin_source("UriPermissionRegistry.kt")
    assert 'split("\\t", -1)' not in src
    assert "val tab = line.indexOf('\\t')" in src
    assert "if (tab < 0)" in src
    assert "second.contains('\\t')" in src
    assert "entries.add(arrayOf(first, second))" in src


def test_kotlin_batch_d_java_callers_unchanged():
    """Batch D: the caller was NOT modified in behaviour — LogActivity
    (Kotlin since Stage 3 batch 2) still calls the registry the
    same way, which only compiles because the Kotlin members are
    @JvmStatic (registryFile is public @JvmStatic: Kotlin has no
    package-private)."""
    src = _kotlin_source("LogActivity.kt")
    assert "UriPermissionRegistry.registryFile(ctx)" in src
    assert "UriPermissionRegistry.register(this," in src
    assert "UriPermissionRegistry.releaseIfUnused(this," in src
    assert "UriPermissionRegistry.reconcile(this)" in src
    assert "UriPermissionRegistry.FEATURE_LOG_FOLDER" in src


_KOTLIN_BATCH_E_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12")


def test_kotlin_batch_e_pingprober_is_kotlin():
    """Batch E: the .java file is gone, the .kt file is the
    source, and it carries the migration attribution as line 1.
    (No pre-existing test referenced PingProber — the migration
    adds the first source-level coverage for it.)"""
    assert not _java_source_exists("PingProber.java")
    assert _kotlin_source_exists("PingProber.kt")
    first = _kotlin_source("PingProber.kt").splitlines()[0]
    assert first == _KOTLIN_BATCH_E_ATTRIBUTION


def test_kotlin_batch_e_pingprober_interop():
    """Batch E (D4 checklist): @JvmStatic on the five public
    methods so the Java callers keep compiling; Result fields are
    @JvmField so Java keeps its field access (r.target, r.rttMs,
    r.error, ...); Listener is a fun interface so the Java lambda
    in MainActivity keeps working; the four state fields stay
    @Volatile (the unsynchronised readers rely on it); the lock
    moved from the Class object to the singleton instance
    (@Synchronized + synchronized(PingProber)) with no outside
    locker."""
    src = _kotlin_source("PingProber.kt")
    assert "object PingProber" in src
    for name in ("setLastClientIp(", "getLastClientIp(",
                 "getLastResult(", "isProbing(", "probe("):
        assert "    @JvmStatic\n    fun " + name in src, name
    assert "fun interface Listener" in src
    for field in ("target: String?", "method: String?",
                  "rttMs: Long", "sent: Int", "received: Int",
                  "measuredAtMs: Long", "measuredAtElapsedMs: Long",
                  "error: String?"):
        assert "        @JvmField val " + field in src, field
    assert src.count("@Volatile") >= 4
    assert "private var lastClientIp" in src
    assert "private var lastResult" in src
    assert "private var lastProbeStartedMs" in src
    assert "private var probing" in src
    # One monitor for all probe-state critical sections (PROBE_LOCK).
    # @Synchronized is deliberately NOT used: on a @JvmStatic
    # function it locks the Class object, while synchronized
    # (PingProber) would lock the singleton instance — two
    # different monitors, which would break the probe/throttle
    # exclusion (caught by javap on the first build).
    assert "@Synchronized" not in src
    assert "private val PROBE_LOCK = Any()" in src
    assert src.count("synchronized(PROBE_LOCK)") == 3
    assert "PingProber.class" not in src


def test_kotlin_batch_e_java_callers_unchanged():
    """Batch E: the Java callers were NOT modified — MainActivity
    (Kotlin since Stage 3 batch 1) still probes with a lambda and
    reads Result fields directly,
    and HttpBridgeServer still records the client IP and reads
    the last result's fields. That only compiles because the
    Kotlin members are @JvmStatic and the Result fields are
    @JvmField."""
    main = _kotlin_source("MainActivity.kt")
    assert "PingProber.probe { runOnUiThread { updatePingDisplay() } }" in main
    assert "val r = PingProber.getLastResult()" in main
    assert "PingProber.getLastClientIp()" in main
    assert "PingProber.isProbing()" in main
    assert "r.target" in main
    assert "r.rttMs" in main
    assert "r.error" in main
    srv = _kotlin_source("HttpBridgeServer.kt")
    assert "PingProber.setLastClientIp(remote.hostAddress)" in srv
    assert "val r = PingProber.getLastResult()" in srv
    assert ".put(\"target\", r.target)" in srv
    assert ".put(\"rtt_ms\", r.rttMs)" in srv


_KOTLIN_BATCH_F_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-12")


def test_kotlin_batch_f_notification_listener_is_kotlin():
    """Batch F: the .java file is gone, the .kt file is the
    source, and it carries the migration attribution as line 1.
    (No pre-existing test referenced BridgeNotificationListener
    by source — the migration adds the first source-level
    coverage for it.)"""
    assert not _java_source_exists("BridgeNotificationListener.java")
    assert _kotlin_source_exists("BridgeNotificationListener.kt")
    first = _kotlin_source("BridgeNotificationListener.kt").splitlines()[0]
    assert first == _KOTLIN_BATCH_F_ATTRIBUTION


def test_kotlin_batch_f_notification_listener_interop():
    """Batch F (D4 checklist): a service class (not an object)
    extending NotificationListenerService; @JvmStatic on the
    eight public companion methods so the Java callers keep
    compiling; all NotificationEvent / NotificationsResult
    fields are @JvmField so Java keeps its field access
    (e.seq, e.type, nr.latestSeq, nr.dropped, ...); accepting
    stays @Volatile; the buffer critical sections share one
    explicit BUFFER_LOCK (no @Synchronized — the batch E
    lesson); wait/notify go through a java.lang.Object cast,
    which Kotlin's Any does not expose."""
    src = _kotlin_source("BridgeNotificationListener.kt")
    assert "class BridgeNotificationListener : NotificationListenerService()" in src
    for name in ("setAccepting(", "awaitQuiesce(", "inFlightCount(",
                 "getInstance(", "isAccessGranted(", "getEvents(",
                 "openNotification(", "replyNotification("):
        assert "        @JvmStatic\n        fun " + name in src, name
    for field in ("seq: Long", "type: String", "key: String",
                  "packageName: String", "id: Int", "tag: String?",
                  "title: String", "text: String", "t: Long",
                  "ongoing: Boolean", "groupSummary: Boolean",
                  "removedT: Long"):
        assert "        @JvmField var " + field in src, field
    for field in ("events: MutableList<NotificationEvent>",
                  "latestSeq: Long", "dropped: Boolean"):
        assert "        @JvmField var " + field in src, field
    assert src.count("@Volatile") >= 1
    assert "private var accepting" in src
    assert "        @Volatile\n        private var instance: BridgeNotificationListener?" in src
    assert "@Synchronized" not in src
    assert "private val BUFFER_LOCK = Any()" in src
    assert src.count("synchronized(BUFFER_LOCK)") == 3
    assert "(quiesceLock as Object).wait(remaining)" in src
    assert "(quiesceLock as Object).notifyAll()" in src
    assert "BridgeNotificationListener.class" not in src


def test_kotlin_batch_f_java_callers_unchanged():
    """Batch F: the Java callers were NOT modified — HttpBridgeServer
    still drives the listener the same way (accepting, quiesce,
    access grant, instance, events, open/reply) and reads the
    result/event fields directly, and MainActivity (Kotlin since
    Stage 3 batch 1) still checks
    the access grant. That only compiles because the Kotlin
    members are @JvmStatic and the fields are @JvmField."""
    srv = _kotlin_source("HttpBridgeServer.kt")
    assert "BridgeNotificationListener.setAccepting(true)" in srv
    assert "BridgeNotificationListener.setAccepting(false)" in srv
    assert "BridgeNotificationListener.awaitQuiesce(5000)" in srv
    assert "BridgeNotificationListener.inFlightCount()" in srv
    assert "BridgeNotificationListener.isAccessGranted(context)" in srv
    assert "BridgeNotificationListener.getInstance()" in srv
    assert "BridgeNotificationListener.getEvents(since, limit, pkg)" in srv
    assert "BridgeNotificationListener.openNotification(key)" in srv
    assert "BridgeNotificationListener.replyNotification(key, text)" in srv
    assert ".put(\"latest_seq\", nr.latestSeq)" in srv
    assert ".put(\"dropped\", nr.dropped)" in srv
    assert "e.seq" in srv
    assert "e.type" in srv
    assert "e.key" in srv
    main = _kotlin_source("MainActivity.kt")
    assert "BridgeNotificationListener.isAccessGranted(this)" in main


_KOTLIN_BATCH_G_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-13")


def test_kotlin_batch_g_bridge_log_is_kotlin():
    """Batch G: the .java file is gone, the .kt file is the
    source, and it carries the migration attribution as line 1.
    (Two pre-existing tests read BridgeLog.java source — T-33
    and T-36, updated to read the .kt — so the migration keeps
    their coverage and adds the first D4 interop test.)"""
    assert not _java_source_exists("BridgeLog.java")
    assert _kotlin_source_exists("BridgeLog.kt")
    first = _kotlin_source("BridgeLog.kt").splitlines()[0]
    assert first == _KOTLIN_BATCH_G_ATTRIBUTION


_STAGE3_MAIN_ACTIVITY_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-13")


_STAGE3_LOG_ACTIVITY_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-13")


def test_kotlin_stage3_main_activity_is_kotlin():
    """Stage 3 batch 1: the .java file is gone, the .kt file is the
    source, and it carries the migration attribution as line 1."""
    assert not _java_source_exists("MainActivity.java")
    assert _kotlin_source_exists("MainActivity.kt")
    first = _kotlin_source("MainActivity.kt").splitlines()[0]
    assert first == _STAGE3_MAIN_ACTIVITY_ATTRIBUTION


def test_kotlin_stage3_main_activity_d4_interop():
    """Stage 3 batch 1 (D4 checklist): the public statics stay
    Java-visible — @JvmStatic on the seven public methods so the
    Java callers (HttpBridgeServer, BridgeWidgetProvider) keep
    compiling, const val for the five public constants so Java
    keeps its field access (MainActivity.PREFS, ...); the class
    still extends Activity, and stays open because the Java
    original was a non-final public class (CONS-161 P2-02)."""
    src = _kotlin_source("MainActivity.kt")
    assert "open class MainActivity : Activity()" in src
    assert "companion object" in src
    for name in ("getOrCreateToken(", "getPersistedMode(", "setPersistedMode(",
                 "isTailscaleIp(", "findTailscaleIps(", "findTailscaleIp(",
                 "isAccessibilityEnabled("):
        assert "        @JvmStatic\n        fun " + name in src, name
    for name in ("PREFS", "KEY_TOKEN", "KEY_MODE", "MODE_MINIMAL",
                 "MODE_BANKING_SAFE"):
        assert "        const val " + name + " = " in src, name


def test_kotlin_stage3_main_activity_setpersistedmode_null_contract():
    """CONS-161 P2-01 regression: setPersistedMode keeps the Java
    null contract — the Java original used
    MODE_BANKING_SAFE.equals(mode), null-safe, falling back to
    MODE_MINIMAL. The Kotlin parameter must be String? (a non-null
    String would throw NPE on a Java null call) and the comparison
    must stay null-safe (==)."""
    src = _kotlin_source("MainActivity.kt")
    assert "fun setPersistedMode(ctx: Context, mode: String?)" in src
    start = src.index("fun setPersistedMode(ctx: Context, mode: String?)")
    end = src.index("fun isTailscaleIp(", start)
    body = src[start:end]
    assert "MODE_BANKING_SAFE == mode" in body
    assert "MODE_MINIMAL" in body


def test_kotlin_stage3_main_activity_refreshaddress_label():
    """CONS-162 P1-01 regression: refreshAddress() must drive the
    address toggle label from addressRevealed, not tokenRevealed.
    The Java original used addressRevealed; the first Kotlin
    conversion copied refreshToken's tokenRevealed by mistake
    (a D5 behavioral regression Mitsuki caught by direct
    inspection of the original)."""
    src = _kotlin_source("MainActivity.kt")
    start = src.index("private fun refreshAddress()")
    end = src.index("private fun probePing()", start)
    body = src[start:end]
    assert ("if (addressRevealed) R.string.token_hide "
            "else R.string.token_show") in body
    assert "tokenRevealed" not in body


def test_kotlin_stage3_log_activity_is_kotlin():
    """Stage 3 batch 2: the .java file is gone, the .kt file is the
    source, and it carries the migration attribution as line 1."""
    assert not _java_source_exists("LogActivity.java")
    assert _kotlin_source_exists("LogActivity.kt")
    first = _kotlin_source("LogActivity.kt").splitlines()[0]
    assert first == _STAGE3_LOG_ACTIVITY_ATTRIBUTION


def test_kotlin_stage3_log_activity_d4_interop():
    """Stage 3 batch 2 (D4 checklist): the one public static
    (ensureDriveInit) stays Java-visible via @JvmStatic so the
    Java caller (HttpBridgeServer) and the Kotlin callers
    (MainActivity, BootReceiver) keep compiling; the class stays
    open because the Java original was a non-final public class
    (CONS-161 P2-02 precedent); the SAVE_LOCK stays a private
    Any() guarded by synchronized blocks (eight, as in the
    Java); the operation state machine stays static in the
    companion object."""
    src = _kotlin_source("LogActivity.kt")
    assert "open class LogActivity : Activity()" in src
    assert "    companion object {" in src
    assert "        @JvmStatic\n        fun ensureDriveInit(ctx: Context)" in src
    assert "private val SAVE_LOCK = Any()" in src
    assert src.count("synchronized(SAVE_LOCK)") >= 8
    assert "private var state = OpState.IDLE" in src
    assert "private enum class OpState" in src
    assert "private var resumedActivity: WeakReference<LogActivity>?" in src


def test_kotlin_stage3_log_activity_nullable_views():
    """Stage 3 batch 2 (D4): the Java view fields start null and
    setButtonsEnabled / refreshFolderDisplay guard against null,
    so the Kotlin fields are nullable vars — not lateinit, whose
    null checks would be dead. The guards stay meaningful."""
    src = _kotlin_source("LogActivity.kt")
    for name in ("logText", "scrollView", "refreshButton", "exportButton",
                 "folderButton", "folderClearButton", "folderText"):
        assert "private var " + name + ":" in src, name
    assert "lateinit var" not in src
    assert "refreshButton?.isEnabled = enabled" in src
    assert "if (folderText == null) {" in src


def test_kotlin_stage3_log_activity_in_keyword():
    """Stage 3 batch 2: `in` is a Kotlin keyword, so the Java
    FileInputStream local `in` is renamed `input` in readSmallFile
    (same close-in-finally behaviour)."""
    src = _kotlin_source("LogActivity.kt")
    start = src.index("private fun readSmallFile(")
    end = src.index("private fun readDecimalFile(", start)
    body = src[start:end]
    assert "var input: FileInputStream? = null" in body
    assert "input = FileInputStream(f)" in body
    assert "input.close()" in body
    assert "    var in:" not in body


_STAGE3_A11Y_SERVICE_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-13")


def test_kotlin_stage3_a11y_service_is_kotlin():
    """Stage 3 batch 3: the .java file is gone, the .kt file is the
    source, and it carries the migration attribution as line 1."""
    assert not _java_source_exists("BridgeAccessibilityService.java")
    assert _kotlin_source_exists("BridgeAccessibilityService.kt")
    first = _kotlin_source("BridgeAccessibilityService.kt").splitlines()[0]
    assert first == _STAGE3_A11Y_SERVICE_ATTRIBUTION


def test_kotlin_stage3_a11y_service_d4_interop():
    """Stage 3 batch 3 (D4 checklist): the class stays open (the
    Java original was a non-final public class, CONS-161 P2-02
    precedent); getInstance() stays a public static via @JvmStatic
    so the Java caller (HttpBridgeServer) and the Kotlin callers
    (MainActivity, A11yOffExecutor) keep compiling; the companion
    stays public for the same reason (CONS-163 P2-01 lesson);
    ACTION_A11Y_STATE_CHANGED stays a const val so Java and
    Kotlin keep their field access; the @Volatile instance field
    and the @Volatile BatchRegistry field stay volatile; the
    synchronized methods stay @Synchronized (recordEvent,
    getEvents, the *Internal node actions, runBatchInternal,
    currentStateInternal, recycleLastNodes). CONS-165 P2-01:
    the 13 public nested classes stay `open` — the Java
    originals were non-final public static classes, so closing
    them would narrow the D4 inheritance contract."""
    src = _kotlin_source("BridgeAccessibilityService.kt")
    assert "open class BridgeAccessibilityService : AccessibilityService()" in src
    for cls in ("ServiceTimeoutException", "BatchMismatchException",
                "BatchGoneException", "StaleRevisionException",
                "NodeNotFoundException", "ActionOutcome", "BatchAction",
                "ActionResult", "BatchResult", "State", "Node",
                "UiEvent", "EventsResult"):
        assert "    open class %s" % cls in src, cls
    assert "    companion object {" in src
    assert "        @JvmStatic\n        fun getInstance(): BridgeAccessibilityService?" in src
    assert '        const val ACTION_A11Y_STATE_CHANGED =' in src
    assert "        @Volatile\n        private var instance: BridgeAccessibilityService?" in src
    assert "    @Volatile\n    private var batchRegistry: BatchRegistry? = null" in src
    for name in ("recordEvent(", "getEvents(", "scrollNodeInternal(",
                 "nodeActionInternal(", "clickNodeInternal(",
                 "typeNodeInternal(", "runBatchInternal(",
                 "currentStateInternal(", "recycleLastNodes("):
        assert "    @Synchronized\n" in src and name in src, name
    assert src.count("@Synchronized") >= 9


def test_kotlin_stage3_a11y_service_jvmfield():
    """Stage 3 batch 3 (D4): HttpBridgeServer.kt reads AND writes
    the nested data classes' fields directly (br.batchId,
    ba.actionId = ..., st.texts, e.seq, o.ok, r.hashBefore ...),
    so every public field of the eight nested classes carries
    @JvmField — without it Java would see only getters and the
    server would not compile. The BatchResult copy constructor
    deliberately does NOT copy `replayed` (the Java original
    left it at its default and BatchRegistry sets it on the copy
    afterwards); the lists are shared references, as in Java."""
    src = _kotlin_source("BridgeAccessibilityService.kt")
    for field in ("var ok: Boolean", "var snapshotId: Int",
                  "var actionId: String", "var type: String",
                  "var nodeId: String", "var x: Int", "var y: Int",
                  "var durationMs: Long", "var text: String",
                  "var key: String", "var waitMs: Long",
                  "var expectChange: Boolean", "var status: String",
                  "var error: String", "var hashBefore: Int",
                  "var hashAfter: Int", "var batchId: String",
                  "var replayed: Boolean",
                  "var results: MutableList<ActionResult>",
                  "var stoppedAt: Int", "var stopReason: String",
                  "var payloadHash: String",
                  "var completedActionIds: MutableList<String>",
                  "var failedActionId: String", "var width: Int",
                  "var height: Int", "var packageName: String",
                  "var texts: MutableList<String>",
                  "var revision: Int", "var treeHash: Int",
                  "var eventSeq: Long", "var snapshotValid: Boolean",
                  "var snapshotStable: Boolean",
                  "var nodes: MutableList<Node>",
                  "var focusedId: String?", "var imeVisible: Boolean",
                  "var truncated: Boolean", "var parentId: String?",
                  "var className: String",
                  "var contentDescription: String", "var left: Int",
                  "var top: Int", "var right: Int", "var bottom: Int",
                  "var clickable: Boolean", "var focusable: Boolean",
                  "var focused: Boolean", "var editable: Boolean",
                  "var scrollable: Boolean", "var enabled: Boolean",
                  "var selected: Boolean", "var password: Boolean",
                  "var inputType: Int", "var resourceId: String",
                  "var actions: MutableList<String>", "var seq: Long",
                  "var t: Long", "var desc: String",
                  "var coalesced: Int",
                  "var events: MutableList<UiEvent>",
                  "var latestSeq: Long", "var dropped: Boolean"):
        assert "        @JvmField " + field in src, field
    start = src.index("constructor(other: BatchResult)")
    body = src[start:src.index("\n    }\n", start)]
    assert "replayed" not in body


def test_kotlin_stage3_a11y_service_null_contracts():
    """Stage 3 batch 3 (D4): the Java original is null-tolerant in
    these places, so the Kotlin parameters stay nullable — a
    non-null parameter would throw NPE where Java returned a
    value or threw a domain exception. pressKey's literal-on-left
    equalsIgnoreCase returns false for null; takeScreenshot gets
    the raw ?format= query param (null when absent) and falls
    back to PNG; nodeAction's actionIdForName(null) is -1, so a
    null action name is an IllegalArgumentException, not an NPE;
    resolveFresh's Map.get(null) is a NodeNotFound, not an NPE;
    runBatch's supersedes is documented nullable (SPEC SS2.6),
    and its C-04 guard params are nullable with defaults (a batch
    without a guard context still type-checks);
    getForegroundPackage and getBatchRegistry return null when
    there is no active window / the service has not connected."""
    src = _kotlin_source("BridgeAccessibilityService.kt")
    assert "fun pressKey(key: String?): Boolean" in src
    assert "fun takeScreenshot(scale: Float, format: String?, quality: Int): ByteArray" in src
    assert "fun typeText(text: String?): Boolean" in src
    assert "fun scrollNode(nodeId: String?, treeHash: Int, direction: String?): ActionOutcome" in src
    assert "fun nodeAction(\n        nodeId: String?,\n        treeHash: Int,\n        actionName: String?,\n        args: JSONObject?\n    ): ActionOutcome" in src
    assert "fun clickNode(nodeId: String?, treeHash: Int): ActionOutcome" in src
    assert "fun typeNode(nodeId: String?, treeHash: Int, text: String?): ActionOutcome" in src
    assert ("        supersedes: String?,\n"
            "        guardExpectedPackage: String? = null,\n"
            "        guardTreeHash: Int? = null,\n"
            "        guardGeneration: Int? = null,\n"
            "        guardSessionValid: (() -> Boolean)? = null,\n") in src
    # CONS-167 P1-02: the batch operation hook runs only after the
    # final guard — both runBatch and runBatchInternal carry it as a
    # nullable callback with a default (a batch without a guard
    # context still type-checks).
    assert src.count("onFinalGuardPassed: (() -> Unit)? = null") == 2
    assert "fun getForegroundPackage(): String?" in src
    assert "fun getBatchRegistry(): BatchRegistry?" in src


def test_kotlin_batch_g_bridge_log_interop():
    """Batch G (D4 checklist): an object (all-static Java
    class); @JvmStatic on the ten public methods so the Java
    callers keep compiling; one explicit LOG_LOCK for the six
    former static-synchronized methods (no @Synchronized — the
    batch E lesson); atomicWriteText/fsyncDirectory are public
    @JvmStatic because LogActivity.kt calls them and Kotlin
    internal would mangle the name (batch D precedent);
    runMetaFile has no callers, so it stays internal; the
    private field is renamed runMeta because a property and a
    function cannot share a name in Kotlin; split semantics
    are Java-exact (split(':') keeps trailing empties like
    split(":", -1); maskIpv4 drops them like split("\\.")); the
    DEL check is the explicit '\\u007f' escape, not a raw byte."""
    src = _kotlin_source("BridgeLog.kt")
    assert "object BridgeLog {" in src
    for name in ("init(ctx: Context?)", "log(event: String?, detail: String?)",
                 "logThrottled(key: String?, event: String?, detail: String?)",
                 "flushThrottled(key: String?, event: String?)",
                 "snapshot()", "previousSnapshot()",
                 "renderText(appVersion: String?, appVersionCode: Int)",
                 "maskIp(ip: String?)", "atomicWriteText(",
                 "fsyncDirectory("):
        assert "    @JvmStatic\n    fun " + name in src, name
    # CONS-151 P2-01 fix: the Java original tolerated null inputs,
    # so the Kotlin parameters stay nullable (log/logThrottled/
    # flushThrottled/renderText/init/runMetaFile) and the throttle
    # maps accept a null key, as Java HashMap does.
    assert "HashMap<String?, Long>" in src
    assert "HashMap<String?, Int>" in src
    assert 'var d = detail ?: ""' in src
    assert 'internal fun runMetaFile(ctx: Context?)' in src
    assert src.count("File(ctx!!.filesDir") == 3
    # The word appears in the LOG_LOCK comment, so the check is
    # per-line: no line may START with the annotation.
    assert not any(
        line.strip().startswith("@Synchronized")
        for line in src.splitlines()
    )
    assert "private val LOG_LOCK = Any()" in src
    # The phrase also appears in the P2-02 comment, so count
    # only the code lines (each block opens with exactly this).
    assert sum(
        1 for line in src.splitlines()
        if line.strip() == "synchronized(LOG_LOCK) {"
    ) == 6
    assert "private var runMeta: File? = null" in src
    assert "ip.split(':')" in src
    assert "dropLastWhile { it.isEmpty() }" in src
    assert "fos.fd.sync()" in src
    assert "'\\u007f'" in src
    assert "BridgeLog.class" not in src


def test_kotlin_batch_g_bridge_log_java_callers_unchanged():
    """Batch G: the callers were NOT modified in behaviour —
    HttpBridgeServer still drives the log the same way (init,
    log, logThrottled, flushThrottled, maskIp, snapshot,
    previousSnapshot), LogActivity (Kotlin since Stage 3 batch 2)
    still uses atomicWriteText / fsyncDirectory / renderText,
    UriPermissionRegistry still uses atomicWriteText, and
    BootReceiver still inits and logs. That only compiles because
    the Kotlin members are @JvmStatic (and atomicWriteText /
    fsyncDirectory public)."""
    srv = _kotlin_source("HttpBridgeServer.kt")
    assert "BridgeLog.init(this.context)" in srv
    assert "BridgeLog.log(" in srv
    assert "BridgeLog.logThrottled(" in srv
    assert "BridgeLog.flushThrottled(" in srv
    assert "BridgeLog.maskIp(" in srv
    assert "BridgeLog.snapshot()" in srv
    assert "BridgeLog.previousSnapshot()" in srv
    log = _kotlin_source("LogActivity.kt")
    assert "BridgeLog.atomicWriteText(" in log
    assert "BridgeLog.fsyncDirectory(" in log
    assert "BridgeLog.renderText(" in log
    reg = _kotlin_source("UriPermissionRegistry.kt")
    assert "BridgeLog.atomicWriteText(ctx.filesDir, REGISTRY_NAME," in reg
    boot = _kotlin_source("BootReceiver.kt")
    assert "BridgeLog.init(appContext)" in boot
    assert "BridgeLog.log(" in boot


def test_kotlin_pilot_build_sh_two_compiler_pipeline():
    """Pilot (D2): build.sh runs the two-compiler pipeline —
    kotlinc first (-jvm-target 17, 256m heap, no auto-escalation),
    then javac as a normal mandatory step with the Kotlin output
    and kotlin-stdlib on the classpath, then d8 with exactly one
    kotlin-stdlib.jar. Version is the Stage 3 batch 3 build's v2.9.25 / code 78."""
    build = _build_sh()
    assert 'VERSION_NAME="2.9.25"' in build
    assert "VERSION_CODE=78" in build
    assert "kotlinc" in build
    assert '"$KOTLINC" -J-Xmx256m -jvm-target 17' in build
    assert '"-cp" "$ANDROID_JAR:$BUILD/classes:$KOTLIN_STDLIB"' not in build
    assert '-cp "$ANDROID_JAR:$BUILD/classes:$KOTLIN_STDLIB"' in build
    assert 'KOTLIN_STDLIB="$KOTLIN_HOME/lib/kotlin-stdlib.jar"' in build
    assert '"$BUILD/classes.jar" "$KOTLIN_STDLIB"' in build


def test_kotlin_batch_a_bootreceiver_is_kotlin():
    """Batch A (D3 stage 1): BootReceiver.java is gone,
    BootReceiver.kt is the source, and it still extends
    BroadcastReceiver with the BOOT_COMPLETED guard and the
    Drive first-init hook before BridgeLog.init."""
    assert not _java_source_exists("BootReceiver.java")
    assert _kotlin_source_exists("BootReceiver.kt")
    src = _kotlin_source("BootReceiver.kt")
    assert "class BootReceiver : BroadcastReceiver()" in src
    assert "Intent.ACTION_BOOT_COMPLETED" in src
    assert "LogActivity.ensureDriveInit(" in src
    assert src.index("ensureDriveInit") < src.index("BridgeLog.init")


def test_kotlin_batch_a_vpnbindselector_is_kotlin():
    """Batch A (D3 stage 1, D4 checklist): VpnBindSelector.java
    is gone, VpnBindSelector.kt is the source. The public API
    keeps its JVM shape for the Java callers: @JvmStatic on
    select/isTailscaleRange and on the Decision factories,
    @JvmField on the VpnNetwork/Decision fields, const val
    constants (static final fields in the Java original)."""
    assert not _java_source_exists("VpnBindSelector.java")
    assert _kotlin_source_exists("VpnBindSelector.kt")
    src = _kotlin_source("VpnBindSelector.kt")
    assert "class VpnBindSelector private constructor()" in src
    assert 'const val DEGRADED_NO_VPN = "tailscale-interface-unavailable"' in src
    assert 'const val DEGRADED_AMBIGUOUS = "tailscale-network-ambiguous"' in src
    assert "        @JvmStatic\n        fun isTailscaleRange(" in src
    assert "        @JvmStatic\n        fun select(" in src
    assert "        @JvmField val interfaceName: String?" in src
    assert "        @JvmField\n        val ipv4Addresses: List<String>" in src
    assert "        @JvmField val bindIp: String?" in src
    assert "        @JvmField val bindInterface: String?" in src
    assert "        @JvmField val degradedReason: String?" in src
    assert "            @JvmStatic\n            fun bind(" in src
    assert "            @JvmStatic\n            fun degraded(" in src


def test_kotlin_batch_a_p2_regex_precompiled():
    """Batch A P2-1 (Argon review): the dot Regex is precompiled
    once in the companion object, not built on every
    isTailscaleRange call. Behaviour is the Java original's:
    trailing empty strings are dropped (Kotlin's split keeps
    them, unlike Java's String.split), so "100.64.1.2."
    still parses as a four-part quad."""
    src = _kotlin_source("VpnBindSelector.kt")
    assert 'private val DOT_REGEX = "\\\\.".toRegex()' in src
    assert "ip.split(DOT_REGEX).dropLastWhile { it.isEmpty() }" in src
    assert src.count(".toRegex()") == 1


def test_kotlin_batch_a_p2_ipv4addresses_drops_nulls():
    """Batch A P2-2 (Argon review): a null element smuggled in
    by the Java caller is dropped in the VpnNetwork
    constructor instead of NPE-ing at the for-loop
    assignment in select(). The field stays List<String>,
    so the Java callers are unchanged."""
    src = _kotlin_source("VpnBindSelector.kt")
    assert "ipv4Addresses?.filterNotNull() ?: emptyList()" in src
    assert "        @JvmField\n        val ipv4Addresses: List<String>" in src


def test_kotlin_batch_a_java_callers_unchanged():
    """Batch A: the Java callers were NOT modified — they still
    call VpnBindSelector.select(...) and isTailscaleRange(...)
    as statics, construct VpnNetwork, and read the Decision
    fields directly, which only compiles because the Kotlin
    members are @JvmStatic / @JvmField / const val."""
    server = _kotlin_source("HttpBridgeServer.kt")
    assert "VpnBindSelector.select(vpns)" in server
    assert "VpnBindSelector.VpnNetwork(ifName, v4)" in server
    assert "decision.bindIp" in server
    assert "decision.bindInterface" in server
    assert "decision.degradedReason" in server
    assert "decision.isBind()" in server
    main = _kotlin_source("MainActivity.kt")
    assert "VpnBindSelector.isTailscaleRange(ip)" in main


def test_kotlin_batch_a_attribution_first_line():
    """Batch A: new Kotlin files carry the required attribution
    as line 1."""
    for name in ("BootReceiver.kt", "VpnBindSelector.kt"):
        first = _kotlin_source(name).splitlines()[0]
        assert first == _KOTLIN_PILOT_ATTRIBUTION, name


# ---------------------------------------------------------------------------
# C-01 CONS-152 fixes — BatchRegistry source guards
#
# There is no JVM test harness for BatchRegistry (the CONS-130 P2-02
# gap), so the phone-side fixes are pinned by source inspection
# here, in the established style of the Kotlin batch tests above.
# The behaviour itself is covered by the live kill-and-restart /
# fault-injection tests (P1-05, needs the installed build + user).
# ---------------------------------------------------------------------------

def _cons152_registry_source():
    return _kotlin_source("BatchRegistry.kt")


def _cons152_admit_source():
    """The admit() body, from its signature to the next top-level
    function — the scope the ordering assertions check."""
    src = _cons152_registry_source()
    start = src.index("    fun admit(")
    end = src.index("\n    /**\n     * Step 4 of the fixed order", start)
    return src[start:end]


def test_cons152_p0_01_unknown_tombstone_recomputed():
    """P0-01: every IN_PROGRESS -> UNKNOWN conversion recomputes
    the tombstone to max(createdAt + 20 min, unknownAt) + 24 h
    (SPEC §2.2 / §2.7), through one helper — init(), the
    result-persist-failure path, and the restore path."""
    src = _cons152_registry_source()
    assert "private fun unknownTombstoneUntil(createdAt: Long, unknownAt: Long): Long" in src
    assert "maxOf(createdAt + RESULT_TTL_MS + TOMBSTONE_MS, unknownAt) + UNKNOWN_RETENTION_MS" in src
    assert "private fun toUnknown(rec: BatchRecord, unknownAt: Long): BatchRecord" in src
    # All three conversion sites go through toUnknown(), never a
    # bare copy() that leaves the old tombstone in place.
    assert src.count("toUnknown(") >= 4  # definition + init + restore + markUnknown
    assert 'rec.copy(status = "UNKNOWN", result = null, unknownAt = now)' not in src
    assert 'existing.copy(\n            status = "UNKNOWN"' not in src
    # Load-time migration: an UNKNOWN record written by the
    # pre-fix build gets its stale tombstone recomputed on load.
    assert "Load-time migration (CONS-152 P0-01)" in src


def test_cons152_p0_01_admit_unknown_before_tombstone():
    """P0-01: in admit(), the UNKNOWN branch (with its hash check)
    is evaluated BEFORE the `now > tombstoneUntil` expiry
    fall-through, so an UNKNOWN record is always answered UNKNOWN
    and never treated as a new record."""
    admit = _cons152_admit_source()
    unknown_at = admit.index('existing.status == "UNKNOWN"')
    tombstone_at = admit.index("now > existing.tombstoneUntil")
    assert unknown_at < tombstone_at
    assert "UNKNOWN is always answered UNKNOWN, before" in admit


def test_cons152_p0_02_durability_verify_corrupt_guard():
    """P0-02: recordDurabilityVerified() refuses on a CORRUPT store
    (503 batch_store_unavailable) and never persists over it.
    The same guard applies to confirm() and completeRecovery();
    the recovery paths (restore / clear) are deliberately exempt."""
    src = _cons152_registry_source()
    start = src.index("    fun recordDurabilityVerified(")
    end = src.index("\n    /** The durability gate", start)
    body = src[start:end]
    assert "requireNotCorruptLocked()" in body
    assert "private fun requireNotCorruptLocked()" in src
    assert 'throw BatchStoreException("batch_store_unavailable"' in src
    confirm_start = src.index("    ): ManualConfirmation {")
    confirm_body = src[confirm_start:confirm_start + 600]
    assert "requireNotCorruptLocked()" in confirm_body
    recovery_start = src.index("    fun completeRecovery(")
    recovery_body = src[recovery_start:recovery_start + 600]
    assert "requireNotCorruptLocked()" in recovery_body
    # restoreFromBackup and clearStore must NOT take the guard —
    # they are how a corrupt store is recovered.
    restore_start = src.index("    fun restoreFromBackup(")
    restore_body = src[restore_start:src.index("    /**\n     * Deliberately clear", restore_start)]
    assert "requireNotCorruptLocked()" not in restore_body


def test_cons152_p1_01_unreadable_is_corrupt():
    """P1-01: readFileText() returns null only when the file does
    not exist; a file that exists but cannot be read throws, and
    init() classifies that as CORRUPT (fail-closed), never EMPTY."""
    src = _cons152_registry_source()
    assert "return null\n        }\n        } catch" not in src
    assert "catch (_: Exception) {\n            return null" not in src
    assert "store unreadable: " in src
    assert "The file exists but cannot be read: CORRUPT" in src


def test_cons153_p1_01_absence_positively_confirmed():
    """CONS-153 P1-01: readFileText() returns null ONLY when
    absence is positively confirmed (Files.notExists() — the
    ENOENT equivalent). File.exists() / Files.exists() return
    false both when the file does not exist AND when its
    existence cannot be determined, so neither may decide "new
    store": an undeterminable status throws, and init() still
    classifies that throw as CORRUPT, never EMPTY. A
    NoSuchFileException on open (deleted between the check and
    the open) is the ENOENT race and returns null."""
    src = _cons152_registry_source()
    start = src.index("    private fun readFileText(")
    end = src.index("\n    /**\n     * The admission unit", start)
    body = src[start:end]
    assert "Files.notExists(path)" in body
    assert "if (!file.exists()) return null" not in body
    assert "Files.exists(path)" in body
    assert "undeterminable status" in body
    assert "NoSuchFileException" in body
    assert "Files.newInputStream(path)" in body
    assert "FileInputStream" not in body
    init_start = src.index("    fun init() {")
    init_body = src[init_start:src.index("    private fun ensureInitialized()", init_start)]
    assert "store unreadable: " in init_body
    assert "StoreState.CORRUPT" in init_body


def test_cons156_p2_01_in_progress_checks_payload_hash():
    """CONS-156 P2-1: admit() must compare payloadHash for an
    IN_PROGRESS record before answering RUNNING — a re-send of the
    same batch_id with different content is a 409
    (BatchMismatchException), not "running". The same batch_id must
    never name two payloads."""
    admit = _cons152_admit_source()
    start = admit.index('if (existing.status == "IN_PROGRESS")')
    branch = admit[start:admit.index('if (existing.status == "UNKNOWN")', start)]
    assert "existing.payloadHash != payloadHash" in branch
    assert "BatchMismatchException" in branch
    assert "is already in use with different content" in branch
    assert branch.index("existing.payloadHash != payloadHash") < branch.index(
        "AdmitKind.RUNNING")


def test_cons156_p2_02_failed_ready_leaves_outbox_unset(mock_app, tmp_path):
    """CONS-156 P2-2 (behavioral): _batch_outbox() must not cache
    the outbox when require_ready() fails. A failed startup check
    must leave adapter._outbox None, so the next batch() call runs
    the startup decision table again (OUTBOX_NOT_INITIALIZED)
    instead of skipping it and failing later as OUTBOX_CORRUPT."""
    uninitialized = tmp_path / "outbox-p2-02-uninit"
    adapter = ab.AndroidAdapter(actions_enabled=True,
                                outbox_dir=str(uninitialized))
    actions = [{"action_id": "a1", "type": "tap", "x": 1, "y": 2}]
    with pytest.raises(ab.bridge_protocol.ProtocolError) as first:
        adapter.batch("b-p2-02-first", 123456, actions)
    assert first.value.code == "OUTBOX_NOT_INITIALIZED"
    assert adapter._outbox is None
    with pytest.raises(ab.bridge_protocol.ProtocolError) as second:
        adapter.batch("b-p2-02-second", 123456, actions)
    assert second.value.code == "OUTBOX_NOT_INITIALIZED"


def test_cons157_p2_02_success_path_returns_outbox_instance(mock_app, tmp_path):
    """CONS-157 (CONS-156 P2-2 follow-up): the success path of
    _batch_outbox() must return the BatchOutbox instance. The
    CONS-157 excerpt ended at line 998 and cut off the
    `return self._outbox` line (999), so Kohaku could not verify
    it from the excerpt alone — this test verifies it by
    execution: the first call returns a BatchOutbox, the adapter
    caches it, and the second call returns the same instance."""
    outbox_dir = tmp_path / "outbox-p2-02-ready"
    ready = ab.bridge_protocol.BatchOutbox(str(outbox_dir))
    ready.initialize("test — fresh outbox")
    ready.record_durability_verified(
        "file_atomic_v1", "test — crash-durability check passed")
    adapter = ab.AndroidAdapter(actions_enabled=True,
                                outbox_dir=str(outbox_dir))
    first = adapter._batch_outbox()
    assert isinstance(first, ab.bridge_protocol.BatchOutbox)
    assert adapter._outbox is first
    second = adapter._batch_outbox()
    assert second is first


def test_cons156_p2_03_confirm_checks_record_size():
    """CONS-156 P2-3: confirm() must apply the 64 KB per-record cap
    (MAX_RECORD_BYTES) to the updated record before persistLocked(),
    like admit() and complete() do. An oversize confirmation is a
    persist failure (BatchStoreException persist_failed), not a 400
    — the confirmation is not recorded."""
    src = _cons152_registry_source()
    start = src.index("    fun confirm(")
    body = src[start:src.index("\n    fun restoreFromBackup(", start)]
    assert "recordJsonBytes(updated) > MAX_RECORD_BYTES" in body
    check = body.index("recordJsonBytes(updated)")
    persist = body.index("persistLocked(next", check)
    assert check < persist
    segment = body[check:persist]
    assert "BatchStoreException" in segment
    assert "persist_failed" in segment
    assert "64 KB" in segment
    assert "BatchBadRequestException" not in segment


def test_cons152_p1_04_supersedes_needs_confirmation():
    """P1-04: admit() supersedes validation also requires the
    target UNKNOWN record to carry a recorded manualConfirmation."""
    admit = _cons152_admit_source()
    assert "target.manualConfirmation == null" in admit
    assert "no recorded manual confirmation" in admit


def test_cons152_p2_01_file_cap_on_all_persists():
    """P2-01: the 512 KB whole-store cap is enforced on every
    persist path (a central guard in persistLocked()) and on load
    (record count in storeFromJson(), byte size via
    checkStoreSize())."""
    src = _cons152_registry_source()
    persist_start = src.index("    private fun persistLocked(")
    persist_body = src[persist_start:src.index("\n    /** The serialized size", persist_start)]
    assert "MAX_FILE_BYTES" in persist_body
    assert "persist refused: store would exceed 512 KB" in persist_body
    assert "private fun checkStoreSize(text: String)" in src
    assert "store file exceeds 512 KB" in src
    assert "if (arr.length() > MAX_RECORDS)" in src
    assert "too many records: " in src


def test_cons152_p2_02_batch_registry_volatile():
    """P2-02: the BatchRegistry field is volatile — it is written
    on the service main thread in onServiceConnected() and read
    from HTTP worker threads, so publication must be explicit."""
    svc = _kotlin_source("BridgeAccessibilityService.kt")
    assert "    @Volatile\n    private var batchRegistry: BatchRegistry? = null" in svc


# ---------------------------------------------------------------------------
# Stage 3 batch 4 (final): HttpBridgeServer Java -> Kotlin + C-04
# (SPEC-FOREGROUND-GUARD-001 v2.1) woven in.
#
# The server side is pinned by source inspection (no JVM harness —
# the CONS-130 P2-02 gap, as in batches 1-3); the VM client side is
# pinned against the mock server. Live C-04 acceptance (C-04-1..22)
# needs the installed build + user operation (T-M4, after Stage 3).
# ---------------------------------------------------------------------------

_STAGE3_HTTP_SERVER_ATTRIBUTION = (
    "// Implemented by MUSE-Koyuki (Muse Spark) "
    "\u2014 SPEC-KOTLIN-MIGRATION-001 v2.2, 2026-11-14")


def test_kotlin_stage3_http_server_is_kotlin():
    """Stage 3 batch 4: the .java file is gone, the .kt file is the
    source, and it carries the migration attribution as line 1."""
    assert not _java_source_exists("HttpBridgeServer.java")
    assert _kotlin_source_exists("HttpBridgeServer.kt")
    first = _kotlin_source("HttpBridgeServer.kt").splitlines()[0]
    assert first == _STAGE3_HTTP_SERVER_ATTRIBUTION


def test_kotlin_stage3_http_server_d4_interop():
    """Stage 3 batch 4 (D4): the public API the callers use stays
    put — open class (the Java original was a non-final public
    class); start/stop/isRunning/boundTailscaleIps stay public
    and synchronized where Java had them; PORT and
    INSTALL_RESULT_ACTION stay public consts; loggablePath stays
    reachable from the companion. The nested helpers
    (BindTarget, BindResult, ServiceUnavailable, HeaderTooLarge)
    were private in Java and stay private classes in Kotlin, so
    the CONS-165 P2-01 `open` rule does not apply to them. There
    is no Java caller left, so no @JvmStatic is needed."""
    src = _kotlin_source("HttpBridgeServer.kt")
    assert "open class HttpBridgeServer(" in src
    assert "public final class HttpBridgeServer" not in src
    assert "fun start(): Boolean" in src
    assert "fun stop()" in src
    assert "fun stop(reason: StopReason?)" in src
    assert "fun isRunning(): Boolean" in src
    assert "@Synchronized" in src
    assert "fun boundTailscaleIps(): List<String>" in src
    assert "const val PORT = 8080" in src
    assert 'const val INSTALL_RESULT_ACTION = "dev.muse.bridge.INSTALL_RESULT"' in src
    assert "fun loggablePath(path: String): String" in src
    assert "interface TokenProvider" in src
    for nested in ("private class BindTarget", "private class BindResult",
                   "private class ServiceUnavailable",
                   "private class HeaderTooLarge"):
        assert nested in src, nested
    assert "open class BindTarget" not in src
    assert "open class ServiceUnavailable" not in src


def test_kotlin_stage3_http_server_c04_guard():
    """Stage 3 batch 4 (C-04, SPEC-FOREGROUND-GUARD-001 v2.1):
    the guard machinery is present in the Kotlin server —
    OperationContext + parseOperationContext (expected_package
    required, session bootstrap, generation >= 0) + the launch
    session reader + the session store + checkGuard +
    runGuardedOperation, the SPEC error ids, session_id on the
    guarded responses, and the guard arguments threaded from the
    /batch handler into runBatch. The session id is a server-issued
    UUID (noteOperation issues it, endOperationLocked and stop()
    clear it)."""
    src = _kotlin_source("HttpBridgeServer.kt")
    svc = _kotlin_source("BridgeAccessibilityService.kt")
    # The context type and its parsers.
    assert "private class OperationContext(" in src
    assert "private fun parseOperationContext(j: JSONObject): OperationContext" in src
    assert 'throw org.json.JSONException("missing operation_context")' in src
    assert "missing expected_package in operation_context" in src
    assert "generation must not be negative" in src
    assert "private fun parseLaunchSession(j: JSONObject): String?" in src
    # The session store: issued on note, cleared on end/stop.
    assert "private var operationSessionId: String? = null" in src
    assert "operationSessionId = UUID.randomUUID().toString()" in src
    assert "private fun currentSessionId(): String?" in src
    assert "private fun isSessionCurrent(suppliedId: String?): Boolean" in src
    assert "private fun requireSessionCurrent(suppliedId: String?)" in src
    # The guard itself lives on the service (serialized admission
    # boundary, SPEC §2.2) and the server calls it from the
    # guarded handlers.
    assert "open class OperationGuardException(" in svc
    assert "fun checkGuard(" in svc
    assert "fun <T> runGuardedOperation(" in svc
    for endpoint in ("/click", "/swipe", "/type_text", "/press_key",
                     "/notification_action"):
        assert f'"{endpoint}" == path' in src, endpoint
    assert "runGuardedOperation(" in src
    assert "parseOperationContext(j)" in src
    # SPEC error ids.
    for error_id in ("foreground_mismatch", "tree_hash_mismatch",
                     "generation_mismatch", "session_mismatch",
                     "foreground_unverifiable", "snapshot_unavailable"):
        assert f'"{error_id}"' in src or f'"{error_id}"' in svc, error_id
    # session_id is returned on the guarded successes.
    assert '.put("session_id", currentSessionId() ?: JSONObject.NULL)' in src
    # /batch threads the guard into runBatch (worker-side pre-check
    # + in-task re-check inside runBatchInternal).
    assert "guardExpectedPackage" in src
    assert "guardTreeHash" in src
    assert "guardGeneration" in src
    assert "guardSessionValid" in src
    assert "batchCtx.treeHash" in src


def test_kotlin_stage3_batch_registry_discard():
    """Stage 3 batch 4 (C-04): BatchRegistry.discard removes only
    an IN_PROGRESS record with a matching payloadHash — the escape
    hatch runBatch uses when its in-task guard refuses after
    admission (otherwise a later re-send would wrongly answer
    "running"). A persist failure is fail-closed (CONS-167 P2-01:
    the record is marked UNKNOWN in memory, never "running", and
    the call reports false)."""
    src = _kotlin_source("BatchRegistry.kt")
    assert "fun discard(batchId: String, payloadHash: String): Boolean" in src
    start = src.index("fun discard(")
    body = src[start:src.index("\n    }", start)]
    # The record status is a stored string (C-01: the persisted
    # status is uppercase), compared directly — not the enum.
    assert 'existing.status != "IN_PROGRESS"' in body
    assert "existing.payloadHash != payloadHash" in body
    assert "persistLocked(" in body
    assert "return false" in body


def test_cons167_p1_01_note_operation_validates_session_atomically():
    """CONS-167 P1-01: noteOperation re-validates the supplied session
    id in the same operationLock critical section that starts or
    refreshes the session — so the 10 s auto-end timer cannot end
    session A between validation and start and silently move the
    operation into a fresh session B. The check must sit before the
    session-creation branch, and a mismatch must throw
    session_mismatch (the handle() catch turns it into a 409)."""
    src = _kotlin_source("HttpBridgeServer.kt")
    assert "expectedSessionId: String? = null" in src
    start = src.index("private fun noteOperation(")
    creation = src.index("if (!operationActive) {", start)
    head = src[start:creation]
    assert "synchronized(operationLock) {" in head
    assert "expectedSessionId != operationSessionId" in head
    assert "expectedSessionId != null" in head
    assert '"session_mismatch"' in head
    assert "throw BridgeAccessibilityService.OperationGuardException" in head


def test_cons167_p1_01_guarded_handlers_pass_session_to_note_operation():
    """CONS-167 P1-01: every guarded handler that validates a session
    id passes that same id to noteOperation, so the atomic check
    inside noteOperation can reject a session that ended after the
    worker-side / in-task checks. Node operations take no session
    id (SPEC §2.8, unchanged) and keep the two-argument call."""
    src = _kotlin_source("HttpBridgeServer.kt")
    assert 'noteOperation(lifecycleGeneration, "通知を開いています", ctx.sessionId)' in src
    assert 'noteOperation(lifecycleGeneration, "通知に返信しています", ctx.sessionId)' in src
    assert 'noteOperation(lifecycleGeneration, "タップ " + x + "/" + y, ctx.sessionId)' in src
    assert '"スワイプ " + x1 + "/" + y1 + " → " + x2 + "/" + y2, ctx.sessionId)' in src
    assert 'noteOperation(lifecycleGeneration, "文字を入力中", ctx.sessionId)' in src
    assert 'noteOperation(lifecycleGeneration, "キー操作中（" + key + "）", ctx.sessionId)' in src
    assert 'noteOperation(lifecycleGeneration, "アプリを起動中：" + operationAppLabel(pkg), launchSessionId)' in src
    assert 'noteOperation(lifecycleGeneration, "リンクを開いています", openSessionId)' in src
    # Node operations: no session concept — the two-argument call stays.
    assert 'noteOperation(lifecycleGeneration, "タップ中")' in src
    assert 'noteOperation(lifecycleGeneration, "スクロール中")' in src


def test_cons167_p1_02_batch_hook_runs_after_final_guard():
    """CONS-167 P1-02: /batch no longer calls noteOperation before
    runBatch — the hook runs only after the final in-task guard
    passes, via the onFinalGuardPassed callback, inside the same
    main-thread task as the actions. A guard-refused, replayed, or
    unknown batch posts no "操作中" and starts no session."""
    srv = _kotlin_source("HttpBridgeServer.kt")
    batch = srv.index('"/batch" == path')
    run = srv.index("svc!!.runBatch(", batch)
    assert "noteOperation(" not in srv[batch:run]
    assert "onFinalGuardPassed = {" in srv[run:srv.index("opGeneration = batchOpGeneration", run)]
    # CONS-168 P1-01: the generation is an AtomicLong now — set in the
    # callback, copied into opGeneration in the finally around runBatch.
    assert "batchOpGeneration.set(noteOperation(" in srv
    svc = _kotlin_source("BridgeAccessibilityService.kt")
    assert "onFinalGuardPassed: (() -> Unit)? = null" in svc
    internal = svc.index("private fun runBatchInternal(")
    guard = svc.index("checkGuard(", internal)
    hook = svc.index("onFinalGuardPassed?.invoke()", internal)
    assert guard < hook


def test_cons167_p2_01_discard_persist_failure_marks_unknown():
    """CONS-167 P2-01: when discard cannot persist the removal, the
    record is marked UNKNOWN in memory (the complete() precedent) —
    a re-send gets "unknown", never "running" for a batch that never
    ran. The file still says IN_PROGRESS; the next restart converts
    it to UNKNOWN the same way. The failure is still reported
    (false)."""
    src = _kotlin_source("BatchRegistry.kt")
    start = src.index("fun discard(")
    body = src[start:src.index("\n    }", start)]
    assert "markUnknownInMemoryLocked(existing" in body
    assert "marked UNKNOWN in memory" in body
    assert "record kept" not in body
    assert "return false" in body


def test_cons167_p2_01_run_batch_logs_failed_discard():
    """CONS-167 P2-01: runBatch checks discard's return value — a
    failed discard is logged instead of silently ignored, while the
    guard refusal still propagates to the client."""
    src = _kotlin_source("BridgeAccessibilityService.kt")
    start = src.index("} catch (e: OperationGuardException) {")
    body = src[start:src.index("throw e", start)]
    assert "if (!registry.discard(batchId, payloadHash)) {" in body
    assert 'BridgeLog.log("batch_registry"' in body


def test_c04_build_operation_context():
    """C-04 (§2.1): build_operation_context validates the context
    the guarded endpoints send — expected_package is required and
    non-empty, tree_hash is a Java int, generation is >= 0,
    session_id is a non-empty string when present. Fields left as
    None are omitted, never sent as null."""
    bp = ab.bridge_protocol
    ctx = bp.build_operation_context("com.example", tree_hash=123456,
                                     generation=7)
    assert ctx == {"expected_package": "com.example",
                   "tree_hash": 123456, "generation": 7}
    ctx = bp.build_operation_context("com.example",
                                     session_id="550e8400-e29b-41d4-a716-446655440000")
    assert ctx == {"expected_package": "com.example",
                   "session_id": "550e8400-e29b-41d4-a716-446655440000"}
    assert bp.build_operation_context("com.example") == {
        "expected_package": "com.example"}
    with pytest.raises(bp.ProtocolError):
        bp.build_operation_context("")
    with pytest.raises(bp.ProtocolError):
        bp.build_operation_context("com.example", generation=-1)
    with pytest.raises(bp.ProtocolError):
        bp.build_operation_context("com.example", tree_hash=2 ** 31)
    with pytest.raises(bp.ProtocolError):
        bp.build_operation_context("com.example", session_id="")


def test_c04_builders_attach_operation_context():
    """C-04 (§2.1): the guarded builders attach the context
    verbatim; a non-dict context is a client bug, not a server
    rejection. Without a context the bodies are exactly the old
    ones (the builders stay backward compatible for callers that
    do not guard)."""
    bp = ab.bridge_protocol
    ctx = {"expected_package": "com.example", "tree_hash": 7, "generation": 7}
    assert bp.build_click(1, 2, operation_context=ctx)["operation_context"] == ctx
    assert bp.build_swipe(0, 0, 1, 1, operation_context=ctx)["operation_context"] == ctx
    assert bp.build_type_text("hi", operation_context=ctx)["operation_context"] == ctx
    assert bp.build_press_key("HOME", operation_context=ctx)["operation_context"] == ctx
    assert bp.build_batch("b", 7, [{"type": "wait", "wait_ms": 100}],
                          operation_context=ctx)["operation_context"] == ctx
    assert bp.build_click(1, 2) == {"x": 1, "y": 2}
    assert bp.build_press_key("HOME") == {"key": "HOME"}
    with pytest.raises(bp.ProtocolError):
        bp.build_click(1, 2, operation_context="not-a-dict")


def test_c04_submit_passes_context_without_changing_hash():
    """C-04 (§2.2, §2.8): the batch registry passes the context
    through to the body, and the content hash is unchanged by it —
    the server's canonical payload hashes protocol_version,
    tree_hash, and actions only, so attaching a context cannot
    change the hash the registry computed."""
    bp = ab.bridge_protocol
    actions = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
    ctx = {"expected_package": "com.example", "tree_hash": 7, "generation": 7}
    reg_plain = bp.BatchRegistry()
    plain = reg_plain.submit("b-plain", 7, actions)
    reg_guarded = bp.BatchRegistry()
    guarded = reg_guarded.submit("b-guarded", 7, actions,
                                 operation_context=ctx)
    assert plain["content_hash"] == guarded["content_hash"]
    assert guarded["body"]["operation_context"] == ctx
    assert "operation_context" not in plain["body"]


def test_c04_click_sends_guard_context_from_fresh_state(mock_app):
    """C-04 (§2.2): click() fetches a fresh /state first and posts
    the operation_context of that snapshot (mock: package
    com.example, tree_hash 123456, revision 7). The first action
    bootstraps — no session_id in the context — and the GET
    precedes the POST in request order."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.click(500, 800)["ok"] is True
    requests = MockAppHandler.requests
    assert requests[0] == ("GET", "/state", None)
    assert requests[1][0] == "POST" and requests[1][1] == "/click"
    ctx = requests[1][2]["operation_context"]
    assert ctx == {"expected_package": "com.example",
                   "tree_hash": 123456, "generation": 7}


def test_c04_session_id_retained_across_actions(mock_app):
    """C-04 (§2.4): the mock returns the server-issued session id
    on guarded successes; the adapter retains it and the next
    guarded action joins the same session (its context carries the
    id) instead of bootstrapping a new one."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.click(1, 2)["ok"] is True
    assert adapter._operation_session_id == "mock-session-1"
    assert adapter.swipe(0, 0, 10, 10)["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert posts[1][2]["operation_context"]["session_id"] == "mock-session-1"
    assert adapter._operation_session_id == "mock-session-1"


def test_c04_session_mismatch_clears_retained_id(mock_app):
    """C-04 (§2.4): a session_mismatch proves the retained id is
    not the active one (server restarted, or the session auto-ended
    after 10 s idle). The adapter drops it, so the NEXT guarded
    action bootstraps a fresh session instead of 409ing again on
    the same stale id. Without the clear the mismatch is sticky:
    the server 409s any non-null id that is not active, including
    when no session is active at all."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.click(1, 2)["ok"] is True
    assert adapter._operation_session_id == "mock-session-1"
    MockAppHandler.fail_next[("POST", "/click")] = (409, 1)
    MockAppHandler.fail_body = {
        "ok": False, "error": "session_mismatch",
        "message": "the operation session mock-session-1 is not active"}
    with pytest.raises(ab.TransportError) as excinfo:
        adapter.click(3, 4)
    assert excinfo.value.error_id == "session_mismatch"
    assert adapter._operation_session_id is None
    # The next action bootstraps again — no session_id in its context.
    assert adapter.click(5, 6)["ok"] is True
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/click"]
    assert "session_id" not in posts[-1][2]["operation_context"]


def test_c04_foreground_mismatch_is_not_retried(mock_app, monkeypatch):
    """C-04 (§2.2, §2.6): a 409 guard refusal is definitive
    not-executed — the POST runs exactly once, and the error id is
    carried structured on the TransportError (no re-parse)."""
    monkeypatch.setattr(ab, "APP_RETRY_BASE_DELAY_S", 0.01)
    MockAppHandler.fail_next[("POST", "/click")] = (409, 1)
    MockAppHandler.fail_body = {
        "ok": False, "error": "foreground_mismatch",
        "message": "foreground is com.other, expected com.example",
        "expected_package": "com.example",
        "current_package": "com.other"}
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.TransportError) as excinfo:
        adapter.click(100, 100)
    assert excinfo.value.error_id == "foreground_mismatch"
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/click"]
    assert len(posts) == 1


def test_c04_operation_end_clears_retained_id(mock_app):
    """C-04 (§2.4): ending the session invalidates the id
    server-side, so the adapter drops its retained copy — the next
    guarded action bootstraps instead of 409ing on a dead id."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.click(1, 2)["ok"] is True
    assert adapter._operation_session_id == "mock-session-1"
    assert adapter.operation_end()["ok"] is True
    assert adapter._operation_session_id is None
    assert adapter.click(3, 4)["ok"] is True
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/click"]
    assert "session_id" not in posts[-1][2]["operation_context"]


def test_c04_launch_app_joins_session_when_retained(mock_app):
    """C-04 (§2.5): a launch joins the active session when one is
    retained (session_id only — expected_package does not apply,
    the launch itself changes the foreground); with no session
    retained the client sends no operation_context at all
    (bootstrap)."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.launch_app("com.twitter.android")
    assert result["status"] == "launched"
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/launch_app"]
    assert posts[0][2] == {"package": "com.twitter.android"}
    assert adapter.click(1, 2)["ok"] is True
    assert adapter._operation_session_id == "mock-session-1"
    result = adapter.launch_app("com.twitter.android")
    assert result["status"] == "launched"
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/launch_app"]
    assert posts[1][2] == {
        "package": "com.twitter.android",
        "operation_context": {"session_id": "mock-session-1"}}


def test_c04_open_joins_session_when_retained(mock_app):
    """C-04 (§2.5): /open follows the same launch contract as
    /launch_app — session_id only, omitted when nothing is
    retained."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.open("https://example.com")["ok"] is True
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/open"]
    assert posts[0][2] == {"uri": "https://example.com"}
    assert adapter.click(1, 2)["ok"] is True
    assert adapter.open("https://example.com")["ok"] is True
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/open"]
    assert posts[1][2] == {
        "uri": "https://example.com",
        "operation_context": {"session_id": "mock-session-1"}}


def test_c04_batch_sends_guard_context(mock_app):
    """C-04 (§2.2, §2.8): batch() posts the operation_context of
    the snapshot it acts on (mock: com.example / 123456 / 7,
    bootstrap on the first action), and retains the session id
    the batch response carries for the next guarded action."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    actions = [{"action_id": "a1", "type": "tap", "x": 1, "y": 2}]
    result = adapter.batch("b-c04", 123456, actions)
    assert result["ok"] is True
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/batch"]
    assert len(posts) == 1
    assert posts[0][2]["operation_context"] == {
        "expected_package": "com.example",
        "tree_hash": 123456, "generation": 7}
    assert adapter._operation_session_id == "mock-session-1"


def test_c04_notification_action_carries_session_after_click(mock_app):
    """C-04 (§2.2): notification_action is a guarded ordinary
    screen operation — after an earlier action bootstrapped the
    session, its context carries the retained session id."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.click(1, 2)["ok"] is True
    assert adapter.notification_action(NOTIF_KEY, "open")["ok"] is True
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/notification_action"]
    assert posts[0][2] == {
        "key": NOTIF_KEY, "action": "open",
        "operation_context": {
            "expected_package": "com.example",
            "tree_hash": 123456, "generation": 7,
            "session_id": "mock-session-1"}}


def test_c04_guard_context_requires_foreground_package():
    """C-04 (§2.1): a snapshot with no foreground package cannot
    produce a context — the adapter fails fast client-side with a
    clear ProtocolError instead of sending a context the server
    would 400."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.bridge_protocol.ProtocolError,
                       match="no foreground package"):
        adapter._guard_context({"package": "", "tree_hash": 7, "revision": 7})
    with pytest.raises(ab.bridge_protocol.ProtocolError,
                       match="no foreground package"):
        adapter._guard_context({"tree_hash": 7, "revision": 7})


def test_cons168_p1_01_batch_generation_copied_in_finally():
    """CONS-168 P1-01: the /batch handler must copy the generation the
    onFinalGuardPassed hook noted into opGeneration in a finally around
    runBatch — not only after a normal return. A ServiceTimeoutException
    from the 20 s main-thread timeout after the hook ran would otherwise
    skip the copy, leave operationInFlight incremented with opGeneration
    == -1, and defeat the 10 s auto-end timer until an explicit
    /operation_end or stop(). The generation is an AtomicLong (the hook
    writes on the main thread, the worker reads after a timeout — a
    plain var has no happens-before edge there), and the hook is
    disarmed in the same finally, so a main-thread task still queued
    when the worker timed out cannot note an operation after handle()
    has already exited."""
    src = _kotlin_source("HttpBridgeServer.kt")
    assert "import java.util.concurrent.atomic.AtomicLong" in src
    assert "val batchOpGeneration = AtomicLong(-1L)" in src
    assert "var batchOpGeneration = -1L" not in src
    start = src.index("// CONS-168 P1-01")
    end = src.index("// C-01 (SPEC §2.10)", start)
    handler = src[start:end]
    assert "val br = try {" in handler
    assert "svc!!.runBatch(" in handler
    assert "} finally {" in handler
    assert "opGeneration = batchOpGeneration.get()" in handler
    assert "batchHookArmed = false" in handler
    assert "if (batchHookArmed)" in handler
    assert "synchronized(batchHookLock)" in handler
    assert "batchOpGeneration.set(noteOperation(" in handler
    # The old shape (plain var + copy after a normal return) is gone.
    assert "opGeneration = batchOpGeneration\n" not in src


def test_cons168_p2_01_notification_reply_comment_matches_behavior():
    """CONS-168 P2-01: the /notification_action comment must no longer
    say "reply" does not call noteOperation — since CONS-167 reply notes
    the operation like open does. A stale comment contradicts the code
    one screen below it."""
    src = _kotlin_source("HttpBridgeServer.kt")
    assert '"reply" does not' not in src
    assert 'no "操作中" hook' not in src
    start = src.index("// C-04: an ordinary screen operation")
    end = src.index("if (!BridgeNotificationListener.isAccessGranted", start)
    head = src[start:end]
    assert "CONS-167 P1-01" in head
    assert '"reply" notes it too' in head


def test_cons168_p2_02_no_redundant_null_checks():
    """CONS-168 P2-02: TokenProvider.currentToken() is declared non-null
    String (its only implementation delegates to
    MainActivity.getOrCreateToken, which returns String), so the
    `expected == null` check in the token gate is dead — drop it.
    requireBatchRegistry must not use `svc!!` either: the Elvis throws
    the same fail-closed ServiceUnavailable its callers rely on."""
    src = _kotlin_source("HttpBridgeServer.kt")
    assert "expected == null" not in src
    assert "if (expected != token)" in src
    start = src.index("private fun requireBatchRegistry(")
    body = src[start:start + 700]
    # (The KDoc above mentions `svc!!` in prose; assert on the code line.)
    assert "val registry = svc!!" not in body
    assert "svc ?: throw ServiceUnavailable" in body
