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
            if not v.is_integer() or abs(v) > 2**31 - 1:
                return f"{action} needs integer {key} (got {v!r})"
            v = int(v)
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
    # P0-4 deny list (Saardeim decision 2026-11-06 20:16,
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
            # in a log line.
            return self._send_json(200, {"ok": True,
                                         "app_version": "1.9.5",
                                         "app_version_code": 15,
                                         "lines": ["[11-06 03:55:00] listen 100.111.*.*:8080",
                                                   "[11-06 03:55:00] server RUNNING",
                                                   "[11-06 03:55:01] batch completed 1/1"],
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
    address or a package name — IPs are masked to two octets
    by BridgeLog.sanitize, and package names are dropped at
    the call sites. The listen line is masked, not full."""
    import re
    adapter = ab.AndroidAdapter()
    result = adapter.logs()
    lines = result["lines"] + result["previous_lines"]
    assert lines[0] == "[11-06 03:55:00] listen 100.111.*.*:8080"
    ipv4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
    for line in lines:
        assert not ipv4.search(line), line
        assert "com." not in line, line


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
    """P0-4 deny list (Saardeim decision 2026-11-06 20:16,
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
