"""Unit tests for the Android bridge (Phase 4). No phone needed.

Covers the safety-critical parts: the action gate (read-only by default),
the ProxyCommand helper (CONNECT framing + relay against a fake proxy),
and screenshot PNG validation.
"""
import os
import socket
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import android_bridge
import pytest


def test_actions_refused_by_default():
    adapter = android_bridge.AndroidAdapter(actions_enabled=False)
    with pytest.raises(PermissionError):
        adapter.click(100, 200)
    with pytest.raises(PermissionError):
        adapter.swipe(0, 0, 100, 100)
    with pytest.raises(PermissionError):
        adapter.type_text("hello")
    with pytest.raises(PermissionError):
        adapter.press_key("BACK")


def test_actions_allowed_flag_only_enables_explicit_calls():
    adapter = android_bridge.AndroidAdapter(actions_enabled=True)
    # With actions enabled but no phone reachable, the failure must be a
    # transport error, not a permission error: the gate opened, the
    # app transport refused (no APP_URL/APP_TOKEN in this env).
    with pytest.raises(android_bridge.TransportError):
        adapter.click(100, 200)


def test_adapter_takes_no_transport_argument():
    """Decision 60: the transport parameter is gone — passing it is
    a TypeError, not a ValueError for an unknown value."""
    with pytest.raises(TypeError):
        android_bridge.AndroidAdapter(transport="app")


def _fake_proxy(port_holder):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_holder.append(srv.getsockname()[1])

    def serve():
        conn, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        data = conn.recv(4096)
        conn.sendall(b"ECHO:" + data)
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()


def test_proxy_helper_connect_and_relay(monkeypatch, tmp_path):
    ports = []
    _fake_proxy(ports)
    monkeypatch.setenv("TUNNEL_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("TUNNEL_PROXY_PORT", str(ports[0]))
    # Re-read env into the module (it reads them at import time).
    monkeypatch.setattr(android_bridge, "TUNNEL_PROXY_HOST", "127.0.0.1")
    monkeypatch.setattr(android_bridge, "TUNNEL_PROXY_PORT", ports[0])
    result = subprocess.run(
        [sys.executable, android_bridge.__file__, "_proxy", "example.com", "443"],
        input=b"hello-relay", capture_output=True, timeout=20,
        env={**os.environ, "TUNNEL_PROXY_HOST": "127.0.0.1",
             "TUNNEL_PROXY_PORT": str(ports[0])},
    )
    assert result.returncode == 0
    assert result.stdout == b"ECHO:hello-relay"


def test_proxy_helper_refuses_empty_reply(monkeypatch):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.close()  # empty reply, like the tunnel proxy does now
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(android_bridge, "TUNNEL_PROXY_HOST", "127.0.0.1")
    monkeypatch.setattr(android_bridge, "TUNNEL_PROXY_PORT", port)
    with pytest.raises(ConnectionError, match="empty reply"):
        android_bridge.proxy_connect("192.0.2.1", 8022, timeout=10)


def _capturing_proxy(port_holder, request_holder):
    """Fake proxy that records the CONNECT request, then answers 200."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_holder.append(srv.getsockname()[1])

    def serve():
        conn, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        request_holder.append(req)
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()


def test_proxy_connect_sends_credentials_from_env(monkeypatch):
    """SSH fix (2026-11-06): the tunnel proxy (3130) requires the
    same credentials as the egress proxy (3128). proxy_connect must
    send Proxy-Authorization from the env proxy URL."""
    import base64
    ports, requests = [], []
    _capturing_proxy(ports, requests)
    monkeypatch.setattr(android_bridge, "TUNNEL_PROXY_HOST", "127.0.0.1")
    monkeypatch.setattr(android_bridge, "TUNNEL_PROXY_PORT", ports[0])
    monkeypatch.setenv("HTTPS_PROXY",
                       "http://testuser:testpass@127.0.0.1:3128")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    sock = android_bridge.proxy_connect("192.0.2.1", 8022, timeout=10)
    sock.close()
    assert len(requests) == 1
    expected = base64.b64encode(b"testuser:testpass").decode("ascii")
    assert f"Proxy-Authorization: Basic {expected}".encode() in requests[0]


def test_proxy_connect_sends_no_auth_without_credentials(monkeypatch):
    ports, requests = [], []
    _capturing_proxy(ports, requests)
    monkeypatch.setattr(android_bridge, "TUNNEL_PROXY_HOST", "127.0.0.1")
    monkeypatch.setattr(android_bridge, "TUNNEL_PROXY_PORT", ports[0])
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    sock = android_bridge.proxy_connect("192.0.2.1", 8022, timeout=10)
    sock.close()
    assert len(requests) == 1
    assert b"Proxy-Authorization" not in requests[0]


def test_shot_out_path_matches_format():
    """Naming rule (2026-11-06): the extension matches the content."""
    assert android_bridge._shot_out_path("/tmp/android-screen.png", "jpeg") == "/tmp/android-screen.jpg"
    assert android_bridge._shot_out_path("/tmp/android-screen.png", "png") == "/tmp/android-screen.png"
    assert android_bridge._shot_out_path("logs/android-20261106-000000-shot", "jpeg").endswith(".jpg")
    assert android_bridge._shot_out_path("logs/android-20261106-000000-shot.jpg", "png").endswith(".png")
