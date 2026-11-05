"""Android bridge for the Gemini Computer Use trial (Phase 4).

Three layers, following the GPT Phase 4 design (CONS-20261105-008):
  1. Judgement (Gemini) — decides what to do from a screenshot.
  2. Execution (adapters in this file) — carries out validated actions.
  3. Permission management (Muse) — this bridge never executes an action
     unless it was explicitly invoked with --allow-actions and an explicit
     action on the command line. The default mode is read-only.

Transports (in priority order):
  - termux-rish (primary): SSH into Termux on the phone over the Tailscale
    tunnel proxy, then run commands through rish (Shizuku shell). No adb
    binary is needed on this VM, and SSH is the path whose reachability was
    already proven (2026-11-01). Needs on the phone: Tailscale connected,
    Termux sshd running, Shizuku started, rish installed in Termux.
  - adb (secondary): local adb binary + a local TCP forwarder (adb_tunnel.py)
    that relays 127.0.0.1:5555 through the tunnel proxy to the phone's
    wireless-debugging port. Needs on the phone: wireless debugging enabled
    and paired. Needs on this VM: `apt install adb` (not yet installed).
  - app (bridge app): the phone's Muse Bridge app (android-app/) serves
    HTTP on its Tailscale address; no ADB, USB, or wireless debugging
    needed. Needs on the phone: the APK installed, the accessibility
    service enabled, the bridge started. The runtime token comes from
    ANDROID_APP_TOKEN (from the environment; when stored, only in .env
    chmod 600, git-ignored — see README decision 29).

The phone is reached ONLY over the private Tailscale network. The ADB port
is never exposed to the internet.

Usage:
  python3 android_bridge.py check
      Report transport status (no phone action, no changes).
  python3 android_bridge.py screenshot --out /tmp/screen.png
      Capture one screenshot (read-only) and save it.
  python3 android_bridge.py state
      Print phone state (screen size, current app if available).
  python3 android_bridge.py tap --x 500 --y 800 --allow-actions
      GATED: a tap only runs with --allow-actions and explicit coordinates.
  python3 android_bridge.py _proxy <host> <port>
      Hidden ProxyCommand helper for ssh: CONNECT through the tunnel proxy
      and relay stdin/stdout. Env overrides for testing:
      TUNNEL_PROXY_HOST (default hatch-egress-proxy), TUNNEL_PROXY_PORT
      (default 3130).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading

# No hardcoded phone address: the phone's Tailscale IP and Termux user
# are device-specific and must come from the environment (ANDROID_PHONE_IP,
# ANDROID_TERMUX_USER, or ANDROID_APP_URL). They used to be hardcoded
# defaults; the defaults were removed 2026-11-06 when the code went
# public on GitHub, so no device address ships in the source. The live
# values live in .env (chmod 600, git-ignored), like the bridge token.
PHONE_IP = os.environ.get("ANDROID_PHONE_IP", "")
TERMUX_USER = os.environ.get("ANDROID_TERMUX_USER", "")
TERMUX_SSH_PORT = int(os.environ.get("ANDROID_TERMUX_SSH_PORT", "8022"))
ADB_PORT = int(os.environ.get("ANDROID_ADB_PORT", "5555"))
ADB_FORWARD_PORT = int(os.environ.get("ANDROID_ADB_FORWARD_PORT", "5555"))
TUNNEL_PROXY_HOST = os.environ.get("TUNNEL_PROXY_HOST", "hatch-egress-proxy")
TUNNEL_PROXY_PORT = int(os.environ.get("TUNNEL_PROXY_PORT", "3130"))
# Bridge app transport (Phase 4, app): the phone's Muse Bridge app serves
# HTTP on its Tailscale address. The token is read from the environment at
# run time; when stored, only in .env (chmod 600, git-ignored) — see
# README decision 29. It is never written to a file or printed elsewhere.
# Coordinates are screen pixels; callers convert the model's 0-999
# coordinates using the width/height from get_current_state().
APP_URL = os.environ.get(
    "ANDROID_APP_URL", f"http://{PHONE_IP}:8080" if PHONE_IP else "").rstrip("/")
APP_TOKEN = os.environ.get("ANDROID_APP_TOKEN", "")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------
# Tunnel proxy (ProxyCommand helper)
# --------------------------------------------------------------------------

def proxy_connect(host: str, port: int, timeout: float = 30) -> object:
    """Open a CONNECT tunnel to host:port through the Tailscale proxy.

    Returns a connected socket. Raises on failure. Sends no credentials.
    """
    sock = socket.create_connection((TUNNEL_PROXY_HOST, TUNNEL_PROXY_PORT), timeout=timeout)
    req = (
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Proxy-Connection: keep-alive\r\n\r\n"
    ).encode()
    sock.sendall(req)
    sock.settimeout(timeout)
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("tunnel proxy closed the connection (empty reply)")
        resp += chunk
    status = resp.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
    if " 200 " not in status:
        raise ConnectionError(f"tunnel proxy refused: {status}")
    return sock


def _relay_fd(src_fd, dst_fd):
    """Copy bytes from one file descriptor to another (works for sockets
    and stdio alike). Stops silently on EOF or error."""
    try:
        while True:
            data = os.read(src_fd, 65536)
            if not data:
                break
            view = memoryview(data)
            while view:
                n = os.write(dst_fd, view)
                view = view[n:]
    except OSError:
        pass


def cmd_proxy(host: str, port: int) -> int:
    """ProxyCommand entry point: relay stdio through the tunnel to host:port."""
    try:
        upstream = proxy_connect(host, port)
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        print(f"proxy_connect failed: {exc}", file=sys.stderr)
        return 1
    upstream.settimeout(None)
    sys.stdout.flush()
    t1 = threading.Thread(target=_relay_fd, args=(0, upstream.fileno()), daemon=True)
    t2 = threading.Thread(target=_relay_fd, args=(upstream.fileno(), 1), daemon=True)
    t1.start()
    t2.start()
    t1.join()  # stdin EOF: stop sending to the tunnel
    try:
        upstream.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    t2.join()
    return 0


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

class TransportError(RuntimeError):
    pass


def ssh_base_cmd() -> list:
    """ssh invocation for Termux over the tunnel proxy (no interactive auth)."""
    if not PHONE_IP or not TERMUX_USER:
        raise TransportError(
            "ANDROID_PHONE_IP and ANDROID_TERMUX_USER are not set; put them "
            "in .env (see .env.example) — no phone address is hardcoded")
    here = os.path.abspath(__file__)
    proxy_cmd = f"{sys.executable} {here} _proxy %h %p"
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={os.environ.get('ANDROID_SSH_TIMEOUT', '20')}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ProxyCommand={proxy_cmd}",
        "-p", str(TERMUX_SSH_PORT),
        "-i", os.path.expanduser("~/.ssh/id_ed25519"),
        f"{TERMUX_USER}@{PHONE_IP}",
    ]


def run_ssh(remote_cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run one command in Termux over SSH. Returns the completed process."""
    cmd = ssh_base_cmd() + [remote_cmd]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TransportError(f"ssh to Termux timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise TransportError("ssh binary not found on this VM") from exc


def check_ssh() -> dict:
    """Read-only reachability check: run `true` in Termux over SSH."""
    try:
        result = run_ssh("true", timeout=30)
    except TransportError as exc:
        return {"ok": False, "detail": str(exc)}
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip()
        return {"ok": False, "detail": f"ssh exited {result.returncode}: {err[:200]}"}
    return {"ok": True, "detail": "ssh to Termux works"}


def check_adb() -> dict:
    """Report ADB transport prerequisites (no install, no phone action)."""
    import shutil

    adb = shutil.which("adb")
    if not adb:
        return {
            "ok": False,
            "detail": "adb is not installed on this VM (apt package 'adb' 1:34.0.4-1build3 is available; not installed yet)",
        }
    return {"ok": None, "detail": "adb binary present; phone wireless-debugging state still unknown"}


def _tunnel_proxy_url() -> str | None:
    """Tunnel-proxy URL for reaching the tailnet.

    The phone is reachable only through the tunnel proxy: the same proxy
    as the runtime egress proxy, only the port differs (3130 instead of
    3128). Credentials, when present in the env proxy URL, are preserved.
    """
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        url = os.environ.get(key, "")
        if url:
            return url.replace(":3128", ":3130")
    return None


def _app_opener():
    """urllib opener that routes phone requests through the tunnel proxy.

    The tunnel proxy is only needed for the real phone (a Tailscale IP);
    local addresses (the unit-test mock server) go direct.
    """
    import urllib.parse
    import urllib.request
    host = urllib.parse.urlparse(APP_URL).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    proxy_url = _tunnel_proxy_url()
    if proxy_url:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {"http": proxy_url, "https": proxy_url}))
    return urllib.request.build_opener()


def check_app() -> dict:
    """Report bridge-app transport prerequisites (one read-only HTTP probe)."""
    import urllib.error
    import urllib.request

    if not APP_TOKEN:
        return {
            "ok": False,
            "detail": f"ANDROID_APP_TOKEN is not set (app URL is {APP_URL}); "
                      "enter the token from the phone's Muse Bridge app",
        }
    try:
        req = urllib.request.Request(
            f"{APP_URL}/health", headers={"X-Bridge-Token": APP_TOKEN})
        with _app_opener().open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": f"app returned HTTP {exc.code} (wrong token?)"}
    except Exception as exc:  # noqa: BLE001 - report the probe failure
        return {"ok": False, "detail": f"cannot reach the app at {APP_URL}: {exc}"}
    if data.get("ok") and data.get("accessibility"):
        return {"ok": True, "detail": "app reachable, accessibility service enabled"}
    if data.get("ok"):
        return {"ok": None,
                "detail": "app reachable but the accessibility service is not enabled on the phone"}
    return {"ok": False, "detail": f"app health check failed: {data}"}


# --------------------------------------------------------------------------
# Android adapter (common interface for Playwright / Android execution)
# --------------------------------------------------------------------------

class AndroidAdapter:
    """Common execution interface shared by the Playwright and Android adapters.

    capture_screen / get_current_state are read-only. click / swipe /
    type_text / press_key are gated: they raise unless actions_enabled is
    True, and the CLI only enables them with --allow-actions plus an
    explicit action on the command line.
    """

    def __init__(self, transport: str = "termux-rish", actions_enabled: bool = False):
        if transport not in ("termux-rish", "adb", "app"):
            raise ValueError(f"unknown transport: {transport}")
        self.transport = transport
        self.actions_enabled = actions_enabled

    # -- read-only -----------------------------------------------------------
    def capture_screen(self, scale: float = 0.5, format: str = "jpeg",
                       quality: int = 80) -> bytes:
        """Return a screenshot of the phone screen.

        App transport (v1.2): half-scale JPEG by default — a full-res
        PNG of a photo wallpaper is ~2 MB and costs ~2 s of transfer
        over the tunnel, while 540x1170 JPEG is ~0.3 MB and still
        readable. Pass scale=1.0, format="png" for full quality.
        The rish/adb transports always return a full-res PNG and
        ignore these parameters.
        """
        if self.transport == "termux-rish":
            return self._capture_screen_rish()
        if self.transport == "app":
            return self._capture_screen_app(scale, format, quality)
        return self._capture_screen_adb()

    def get_current_state(self) -> dict:
        """Return screen size and, when available, the foreground app."""
        if self.transport == "termux-rish":
            return self._state_rish()
        if self.transport == "app":
            return self._state_app()
        return self._state_adb()

    # -- gated actions -------------------------------------------------------
    def _require_actions(self, what: str):
        if not self.actions_enabled:
            raise PermissionError(
                f"action refused: {what} needs --allow-actions and an explicit "
                "user-approved action; this bridge defaults to read-only"
            )

    def click(self, x: int, y: int) -> dict:
        self._require_actions(f"click({x}, {y})")
        if self.transport == "app":
            return self._app_post("/click", {"x": x, "y": y})
        return self._run_input(f"input tap {x} {y}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> dict:
        self._require_actions(f"swipe({x1}, {y1} -> {x2}, {y2})")
        if self.transport == "app":
            return self._app_post("/swipe", {
                "x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms})
        return self._run_input(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

    def type_text(self, text: str) -> dict:
        self._require_actions(f"type_text({text!r})")
        if self.transport == "app":
            return self._app_post("/type_text", {"text": text})
        escaped = text.replace(" ", "%s").replace("'", "")
        return self._run_input(f"input text {escaped}")

    def press_key(self, key: str) -> dict:
        """key is a keyevent name or code, e.g. 'BACK', 'HOME', '4', '3'."""
        self._require_actions(f"press_key({key})")
        if self.transport == "app":
            app_key = {"3": "HOME", "4": "BACK"}.get(key, key)
            return self._app_post("/press_key", {"key": app_key})
        return self._run_input(f"input keyevent {key}")

    def click_node(self, node_id: str, tree_hash: int) -> dict:
        """Click a node by id (Phase 2, app transport only)."""
        self._require_actions(f"click_node({node_id})")
        if self.transport != "app":
            raise TransportError("click_node is only available on the app transport")
        return self._app_post("/click_node", {"node_id": node_id, "tree_hash": tree_hash})

    def type_node(self, node_id: str, tree_hash: int, text: str) -> dict:
        """Set text on an editable node by id (Phase 2, app transport only)."""
        self._require_actions(f"type_node({node_id}, {text!r})")
        if self.transport != "app":
            raise TransportError("type_node is only available on the app transport")
        return self._app_post("/type_node",
                              {"node_id": node_id, "tree_hash": tree_hash, "text": text})

    def batch(self, batch_id: str, tree_hash: int, actions: list) -> dict:
        """Run several actions in one request (Phase 2, app transport only).

        actions is a list of dicts, each with action_id, type, and the
        fields its type needs. A re-sent batch_id returns the cached
        result without re-executing (server-side idempotency).
        """
        self._require_actions(f"batch({batch_id}, {len(actions)} actions)")
        if self.transport != "app":
            raise TransportError("batch is only available on the app transport")
        return self._app_post("/batch", {
            "batch_id": batch_id, "tree_hash": tree_hash, "actions": actions})

    def notify(self, title: str, text: str) -> dict:
        """Ask the bridge app (v1.3+) to post an update notification.

        Not gated by --allow-actions: it only shows a notification
        (tap opens Downloads). Returns ok:false when notifications
        are not permitted on the phone.
        """
        if self.transport != "app":
            raise TransportError("notify is only available on the app transport")
        return self._app_post("/notify", {"title": title, "text": text})

    def events(self, since: int = 0, limit: int = 100) -> dict:
        """Return the bridge's recorded accessibility-event history
        (Phase 3, v1.6). Read-only (no --allow-actions gate): it only
        reports what happened on the phone. App transport only.

        `since` is the last seq the caller has seen; the server
        returns events after it. `dropped` true means the buffer
        evicted events the caller never saw — refetch /state.
        """
        if self.transport != "app":
            raise TransportError("events is only available on the app transport")
        import urllib.parse
        params = urllib.parse.urlencode({"since": since, "limit": limit})
        result = self._app_request("GET", f"/events?{params}")
        result["transport"] = "app"
        return result

    # -- termux-rish transport ----------------------------------------------
    def _rish(self, inner: str, timeout: int = 60) -> subprocess.CompletedProcess:
        result = run_ssh(f"rish -c '{inner}'", timeout=timeout)
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", "replace").strip()
            raise TransportError(
                f"rish failed (exit {result.returncode}): {err[:300]} "
                "(is Shizuku started on the phone?)"
            )
        return result

    def _capture_screen_rish(self) -> bytes:
        tmp = "/data/data/com.termux/files/home/.muse-screen.png"
        self._rish(f"screencap -p {tmp}")
        result = run_ssh(f"cat {tmp} && rm -f {tmp}", timeout=60)
        data = result.stdout
        if not data.startswith(PNG_MAGIC):
            raise TransportError(
                f"screenshot did not come back as PNG ({len(data)} bytes, "
                f"starts with {data[:16]!r}); is Shizuku/rish working?"
            )
        return data

    def _state_rish(self) -> dict:
        state: dict = {"transport": "termux-rish"}
        try:
            size = self._rish("wm size").stdout.decode("utf-8", "replace").strip()
            state["screen_size"] = size
        except TransportError as exc:
            state["screen_size_error"] = str(exc)[:200]
        try:
            app = self._rish(
                "dumpsys activity activities | grep -m1 mResumedActivity"
            ).stdout.decode("utf-8", "replace").strip()
            state["foreground_app"] = app
        except TransportError as exc:
            state["foreground_app_error"] = str(exc)[:200]
        return state

    # -- bridge-app transport ----------------------------------------------
    def _app_request(self, method: str, path: str, payload: dict | None = None,
                     expect_png: bool = False, expect_image: bool = False):
        """One authenticated HTTP call to the phone's Muse Bridge app.

        The token comes from the ANDROID_APP_TOKEN env var only and is
        never logged. Raises TransportError on any failure.
        """
        import urllib.error
        import urllib.request

        if not APP_URL:
            raise TransportError(
                "ANDROID_APP_URL or ANDROID_PHONE_IP is not set; put it in "
                ".env (see .env.example) — no phone address is hardcoded")
        if not APP_TOKEN:
            raise TransportError(
                "ANDROID_APP_TOKEN is not set; copy it from the phone's "
                "Muse Bridge app (it is shown in the app UI)")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"{APP_URL}{path}", data=body, method=method,
            headers={"X-Bridge-Token": APP_TOKEN,
                     "Content-Type": "application/json"})
        try:
            with _app_opener().open(req, timeout=30) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - keep the original status
                detail = ""
            # A-1 (v1.9): the bridge now rejects bad input with its own
            # statuses; the hint must not blame the token for those.
            if exc.code == 401:
                hint = "wrong token, or the token changed on the phone"
            elif exc.code == 503:
                hint = ("accessibility service not enabled, or the bridge "
                        "is overloaded")
            elif exc.code in (400, 408, 413, 431):
                hint = ("request rejected by the bridge input validation "
                        "(bad request, timeout, or too large)")
            else:
                hint = "unexpected bridge error"
            raise TransportError(
                f"app returned HTTP {exc.code} for {method} {path}: {detail} "
                f"({hint})") from exc
        except Exception as exc:  # noqa: BLE001 - report the failure
            raise TransportError(
                f"cannot reach the app at {APP_URL}: {exc}") from exc
        if expect_png:
            if not data.startswith(PNG_MAGIC):
                raise TransportError(
                    f"app screenshot did not come back as PNG ({len(data)} bytes)")
            return data
        if expect_image:
            # v1.2: /capture_screen may answer PNG or JPEG.
            if not (data.startswith(PNG_MAGIC)
                    or data.startswith(b"\xff\xd8\xff")):
                raise TransportError(
                    f"app screenshot did not come back as an image "
                    f"({len(data)} bytes)")
            return data
        return json.loads(data.decode("utf-8"))

    def _capture_screen_app(self, scale: float = 0.5, format: str = "jpeg",
                            quality: int = 80) -> bytes:
        import urllib.parse
        params = urllib.parse.urlencode(
            {"scale": scale, "format": format, "quality": quality})
        return self._app_request(
            "GET", f"/capture_screen?{params}", expect_image=True)

    def _state_app(self) -> dict:
        state = self._app_request("GET", "/state")
        state["transport"] = "app"
        return state

    def _app_post(self, path: str, payload: dict) -> dict:
        result = self._app_request("POST", path, payload)
        result["transport"] = "app"
        return result

    def _run_input(self, input_cmd: str) -> dict:
        if self.transport == "termux-rish":
            self._rish(input_cmd)
            return {"ok": True, "transport": "termux-rish", "command": input_cmd}
        import shutil

        adb = shutil.which("adb")
        if not adb:
            raise TransportError("adb is not installed on this VM")
        target = f"127.0.0.1:{ADB_FORWARD_PORT}"
        result = subprocess.run(
            [adb, "-s", target, "shell", input_cmd],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            raise TransportError(
                f"adb shell failed (exit {result.returncode}): "
                f"{result.stderr.decode('utf-8', 'replace')[:300]}"
            )
        return {"ok": True, "transport": "adb", "command": input_cmd}

    # -- adb transport -------------------------------------------------------
    def _capture_screen_adb(self) -> bytes:
        import shutil

        adb = shutil.which("adb")
        if not adb:
            raise TransportError("adb is not installed on this VM")
        target = f"127.0.0.1:{ADB_FORWARD_PORT}"
        result = subprocess.run(
            [adb, "-s", target, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=60,
        )
        data = result.stdout
        if not data.startswith(PNG_MAGIC):
            raise TransportError(
                f"adb screencap did not return PNG ({len(data)} bytes); "
                "is the phone connected (adb connect 127.0.0.1:5555)?"
            )
        return data

    def _state_adb(self) -> dict:
        import shutil

        adb = shutil.which("adb")
        if not adb:
            raise TransportError("adb is not installed on this VM")
        target = f"127.0.0.1:{ADB_FORWARD_PORT}"
        state: dict = {"transport": "adb"}
        size = subprocess.run(
            [adb, "-s", target, "shell", "wm", "size"],
            capture_output=True, timeout=30,
        )
        state["screen_size"] = size.stdout.decode("utf-8", "replace").strip()
        return state


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _shot_out_path(out: str, format: str) -> str:
    """Return the screenshot output path with the correct extension.

    Naming rule (2026-11-06): the extension always matches the
    content — JPEG bytes get .jpg, PNG bytes get .png — so a file
    is never a JPEG wearing a .png name (the v1.2 default is JPEG).
    """
    import os
    ext = ".jpg" if format == "jpeg" else ".png"
    base, _ = os.path.splitext(out)
    return base + ext


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Android bridge (Phase 4, read-only by default)")
    parser.add_argument("command", choices=[
        "check", "screenshot", "state", "tap", "swipe", "type", "key", "notify",
        "click-node", "type-node", "batch", "events", "_proxy",
    ])
    parser.add_argument("--transport", default=os.environ.get("ANDROID_TRANSPORT", "termux-rish"),
                        choices=["termux-rish", "adb", "app"])
    parser.add_argument("--out", default="/tmp/android-screen.png")
    parser.add_argument("--x", type=int, default=0)
    parser.add_argument("--y", type=int, default=0)
    parser.add_argument("--x2", type=int, default=0)
    parser.add_argument("--y2", type=int, default=0)
    parser.add_argument("--text", default="")
    parser.add_argument("--title", default="Muse Bridge の更新")
    parser.add_argument("--key", default="")
    parser.add_argument("--node-id", default="")
    parser.add_argument("--tree-hash", type=int, default=0)
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--actions", default="")
    parser.add_argument("--since", type=int, default=0,
                        help="Events after this seq (v1.6; default 0 = all buffered)")
    parser.add_argument("--limit", type=int, default=100,
                        help="Max events to return (v1.6; default 100, max 200)")
    parser.add_argument("--scale", type=float, default=0.5,
                        help="App-transport screenshot scale 0.1-1.0 (v1.2; default 0.5)")
    parser.add_argument("--format", default="jpeg", choices=["jpeg", "png"],
                        help="App-transport screenshot format (v1.2; default jpeg)")
    parser.add_argument("--quality", type=int, default=80,
                        help="JPEG quality 10-100 (v1.2; default 80)")
    parser.add_argument("--allow-actions", action="store_true",
                        help="Enable gated actions. Only use with an explicit user-approved action.")
    parser.add_argument("proxy_args", nargs="*", help="host and port for _proxy")
    args = parser.parse_args(argv)

    if args.command == "_proxy":
        if len(args.proxy_args) != 2:
            print("usage: android_bridge.py _proxy <host> <port>", file=sys.stderr)
            return 2
        return cmd_proxy(args.proxy_args[0], int(args.proxy_args[1]))

    if args.command == "check":
        ssh = check_ssh()
        adb = check_adb()
        app = check_app()
        print(json.dumps({
            "phone_ip": PHONE_IP,
            "termux_ssh_8022": ssh,
            "adb_5555": adb,
            "bridge_app": app,
            "note": "ADB port probe needs the tunnel too; run screenshot with --transport adb to test it.",
        }, indent=2, ensure_ascii=False))
        return 0 if (ssh["ok"] or app["ok"]) else 1

    adapter = AndroidAdapter(transport=args.transport, actions_enabled=args.allow_actions)
    try:
        if args.command == "screenshot":
            data = adapter.capture_screen(
                scale=args.scale, format=args.format, quality=args.quality)
            out_path = _shot_out_path(args.out, args.format)
            with open(out_path, "wb") as f:
                f.write(data)
            print(f"saved {out_path} ({len(data)} bytes, sha256:{_sha256(data)})")
            return 0
        if args.command == "state":
            print(json.dumps(adapter.get_current_state(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "tap":
            print(json.dumps(adapter.click(args.x, args.y), ensure_ascii=False))
            return 0
        if args.command == "swipe":
            print(json.dumps(adapter.swipe(args.x, args.y, args.x2, args.y2), ensure_ascii=False))
            return 0
        if args.command == "type":
            print(json.dumps(adapter.type_text(args.text), ensure_ascii=False))
            return 0
        if args.command == "key":
            print(json.dumps(adapter.press_key(args.key), ensure_ascii=False))
            return 0
        if args.command == "notify":
            print(json.dumps(adapter.notify(args.title, args.text), ensure_ascii=False))
            return 0
        if args.command == "click-node":
            print(json.dumps(adapter.click_node(args.node_id, args.tree_hash),
                             ensure_ascii=False))
            return 0
        if args.command == "type-node":
            print(json.dumps(adapter.type_node(args.node_id, args.tree_hash, args.text),
                             ensure_ascii=False))
            return 0
        if args.command == "batch":
            actions = json.loads(args.actions) if args.actions else []
            print(json.dumps(adapter.batch(args.batch_id, args.tree_hash, actions),
                             ensure_ascii=False))
            return 0
        if args.command == "events":
            print(json.dumps(adapter.events(since=args.since, limit=args.limit),
                             indent=2, ensure_ascii=False))
            return 0
    except PermissionError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    except TransportError as exc:
        print(f"TRANSPORT ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
