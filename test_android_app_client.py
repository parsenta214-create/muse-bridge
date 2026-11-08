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
                return self._send_json(status, {"ok": False, "error": "simulated (test hook)"})
        elif fail > 0:
            MockAppHandler.fail_next[key] -= 1
            return self._send_json(503, {"ok": False, "error": "overloaded (test hook)"})
        # P0-1 (v2.0): a11y-off mode. The a11y-dependent POSTs
        # answer the structured 503 body; the available POSTs
        # (/launch_app, /open, /install_apk, /notify,
        # /notification_action, /clipboard, /operation_end) fall
        # through to normal handling.
        if not MockAppHandler.accessibility:
            base = self.path.split("?")[0]
            if base in ("/batch", "/click", "/swipe", "/type_text",
                        "/press_key", "/click_node", "/type_node",
                        "/scroll_node", "/node_action"):
                return self._send_json(503, _a11y_off_body(base))
        if self.path == "/batch":
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
                    "verification": "accessibility"})
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
                    "foreground_confirmed": None, "verification": "none"})
            outcome = MockAppHandler.launch_outcomes.get(
                pkg, MockAppHandler.launch_outcome)
            if outcome in ("unconfirmed", "foreground_left", "no_window"):
                return self._send_json(200, {
                    "ok": False, "status": "unconfirmed",
                    "reason": "FOREGROUND_NOT_CONFIRMED",
                    "accepted": True, "package": pkg,
                    "launch_requested": True,
                    "foreground_confirmed": False,
                    "verification": "accessibility"})
            return self._send_json(200, {
                "ok": True, "status": "launched", "accepted": True,
                "package": pkg, "launch_requested": True,
                "foreground_confirmed": True,
                "verification": "accessibility"})
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
        return self._send_json(200, {"ok": True, "echo": payload})


@pytest.fixture()
def mock_app(monkeypatch):
    MockAppHandler.requests.clear()
    MockAppHandler.fail_next.clear()
    MockAppHandler.batch_id_override = None
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
    assert (("POST", "/click", {"x": 500, "y": 800}) in posts)
    assert (("POST", "/press_key", {"key": "HOME"}) in posts)


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
    assert ("POST", "/batch",
            {"batch_id": "b-001", "protocol_version": 1,
             "tree_hash": 123456, "actions": actions}) in posts


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
    assert ("POST", "/click", {"x": 100, "y": 200}) in posts
    assert ("POST", "/swipe", {"x1": 0, "y1": 0, "x2": 10, "y2": 20,
                               "duration_ms": 150}) in posts
    assert ("POST", "/type_text", {"text": "hi"}) in posts
    assert ("POST", "/press_key", {"key": "DPAD_DOWN"}) in posts
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
    content raises STALE_REVISION and sends NO request; a second
    call with identical content re-sends (the server replays from
    its cache) and the registry records the outcome."""
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
    # Identical re-send goes out again (the server answers from
    # its cache); the registry marks it as a replay.
    result2 = adapter.batch("b-reg", 123456, actions)
    assert result2["status"] == "completed"
    assert adapter._batch_registry.get("b-reg")["status"] == "completed"


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


def test_app_batch_retries_transient_503(mock_app, monkeypatch):
    """Client improvement 1: POST /batch retries with the SAME
    batch_id (server-side idempotency, A-4), so a transient 503
    does not lose the batch."""
    monkeypatch.setattr(ab, "APP_RETRY_BASE_DELAY_S", 0.01)
    MockAppHandler.fail_next[("POST", "/batch")] = 2
    adapter = ab.AndroidAdapter(actions_enabled=True)
    result = adapter.batch("retry-1", 123456,
                           [{"action_id": "a1", "type": "wait", "wait_ms": 100}])
    assert result["status"] == "completed"
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/batch"]
    assert len(posts) == 3
    assert all(p[2]["batch_id"] == "retry-1" for p in posts)


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
    HttpBridgeServer.java) and it does NOT read actual Logcat
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
    assert ("POST", "/press_key", {"key": "DPAD_DOWN"}) in posts


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
    src = _java_source("BridgeAccessibilityService.java")
    start = src.index("private boolean collectNodes(")
    end = src.index("Node n = new Node();", start)
    region = src[start:end]
    assert region.count("node.recycle();") == 2, region


def test_p1_06_accept_loop_checks_generation():
    """P1-06 (CONS-014): the accept loop must stop when its
    generation no longer matches the server lifecycle, so a
    stale thread that wakes after stop/restart exits instead
    of spinning on its closed socket. Source-level guard: the
    loop condition must contain the generation equality check."""
    src = _java_source("HttpBridgeServer.java")
    assert "&& acceptGeneration == serverLifecycleGeneration" in src


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
    src = _java_source("HttpBridgeServer.java")
    # (1) the request log goes through loggablePath, never raw path.
    assert 'Log.i(TAG, method + " " + loggablePath(path) + " -> " + status);' in src
    assert 'Log.i(TAG, method + " " + path + " -> " + status);' not in src
    # (2) KNOWN_PATHS matches the handler chain exactly.
    chain = set(re.findall(r'"(/[^"]*)"\.equals\(path\)', src))
    start = src.index("KNOWN_PATHS = new HashSet<>(Arrays.asList(")
    end = src.index("));", start)
    known = set(re.findall(r'"(/[^"]*)"', src[start:end]))
    assert chain, "no endpoints found in the handler chain"
    assert known == chain, (sorted(known - chain), sorted(chain - known))
    # (3) loggablePath maps unknown paths to "UNKNOWN".
    helper = src[src.index("static String loggablePath("):]
    helper = helper[:helper.index("}")]
    assert 'KNOWN_PATHS.contains(path) ? path : "UNKNOWN"' in helper


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
    src = _java_source("HttpBridgeServer.java")
    # (1) handler: pre-read first, baseline from launchApp().
    start = src.index('"/launch_app".equals(path)')
    end = src.index('} else if ("/open".equals(path)', start)
    region = src[start:end]
    pre = region.index("foregroundPackageSafe(svc,")
    call = region.index("long requestTimeMs = launchApp(pkg);")
    assert pre < call, (pre, call)
    assert "elapsedRealtime()" not in region
    assert "if (requestTimeMs < 0)" in region
    # (2) launchApp(): Intent fetch < baseline < startActivity.
    body = src[src.index("private long launchApp(String pkg) {"):]
    body = body[:body.index("\n    }\n")]
    fetch = body.index("getLaunchIntentForPackage(pkg)")
    base = body.index(
        "long requestTimeMs = android.os.SystemClock.elapsedRealtime();")
    start_activity = body.index("context.startActivity(intent);")
    assert fetch < base < start_activity, (fetch, base, start_activity)
    assert "return requestTimeMs;" in body
    assert "return -1;" in body


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
    src = _java_source("HttpBridgeServer.java")
    # (1) locals inside the handler, timestamp local in launchApp.
    start = src.index('"/launch_app".equals(path)')
    end = src.index('} else if ("/open".equals(path)', start)
    region = src[start:end]
    assert "long requestTimeMs = launchApp(pkg);" in region
    assert "boolean wasForeground = svc != null" in region
    assert "if (requestTimeMs < 0)" in region
    launch = src[src.index("private long launchApp(String pkg) {"):]
    launch = launch[:launch.index("\n    }\n")]
    assert ("long requestTimeMs = android.os.SystemClock.elapsedRealtime();"
            in launch)
    # (2) neither name is an instance field. Fields live at
    # class level (exactly 4-space indent); method locals are
    # indented deeper, so anchoring on 4 spaces keeps locals
    # out. The optional initializer covers `= 0;` style
    # declarations, and modifiers may appear in any order.
    for name, typ in (("requestTimeMs", "long"),
                      ("wasForeground", "boolean")):
        assert not re.search(
            rf"^    (?:(?:public|protected|private|static|final"
            rf"|volatile|transient)\s+)*{typ}\s+{name}\s*(=[^;]*)?;",
            src, re.MULTILINE), name
    # (3) verifyLaunchForeground takes the state as parameters.
    sig = src[src.index("private JSONObject verifyLaunchForeground("):]
    sig = sig[:sig.index("{")]
    assert "String pkg" in sig
    assert "BridgeAccessibilityService svc" in sig
    assert "long requestTimeMs" in sig
    assert "boolean wasForeground" in sig
    # (4) a dead service degrades to a missed poll, not a throw.
    fg = src[src.index("private String foregroundPackageSafe("):]
    fg = fg[:fg.index("private static JSONObject launchResultJson(")]
    assert "if (svc == null) {\n            return null;\n        }" in fg
    assert "} catch (Exception e) {" in fg
    assert "return null;" in fg


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
    src = _java_source("HttpBridgeServer.java")
    start = src.index("} catch (ServiceUnavailable e) {")
    end = src.index(
        "} catch (BridgeAccessibilityService.ServiceTimeoutException e) {",
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
    svc = _java_source("BridgeService.java")
    start = svc.index("public void onCreate()")
    end = svc.index("public int onStartCommand", start)
    assert "BridgeWidgetProvider.updateAllWidgets(this)" in svc[start:end]
    start = svc.index("public void onDestroy()")
    end = svc.index("public IBinder onBind", start)
    assert "BridgeWidgetProvider.updateAllWidgets(this)" in svc[start:end]
    main = _java_source("MainActivity.java")
    start = main.index("modeButton.setOnClickListener")
    # NOTE (2026-11-08, v2.8.6): the end marker was the pre-redesign
    # `Button notif = findViewById`; the current code declares the
    # field as `notifButton` and assigns it with
    # `notifButton = findViewById(...)`. Same region, same intent.
    end = main.index("notifButton = findViewById", start)
    assert ("BridgeWidgetProvider.updateAllWidgets(MainActivity.this)"
            in main[start:end])


def test_widget_a11y_broadcast_is_explicit():
    """W3: the a11y-state broadcast must reach the manifest
    widget receiver — since API 26 a manifest receiver cannot
    receive an implicit broadcast, so the send must be explicit
    (setPackage). Runtime receivers in this app are unaffected."""
    src = _java_source("BridgeAccessibilityService.java")
    start = src.index("private void broadcastA11yStateChanged()")
    end = src.index("@Override", start)
    region = src[start:end]
    assert ".setPackage(getPackageName())" in region


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
    attempts and backoff time. GET /state and POST /batch each
    send exactly one request."""
    MockAppHandler.accessibility = False
    adapter = ab.AndroidAdapter(actions_enabled=True)
    with pytest.raises(ab.AccessibilityUnavailableError):
        adapter.get_current_state()
    with pytest.raises(ab.AccessibilityUnavailableError):
        adapter.batch("b-1", 123456,
                      [{"action_id": "a1", "type": "tap", "x": 1, "y": 2}])
    gets = [r for r in MockAppHandler.requests
            if r[0] == "GET" and r[1] == "/state"]
    assert len(gets) == 1
    posts = [r for r in MockAppHandler.requests
             if r[0] == "POST" and r[1] == "/batch"]
    assert len(posts) == 1


def test_app_a11y_off_available_endpoints(mock_app):
    """P0-1: with a11y off, the endpoints that do not need the
    service still answer 200 — /health, /logs, /notifications,
    /open, /install_apk, GET/POST /clipboard, /launch_app,
    /notify, /notification_action."""
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
    assert adapter.notification_action("key-1", "open")["ok"] is True


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
    assert ("POST", "/notification_action",
            {"key": NOTIF_KEY, "action": "open"}) in posts


def test_app_notification_reply(mock_app):
    """Decision 77: notification_action reply posts key + action +
    the exact text."""
    adapter = ab.AndroidAdapter(actions_enabled=True)
    assert adapter.notification_action(
        NOTIF_KEY, "reply", text="はい")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/notification_action",
            {"key": NOTIF_KEY, "action": "reply", "text": "はい"}) in posts


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
    return _java_source("LogActivity.java")


def _drive_registry():
    return _java_source("UriPermissionRegistry.java")


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
    assert "getPersistedUriPermissions" in src
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
    assert "data.getFlags()" in src
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
    assert "!oldUri.equals(uriString)" in src


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
    ok = src.index('atomicWriteText(getFilesDir(), FOLDER_URI_NAME,')
    after = src[ok:ok + 800]
    assert "releaseIfUnused" in after


def test_drive001_t12_ioexception_keeps_folder():
    """T-12: an IOException during the failure classification
    keeps the folder (classify returns false on any exception)."""
    src = _drive_log()
    start = src.index("private boolean classifyAutoSaveFailure")
    region = src[start:start + 2600]
    assert "catch (Exception ignored)" in region
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
    start = src.index("private void handleFolderPicked")
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
    start = src.index("private void doClearFolder")
    region = src[start:start + 1200]
    assert "toast_log_folder_clear_failed" in region
    assert "atomicWriteText(getFilesDir(), CLEAR_PENDING_NAME" in region


def test_drive001_t21_security_exception_keeps_folder():
    """T-21: a SecurityException alone never clears the folder
    (the classification catch returns keep)."""
    src = _drive_log()
    start = src.index("private boolean classifyAutoSaveFailure")
    region = src[start:start + 2600]
    assert "catch (Exception ignored)" in region
    assert "return false" in region


def test_drive001_t22_file_not_found_keeps_folder():
    """T-22: a FileNotFoundException alone never clears the
    folder (same classification catch as T-21)."""
    src = _drive_log()
    start = src.index("private boolean classifyAutoSaveFailure")
    region = src[start:start + 2600]
    assert "catch (Exception ignored)" in region
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
    start = src.index("private void writeLogToUriInBackground")
    region = src[start:start + 2600]
    assert "finally" in region
    assert "endOperation(opId)" in region


def test_drive001_t25_no_stale_view_ops_after_destroy():
    """T-25: completion notifications check isFinishing /
    isDestroyed and go through a WeakReference."""
    src = _drive_log()
    assert "WeakReference<LogActivity> resumedActivity" in src
    assert "isFinishing()" in src
    assert "isDestroyed()" in src


def test_drive001_t26_delete_attempted_on_null_stream():
    """T-26: when openOutputStream returns null, the created
    document is still deleted (the outer catch runs
    deleteCreatedBestEffort)."""
    src = _drive_log()
    start = src.index("private void writeLogToUriInBackground")
    region = src[start:start + 2600]
    assert "deleteCreatedBestEffort" in region
    assert "openOutputStream returned null" in region


def test_drive001_t27_parent_absence_vs_transient_failure():
    """T-27: parent absence is confirmed by two independent
    queries (with a pause between them); a single failure
    keeps the folder."""
    src = _drive_log()
    start = src.index("private boolean classifyAutoSaveFailure")
    region = src[start:start + 2600]
    assert "for (int i = 0; i < 2; i++)" in region
    assert "Thread.sleep(500)" in region
    assert "emptyCount == 2" in region


def test_drive001_t28_memory_and_disk_uri_agree():
    """T-28: the saved URI is always re-read from disk
    (readFolderUri), never trusted from a stale in-memory copy."""
    src = _drive_log()
    assert "readFolderUri()" in src
    assert "private String readFolderUriString()" in src


def test_drive001_t29_fallback_cancel_releases_state():
    """T-29: cancelling the fallback picker releases the
    operation (endOperation + UI sync)."""
    src = _drive_log()
    start = src.index("protected void onActivityResult")
    region = src[start:start + 2600]
    assert "User cancelled" in region
    assert "endOperation(req.opId)" in region


def test_drive001_t30_state_is_static_and_shared():
    """T-30: the operation state is static, so a recreated
    activity sees the same state."""
    src = _drive_log()
    assert "private static OpState state" in src
    assert "private static long currentOpId" in src
    assert "private static final Map<Integer, PickerRequest>" in src


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
    assert "if (!newUri.equals(oldUri))" in region


def test_drive001_t33_atomic_write_pattern():
    """T-33: atomic write is temp + fsync + Os.rename + parent
    dir fsync (BridgeLog.atomicWriteText)."""
    src = _java_source("BridgeLog.java")
    start = src.index("static boolean atomicWriteText")
    region = src[start:start + 1600]
    assert 'fileName + ".tmp"' in region
    assert "getFD().sync()" in region
    assert "Os.rename(" in region
    assert "fsyncDirectory(dir)" in region


def test_drive001_t34_three_way_folder_read():
    """T-34: the saved-folder read distinguishes OK / MISSING /
    ERROR (FolderKind)."""
    src = _drive_log()
    assert "enum FolderKind" in src
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
    bridge = _java_source("BridgeLog.java")
    start = bridge.index("static boolean atomicWriteText")
    region = bridge[start:start + 1600]
    assert "return fsyncDirectory(dir)" in region


def test_drive001_t37_state_machine_with_op_id():
    """T-37: the state machine carries an op id; a stale
    callback cannot clear a newer operation."""
    src = _drive_log()
    assert "req.opId != currentOpId" in src
    assert "private static boolean transition" in src


def test_drive001_t38_lock_not_held_during_async_work():
    """T-38: SAVE_LOCK is never held during the background
    write — the write thread only calls endOperation /
    notifySaveFinished (which take the lock briefly)."""
    src = _drive_log()
    start = src.index("new Thread(new Runnable()")
    region = src[start:start + 2600]
    assert "synchronized (SAVE_LOCK)" not in region
    assert "synchronized(SAVE_LOCK)" not in region


def test_drive001_t39_strict_absence_check():
    """T-39: absence is decided by Os.stat + ENOENT, never by
    File.exists() alone."""
    src = _drive_log()
    assert "Os.stat(f.getAbsolutePath())" in src
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
    assert src.count("syncUiFromState();") >= 3


def test_drive001_t41_rename_ok_dir_fsync_fail_rereads():
    """T-41: after a rename-OK / dir-fsync-fail, the stored
    file is re-read instead of assuming the old file."""
    src = _drive_log()
    start = src.index("handleFolderWriteFailed")
    region = src[start:start + 1800]
    assert "readFolderUriString()" in region
    assert "stored.equals(oldUri)" in region


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
    start = src.index("protected void onActivityResult")
    region = src[start:start + 2600]
    assert "req.opId != currentOpId" in region
    assert "requestCodeToOpId.remove(requestCode)" in region


def test_drive001_t44_mapping_survives_recreation():
    """T-44: the requestCode -> op id map is static, so it
    survives activity recreation."""
    src = _drive_log()
    assert "private static final Map<Integer, PickerRequest>" in src
    assert "private static int currentPickerRequestCode" in src


def test_drive001_t45_transition_under_exclusion():
    """T-45: the PICKER_PENDING -> WRITING transition runs
    inside the same SAVE_LOCK section as the checks."""
    src = _drive_log()
    start = src.index("protected void onActivityResult")
    region = src[start:start + 2600]
    assert "synchronized (SAVE_LOCK)" in region
    assert "transition(OpState.PICKER_PENDING, req.opId," in region
    assert "OpState.WRITING" in region


def test_drive001_t46_absence_vs_io_error():
    """T-46: file absence (ENOENT) and I/O errors (EACCES/EIO)
    are distinct FileState values."""
    src = _drive_log()
    assert "enum FileState" in src
    assert "EXISTS, NOT_EXISTS, UNKNOWN" in src


def test_drive001_t47_recreated_activity_syncs_on_finish():
    """T-47: the completion notification resyncs the resumed
    activity from the static state (not from the notification)."""
    src = _drive_log()
    start = src.index("private void notifySaveFinished")
    region = src[start:start + 1200]
    assert "syncUiFromState()" in region
    assert "resumedActivity" in region


def test_drive001_t48_process_death_no_stale_state():
    """T-48: after a process death the static state is fresh,
    so an unknown requestCode is ignored in onActivityResult."""
    src = _drive_log()
    start = src.index("protected void onActivityResult")
    region = src[start:start + 1200]
    assert "req == null" in region
    assert "unknown or already invalidated" in region


def test_drive001_t49_clear_order_marker_then_delete_then_release():
    """T-49: clear writes the marker, deletes the settings
    file, syncs the dir, then releases (doClearFolder order)."""
    src = _drive_log()
    start = src.index("private void doClearFolder")
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
    start = src.index("protected void onActivityResult")
    region = src[start:start + 2600]
    assert region.count("synchronized (SAVE_LOCK)") == 1
    assert "requestCodeToOpId.get(requestCode)" in region
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
    assert "private static long beginOperation" in src
    assert "private static boolean transition" in src
    assert "private static void endOperation" in src


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
    start = src.index("protected void onActivityResult")
    region = src[start:start + 2600]
    assert "PICKER_TIMEOUT_MS" in region
    assert "endOperation(req.opId)" in region


def test_drive001_t55_timeout_vs_callback_single_winner():
    """T-55: the timeout path and the result callback share one
    SAVE_LOCK section, so only one of them wins."""
    src = _drive_log()
    start = src.index("protected void onActivityResult")
    region = src[start:start + 2600]
    assert region.count("synchronized (SAVE_LOCK)") == 1
    start2 = src.index("private void checkPickerTimeout")
    region2 = src[start2:start2 + 1200]
    assert "synchronized (SAVE_LOCK)" in region2


def test_drive001_t56_op_id_mismatch_keeps_current_op():
    """T-56: on an op id mismatch only that map entry is
    removed; the current operation is untouched."""
    src = _drive_log()
    start = src.index("protected void onActivityResult")
    region = src[start:start + 1200]
    assert "req.opId != currentOpId" in region
    assert "Stale result" in region


def test_drive001_t57_request_code_exhaustion_refuses():
    """T-57: requestCode exhaustion (beyond 65535) refuses the
    launch and leaves the state IDLE."""
    src = _drive_log()
    start = src.index("allocateRequestCode")
    region = src[start:start + 1400]
    assert "REQUEST_CODE_MAX" in region
    assert "return -1" in region


def test_drive001_t58_next_launch_releases_only_orphans():
    """T-58: the next-launch check releases only orphaned
    permissions (registry reconcile + clear-marker handling),
    never a bulk release."""
    src = _drive_log()
    assert "startupConsistencyCheck" in src
    assert "UriPermissionRegistry.reconcile" in src
    assert "getPersistedUriPermissions" in src
    reg = _drive_registry()
    assert "public static void reconcile" in reg


def test_drive001_t59_allowed_transition_combinations():
    """T-59: beginOperation only from IDLE; transition only
    PICKER_PENDING -> WRITING; endOperation only to IDLE."""
    src = _drive_log()
    assert "if (state != expected)" in src
    assert "transition(OpState.PICKER_PENDING, req.opId," in src
    start = src.index("private static void endOperation")
    region = src[start:start + 400]
    assert "state = OpState.IDLE" in region


def test_drive001_t60_old_process_code_no_effect():
    """T-60: a requestCode issued by an old process has no
    effect after a restart (unknown code ignored; the
    persistent counter never reuses a code)."""
    src = _drive_log()
    assert "req == null" in src
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
    start = src.index("protected void onActivityResult")
    region = src[start:start + 2600]
    mismatch = region.index("req.opId != currentOpId")
    deadline = region.index("PICKER_TIMEOUT_MS")
    assert mismatch < deadline


def test_drive001_t63_marker_before_settings_delete():
    """T-63: the clear marker is created (and synced) before
    the settings file is deleted."""
    src = _drive_log()
    start = src.index("private void doClearFolder")
    region = src[start:start + 2400]
    marker = region.index("CLEAR_PENDING_NAME")
    delete = region.index("settings.delete()")
    assert marker < delete


def test_drive001_t64_eacces_not_mistaken_for_absence():
    """T-64: EACCES/EIO is never mistaken for absence (statFile
    returns UNKNOWN, never NOT_EXISTS, for other errnos)."""
    src = _drive_log()
    start = src.index("private static FileState statFile")
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
    start = src.index("public static void ensureDriveInit")
    region = src[start:start + 3600]
    assert "never auto-resets to 1000" in region
    assert "checkDriveInitOk" in src


def test_drive001_t67_first_init_random_epoch():
    """T-67: the first init starts the counter from a random
    epoch (SecureRandom), not from a fixed 1000."""
    src = _drive_log()
    assert "SecureRandom" in src
    assert "EPOCH_MAX" in src
    assert "REQUEST_CODE_MIN + new SecureRandom()" in src


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
    start = src.index("private void doClearFolder")
    region = src[start:start + 1200]
    assert "toast_log_folder_clear_failed" in region


def test_drive001_t70_release_failure_keeps_marker():
    """T-70: when the release fails at the startup check, the
    marker is kept and retried at the next launch."""
    src = _drive_log()
    start = src.index("private void startupConsistencyCheck")
    region = src[start:start + 2400]
    assert "if (released)" in region
    assert "deleteClearMarker()" in region


def test_drive001_t71_marker_present_counter_missing_refuses():
    """T-71: marker present + counter missing is abnormal and
    refuses the picker (the marker alone is not proof)."""
    src = _drive_log()
    start = src.index("public static void ensureDriveInit")
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
    start = src.index("private void doClearFolder")
    region = src[start:start + 2400]
    assert "if (released)" in region
    assert "keep the marker for next-launch reconciliation" in region


def test_drive001_t74_shared_uri_keeps_other_permission():
    """T-74: releasing a URI used by another feature is
    refused (isUsedByOther / the other-feature check)."""
    reg = _drive_registry()
    assert "isUsedByOther" in reg
    assert "!e[0].equals(feature)" in reg


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
    assert "REQUEST_CODE_MIN + new SecureRandom()" in src


def test_drive001_t76_folder_absent_history_lost_refuses():
    """T-76: when the folder URI file is absent and the
    counter history is lost, the picker refuses (the init
    gate requires a readable counter)."""
    src = _drive_log()
    assert "checkDriveInitOk" in src
    start = src.index("private static boolean checkDriveInitOk")
    region = src[start:start + 800]
    assert "counter.kind == ReadKind.OK" in region


def test_drive001_t77_marker_absent_counter_corrupt_refuses():
    """T-77: marker absent + counter corrupt refuses the
    picker (both must be OK)."""
    src = _drive_log()
    start = src.index("private static boolean checkDriveInitOk")
    region = src[start:start + 800]
    assert "marker.kind == ReadKind.OK" in region
    assert "counter.kind == ReadKind.OK" in region


def test_drive001_t78_registry_write_failure_disables_feature():
    """T-78: when the registry write fails after the grant,
    the feature is not enabled for that URI (the permission
    is kept)."""
    src = _drive_log()
    start = src.index("private void handleFolderPicked")
    region = src[start:start + 2600]
    assert "UriPermissionRegistry.register" in region
    assert "toast_log_folder_unreadable" in region


def test_drive001_t79_registry_read_failure_no_release():
    """T-79: when the registry cannot be read, no permission
    is released (isUsedByOther / releaseIfUnused fail safe)."""
    reg = _drive_registry()
    assert "entries == null" in reg
    start = reg.index("public static boolean isUsedByOther")
    region = reg[start:start + 900]
    assert "return true" in region


def test_drive001_t80_registry_ops_serialized_under_one_lock():
    """T-80: register / unregister / read / release all run
    under the one private REGISTRY_LOCK."""
    reg = _drive_registry()
    assert "private static final Object REGISTRY_LOCK" in reg
    assert reg.count("synchronized (REGISTRY_LOCK)") >= 5


def test_drive001_t81_release_registry_failure_reconciled():
    """T-81: a registry-update failure after a release leaves
    a stale entry that reconcile() drops at next launch."""
    reg = _drive_registry()
    assert "public static void reconcile" in reg
    start = reg.index("public static void reconcile")
    region = reg[start:start + 1200]
    assert "getPersistedUriPermissions" in region
    assert "writeLocked(ctx, entries)" in region


def test_drive001_t82_kind_a_marker_deletable():
    """T-82: Kind A (file present + old URI readable) may
    delete the marker."""
    src = _drive_log()
    start = src.index("private void doClearFolder")
    region = src[start:start + 2400]
    assert "kind A" in region
    assert "deleteClearMarker(); // kind A" in region


def test_drive001_t83_kind_b_marker_kept():
    """T-83: Kind B (state unconfirmable) keeps the marker."""
    src = _drive_log()
    start = src.index("private void doClearFolder")
    region = src[start:start + 2400]
    assert "kind B" in region
    assert "keep the permission and the marker" in region


def test_drive001_t84_kind_c_release_then_marker():
    """T-84: Kind C (ENOENT confirmed) proceeds to release,
    and the marker stays until the release succeeds."""
    src = _drive_log()
    start = src.index("private void doClearFolder")
    region = src[start:start + 2400]
    assert "kind C" in region
    assert "if (released)" in region


def test_drive001_t85_first_init_random_start_recorded():
    """T-85: the first init picks a random start in
    1000..60000 and records it in the counter file."""
    src = _drive_log()
    assert "EPOCH_MAX = 60000" in src
    assert "atomicWriteText(ctx.getFilesDir(), COUNTER_NAME," in src


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
    start = src.index("private void doClearFolder")
    region = src[start:start + 2400]
    assert "stored.trim().equals(uriString)" in region


def test_drive001_t89_registry_lock_private_and_single_entry():
    """T-89: REGISTRY_LOCK is private to UriPermissionRegistry
    and every operation goes through its synchronized methods."""
    reg = _drive_registry()
    assert "private static final Object REGISTRY_LOCK" in reg
    assert "public static boolean register" in reg
    assert "public static boolean unregister" in reg
    assert "public static boolean isUsedByOther" in reg
    assert "public static boolean releaseIfUnused" in reg
    assert "public static void reconcile" in reg
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
    start = src.index("public static void ensureDriveInit")
    region = src[start:start + 400]
    assert "verification impossible" in region


def test_drive001_t92_corrupt_registry_refuses_and_keeps():
    """T-92: a corrupt registry file refuses every
    registry-dependent operation, keeps existing permissions,
    and is never auto-deleted."""
    reg = _drive_registry()
    assert "return null" in reg
    start = reg.index("private static List<String[]> readLocked")
    region = reg[start:start + 1400]
    assert "parts.length != 2" in region
    assert "never auto-deleted" in reg


def test_drive001_t93_update_install_can_initialise():
    """T-93 (v2.9.1): an UPDATE install can initialise. The
    v2.9.0 first init required firstInstallTime ==
    lastUpdateTime, which is false on every updated install,
    so the picker could never open after an update (live
    defect 2026-11-08). The init must not compare the two
    install times."""
    src = _drive_log()
    start = src.index("public static void ensureDriveInit")
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
    start = src.index("public static void ensureDriveInit")
    region = src[start:start + 3200]
    assert "runMetaFile" not in region
    assert "FOLDER_URI_NAME" in region
    assert "CLEAR_PENDING_NAME" in region
    assert "registryFile" in region
    assert "getPersistedUriPermissions" in region


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


def test_override_version_2_9_2():
    """Version: the build carries v2.9.2 / code 55."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent
    build = (root / "android-app" / "build.sh").read_text(
        encoding="utf-8")
    assert 'VERSION_NAME="2.9.2"' in build
    assert "VERSION_CODE=55" in build


def test_drive001_main_activity_calls_ensure_init():
    """First-init hooks: MainActivity, BootReceiver and the
    server constructor all run ensureDriveInit before any
    app-owned state file is created."""
    main = _java_source("MainActivity.java")
    assert "LogActivity.ensureDriveInit(this);" in main
    boot = _java_source("BootReceiver.java")
    assert "LogActivity.ensureDriveInit(" in boot
    assert boot.index("ensureDriveInit") < boot.index("BridgeLog.init")
    server = _java_source("HttpBridgeServer.java")
    assert "LogActivity.ensureDriveInit(" in server
    assert (server.index("ensureDriveInit")
            < server.index("BridgeLog.init(this.context)"))

# ----------------------------------------------------------------------
# SPEC-OVERRIDE-001 (v2.9.2): live "Override - x/y" operation
# notification. Source-level guards (OV-01..OV-15); the actual
# notification behaviour (OV-L1..OV-L7) is verified live on the phone.
# ----------------------------------------------------------------------

def _override_server():
    return _java_source("HttpBridgeServer.java")


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
    assert 'noteOperation(lifecycleGeneration, "リンクを開いています")' in src
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
    assert 'noteOperation(lifecycleGeneration, "複数の操作を実行中")' in src
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
    assert "final long capturedGeneration = operationGeneration" in src
    assert "final long capturedToken = pendingToken" in src
    assert "capturedGeneration != operationGeneration" in src
    assert "capturedToken != pendingToken" in src
    assert "operationScheduler.schedule" in src


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
    start = src.index("private boolean endOperationLocked()")
    seg = src[start:start + 1500]
    assert "invalidatePendingPostLocked();" in seg


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
    start = src.index("private void runDeferredPost(")
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
    ensureDriveInit is NOT "absent" — init is refused."""
    log = _drive_log()
    assert "noPersistedPermissions = false;" in log
    assert "noPersistedPermissions" in log


def test_override_p202_classify_failure_keeps_folder():
    """CONS-078 P2-02: a failed persisted-URI-permission check in
    classifyAutoSaveFailure keeps the folder (failure is not
    "permission absent")."""
    log = _drive_log()
    start = log.index("private boolean classifyAutoSaveFailure")
    seg = log[start:start + 2000]
    assert "getPersistedUriPermissions" in seg
    assert "catch (Exception ignored)" in seg
    assert "return false;" in seg
