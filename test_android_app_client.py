"""Tests for the bridge-app ("app") transport of android_bridge.py.

A local mock HTTP server stands in for the phone's Muse Bridge app: it
checks the X-Bridge-Token header and answers the same endpoints. No phone
is involved, and no real token is used (the mock token is a test-only
constant, never a real credential).
"""
import json
import struct
import threading
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


class MockAppHandler(BaseHTTPRequestHandler):
    requests = []
    # A-3 (v1.9): how /state answers, per test. "normal" always returns
    # a stable snapshot; "unstable_then_same" returns an unstable
    # snapshot first, then the same tree hash stable; "unstable_then_
    # changed" returns an unstable snapshot first, then a changed tree
    # hash stable. Reset by the fixture.
    state_mode = "normal"
    state_calls = 0

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
        if self.path == "/health":
            return self._send_json(200, {"ok": True, "service": "muse-bridge",
                                         "version": 1, "accessibility": True,
                                         "app_version": "1.9.1",
                                         "app_version_code": 11})
        if self.path.startswith("/state?"):
            # A-1 (v1.9): simulated bridge input-validation rejections.
            import urllib.parse
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            err = query.get("err", [None])[0]
            if err in ("400", "408", "413", "431", "503"):
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
            return self._send_json(200, {"ok": True, "width": 1080, "height": 2340,
                                         "package": "com.example", "texts": ["Hi"],
                                         "revision": 7, "tree_hash": tree_hash,
                                         "snapshot_id": snap,
                                         "snapshot_valid": True,
                                         "snapshot_stable": stable,
                                         "event_seq": 42,
                                         "app_version": "1.9.1",
                                         "app_version_code": 11,
                                         "nodes": [
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
                                         ],
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
        return self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        payload = self._read_json()
        MockAppHandler.requests.append(("POST", self.path, payload))
        if not self._check_token():
            return self._send_json(401, {"ok": False, "error": "unauthorized"})
        if self.path == "/batch":
            # A-4 (v1.9): a realistic batch response, with the status,
            # payload hash, and completed/failed action ids.
            acts = payload.get("actions", [])
            return self._send_json(200, {
                "ok": True, "batch_id": payload.get("batch_id"),
                "snapshot_id": 7, "status": "completed",
                "payload_hash": "ab" * 32, "replayed": False,
                "results": [{"action_id": a.get("action_id"),
                             "status": "executed", "error": None,
                             "hash_before": 123456, "hash_after": 123456}
                            for a in acts],
                "completed_action_ids": [a.get("action_id") for a in acts],
                "failed_action_id": None,
                "stopped_at": -1, "stop_reason": None})
        return self._send_json(200, {"ok": True, "echo": payload})


@pytest.fixture()
def mock_app(monkeypatch):
    MockAppHandler.requests.clear()
    MockAppHandler.state_mode = "normal"
    MockAppHandler.state_calls = 0
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
    adapter = ab.AndroidAdapter(transport="app")
    data = adapter.capture_screen()
    assert data.startswith(b"\xff\xd8\xff")
    gets = [r for r in MockAppHandler.requests if r[0] == "GET"]
    assert len(gets) == 1
    path = gets[0][1]
    assert path.startswith("/capture_screen?")
    assert "scale=0.5" in path and "format=jpeg" in path and "quality=80" in path


def test_app_screenshot_full_png_on_request(mock_app):
    adapter = ab.AndroidAdapter(transport="app")
    data = adapter.capture_screen(scale=1.0, format="png")
    assert data.startswith(ab.PNG_MAGIC)
    gets = [r for r in MockAppHandler.requests if r[0] == "GET"]
    assert "scale=1.0" in gets[0][1] and "format=png" in gets[0][1]


def test_app_screenshot_rejects_non_image(mock_app):
    # Ask the mock for its test-only garbage body, then check the
    # client's image validation rejects it.
    adapter = ab.AndroidAdapter(transport="app")
    with pytest.raises(ab.TransportError):
        adapter._app_request("GET", "/capture_screen?bad=1", expect_image=True)


def test_app_state(mock_app):
    adapter = ab.AndroidAdapter(transport="app")
    state = adapter.get_current_state()
    assert state["width"] == 1080
    assert state["height"] == 2340
    assert state["transport"] == "app"


def test_app_state_tree_v14(mock_app):
    """Phase 1 (v1.4): /state carries the node tree, focus, IME state,
    and revision; password values arrive already redacted."""
    adapter = ab.AndroidAdapter(transport="app")
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
    adapter = ab.AndroidAdapter(transport="app", actions_enabled=True)
    assert adapter.click(500, 800)["ok"] is True
    assert adapter.swipe(100, 200, 300, 400)["ok"] is True
    assert adapter.type_text("hello")["ok"] is True
    assert adapter.press_key("HOME")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert (("POST", "/click", {"x": 500, "y": 800}) in posts)
    assert (("POST", "/press_key", {"key": "HOME"}) in posts)


def test_app_actions_refused_without_flag(mock_app):
    adapter = ab.AndroidAdapter(transport="app")
    with pytest.raises(PermissionError):
        adapter.click(1, 1)
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_wrong_token_is_transport_error(mock_app, monkeypatch):
    monkeypatch.setattr(ab, "APP_TOKEN", "wrong")
    adapter = ab.AndroidAdapter(transport="app")
    with pytest.raises(ab.TransportError):
        adapter.capture_screen()


def test_app_missing_token_is_transport_error(mock_app, monkeypatch):
    monkeypatch.setattr(ab, "APP_TOKEN", "")
    adapter = ab.AndroidAdapter(transport="app")
    with pytest.raises(ab.TransportError):
        adapter.get_current_state()


def test_app_notify_posts_title_and_text(mock_app):
    """v1.3: notify posts to /notify without the --allow-actions gate
    (it only shows a notification; it cannot change phone state)."""
    adapter = ab.AndroidAdapter(transport="app", actions_enabled=False)
    result = adapter.notify("Muse Bridge の更新", "Muse_Bridge_v1.3.apk が届いたよ。")
    assert result["ok"] is True
    posts = MockAppHandler.requests
    assert ("POST", "/notify",
            {"title": "Muse Bridge の更新", "text": "Muse_Bridge_v1.3.apk が届いたよ。"}) in posts


def test_app_notify_error_surfaces(mock_app, monkeypatch):
    """A 422 (notifications not permitted) must reach the caller."""
    adapter = ab.AndroidAdapter(transport="app", actions_enabled=False)

    def fake_post(path, payload):
        raise ab.TransportError("HTTP 422: notifications not permitted")
    monkeypatch.setattr(adapter, "_app_post", fake_post)
    with pytest.raises(ab.TransportError, match="notifications not permitted"):
        adapter.notify("t", "x")


def test_app_click_node_and_type_node_send_json(mock_app):
    """Phase 2 (v1.5): node actions post node_id + tree_hash."""
    adapter = ab.AndroidAdapter(transport="app", actions_enabled=True)
    assert adapter.click_node("n5", 123456)["ok"] is True
    assert adapter.type_node("n1", 123456, "hello")["ok"] is True
    posts = [r for r in MockAppHandler.requests if r[0] == "POST"]
    assert ("POST", "/click_node", {"node_id": "n5", "tree_hash": 123456}) in posts
    assert ("POST", "/type_node",
            {"node_id": "n1", "tree_hash": 123456, "text": "hello"}) in posts


def test_app_batch_posts_actions(mock_app):
    """Phase 2 (v1.5): batch posts batch_id + tree_hash + actions."""
    adapter = ab.AndroidAdapter(transport="app", actions_enabled=True)
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
    completed_action_ids, and failed_action_id."""
    adapter = ab.AndroidAdapter(transport="app", actions_enabled=True)
    actions = [{"action_id": "a1", "type": "tap", "x": 1, "y": 2}]
    result = adapter.batch("b-002", 123456, actions)
    assert result["status"] == "completed"
    assert result["payload_hash"] == "ab" * 32
    assert result["completed_action_ids"] == ["a1"]
    assert result["failed_action_id"] is None
    assert result["replayed"] is False


def test_app_phase2_actions_refused_without_flag(mock_app):
    """Node actions and batch need --allow-actions, like other actions."""
    adapter = ab.AndroidAdapter(transport="app")
    with pytest.raises(PermissionError):
        adapter.click_node("n5", 123456)
    with pytest.raises(PermissionError):
        adapter.type_node("n1", 123456, "x")
    with pytest.raises(PermissionError):
        adapter.batch("b-001", 123456, [])
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_phase2_actions_need_app_transport():
    adapter = ab.AndroidAdapter(transport="termux-rish", actions_enabled=True)
    with pytest.raises(ab.TransportError):
        adapter.click_node("n5", 1)
    with pytest.raises(ab.TransportError):
        adapter.batch("b-001", 1, [])


def test_app_state_event_seq_v16(mock_app):
    """Phase 3 (v1.6): /state carries the latest event seq, so the
    client can tell from /state alone whether anything happened
    since its last /events poll."""
    adapter = ab.AndroidAdapter(transport="app")
    state = adapter.get_current_state()
    assert state["event_seq"] == 42


def test_app_state_app_version_v191(mock_app):
    """v1.9: /state and /health carry the installed app version, so
    the VM can tell which build is on the phone without guessing
    from behaviour. The version comes from the APK itself (aapt2
    --version-name/--version-code), never a hardcoded constant."""
    adapter = ab.AndroidAdapter(transport="app")
    state = adapter.get_current_state()
    assert state["app_version"] == "1.9.1"
    assert state["app_version_code"] == 11
    import urllib.request
    req = urllib.request.Request(
        f"{ab.APP_URL}/health", headers={"X-Bridge-Token": MOCK_TOKEN})
    with urllib.request.urlopen(req, timeout=10) as resp:
        health = json.loads(resp.read().decode("utf-8"))
    assert health["app_version"] == "1.9.1"
    assert health["app_version_code"] == 11
    # The old protocol version field is untouched.
    assert health["version"] == 1


@pytest.mark.parametrize("code", ["400", "408", "413", "431"])
def test_app_http_input_errors_classified_v19(mock_app, code):
    """A-1 (v1.9): bridge input-validation rejections (400/408/413/
    431) reach the caller as TransportError with the input-validation
    hint, never blamed on the token."""
    adapter = ab.AndroidAdapter(transport="app")
    with pytest.raises(ab.TransportError) as excinfo:
        adapter._app_request("GET", f"/state?err={code}")
    msg = str(excinfo.value)
    assert f"HTTP {code}" in msg
    assert "input validation" in msg
    assert "wrong token" not in msg


def test_app_503_hint_mentions_overload_v19(mock_app):
    """A-1 (v1.9): a 503 names the accessibility service or the
    overload, not the token."""
    adapter = ab.AndroidAdapter(transport="app")
    with pytest.raises(ab.TransportError) as excinfo:
        adapter._app_request("GET", "/state?err=503")
    msg = str(excinfo.value)
    assert "HTTP 503" in msg
    assert "overloaded" in msg
    assert "wrong token" not in msg


def test_app_state_snapshot_fields_v19(mock_app):
    """A-3 (v1.9): /state carries snapshot_id/snapshot_valid/
    snapshot_stable, so the client can tell whether the snapshot it
    holds is trustworthy before acting on its node ids."""
    adapter = ab.AndroidAdapter(transport="app")
    state = adapter.get_current_state()
    assert state["snapshot_id"] == 7
    assert state["snapshot_valid"] is True
    assert state["snapshot_stable"] is True


def test_app_stable_snapshot_no_refetch_v19(mock_app):
    """A-3: a stable snapshot needs no refetch before a node action."""
    adapter = ab.AndroidAdapter(transport="app", actions_enabled=True)
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
    adapter = ab.AndroidAdapter(transport="app", actions_enabled=True)
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
    adapter = ab.AndroidAdapter(transport="app", actions_enabled=True)
    state = adapter.get_current_state()
    assert state["snapshot_stable"] is False
    with pytest.raises(ab.TransportError, match="stale snapshot"):
        adapter.click_node("n5", state["tree_hash"])
    assert not [r for r in MockAppHandler.requests if r[0] == "POST"]


def test_app_events_returns_history(mock_app):
    """Phase 3 (v1.6): /events returns the recorded history with
    seq, type, package, text, bounds, and the coalesced count;
    password text arrives already redacted."""
    adapter = ab.AndroidAdapter(transport="app")
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
    adapter = ab.AndroidAdapter(transport="app")
    result = adapter.events(since=1, limit=1)
    assert [e["seq"] for e in result["events"]] == [2]
    gets = [r for r in MockAppHandler.requests if r[0] == "GET"]
    assert "since=1" in gets[0][1] and "limit=1" in gets[0][1]


def test_app_events_is_read_only(mock_app):
    """Events need no --allow-actions gate: they only report what
    happened on the phone."""
    adapter = ab.AndroidAdapter(transport="app")
    assert adapter.events()["ok"] is True


def test_app_events_needs_app_transport():
    adapter = ab.AndroidAdapter(transport="termux-rish")
    with pytest.raises(ab.TransportError):
        adapter.events()
