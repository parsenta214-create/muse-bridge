"""Android bridge for the Gemini Computer Use trial (Phase 4).

Three layers, following the GPT Phase 4 design (CONS-20261105-008):
  1. Judgement (Gemini) — decides what to do from a screenshot.
  2. Execution (adapters in this file) — carries out validated actions.
  3. Permission management (Muse) — this bridge never executes an action
     unless it was explicitly invoked with --allow-actions and an explicit
     action on the command line. The default mode is read-only.

Transport (decision 60): the phone's Muse Bridge app (android-app/)
is the ONLY route. It serves HTTP on its Tailscale address; no ADB,
USB, or wireless debugging is needed. Needs on the phone: the APK
installed, the accessibility service enabled, the bridge started. The
runtime token comes from ANDROID_APP_TOKEN (from the environment; when
stored, only in .env chmod 600, git-ignored — see README decision 29).
The rish and adb transports were deleted (decision 60): rish does not
exist on the phone and adb was never verified (no adb binary on this
VM, wireless debugging unavailable), so neither was a real fallback.
If the accessibility service breaks, no screenshot/tap path remains —
that is a documented design constraint, not an oversight.

The phone is reached ONLY over the private Tailscale network. The
Termux SSH channel (run_ssh / check_ssh) is separate infrastructure
for pushing APKs and running Termux commands — it is not a screen
transport.

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
import contextlib
import errno
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error

import bridge_protocol  # Local sibling: protocol error codes (Step 2 wiring).
import retry  # Local sibling (retry.py): backoff math only; the bridge
# classifier below is its own, because the bridge retries a different
# set of failures than the Gemini API.

# No hardcoded phone address: the phone's Tailscale IP and Termux user
# are device-specific and must come from the environment (ANDROID_PHONE_IP,
# ANDROID_TERMUX_USER, or ANDROID_APP_URL). They used to be hardcoded
# defaults; the defaults were removed 2026-11-06 when the code went
# public on GitHub, so no device address ships in the source. The live
# values live in .env (chmod 600, git-ignored), like the bridge token.
PHONE_IP = os.environ.get("ANDROID_PHONE_IP", "")
TERMUX_USER = os.environ.get("ANDROID_TERMUX_USER", "")
TERMUX_SSH_PORT = int(os.environ.get("ANDROID_TERMUX_SSH_PORT", "8022"))
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

def _proxy_auth_header() -> str | None:
    """Proxy-Authorization value from the env proxy URL, or None.

    The tunnel proxy (port 3130) requires the same credentials as the
    runtime egress proxy (port 3128); the bridge HTTP path already
    sends them (urllib reads them from the env proxy URL), but
    proxy_connect() did not — that is why SSH broke while bridge
    HTTP kept working. The credentials live only in the environment;
    this function never writes them to a file or prints them.
    """
    import base64
    import urllib.parse
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        url = os.environ.get(key, "")
        if not url:
            continue
        parsed = urllib.parse.urlparse(url)
        user = parsed.username
        if user is None:
            return None
        # urlparse already percent-decodes username/password.
        password = parsed.password or ""
        token = base64.b64encode(
            f"{user}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"
    return None


def proxy_connect(host: str, port: int, timeout: float = 30) -> object:
    """Open a CONNECT tunnel to host:port through the Tailscale proxy.

    Returns a connected socket. Raises on failure. Sends the proxy
    credentials from the env proxy URL when present (the tunnel
    proxy requires them; see _proxy_auth_header).
    """
    sock = socket.create_connection((TUNNEL_PROXY_HOST, TUNNEL_PROXY_PORT), timeout=timeout)
    lines = [
        f"CONNECT {host}:{port} HTTP/1.1",
        f"Host: {host}:{port}",
    ]
    auth = _proxy_auth_header()
    if auth is not None:
        lines.append(f"Proxy-Authorization: {auth}")
    lines.append("Proxy-Connection: keep-alive")
    req = ("\r\n".join(lines) + "\r\n\r\n").encode()
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


class AccessibilityUnavailableError(TransportError):
    """The bridge answered 503 with reason ACCESSIBILITY_DISABLED:
    the AccessibilityService is off on the phone (MINIMAL /
    BANKING SAFE mode). .reason and .required_mode carry the
    server's structured fields. A TransportError subclass, so
    existing `except TransportError` handlers still catch it.
    P0-1 (v2.0)."""
    def __init__(self, message, reason="ACCESSIBILITY_DISABLED",
                 required_mode="FULL_ACCESS"):
        super().__init__(message)
        self.reason = reason
        self.required_mode = required_mode


# --------------------------------------------------------------------------
# Retry policy for the bridge client (client improvement 1).
# --------------------------------------------------------------------------
# Retried: reads (GET — /health, /state, /events, /logs,
# /capture_screen) and POST /batch. Reads are idempotent, and a
# re-sent batch_id is answered from the server cache (A-4) instead
# of executing twice. NOT retried: click, swipe, type_text,
# press_key, click_node, type_node, notify — a second send could
# double-execute the action (or double-post a notification).
# Only transient failures are retried: HTTP 500/502/503/504 and
# transport failures (reset, refused, aborted, timeout). 4xx fail
# fast — retrying a bad request, a wrong token, or a stale snapshot
# cannot help, and the caller must re-plan (409/410/422).
APP_MAX_ATTEMPTS = 3
APP_RETRY_BASE_DELAY_S = 1.0
APP_RETRYABLE_STATUS = {500, 502, 503, 504}


# --------------------------------------------------------------------------
# Timeouts and per-call measurement (client improvement 2, decision 59).
# --------------------------------------------------------------------------
# One named place for client timeouts. The batch timeout is 60 s
# because the server may take up to 45 s total for a 20-action
# batch — the client must never give up before the server answers
# (a re-send would then return "running" instead of a result).
# Note: urllib's timeout is per socket operation (connect/read),
# not a total deadline — a slow-drip response can run longer than
# these values in total. That is a known limitation, not a
# violation of the design goal (the client still waits at least
# this long per operation).
# Env overrides are validated at import: a non-numeric value, a
# value <= 0, or a value > 300 s raises ValueError immediately —
# never silently fall back to the defaults.
def _timeout_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"{name} must be a number of seconds (got {raw!r})") from None
    if not 0 < value <= 300:
        raise ValueError(
            f"{name} must be > 0 and <= 300 seconds (got {value})")
    return value


APP_TIMEOUT_READ_S = _timeout_env("ANDROID_APP_TIMEOUT_READ_S", 30.0)
APP_TIMEOUT_BATCH_S = _timeout_env("ANDROID_APP_TIMEOUT_BATCH_S", 60.0)
APP_TIMEOUT_HEALTH_S = _timeout_env("ANDROID_APP_TIMEOUT_HEALTH_S", 15.0)
APP_SLOW_CALL_S = 10.0


def _is_retryable_bridge_error(exc: BaseException) -> bool:
    """True when a bridge call failed transiently and may be retried."""
    # P0-1 (v2.0): a structured a11y-off 503 is NOT transient —
    # enabling the service is a manual user action, so retrying
    # only burns attempts and backoff time. Bare 503s (overload,
    # no reason field) still retry below.
    if isinstance(exc, AccessibilityUnavailableError):
        return False
    status = retry.extract_status(exc)
    if status is not None:
        return status in APP_RETRYABLE_STATUS
    cause = exc
    while getattr(cause, "__cause__", None) is not None:
        cause = cause.__cause__
    if isinstance(cause, (urllib.error.URLError, ConnectionResetError,
                          BrokenPipeError, TimeoutError, socket.timeout)):
        return True
    if isinstance(cause, OSError) and cause.errno in (
            errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED,
            errno.ETIMEDOUT, errno.EPIPE):
        return True
    return False


def _call_with_bridge_retry(fn, method: str, path: str,
                            max_attempts: int = APP_MAX_ATTEMPTS,
                            stats: dict | None = None):
    """Call fn(), retrying transient bridge failures (max_attempts).

    Returns (result, attempts, total_seconds). When stats is given,
    it is filled with {"attempts", "seconds"} on every exit —
    including failure — so the caller can record the call even
    when it raises. The delay reuses retry.py's full-jitter
    backoff. Each retry is noted on stderr (method + path + status
    or error type only — never the token, and never a request
    body). A single attempt slower than APP_SLOW_CALL_S is also
    noted on stderr (per attempt, not the retry total).
    max_attempts=1 means "run exactly once" — the non-retryable
    calls go through here too, so every call is timed the same way.
    """
    last_error = None
    total = 0.0
    for attempt in range(1, max_attempts + 1):
        start = time.monotonic()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - classify, then re-raise
            last_error = exc
            error = exc
        else:
            error = None
        elapsed = time.monotonic() - start
        total += elapsed
        if elapsed > APP_SLOW_CALL_S:
            print(f"bridge: slow call {method} {path} took {elapsed:.1f}s "
                  f"(attempt {attempt})", file=sys.stderr)
        if stats is not None:
            stats["attempts"] = attempt
            stats["seconds"] = round(total, 3)
        if error is None:
            return result, attempt, total
        if not _is_retryable_bridge_error(error) or attempt == max_attempts:
            raise error
        status = retry.extract_status(error)
        cause = error
        while getattr(cause, "__cause__", None) is not None:
            cause = cause.__cause__
        reason = f"HTTP {status}" if status is not None else type(cause).__name__
        delay = retry.backoff_delay_s(attempt, base_delay_s=APP_RETRY_BASE_DELAY_S)
        print(f"bridge: retry {attempt}/{max_attempts} for "
              f"{method} {path} after {reason} "
              f"(sleep {delay:.1f}s)", file=sys.stderr)
        time.sleep(delay)
    raise last_error  # Unreachable; keeps the control flow explicit.


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


def get_health_detail() -> dict:
    """GET /health, validated by bridge_protocol.parse_health.

    Returns the full parsed health dict (all fields the server
    sent, incl. mode/overall_status/modules/capabilities/
    tailscale_ips on v2.0.0+ servers, and tailscale_ips_bound
    on servers new enough to report it, P1-3) plus "seconds". Raises
    TransportError on transport/auth failure and ProtocolError
    on a malformed body. Old servers simply omit the new keys
    (parse_health treats them as optional); callers must use
    .get() for them. P0-3 (v2.0).
    """
    import urllib.error
    import urllib.request

    if not APP_TOKEN:
        raise TransportError(
            f"ANDROID_APP_TOKEN is not set (app URL is {APP_URL}); "
            "enter the token from the phone's Muse Bridge app")

    def probe():
        req = urllib.request.Request(
            f"{APP_URL}/health", headers={"X-Bridge-Token": APP_TOKEN})
        with _app_opener().open(req, timeout=APP_TIMEOUT_HEALTH_S) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # Decision 59: the probe is timed (seconds, incl. retries) and
    # retried like any other read.
    probe_start = time.monotonic()
    try:
        data, _attempts, _total = _call_with_bridge_retry(probe, "GET", "/health")
    except urllib.error.HTTPError as exc:
        raise TransportError(
            f"app returned HTTP {exc.code} (wrong token?)") from exc
    except Exception as exc:  # noqa: BLE001 - report the probe failure
        raise TransportError(
            f"cannot reach the app at {APP_URL}: {exc}") from exc
    seconds = round(time.monotonic() - probe_start, 3)
    # Step 3: the /health response is validated by the protocol
    # module. A malformed health body is a failed check, not a
    # reachable bridge.
    parsed = bridge_protocol.parse_health(data)
    parsed["seconds"] = seconds
    return parsed


def check_app() -> dict:
    """Report bridge-app transport prerequisites (one read-only HTTP probe).

    The probe is a GET /health, so it retries transient failures
    (same policy as the adapter's reads). P0-3 (v2.0): the return
    now also carries mode/overall_status/modules/capabilities/
    tailscale_ips (None on old servers); P1-3: it also carries
    tailscale_ips_bound (None when the server does not report
    it) so the VM can tell whether the bound IPs have gone
    stale (bound != current). The ok/detail contract
    is unchanged.
    """
    if not APP_TOKEN:
        return {
            "ok": False,
            "detail": f"ANDROID_APP_TOKEN is not set (app URL is {APP_URL}); "
                      "enter the token from the phone's Muse Bridge app",
        }

    probe_start = time.monotonic()
    try:
        data = get_health_detail()
    except TransportError as exc:
        return {"ok": False, "detail": str(exc),
                "seconds": round(time.monotonic() - probe_start, 3)}
    except bridge_protocol.ProtocolError as exc:
        return {"ok": False, "detail": f"app health check failed: {exc}",
                "seconds": round(time.monotonic() - probe_start, 3)}
    seconds = data.get("seconds", round(time.monotonic() - probe_start, 3))
    extra = {
        "mode": data.get("mode"),
        "overall_status": data.get("overall_status"),
        "modules": data.get("modules"),
        "capabilities": data.get("capabilities"),
        "tailscale_ips": data.get("tailscale_ips"),
        "tailscale_ips_bound": data.get("tailscale_ips_bound"),
    }
    if data.get("ok") and data.get("accessibility"):
        return {"ok": True, "detail": "app reachable, accessibility service enabled",
                "notification_listener": data.get("notification_listener"),
                "seconds": seconds, **extra}
    if data.get("ok"):
        return {"ok": None,
                "detail": "app reachable but the accessibility service is not enabled on the phone",
                "notification_listener": data.get("notification_listener"),
                "seconds": seconds, **extra}
    return {"ok": False, "detail": f"app health check failed: {data}",
            "seconds": seconds}


# --------------------------------------------------------------------------
# High-level helper validation (decision 61).
# --------------------------------------------------------------------------

def _validate_wait_args(timeout_s: float, poll_s: float, match: str) -> None:
    """Validate wait_for_text / click_text arguments (fail fast)."""
    if match not in ("exact", "contains"):
        raise ValueError(f"match must be 'exact' or 'contains' (got {match!r})")
    if not 0 < timeout_s <= 60:
        raise ValueError(f"timeout_s must be > 0 and <= 60 (got {timeout_s})")
    if not poll_s >= 1.0:
        raise ValueError(f"poll_s must be >= 1.0 (got {poll_s})")


def _match_text_node(nodes: list, text: str, match: str) -> dict | None:
    """First node whose "text" matches (exact or substring); text only."""
    for node in nodes:
        node_text = node.get("text")
        if match == "exact" and node_text == text:
            return node
        if (match == "contains" and isinstance(node_text, str)
                and text in node_text):
            return node
    return None


def _is_stale_click_error(exc: BaseException) -> bool:
    """True when the server refused a click before acting on it:
    HTTP 409 (stale tree_hash) or the snapshot guard's stale error.
    A re-find by text is safe then — the click never executed."""
    if retry.extract_status(exc) == 409:
        return True
    return "stale snapshot" in str(exc)


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

    def __init__(self, actions_enabled: bool = False):
        self.actions_enabled = actions_enabled
        # A-3 (v1.9): the most recent /state, so node actions can tell
        # whether that snapshot was stable before acting on it.
        self._last_state: dict | None = None
        # Decision 59: the latest logical call's measurement
        # (cleared when a call starts, filled when it finishes).
        self.last_call: dict | None = None
        # Step 4 (v1.2.0): the batch idempotency registry (A-4).
        # It enforces the id rules client-side (conflict, expired
        # id) and records each batch's server outcome.
        self._batch_registry = bridge_protocol.BatchRegistry()

    # -- snapshot guard (A-3) ------------------------------------------------
    def _ensure_fresh_snapshot(self, tree_hash: int) -> int:
        """Refetch /state when the last snapshot was unstable.

        A snapshot with snapshot_stable=false may be mid-change, so a
        node id from it is not trusted: fetch a fresh one. When the
        fresh tree_hash moved, the screen changed — raise instead of
        acting on stale ids (the caller refetches and re-plans).
        Returns the tree_hash to act on (unchanged when it matches).
        """
        last = self._last_state
        if last is None or last.get("snapshot_stable") is not False:
            return tree_hash
        fresh = self._state_app()
        if fresh.get("tree_hash") != tree_hash:
            raise TransportError(
                "stale snapshot: the screen changed after an unstable "
                "/state — refetch state and re-plan the action")
        return fresh.get("tree_hash", tree_hash)

    # -- read-only -----------------------------------------------------------
    def capture_screen(self, scale: float = 0.5, format: str = "jpeg",
                       quality: int = 80) -> bytes:
        """Return a screenshot of the phone screen.

        Half-scale JPEG by default (v1.2) — a full-res PNG of a photo
        wallpaper is ~2 MB and costs ~2 s of transfer over the tunnel,
        while 540x1170 JPEG is ~0.3 MB and still readable. Pass
        scale=1.0, format="png" for full quality.
        """
        return self._capture_screen_app(scale, format, quality)

    def get_current_state(self) -> dict:
        """Return screen size and, when available, the foreground app."""
        return self._state_app()

    # -- gated actions -------------------------------------------------------
    def _require_actions(self, what: str):
        if not self.actions_enabled:
            raise PermissionError(
                f"action refused: {what} needs --allow-actions and an explicit "
                "user-approved action; this bridge defaults to read-only"
            )

    def click(self, x: int, y: int) -> dict:
        self._require_actions(f"click({x}, {y})")
        # Step 3: the request body is built (and validated) by the
        # protocol module — coordinates must be >= 0, body size capped.
        return self._app_post("/click", bridge_protocol.build_click(x, y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> dict:
        self._require_actions(f"swipe({x1}, {y1} -> {x2}, {y2})")
        # Step 3: validated by the protocol module.
        return self._app_post("/swipe", bridge_protocol.build_swipe(
            x1, y1, x2, y2, duration_ms))

    def type_text(self, text: str) -> dict:
        self._require_actions(f"type_text({text!r})")
        # Step 3: validated by the protocol module.
        return self._app_post("/type_text", bridge_protocol.build_type_text(text))

    def press_key(self, key: str) -> dict:
        """key is a keyevent name or code, e.g. 'BACK', 'HOME', '4', '3',
        or a DPAD key ('DPAD_UP', 'DPAD_DOWN', 'DPAD_LEFT', 'DPAD_RIGHT',
        'DPAD_CENTER', decision 66, v1.9.9). DPAD keys move focus, not
        the viewport — in a list, focus movement scrolls as a side
        effect, which depends on the app."""
        self._require_actions(f"press_key({key})")
        app_key = {"3": "HOME", "4": "BACK"}.get(key, key)
        # Step 3: validated by the protocol module (VALID_KEYS now
        # includes the DPAD keys, matching the server's pressKey).
        return self._app_post("/press_key", bridge_protocol.build_press_key(app_key))

    def click_node(self, node_id: str, tree_hash: int) -> dict:
        """Click a node by id (Phase 2)."""
        self._require_actions(f"click_node({node_id})")
        tree_hash = self._ensure_fresh_snapshot(tree_hash)
        # Step 3: validated by the protocol module.
        return self._app_post("/click_node",
                              bridge_protocol.build_click_node(node_id, tree_hash))

    def type_node(self, node_id: str, tree_hash: int, text: str) -> dict:
        """Set text on an editable node by id (Phase 2)."""
        self._require_actions(f"type_node({node_id}, {text!r})")
        tree_hash = self._ensure_fresh_snapshot(tree_hash)
        # Step 3: validated by the protocol module.
        return self._app_post("/type_node",
                              bridge_protocol.build_type_node(node_id, tree_hash, text))

    def scroll_node(self, node_id: str, tree_hash: int,
                    direction: str = "forward") -> dict:
        """Scroll a node by id (decision 67, v1.9.10): ACTION_SCROLL_FORWARD
        or ACTION_SCROLL_BACKWARD on the node or its first scrollable
        ancestor. direction is "forward" or "backward" (anything else
        raises ValueError before any request). Never retried: a second
        scroll would move the list twice."""
        if direction not in ("forward", "backward"):
            raise ValueError(f"unknown direction: {direction!r} "
                             "(expected 'forward' or 'backward')")
        self._require_actions(f"scroll_node({node_id}, {direction})")
        tree_hash = self._ensure_fresh_snapshot(tree_hash)
        return self._app_post("/scroll_node",
                              {"node_id": node_id, "tree_hash": tree_hash,
                               "direction": direction})

    # Decision 68 (v1.9.11): every one-shot node action, by name.
    # The server maps each name to its AccessibilityAction id; only
    # the actions listed in NODE_ACTION_ARGS take args.
    NODE_ACTIONS = frozenset([
        "click", "long_click", "focus", "clear_focus",
        "accessibility_focus", "clear_accessibility_focus",
        "select", "clear_selection", "set_selection",
        "copy", "cut", "paste", "expand", "collapse", "dismiss",
        "show_on_screen", "context_click", "ime_enter",
        "scroll_forward", "scroll_backward",
        "scroll_up", "scroll_down", "scroll_left", "scroll_right",
        "page_up", "page_down", "page_left", "page_right",
        "press_and_hold", "set_progress", "scroll_to_position",
        "set_text",
    ])
    NODE_ACTION_ARGS = {
        "set_selection": ("start", "end"),
        "set_progress": ("value",),
        "scroll_to_position": ("row",),
    }
    # P1-07 (CONS-014): the argument keys each action accepts —
    # mirrors the server's nodeActionArgs. Only these keys are
    # sent; anything else is a ValueError before any request.
    NODE_ACTION_ARG_KEYS = {
        "set_selection": ("start", "end"),
        "set_progress": ("value",),
        "scroll_to_position": ("row",),
        "press_and_hold": ("duration_ms",),
        "set_text": ("text",),
    }

    @staticmethod
    def _require_int_arg(action: str, key: str, value, minimum=None,
                         maximum=None):
        """P1-07 (CONS-014): an integer argument — bools are not
        integers here, and a fractional float (1.8) is rejected
        instead of truncating. An integral float (2.0) is accepted
        and normalised to int, mirroring the server."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{action} needs integer {key}")
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(
                    f"{action} needs integer {key} (got {value!r})")
            value = int(value)
        # P1-07 fix round (CONS-032) + fix round 2 (CONS-033):
        # the 32-bit range check must apply to int values too — a
        # Python int is arbitrary precision, so e.g. 2**40 used to
        # pass the client while the Java server's requireIntArg
        # rejects out-of-int-range values. The range matches the
        # server exactly at both edges: Integer.MIN_VALUE (-2**31)
        # through Integer.MAX_VALUE (2**31 - 1).
        if value < -(2**31) or value > 2**31 - 1:
            raise ValueError(
                f"{action} needs integer {key} (got {value!r})")
        if minimum is not None and value < minimum:
            raise ValueError(f"{action} needs {key} >= {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{action} needs {key} <= {maximum}")
        return value

    def _validate_node_action_args(self, action: str, args: dict) -> None:
        """P1-07 (CONS-014): integer/range checks mirroring the
        server's nodeActionArgs. Raises ValueError before any
        request is sent. Integral floats are normalised to int
        in place."""
        if action == "set_selection":
            for key in ("start", "end"):
                args[key] = self._require_int_arg(
                    action, key, args[key], minimum=0)
        elif action == "set_progress":
            v = args["value"]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError("set_progress needs numeric value")
            if not 0.0 <= v <= 1.0:
                raise ValueError(
                    "set_progress value must be between 0.0 and 1.0")
        elif action == "scroll_to_position":
            args["row"] = self._require_int_arg(
                action, "row", args["row"], minimum=0)
        elif action == "press_and_hold":
            if "duration_ms" in args:
                args["duration_ms"] = self._require_int_arg(
                    action, "duration_ms", args["duration_ms"],
                    minimum=1, maximum=60000)
        elif action == "set_text":
            if not isinstance(args["text"], str):
                raise ValueError("set_text needs text")

    def node_action(self, node_id: str, tree_hash: int, action: str,
                    args: dict | None = None) -> dict:
        """Run any one-shot node action by name (decision 68, v1.9.11):
        POST /node_action. The action runs on the node or the first
        ancestor whose action list contains it (the client sees the
        same lists in /state's "actions"). Only set_selection
        (start, end), set_progress (value), scroll_to_position (row)
        and set_text (text) take args; press_and_hold takes an
        optional duration_ms. Unknown actions and missing required
        args raise ValueError before any request. Gated by
        --allow-actions, never retried."""
        if action not in self.NODE_ACTIONS:
            raise ValueError(f"unknown action: {action!r}")
        # P1-07 (CONS-014): args must be an object when present —
        # the server rejects a non-object with 400 instead of
        # silently treating it as "no args".
        if args is not None and not isinstance(args, dict):
            raise ValueError("args must be an object")
        args = dict(args or {})
        for key in args:
            if key not in self.NODE_ACTION_ARG_KEYS.get(action, ()):
                raise ValueError(f"unknown argument for {action}: {key!r}")
        for required in self.NODE_ACTION_ARGS.get(action, ()):
            if required not in args:
                raise ValueError(f"{action} needs argument: {required}")
        if action == "set_text" and "text" not in args:
            raise ValueError("set_text needs argument: text")
        self._validate_node_action_args(action, args)
        self._require_actions(f"node_action({node_id}, {action})")
        tree_hash = self._ensure_fresh_snapshot(tree_hash)
        return self._app_post("/node_action",
                              {"node_id": node_id, "tree_hash": tree_hash,
                               "action": action, "args": args})

    def launch_app(self, package: str) -> dict:
        """Launch an app by package name (decision 69, v1.9.12):
        POST /launch_app. The bridge asks the PackageManager for
        the package's launcher intent and starts it — no home
        screen hunt, no icon tap. P0-4 (v2.0): the answer is a
        structured launch report, validated by
        bridge_protocol.parse_launch_app — {"ok": true, "status":
        "launched", "foreground_confirmed": true, ...} only when
        the target was actually observed in the foreground;
        {"ok": false, "status": "unconfirmed", "reason": ...}
        when it was not confirmed (or could not be verified with
        a11y off). Callers must read status /
        foreground_confirmed, not ok alone. 422 when the package
        is not installed or has no launcher; 503 when Android
        refuses the start; 403 when the package is on the launch
        deny list (banking / heavy-security apps — System Development Lead
        decision 2026-11-06, README decision 91). The 403 is a
        TransportError and is never retried. Gated by
        --allow-actions, never retried."""
        if not package:
            raise ValueError("package must not be empty")
        self._require_actions(f"launch_app({package})")
        return bridge_protocol.parse_launch_app(
            self._app_post("/launch_app", {"package": package}))

    def repair_termux(self) -> dict:
        """Repair Termux's sshd (Decision 95, v2.5.0, TARMUX_KICKASS
        step 2): POST /repair_termux. The bridge sends only its two
        fixed commands (termux-wake-lock, then sshd) to Termux via
        RUN_COMMAND and re-checks sshd locally; the answer is
        validated by bridge_protocol.parse_repair_termux — {"ok":
        true, "repaired": true, "reason": "REPAIRED"} only when
        sshd answers locally after the attempt. No command, path
        or argument is ever sent from here. 403 when the
        RUN_COMMAND permission is not granted on the phone;
        409 inside the 60 s repair cooldown. Gated by
        --allow-actions, never retried."""
        self._require_actions("repair_termux()")
        return bridge_protocol.parse_repair_termux(
            self._app_post("/repair_termux", {"action": "sshd"}))

    def open(self, uri: str) -> dict:
        """Open a URI with ACTION_VIEW (decision 74, v1.9.15):
        POST /open. A web URL, or an app's deep link when the
        URI's scheme belongs to an installed app — the
        generalisation of launch_app beyond the launcher
        activity. 422 when no app handles the URI; 503 when
        Android refuses the start. Works even when the
        accessibility service is off. Gated by --allow-actions,
        never retried."""
        if not uri:
            raise ValueError("uri must not be empty")
        self._require_actions(f"open({uri})")
        return self._app_post("/open", {"uri": uri})

    def install_apk(self, url=None, path=None) -> dict:
        """Install an APK (decision 75, v1.9.16): POST
        /install_apk. Exactly one of url (https, downloaded on
        the phone by the bridge itself) or path (a file already
        on the phone). The job runs in the background and the
        call returns {"ok": true, "status": "started"} at once;
        PackageInstaller then shows the system confirmation
        dialog and the USER taps Install — a normal app cannot
        install silently. Track it with install_status(). Gated
        by --allow-actions, never retried."""
        if (url is None) == (path is None):
            raise ValueError("give exactly one of url, path")
        if url is not None and not url.startswith("https://"):
            raise ValueError("url must be https")
        self._require_actions(f"install_apk({url or path})")
        body = {"url": url} if url is not None else {"path": path}
        return self._app_post("/install_apk", body)

    def install_status(self) -> dict:
        """Install-apk job state (decision 75, v1.9.16; states
        extended by decision 80, v1.9.21): GET /install_status.
        Read-only: {"ok": true, "status": "idle"|"downloading"|
        "installing"|"committed"|"pending_user_action"|
        "installed"|"failed", "detail": "..."}. "pending_user_action"
        means the install confirmation dialog is on the phone
        waiting for the user's tap (STATUS_PENDING_USER_ACTION)."""
        return self._app_request("GET", "/install_status")

    def get_clipboard(self) -> dict:
        """Read the clipboard (decision 76, v1.9.17): GET
        /clipboard. Read-only, no gate: {"ok": true, "text":
        "..."|null, "truncated": bool}. text is null when the
        clipboard is empty — or when Android 10+ blocks the
        read while the app runs in the background (the live
        test settles which one it is)."""
        return self._app_request("GET", "/clipboard")

    def set_clipboard(self, text: str) -> dict:
        """Write the clipboard (decision 76, v1.9.17): POST
        /clipboard. Empty text clears it. Gated by
        --allow-actions, never retried."""
        if text is None or not isinstance(text, str):
            raise ValueError("text must be a string")
        self._require_actions("set_clipboard(...)")
        return self._app_post("/clipboard", {"text": text})

    def notification_action(self, key: str, action: str, text=None) -> dict:
        """Act on a live notification (decision 77, v1.9.18):
        POST /notification_action. action "open" fires the
        notification's contentIntent (brings the app forward);
        "reply" sends an inline reply through its RemoteInput
        (text required). The lookup is live, so a gone key is
        422. Reply SENDS a message — call it only with the
        user's approval of the exact text. Gated by
        --allow-actions, never retried."""
        if not key:
            raise ValueError("key must not be empty")
        if action not in ("open", "reply"):
            raise ValueError("action must be 'open' or 'reply'")
        if action == "reply" and not text:
            raise ValueError("text is required for reply")
        self._require_actions(f"notification_action({action})")
        body = {"key": key, "action": action}
        if text is not None:
            body["text"] = text
        return self._app_post("/notification_action", body)

    def batch(self, batch_id: str, tree_hash: int, actions: list) -> dict:
        """Run several actions in one request (Phase 2).

        actions is a list of dicts, each with action_id, type, and the
        fields its type needs. A re-sent batch_id returns the cached
        result without re-executing (server-side idempotency, A-4).

        Resend rules (A-5): if the request fails with a transport
        error and the outcome is unknown, re-send the SAME batch_id
        and the SAME payload — the server answers from its cache
        instead of executing twice. Never reuse a batch_id with
        different content (409), and never reuse one after a 410
        (expired: use a NEW batch_id). At most 20 actions per batch.
        """
        self._require_actions(f"batch({batch_id}, {len(actions)} actions)")
        tree_hash = self._ensure_fresh_snapshot(tree_hash)
        # Step 3: the request body is built (and validated) by the
        # protocol module — actions are type-checked, capped at 20,
        # and protocol_version: 1 is explicit. The response is
        # validated by parse_batch_result (ok, batch_id, replayed,
        # results, stopped_at, stop_reason). The "transport" key is
        # added by _app_post before parsing and survives in the
        # returned dict (extra keys are tolerated).
        # Step 4: the registry enforces the A-4 id rules client-side
        # before anything is sent (same id + different content ->
        # STALE_REVISION; expired id -> BATCH_EXPIRED) and records
        # the server's outcome (status incl. partial/running,
        # payload_hash verified against what was sent).
        submission = self._batch_registry.submit(batch_id, tree_hash, actions)
        result = self._app_post("/batch", submission["body"])
        parsed = bridge_protocol.parse_batch_result(result)
        # Step 4 (CONS-020 P0-04): the response must be for the
        # batch that was asked for. batch_id is NOT in the hash, so
        # a result for a different id could otherwise pass the
        # payload_hash check. Never record a mismatched id.
        if parsed["batch_id"] != batch_id:
            raise bridge_protocol.ProtocolError(
                f"batch_id mismatch: asked for {batch_id!r}, "
                f"server answered for {parsed['batch_id']!r}",
                code="BATCH_ID_MISMATCH",
            )
        self._batch_registry.record_result(batch_id, parsed)
        return parsed

    def notify(self, title: str, text: str) -> dict:
        """Ask the bridge app (v1.3+) to post an update notification.

        Not gated by --allow-actions: it only shows a notification
        (tap opens Downloads). Returns ok:false when notifications
        are not permitted on the phone.
        """
        # Step 3: validated by the protocol module.
        return self._app_post("/notify", bridge_protocol.build_notify(title, text))

    def operation_end(self) -> dict:
        """End the current operation session (v2.1): POST
        /operation_end. The bridge dismisses the "操作中"
        notification and posts "操作終了" (when a session was
        active); the response carries was_active.

        Not gated by --allow-actions: it only dismisses a
        notification, it changes nothing on screen. Exactly-once,
        never retried — if the end signal is lost, the server
        auto-ends after 10 s.
        """
        # Step 3: validated by the protocol module.
        return self._app_post("/operation_end",
                              bridge_protocol.build_operation_end())

    @contextlib.contextmanager
    def operation_session(self):
        """Wrap a whole task's screen operations (v2.1): guarantees
        the explicit end signal is sent in a finally, even if the
        task raises. Sends nothing on entry — begin is automatic,
        server-side. The finally swallows end-signal failures
        deliberately: a failed end must never mask the task's own
        exception, and the server auto-end covers it. Wrap a whole
        task, not individual actions."""
        try:
            yield
        finally:
            try:
                self.operation_end()
            except Exception:
                pass  # best-effort; the server auto-end is the fallback

    def logs(self, limit: int = 100) -> dict:
        """Return the bridge's debug log (v1.9.3).

        Pull-based: the phone never pushes logs anywhere. The log
        holds key events only (server start/stop, bind results,
        accept failures, error-status counts, batch outcomes) — never
        request bodies, node text, or tokens. `lines` is the current
        run; `previous_lines` is the previous run's tail.
        """
        return self._app_request("GET", f"/logs?limit={limit}")

    def events(self, since: int = 0, limit: int = 100) -> dict:
        """Return the bridge's recorded accessibility-event history
        (Phase 3, v1.6). Read-only (no --allow-actions gate): it only
        reports what happened on the phone.

        `since` is the last seq the caller has seen; the server
        returns events after it. `dropped` true means the buffer
        evicted events the caller never saw — refetch /state.
        """
        import urllib.parse
        # Step 3: the query string is built (and validated) by the
        # protocol module, and the response is validated by
        # parse_events (each event's shape is checked).
        params = bridge_protocol.build_events_query(since, limit)
        result = bridge_protocol.parse_events(
            self._app_request("GET", f"/events?{params}"))
        result["transport"] = "app"
        return result

    def notifications(self, package=None, since=0, limit=100):
        """Return the phone's notification event stream (decision 63,
        v1.9.7). Read-only (no --allow-actions gate): it only reports
        what the phone received. Each event is posted / updated /
        removed (a removal is a tombstone with its own seq, so a
        client polling with since= sees it). `since` is valid only
        within the phone's current process lifetime (seq resets when
        the bridge process restarts). `dropped` true means the buffer
        evicted events the caller never saw — refetch with since=0.
        The server returns every package; `package` filters
        client-side. Raises TransportError with the server's 503 text
        when the listener is not granted
        ("notification_listener_access_not_granted") or not connected
        ("notification_listener_not_connected").
        """
        import urllib.parse
        params = {"since": since, "limit": limit}
        if package is not None:
            params["package"] = package
        query = urllib.parse.urlencode(params)
        result = self._app_request("GET", f"/notifications?{query}")
        result["transport"] = "app"
        return result

    # -- high-level helpers (decision 61) -----------------------------------
    def wait_for_text(self, text: str, *, timeout_s: float = 15.0,
                      poll_s: float = 1.5, match: str = "exact") -> dict:
        """Wait until a node with the given text appears (read-only).

        Polls GET /state. Returns the node dict only, with
        "tree_hash" added (the tree_hash of the state it was found
        in), so the caller can act on it without re-fetching.
        match is "exact" (node["text"] == text) or "contains"
        (substring); text only, never desc; first match in tree
        order. timeout_s is the overall deadline on a monotonic
        clock (default 15.0, max 60.0); poll_s is the interval
        between polls (default 1.5, min 1.0 — each poll costs
        ~0.75-1 s over the tunnel, so below 1.0 s is pointless).
        Out of range -> ValueError. A single poll can take longer
        than poll_s because GETs retry (decision 58); the deadline
        is what bounds the total — never start a new poll after it.
        On timeout -> TimeoutError (not TransportError). A failed
        GET raises at once (it is not "not ready yet").
        """
        _validate_wait_args(timeout_s, poll_s, match)
        deadline = time.monotonic() + timeout_s
        return self._find_text_node(text, deadline, poll_s, match, timeout_s)

    def click_text(self, text: str, *, timeout_s: float = 15.0,
                   poll_s: float = 1.5, match: str = "exact") -> dict:
        """Wait for a node with the given text, then click it once.

        Gated by _require_actions. Returns the click result dict
        plus "node_id" and "attempts" (the number of click_node
        calls). timeout_s bounds the WAIT only; the click itself is
        a separate call with the 30 s read timeout (worst case
        60 + 30 = 90 s). The click runs exactly once per found node
        and is never retried (decision 58) — if the server refused
        before acting (409, or a stale snapshot), the helper
        re-finds the node by text and clicks the (possibly new) node
        id instead. A click whose outcome is unknown (a transport
        error) is never re-sent: it raises at once.
        """
        self._require_actions(f"click_text({text!r})")
        _validate_wait_args(timeout_s, poll_s, match)
        deadline = time.monotonic() + timeout_s
        attempts = 0
        while True:
            node = self._find_text_node(text, deadline, poll_s, match, timeout_s)
            attempts += 1
            try:
                result = self.click_node(node["id"], node["tree_hash"])
            except TransportError as exc:
                if _is_stale_click_error(exc):
                    # The server refused before acting — re-find.
                    continue
                if retry.extract_status(exc) == 422:
                    # The server refused the click. Fetch a fresh
                    # state: node gone -> re-find; node present ->
                    # walk it + ancestors; if any is clickable and
                    # enabled, the 422 was a race -> re-find; if
                    # none is, the node exists but cannot be clicked.
                    if self._node_clickable_after_422(node["id"]):
                        continue
                    raise TransportError(
                        f"click_text: node with text {text!r} exists but "
                        "cannot be clicked (no clickable, enabled node "
                        "or ancestor)") from exc
                raise
            result["node_id"] = node["id"]
            result["attempts"] = attempts
            return result

    def _find_text_node(self, text: str, deadline: float, poll_s: float,
                        match: str, timeout_s: float) -> dict:
        """Poll /state until a node matches; TimeoutError at the deadline."""
        polls = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"no node with text {text!r} found within {timeout_s}s "
                    f"({polls} polls)")
            state = self.get_current_state()
            polls += 1
            node = _match_text_node(state.get("nodes", []), text, match)
            if node is not None:
                found = dict(node)
                found["tree_hash"] = state.get("tree_hash")
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"no node with text {text!r} found within {timeout_s}s "
                    f"({polls} polls)")
            time.sleep(min(poll_s, remaining))

    def _node_clickable_after_422(self, node_id: str) -> bool:
        """True when a 422 click may be retried (re-find); False when
        the node exists but cannot be clicked.

        Fetches a fresh /state (a failed GET raises at once). The
        node is gone -> True (re-find). Present -> walk it and its
        ancestors; any clickable + enabled node means the 422 was a
        race -> True; none -> False.
        """
        state = self.get_current_state()
        by_id = {n.get("id"): n for n in state.get("nodes", [])}
        node = by_id.get(node_id)
        if node is None:
            return True
        current = node
        while current is not None:
            if current.get("clickable") and current.get("enabled"):
                return True
            parent = current.get("parent")
            current = by_id.get(parent) if parent else None
        return False

    # -- bridge-app transport ----------------------------------------------
    def _app_request(self, method: str, path: str, payload: dict | None = None,
                     expect_png: bool = False, expect_image: bool = False):
        """One authenticated HTTP call to the phone's Muse Bridge app.

        The token comes from the ANDROID_APP_TOKEN env var only and is
        never logged. Raises TransportError on any failure.
        Reads (GET) and POST /batch retry transient failures (see the
        retry policy above the TransportError class); every other POST
        runs exactly once, so an action is never double-executed.
        Decision 59: after every call, self.last_call holds
        {"method", "path", "seconds" (wall incl. retries),
        "attempts", "ok", "status" (None when unavailable), "error"
        (str or None, no confidential info)}; it is cleared when a
        call starts. No timing keys are added to returned dicts.
        """
        retryable = (method == "GET") or (method == "POST" and path == "/batch")
        max_attempts = APP_MAX_ATTEMPTS if retryable else 1
        self.last_call = None
        stats: dict = {}
        try:
            result, attempts, total = _call_with_bridge_retry(
                lambda: self._app_request_once(method, path, payload,
                                               expect_png, expect_image),
                method, path, max_attempts, stats)
        except Exception as exc:  # noqa: BLE001 - record, then re-raise
            status = retry.extract_status(exc)
            self.last_call = {
                "method": method, "path": path,
                "seconds": stats.get("seconds", 0.0),
                "attempts": stats.get("attempts", 1),
                "ok": False, "status": status,
                "error": str(exc)[:200],
            }
            raise
        self.last_call = {
            "method": method, "path": path,
            "seconds": round(total, 3), "attempts": attempts,
            "ok": True, "status": 200, "error": None,
        }
        return result

    def _app_request_once(self, method: str, path: str,
                          payload: dict | None = None,
                          expect_png: bool = False, expect_image: bool = False):
        """A single attempt of _app_request (no retry)."""
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
        # Decision 59: the batch timeout is 60 s (the server may take
        # up to 45 s); every other call gets the 30 s read timeout.
        timeout = (APP_TIMEOUT_BATCH_S
                   if (method == "POST" and path == "/batch")
                   else APP_TIMEOUT_READ_S)
        try:
            with _app_opener().open(req, timeout=timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - keep the original status
                detail = ""
            # Step 2 (MERGE_PLAN_A): the error classification uses
            # bridge_protocol.error_code_for_status. The hints are
            # unchanged — 400/408/413/431 stay input-validation
            # rejections, 401 stays a token problem, 503 stays
            # overload/not-enabled. The code (e.g. INVALID_REQUEST,
            # UNAUTHORIZED, UNAVAILABLE) is now part of the message
            # so callers and logs can classify without re-parsing
            # the status.
            code = bridge_protocol.error_code_for_status(exc.code)
            # P0-1 (v2.0): a structured a11y-off 503 raises the
            # dedicated subclass so callers can tell "a11y off"
            # from "server down" / "overloaded" without parsing
            # the message. A bare 503 (no reason field) keeps the
            # old behaviour — it may be overload, which retries.
            if exc.code == 503:
                try:
                    body = json.loads(detail) if detail else {}
                except (ValueError, TypeError):
                    body = {}
                if isinstance(body, dict) and body.get("reason") == "ACCESSIBILITY_DISABLED":
                    raise AccessibilityUnavailableError(
                        f"app returned HTTP 503 [{code}] for {method} {path}: "
                        f"accessibility service not enabled "
                        f"(reason=ACCESSIBILITY_DISABLED, "
                        f"required_mode={body.get('required_mode', 'FULL_ACCESS')}; "
                        f"detail: {body.get('detail', '')})",
                        reason="ACCESSIBILITY_DISABLED",
                        required_mode=body.get("required_mode", "FULL_ACCESS")) from exc
            # A-1 (v1.9): the bridge now rejects bad input with its own
            # statuses; the hint must not blame the token for those.
            if exc.code == 401:
                hint = "wrong token, or the token changed on the phone"
            elif exc.code == 403:
                hint = ("forbidden — the bridge refused the request "
                        "(e.g. a launch deny-listed package)")
            elif exc.code == 503:
                hint = ("accessibility service not enabled, or the bridge "
                        "is overloaded")
            elif exc.code in (400, 408, 413, 431):
                hint = ("request rejected by the bridge input validation "
                        "(bad request, timeout, or too large)")
            else:
                hint = "unexpected bridge error"
            raise TransportError(
                f"app returned HTTP {exc.code} [{code}] for {method} {path}: "
                f"{detail} ({hint})") from exc
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
        # Step 3: the query string is built (and validated) by the
        # protocol module — scale 0.1-1.0, format jpeg/jpg/png,
        # quality 10-100.
        params = bridge_protocol.build_capture_screen_query(scale, format, quality)
        return self._app_request(
            "GET", f"/capture_screen?{params}", expect_image=True)

    def _state_app(self) -> dict:
        # Step 3: the /state response is validated by the protocol
        # module (required keys, node shapes). Extra keys the server
        # added later (device, snapshot_id, snapshot_valid,
        # snapshot_stable) are tolerated.
        state = bridge_protocol.parse_state(self._app_request("GET", "/state"))
        state["transport"] = "app"
        # A-3: remember the snapshot so node actions can check its
        # stability before acting.
        self._last_state = state
        return state

    def _app_post(self, path: str, payload: dict) -> dict:
        result = self._app_request("POST", path, payload)
        result["transport"] = "app"
        return result

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
        app = check_app()
        print(json.dumps({
            "phone_ip": PHONE_IP,
            "termux_ssh_8022": ssh,
            "bridge_app": app,
        }, indent=2, ensure_ascii=False))
        return 0 if (ssh["ok"] or app["ok"]) else 1

    adapter = AndroidAdapter(actions_enabled=args.allow_actions)
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
