"""Local TCP forwarder for the ADB transport (Phase 4).

Listens on 127.0.0.1:5555 and forwards each connection through the Tailscale
tunnel proxy (port 3130) to the phone's wireless-debugging port, so that
`adb connect 127.0.0.1:5555` works even though adb itself cannot use a proxy.
The phone is reached ONLY over the private Tailscale network; nothing here
is exposed to the internet. Sends no credentials and logs nothing.

Usage:
  python3 adb_tunnel.py            # listen 127.0.0.1:5555 -> phone:5555
  ANDROID_PHONE_IP=... ANDROID_ADB_PORT=... python3 adb_tunnel.py

Stop with Ctrl-C. Start it only for an ADB session and stop it afterwards,
like the CONNECT relay in connect_relay.py.
"""
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from android_bridge import proxy_connect  # noqa: E402

LISTEN_PORT = int(os.environ.get("ANDROID_ADB_FORWARD_PORT", "5555"))
PHONE_IP = os.environ.get("ANDROID_PHONE_IP", "100.111.74.42")
ADB_PORT = int(os.environ.get("ANDROID_ADB_PORT", "5555"))


def relay(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(conn):
    upstream = None
    try:
        upstream = proxy_connect(PHONE_IP, ADB_PORT)
        upstream.settimeout(None)
        conn.settimeout(None)
        t1 = threading.Thread(target=relay, args=(conn, upstream), daemon=True)
        t2 = threading.Thread(target=relay, args=(upstream, conn), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception as exc:  # noqa: BLE001 - report per-connection, keep serving
        print(f"adb tunnel connection failed: {exc}", file=sys.stderr, flush=True)
    finally:
        try:
            conn.close()
        except OSError:
            pass
        if upstream is not None:
            try:
                upstream.close()
            except OSError:
                pass


srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", LISTEN_PORT))
srv.listen(16)
print(f"adb tunnel listening on 127.0.0.1:{LISTEN_PORT} -> {PHONE_IP}:{ADB_PORT}",
      flush=True)
while True:
    conn, _ = srv.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
