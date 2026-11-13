"""muse-bridge HTTP communication protocol models and validators (SPEC-POC-001).

Pure standard-library implementation with no network or socket operations.
"""

from __future__ import annotations

import functools
try:
    import fcntl  # Unix-only: the cross-process outbox lock (CONS-153 P1-03)
except ImportError:  # pragma: no cover - non-Unix platforms
    fcntl = None  # type: ignore[assignment]
import hashlib
import ipaddress
import json
import math
import os
import tempfile
import threading
import time
from typing import Any

DEFAULT_PORT = 8080
TOKEN_HEADER = "X-Bridge-Token"

# Protocol version of this module. The live bridge (Phase B, README
# decision 46) accepts only version 1; anything else is a 400.
# Step 2 (v1.0.1): the version is now explicit in build_batch output
# and in the BatchRegistry content hash (MERGE_PLAN_A item 2).
PROTOCOL_VERSION = 1

MAX_BODY_BYTES = 262144
MAX_HEADER_BYTES = 8192
MAX_EVENTS_LIMIT = 200

HEADER_TIMEOUT_SEC = 5
BODY_TIMEOUT_SEC = 10
TOTAL_TIMEOUT_SEC = 45

MAX_WORKERS = 4

# Batch result lifetime (A-4, README decision 49): the server keeps a
# batch result for 10 minutes, then a tombstone for another 10
# minutes. The BatchRegistry mirrors that window client-side so a
# re-used batch_id is refused locally (BATCH_EXPIRED) instead of
# hitting the server's 410. The boundary is ">" (not ">="): the
# server's check is `now > expiresAt` / `now > tombstoneUntil`, so at
# exactly 600 s the batch is still live and at exactly 1200 s it is
# still a tombstone. The server is the authority; the client mirrors
# it (CONS-020 P0-01).
BATCH_RESULT_TTL_S = 600
BATCH_TOMBSTONE_TTL_S = 600

# Java integer ranges (CONS-020 P1-01): the server's tree_hash and
# coordinates are int (signed 32-bit), duration_ms and wait_ms are
# long (signed 64-bit). The client validates these bounds so a value
# the server would truncate (optInt/optLong) never reaches it.
INT32_MIN = -(2 ** 31)
INT32_MAX = 2 ** 31 - 1
INT64_MAX = 2 ** 63 - 1

# Batch outcome states the server reports (A-4): ACCEPTED -> RUNNING
# -> COMPLETED / PARTIAL / FAILED. "partial" means at least one
# action succeeded and then the batch stopped; "failed" means zero
# successes. The registry validates the status when it records a
# server result (parse_batch_result does not — it only validates
# each result item's status).
VALID_BATCH_OUTCOME_STATUSES = (
    "accepted",
    "running",
    "completed",
    "partial",
    "failed",
    # C-01 (SPEC-BATCH-PERSIST-001 §2.10): the server returns
    # "unknown" when a batch_id names a record whose outcome is not
    # known. It carries no results array and no payload_hash, and
    # it is never re-executed.
    "unknown",
)
MAX_QUEUE = 8
MAX_CONNECTIONS = 16

EXPECT_CHANGE_MAX_WAIT_MS = 2000

# Step 3 (v1.1.0): the DPAD keys were missing from the PoC's list.
# The live server's pressKey (BridgeAccessibilityService.java)
# accepts HOME, BACK, RECENTS, POWER and the five DPAD keys
# (decision 66, v1.9.9); the protocol list must match it, or
# build_press_key would reject keys the server accepts.
VALID_KEYS = ("HOME", "BACK", "RECENTS", "POWER",
              "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
              "DPAD_CENTER")
VALID_CAPTURE_FORMATS = ("jpeg", "jpg", "png")
VALID_EVENT_TYPES = (
    "TYPE_WINDOW_STATE_CHANGED",
    "TYPE_WINDOW_CONTENT_CHANGED",
    "TYPE_VIEW_CLICKED",
    "TYPE_VIEW_FOCUSED",
    "TYPE_VIEW_TEXT_CHANGED",
    "TYPE_VIEW_SCROLLED",
    "TYPE_VIEW_SELECTED",
)
VALID_BATCH_ACTION_TYPES = (
    "click_node",
    "tap",
    "type_node",
    "type",
    "key",
    "swipe",
    "wait",
)
VALID_BATCH_RESULT_STATUSES = ("executed", "failed")

_STATUS_TO_ERROR_CODE: dict[int, str] = {
    200: "OK",
    400: "INVALID_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    408: "TIMEOUT",
    409: "STALE_REVISION",
    413: "TOO_LARGE",
    422: "UNPROCESSABLE",
    431: "HEADER_TOO_LARGE",
    500: "INTERNAL_ERROR",
    503: "UNAVAILABLE",
}


class ProtocolError(ValueError):
    """Validation failure for muse-bridge protocol structures."""

    def __init__(self, message: str, code: str = "INVALID_REQUEST") -> None:
        super().__init__(message)
        self.code = code


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(
    value: Any,
    name: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    if not _is_int(value):
        raise ProtocolError(f"{name} must be an int")
    if min_value is not None and value < min_value:
        raise ProtocolError(f"{name} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise ProtocolError(f"{name} must be <= {max_value}")
    return value


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{name} must be a bool")
    return value


def _require_str(value: Any, name: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a str")
    if non_empty and not value.strip():
        raise ProtocolError(f"{name} must not be empty")
    return value


def _require_ipv4(value: Any, name: str) -> str:
    """Require a non-empty IPv4 dotted-quad string (CONS-031
    follow-up, step 3): tailscale_ips used to accept [""] because
    _require_str was called without non_empty=True, and a bare
    string check cannot tell an address from junk. The server
    builds these lists from real network interfaces, the frozen
    v2.2.0 server sends real IPv4 addresses, and v1.9.x sends
    no extension fields at all — so strict IPv4 validation
    cannot reject a real server response."""
    text = _require_str(value, name, non_empty=True)
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        raise ProtocolError(
            f"{name} must be an IPv4 address (got {text!r})") from None
    return text


def _require_optional_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a str or None")
    return value


def _require_payload_hash(value: Any, name: str) -> str:
    """Require a SHA-256 hex digest (64 lowercase hex chars).

    The server always returns a payload_hash in this form
    (String.format "%02x"); a missing, mistyped, or malformed value
    is a protocol error, not a value to skip verification for
    (CONS-020 P0-02).
    """
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a str")
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ProtocolError(f"{name} must be 64 lowercase hex chars")
    return value


def _require_batch_outcome_status(value: Any, name: str) -> str:
    """Require one of the server's batch outcome states."""
    if not isinstance(value, str) or value not in VALID_BATCH_OUTCOME_STATUSES:
        raise ProtocolError(
            f"{name} must be one of {VALID_BATCH_OUTCOME_STATUSES}"
        )
    return value


def _require_enum(value: Any, name: str, allowed: tuple) -> str:
    """Require a string in the given allowed set (P0-3, v2.0)."""
    if not isinstance(value, str) or value not in allowed:
        raise ProtocolError(f"{name} must be one of {allowed}")
    return value


# P0-3 (v2.0): the /health extension vocabularies. The server
# sends these on v2.0.0+; older servers omit them (optional).
HEALTH_MODES = ("MINIMAL", "FULL_ACCESS", "BANKING_SAFE")
HEALTH_OVERALL_STATUSES = ("HEALTHY", "DEGRADED", "CRITICAL", "RECOVERING")
HEALTH_MODULE_VALUES = {
    "http": ("HEALTHY", "RECOVERING"),
    # CONS-029 P1-3 fix: tailscale is DEGRADED when the phone
    # currently has a Tailscale IP but the HTTP server is bound
    # to a different (old) one — the VM cannot connect on the
    # current IP. Old servers never send DEGRADED here, so this
    # stays backward compatible.
    "tailscale": ("HEALTHY", "UNAVAILABLE", "DEGRADED"),
    "accessibility": ("ENABLED", "DISABLED"),
    "notification_access": ("GRANTED", "GRANTED_NOT_CONNECTED", "NOT_GRANTED"),
    "termux_ssh": ("UNKNOWN",),
}
HEALTH_CAPABILITY_KEYS = (
    "screenshot", "tap", "swipe", "text_input",
    "notification", "notification_post", "http_api",
    "launch_app", "launch_app_guaranteed", "foreground_verification",
    "install_apk",
    # CONS-029 P1-4 fix: install_apk_permitted is an OPTIONAL
    # boolean capability (the frozen v2.2.0 server does not send
    # it) — "Android permits starting an install request"
    # (canRequestPackageInstalls), independent of install_apk.
    "install_apk_permitted",
    # Decision 95 (v2.5.0): repair_termux is an OPTIONAL
    # boolean capability — "the endpoint exists"; and
    # repair_termux_permitted — "the RUN_COMMAND permission
    # is granted", independent of the endpoint existing.
    "repair_termux",
    "repair_termux_permitted",
)

# P1-5 (CONS-022): the /health extension fields. All-or-none:
# NONE present -> an old server; ALL present -> a v2.0 server;
# SOME present -> an incomplete Health response (ProtocolError).
# tailscale_ips_bound (P1-3) is NOT in this set — it is an
# additional optional field, sent only by servers new enough
# to report it (the frozen v2.2.0 server does not send it).
HEALTH_EXTENSION_FIELDS = (
    "mode", "overall_status", "modules",
    "capabilities", "tailscale_ips",
)

# P0-4 (v2.0): /launch_app result vocabulary. ok is true ONLY
# when status is "launched" — startActivity() not throwing is
# never the success condition. "unavailable" is reserved (no
# P0-4 mode forbids launching); "failed" is a definite failure
# (422/503 error bodies, not a 200 body).
LAUNCH_STATUSES = ("launched", "unconfirmed", "unavailable", "failed")
LAUNCH_VERIFICATIONS = ("accessibility", "usage_stats", "none")
LAUNCH_UNCONFIRMED_REASONS = ("FOREGROUND_NOT_CONFIRMED",
                              "VERIFICATION_UNAVAILABLE")


def _require_dict(value: Any, name: str) -> dict[str, Any]:
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except Exception as exc:
            raise ProtocolError(f"{name} contains invalid JSON", code="INVALID_JSON") from exc
        if not isinstance(parsed, dict):
            raise ProtocolError(f"{name} JSON must decode to a dict", code="INVALID_JSON")
        return parsed
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must be a dict")
    return value


def validate_body_size(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure serialized JSON request body does not exceed MAX_BODY_BYTES."""
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_BODY_BYTES:
        raise ProtocolError(
            f"serialized body ({len(encoded)} bytes) exceeds MAX_BODY_BYTES ({MAX_BODY_BYTES})",
            code="TOO_LARGE",
        )
    return payload


def validate_headers_size(headers: dict[str, str]) -> dict[str, str]:
    """Ensure formatted HTTP headers do not exceed MAX_HEADER_BYTES."""
    if not isinstance(headers, dict):
        raise ProtocolError("headers must be a dict")
    total_bytes = 0
    for k, v in headers.items():
        _require_str(k, "header key", non_empty=True)
        _require_str(v, "header value")
        line_bytes = f"{k}: {v}\r\n".encode("utf-8")
        total_bytes += len(line_bytes)
    if total_bytes > MAX_HEADER_BYTES:
        raise ProtocolError(
            f"serialized headers ({total_bytes} bytes) exceeds MAX_HEADER_BYTES ({MAX_HEADER_BYTES})",
            code="HEADER_TOO_LARGE",
        )
    return headers


def build_click(x: int, y: int,
                operation_context: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_int(x, "x", min_value=0)
    _require_int(y, "y", min_value=0)
    body: dict[str, Any] = {"x": x, "y": y}
    if operation_context is not None:
        body["operation_context"] = _require_operation_context(operation_context)
    return validate_body_size(body)


def build_swipe(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int = 300,
    operation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_int(x1, "x1", min_value=0)
    _require_int(y1, "y1", min_value=0)
    _require_int(x2, "x2", min_value=0)
    _require_int(y2, "y2", min_value=0)
    _require_int(duration_ms, "duration_ms", min_value=0)
    body: dict[str, Any] = {
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "duration_ms": duration_ms,
    }
    if operation_context is not None:
        body["operation_context"] = _require_operation_context(operation_context)
    return validate_body_size(body)


def build_type_text(text: str,
                    operation_context: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_str(text, "text")
    body: dict[str, Any] = {"text": text}
    if operation_context is not None:
        body["operation_context"] = _require_operation_context(operation_context)
    return validate_body_size(body)


def build_press_key(key: str, operation_context: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_str(key, "key", non_empty=True)
    # Step 3 (P1-01, CONS-019): the server compares keys
    # case-insensitively (equalsIgnoreCase), so normalise to the
    # canonical uppercase form before validating — otherwise the
    # client would reject "back" while the server accepts it.
    key = key.upper()
    if key not in VALID_KEYS:
        raise ProtocolError(f"invalid key: {key!r}")
    body: dict[str, Any] = {"key": key}
    if operation_context is not None:
        body["operation_context"] = _require_operation_context(operation_context)
    return validate_body_size(body)


def _require_operation_context(operation_context: Any) -> dict[str, Any]:
    """C-04 (SPEC-FOREGROUND-GUARD-001 §2.1): an operation_context
    attached to a guarded body must be a dict (normally built by
    build_operation_context); anything else is a client bug, not a
    server rejection."""
    if not isinstance(operation_context, dict):
        raise ProtocolError("operation_context must be a dict")
    return operation_context


def build_operation_context(
    expected_package: str,
    tree_hash: int | None = None,
    generation: int | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """C-04 (SPEC-FOREGROUND-GUARD-001 §2.1): the operation_context
    for a guarded request. expected_package is required (the server
    400s when it is missing or empty); tree_hash and generation are
    the snapshot the operator acted on (generation is the /state
    revision — the server's snapshot_id is the same number by
    construction, BridgeAccessibilityService sets snapshotId =
    lastRevision); session_id is the server-issued UUID, omitted on
    the first operation of a session (bootstrap — the server creates
    the session and returns its id in the response). Fields left as
    None are omitted, never sent as null."""
    _require_str(expected_package, "operation_context.expected_package", non_empty=True)
    if tree_hash is not None:
        _require_int(tree_hash, "operation_context.tree_hash",
                     min_value=INT32_MIN, max_value=INT32_MAX)
    if generation is not None:
        _require_int(generation, "operation_context.generation", min_value=0)
    if session_id is not None:
        _require_str(session_id, "operation_context.session_id", non_empty=True)
    ctx: dict[str, Any] = {"expected_package": expected_package}
    if tree_hash is not None:
        ctx["tree_hash"] = tree_hash
    if generation is not None:
        ctx["generation"] = generation
    if session_id is not None:
        ctx["session_id"] = session_id
    return ctx


def build_click_node(node_id: str, tree_hash: int) -> dict[str, Any]:
    _require_str(node_id, "node_id", non_empty=True)
    _require_int(tree_hash, "tree_hash")
    return validate_body_size({"node_id": node_id, "tree_hash": tree_hash})


def build_type_node(node_id: str, tree_hash: int, text: str) -> dict[str, Any]:
    _require_str(node_id, "node_id", non_empty=True)
    _require_int(tree_hash, "tree_hash")
    _require_str(text, "text")
    return validate_body_size(
        {
            "node_id": node_id,
            "tree_hash": tree_hash,
            "text": text,
        }
    )


def _validate_batch_action(action: Any, index: int) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise ProtocolError(f"actions[{index}] must be a dict")
    if "type" not in action:
        raise ProtocolError(f"actions[{index}] missing required field 'type'")
    action_type = _require_str(action["type"], f"actions[{index}].type", non_empty=True)
    if action_type not in VALID_BATCH_ACTION_TYPES:
        raise ProtocolError(f"actions[{index}] has unknown action type: {action_type!r}")

    out: dict[str, Any] = {}
    allowed_keys = {"type", "action_id", "expect_change"}

    if "action_id" in action:
        out["action_id"] = _require_str(
            action["action_id"], f"actions[{index}].action_id", non_empty=True
        )
    out["type"] = action_type

    if action_type == "click_node":
        allowed_keys.add("node_id")
        if "node_id" not in action:
            raise ProtocolError(f"actions[{index}] missing required field 'node_id'")
        out["node_id"] = _require_str(
            action["node_id"], f"actions[{index}].node_id", non_empty=True
        )
    elif action_type == "tap":
        allowed_keys.update({"x", "y"})
        if "x" not in action or "y" not in action:
            raise ProtocolError(f"actions[{index}] missing required field 'x' or 'y'")
        # Step 4 (CONS-020 P1-01): coordinates are Java ints
        # (signed 32-bit) on the server.
        out["x"] = _require_int(action["x"], f"actions[{index}].x", min_value=0, max_value=INT32_MAX)
        out["y"] = _require_int(action["y"], f"actions[{index}].y", min_value=0, max_value=INT32_MAX)
    elif action_type == "type_node":
        allowed_keys.update({"node_id", "text"})
        if "node_id" not in action or "text" not in action:
            raise ProtocolError(
                f"actions[{index}] missing required field 'node_id' or 'text'"
            )
        out["node_id"] = _require_str(
            action["node_id"], f"actions[{index}].node_id", non_empty=True
        )
        out["text"] = _require_str(action["text"], f"actions[{index}].text")
    elif action_type == "type":
        allowed_keys.add("text")
        if "text" not in action:
            raise ProtocolError(f"actions[{index}] missing required field 'text'")
        out["text"] = _require_str(action["text"], f"actions[{index}].text")
    elif action_type == "key":
        allowed_keys.add("key")
        if "key" not in action:
            raise ProtocolError(f"actions[{index}] missing required field 'key'")
        key_val = _require_str(action["key"], f"actions[{index}].key", non_empty=True)
        if key_val not in VALID_KEYS:
            raise ProtocolError(f"actions[{index}] has invalid key: {key_val!r}")
        out["key"] = key_val
    elif action_type == "swipe":
        allowed_keys.update({"x1", "y1", "x2", "y2", "duration_ms"})
        for coord in ("x1", "y1", "x2", "y2"):
            if coord not in action:
                raise ProtocolError(f"actions[{index}] missing required field {coord!r}")
            out[coord] = _require_int(
                action[coord], f"actions[{index}].{coord}", min_value=0, max_value=INT32_MAX
            )
        if "duration_ms" in action:
            # Step 4 (CONS-020 P1-01): duration_ms is a Java long
            # (signed 64-bit) on the server.
            out["duration_ms"] = _require_int(
                action["duration_ms"], f"actions[{index}].duration_ms", min_value=0, max_value=INT64_MAX
            )
    elif action_type == "wait":
        allowed_keys.add("wait_ms")
        if "wait_ms" not in action:
            raise ProtocolError(f"actions[{index}] missing required field 'wait_ms'")
        # Step 4 (CONS-020 P1-01): wait_ms is a Java long (signed
        # 64-bit) on the server.
        out["wait_ms"] = _require_int(
            action["wait_ms"], f"actions[{index}].wait_ms", min_value=0, max_value=INT64_MAX
        )

    if "expect_change" in action:
        out["expect_change"] = _require_bool(
            action["expect_change"], f"actions[{index}].expect_change"
        )

    extra_keys = set(action.keys()) - allowed_keys
    if extra_keys:
        raise ProtocolError(
            f"actions[{index}] contains unexpected fields for {action_type!r}: {sorted(extra_keys)}"
        )

    return out


def build_batch(
    batch_id: str,
    tree_hash: int,
    actions: list[dict[str, Any]],
    protocol_version: int = PROTOCOL_VERSION,
    supersedes: str | None = None,
    operation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_str(batch_id, "batch_id", non_empty=True)
    # Step 4 (CONS-020 P1-01): tree_hash is a Java int (signed
    # 32-bit) on the server; a value outside that range would be
    # truncated by the server's getInt, so the client rejects it.
    _require_int(tree_hash, "tree_hash", min_value=INT32_MIN, max_value=INT32_MAX)
    # Step 2 (v1.0.1): the version is validated here, not just
    # hardcoded — the live server rejects any version other than 1
    # with 400, so a caller asking for another version fails fast.
    _require_int(protocol_version, "protocol_version", min_value=1, max_value=1)
    if not isinstance(actions, list) or len(actions) == 0:
        raise ProtocolError("actions must be a non-empty list")
    if len(actions) > 20:
        raise ProtocolError("actions must contain at most 20 actions")
    validated_actions = [
        _validate_batch_action(action, idx) for idx, action in enumerate(actions)
    ]
    # C-01 (SPEC-BATCH-PERSIST-001 §2.6): an optional supersedes
    # names the UNKNOWN record this batch replaces. It is NOT part
    # of the content hash (the server's canonicalBatchPayload hashes
    # protocol_version, tree_hash, and actions only), so it is added
    # to the body after the hash inputs are fixed.
    if supersedes is not None:
        _require_str(supersedes, "supersedes", non_empty=True)
        if len(supersedes) > 128:
            raise ProtocolError("supersedes must be at most 128 chars")
    body: dict[str, Any] = {
        "protocol_version": protocol_version,
        "batch_id": batch_id,
        "tree_hash": tree_hash,
        "actions": validated_actions,
    }
    if supersedes is not None:
        body["supersedes"] = supersedes
    # C-04 (SPEC-FOREGROUND-GUARD-001 §2.2, §2.8): the batch's
    # operation_context is checked once, before the batch executes.
    # It is NOT part of the content hash (the server's
    # canonicalBatchPayload hashes protocol_version, tree_hash, and
    # actions only), so attaching it here cannot change the hash the
    # registry computed from the same three fields.
    if operation_context is not None:
        body["operation_context"] = _require_operation_context(operation_context)
    # Step 2 (v1.0.1, MERGE_PLAN_A item 2): the protocol version is
    # now explicit in the batch body. The server defaults it to 1 and
    # rejects anything else with 400, and the A-5 client rule says the
    # client sends protocol_version: 1.
    return validate_body_size(body)


def build_notify(title: str, text: str) -> dict[str, Any]:
    _require_str(title, "title")
    _require_str(text, "text")
    return validate_body_size({"title": title, "text": text})


def build_operation_end() -> dict[str, Any]:
    """v2.1: the /operation_end body is always empty — the end
    signal carries no fields."""
    return validate_body_size({})


def build_capture_screen_query(
    scale: float = 0.5,
    format: str = "jpeg",
    quality: int = 80,
) -> str:
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        raise ProtocolError("scale must be a number")
    if math.isnan(scale) or math.isinf(scale) or scale < 0.1 or scale > 1.0:
        raise ProtocolError("scale must be between 0.1 and 1.0")
    _require_str(format, "format", non_empty=True)
    if format not in VALID_CAPTURE_FORMATS:
        raise ProtocolError(f"format must be one of {VALID_CAPTURE_FORMATS}")
    _require_int(quality, "quality", min_value=10, max_value=100)
    return f"scale={scale}&format={format}&quality={quality}"


def build_events_query(since: int = 0, limit: int = 100) -> str:
    _require_int(since, "since", min_value=0)
    _require_int(limit, "limit", min_value=1, max_value=MAX_EVENTS_LIMIT)
    return f"since={since}&limit={limit}"


def _validate_bounds(bounds: Any, name: str) -> dict[str, int]:
    if not isinstance(bounds, dict):
        raise ProtocolError(f"{name} must be a dict")
    for key in ("left", "top", "right", "bottom"):
        if key not in bounds:
            raise ProtocolError(f"{name} missing required field {key!r}")
        _require_int(bounds[key], f"{name}.{key}")
    return {
        "left": bounds["left"],
        "top": bounds["top"],
        "right": bounds["right"],
        "bottom": bounds["bottom"],
    }


def _validate_node(node: Any, index: int) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise ProtocolError(f"nodes[{index}] must be a dict")
    required_keys = (
        "id",
        "parent",
        "class",
        "text",
        "desc",
        "bounds",
        "clickable",
        "focusable",
        "focused",
        "editable",
        "scrollable",
        "enabled",
        "selected",
        "password",
        "input_type",
        "resource_id",
        "actions",
    )
    for key in required_keys:
        if key not in node:
            raise ProtocolError(f"nodes[{index}] missing required field {key!r}")

    _require_str(node["id"], f"nodes[{index}].id", non_empty=True)
    _require_optional_str(node["parent"], f"nodes[{index}].parent")
    _require_str(node["class"], f"nodes[{index}].class")
    _require_str(node["text"], f"nodes[{index}].text")
    _require_str(node["desc"], f"nodes[{index}].desc")
    _validate_bounds(node["bounds"], f"nodes[{index}].bounds")
    for bool_key in (
        "clickable",
        "focusable",
        "focused",
        "editable",
        "scrollable",
        "enabled",
        "selected",
        "password",
    ):
        _require_bool(node[bool_key], f"nodes[{index}].{bool_key}")
    _require_int(node["input_type"], f"nodes[{index}].input_type")
    _require_str(node["resource_id"], f"nodes[{index}].resource_id")

    if not isinstance(node["actions"], list):
        raise ProtocolError(f"nodes[{index}].actions must be a list")
    for act_idx, act in enumerate(node["actions"]):
        _require_str(act, f"nodes[{index}].actions[{act_idx}]")

    return dict(node)


def _validate_health_cross_fields(d: dict, modules: dict, caps: dict) -> None:
    """P1-5 (CONS-022, tightened by CONS-029): cross-field
    consistency for a complete v2.0 /health body. Called only
    when all 5 extension fields are present. The internal fields
    are REQUIRED — a complete v2.0 response must carry them, and
    omitting one is a protocol error, not a skip: the earlier
    version let a server bypass validation by leaving a field
    out (e.g. mode FULL_ACCESS + accessibility False + empty
    modules/capabilities passed)."""
    # modules.accessibility and modules.tailscale are required
    # internal fields (CONS-029 P1-5 problem 1).
    if "accessibility" not in modules:
        raise ProtocolError(
            "health.modules.accessibility is required in a complete "
            "v2.0 Health response")
    if "tailscale" not in modules:
        raise ProtocolError(
            "health.modules.tailscale is required in a complete "
            "v2.0 Health response")
    a11y_mod = modules["accessibility"]
    # Mode and the top-level accessibility boolean must agree
    # DIRECTLY (CONS-029 P1-5 problem 1): FULL_ACCESS means the
    # service is connected; MINIMAL / BANKING_SAFE mean it is
    # off. The top-level boolean is the same state.
    expected_a11y = d["mode"] == "FULL_ACCESS"
    if d["accessibility"] is not expected_a11y:
        raise ProtocolError(
            "health.mode and health.accessibility disagree")
    # modules.accessibility must agree with the same expectation.
    if a11y_mod != ("ENABLED" if expected_a11y else "DISABLED"):
        raise ProtocolError(
            f"health.mode is {d['mode']!r} but health.modules.accessibility "
            f"is {a11y_mod!r}")
    # The screen capabilities are required internal fields, and
    # the server always reports them as the a11y state
    # (HttpBridgeServer.buildCapabilities puts a11yOn in all
    # four): all true when the service is connected, all false
    # when it is off (CONS-029 P1-5 problem 3).
    for key in ("screenshot", "tap", "swipe", "text_input"):
        if key not in caps:
            raise ProtocolError(
                f"health.capabilities.{key} is required in a complete "
                "v2.0 Health response")
        if caps[key] is not expected_a11y:
            raise ProtocolError(
                "health.modules.accessibility is "
                f"{a11y_mod!r} but health.capabilities.{key} is "
                f"{caps[key]!r}")
    # No Tailscale address means no Tailscale IPs to report.
    if modules["tailscale"] == "UNAVAILABLE" and d.get("tailscale_ips"):
        raise ProtocolError(
            "health.modules.tailscale is UNAVAILABLE but "
            "health.tailscale_ips is not empty")
    # A DEGRADED Tailscale module means the server is bound to a
    # different IP than the current one (P1-3): the overall
    # status must be DEGRADED too, and there must be a current
    # IP to be stale against (CONS-030). The Java server always
    # sets overall DEGRADED in this case; the validator enforces
    # the same contract.
    if modules["tailscale"] == "DEGRADED":
        if d.get("overall_status") != "DEGRADED":
            raise ProtocolError(
                "health.modules.tailscale is DEGRADED but "
                "health.overall_status is not DEGRADED")
        if not d.get("tailscale_ips"):
            raise ProtocolError(
                "health.modules.tailscale is DEGRADED but "
                "health.tailscale_ips is empty")
    # P0-4: a launch is never guaranteed — in a complete v2.0
    # response the key must be present AND false (CONS-029 P1-5
    # problem 2: a missing key used to pass).
    if "launch_app_guaranteed" not in caps:
        raise ProtocolError(
            "health.capabilities.launch_app_guaranteed is required in "
            "a complete v2.0 Health response")
    if caps["launch_app_guaranteed"] is not False:
        raise ProtocolError(
            "health.capabilities.launch_app_guaranteed must be False")
    # Foreground verification is true exactly when the service
    # is connected (P0 spec §4; CONS-030): the key is required
    # and must equal expected_a11y in BOTH directions — the
    # earlier check only rejected True-when-disabled, so a
    # FULL_ACCESS body with foreground_verification=false (or a
    # missing key) passed.
    if "foreground_verification" not in caps:
        raise ProtocolError(
            "health.capabilities.foreground_verification is required "
            "in a complete v2.0 Health response")
    if caps["foreground_verification"] is not expected_a11y:
        raise ProtocolError(
            "health.capabilities.foreground_verification does not "
            "match accessibility state")


def parse_health(data: dict[str, Any]) -> dict[str, Any]:
    d = _require_dict(data, "health")
    required_keys = (
        "ok",
        "service",
        "version",
        "app_version",
        "app_version_code",
        "accessibility",
    )
    for key in required_keys:
        if key not in d:
            raise ProtocolError(f"health missing required field {key!r}")

    if _require_bool(d["ok"], "health.ok") is not True:
        raise ProtocolError("health.ok must be True")
    service = _require_str(d["service"], "health.service", non_empty=True)
    if service != "muse-bridge":
        raise ProtocolError(f"unexpected health.service: {service!r}")
    _require_int(d["version"], "health.version", min_value=1, max_value=1)
    _require_str(d["app_version"], "health.app_version", non_empty=True)
    _require_int(d["app_version_code"], "health.app_version_code", min_value=0)
    _require_bool(d["accessibility"], "health.accessibility")
    # P0-3 (v2.0): the /health extension fields are OPTIONAL —
    # v1.9.x servers do not send them, and the client must keep
    # working against an old server. When present, each value is
    # validated; unknown keys inside modules/capabilities are
    # ignored (a newer server may add one).
    if "notification_listener" in d:
        _require_bool(d["notification_listener"], "health.notification_listener")
    # P1-5 (CONS-022): the extension is all-or-none. NONE of
    # the 5 fields -> an old server, accept. ALL 5 -> a v2.0
    # server, validate (below). SOME -> an incomplete Health
    # response, reject: a v2.0 server that omits part of the
    # extension is a protocol error, not a half-old server.
    present = [f for f in HEALTH_EXTENSION_FIELDS if f in d]
    if present and len(present) != len(HEALTH_EXTENSION_FIELDS):
        raise ProtocolError(
            f"health has an incomplete extension: present={sorted(present)}; "
            f"expected all of {list(HEALTH_EXTENSION_FIELDS)} or none")
    # P1-3 (CONS-022): tailscale_ips_bound (the bound-at-start
    # list) is an additional optional field, NOT part of the
    # all-or-none set — the frozen v2.2.0 server does not send
    # it, and an old server omits it too.
    if "tailscale_ips_bound" in d:
        bound = d["tailscale_ips_bound"]
        if not isinstance(bound, list):
            raise ProtocolError("health.tailscale_ips_bound must be a list")
        for ip in bound:
            _require_ipv4(ip, "health.tailscale_ips_bound[]")
    # TARMUX_KICKASS step 1 (v2.4.0): local sshd diagnosis is an
    # additional optional field like tailscale_ips_bound — old
    # servers omit it. When present it must be a complete
    # object: reachable (bool), banner (str), latency_ms
    # (int >= 0) and error (str).
    if "termux_sshd_local" in d:
        local = _require_dict(d["termux_sshd_local"],
                              "health.termux_sshd_local")
        _require_bool(local.get("reachable"),
                      "health.termux_sshd_local.reachable")
        _require_str(local.get("banner"), "health.termux_sshd_local.banner")
        _require_int(local.get("latency_ms"),
                     "health.termux_sshd_local.latency_ms", min_value=0)
        _require_str(local.get("error"), "health.termux_sshd_local.error")
    if len(present) == len(HEALTH_EXTENSION_FIELDS):
        _require_enum(d["mode"], "health.mode", HEALTH_MODES)
        _require_enum(d["overall_status"], "health.overall_status",
                      HEALTH_OVERALL_STATUSES)
        modules = _require_dict(d["modules"], "health.modules")
        for key, allowed in HEALTH_MODULE_VALUES.items():
            if key in modules:
                _require_enum(modules[key], f"health.modules.{key}", allowed)
        caps = _require_dict(d["capabilities"], "health.capabilities")
        for key in HEALTH_CAPABILITY_KEYS:
            if key in caps:
                _require_bool(caps[key], f"health.capabilities.{key}")
        ips = d["tailscale_ips"]
        if not isinstance(ips, list):
            raise ProtocolError("health.tailscale_ips must be a list")
        for ip in ips:
            _require_ipv4(ip, "health.tailscale_ips[]")
        _validate_health_cross_fields(d, modules, caps)
    return dict(d)


def parse_launch_app(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a /launch_app 200 body (P0-4, v2.0) and return a
    copy. The core invariant: ok is True exactly when status is
    "launched", and foreground_confirmed is True exactly then
    too. P0-4 fix (CONS-027 P1 #2): the status + reason +
    verification + foreground_confirmed combination must be one
    of the three shapes the server can send — launched (no
    reason, verification "accessibility", foreground True),
    unconfirmed / FOREGROUND_NOT_CONFIRMED (verification
    "accessibility", foreground False), or unconfirmed /
    VERIFICATION_UNAVAILABLE (verification "none", foreground
    null). "unavailable"/"failed" are vocabulary only in P0-4
    (failures stay HTTP 422/503 in the old format), so no 200
    body may carry those statuses. Extra keys (e.g.
    "transport" added by the adapter) are tolerated, as in
    parse_health. P2-02 (CONS-028, step 3): accepted and
    launch_requested must be True, not merely bools - the
    server hardcodes both to True in launchResultJson, so a
    False here is a malformed body, never a real outcome."""
    d = _require_dict(data, "launch_app")
    for key in ("ok", "status", "accepted", "package",
                "launch_requested", "foreground_confirmed", "verification"):
        if key not in d:
            raise ProtocolError(f"launch_app missing required field {key!r}")

    ok = _require_bool(d["ok"], "launch_app.ok")
    status = _require_enum(d["status"], "launch_app.status", LAUNCH_STATUSES)
    if ok != (status == "launched"):
        raise ProtocolError(
            "launch_app.ok must be True exactly when status is 'launched'")
    if _require_bool(d["accepted"], "launch_app.accepted") is not True:
        raise ProtocolError("launch_app.accepted must be True")
    _require_str(d["package"], "launch_app.package", non_empty=True)
    if _require_bool(d["launch_requested"],
                     "launch_app.launch_requested") is not True:
        raise ProtocolError("launch_app.launch_requested must be True")
    fg = d["foreground_confirmed"]
    if fg is not None:
        _require_bool(fg, "launch_app.foreground_confirmed")
    if (status == "launched") != (fg is True):
        raise ProtocolError(
            "launch_app.foreground_confirmed must be True exactly when "
            "status is 'launched'")
    _require_enum(d["verification"], "launch_app.verification",
                  LAUNCH_VERIFICATIONS)
    if status == "launched":
        if "reason" in d:
            raise ProtocolError(
                "launch_app.reason must be absent when status is "
                "'launched'")
        if d["verification"] != "accessibility":
            raise ProtocolError(
                "launch_app.verification must be 'accessibility' when "
                "status is 'launched' (verification 'none' cannot "
                "confirm a foreground)")
    elif status == "unconfirmed":
        if "reason" not in d:
            raise ProtocolError(
                "launch_app.reason is required when status is 'unconfirmed'")
        reason = _require_str(d["reason"], "launch_app.reason",
                              non_empty=True)
        if reason == "FOREGROUND_NOT_CONFIRMED":
            if d["verification"] != "accessibility" or fg is not False:
                raise ProtocolError(
                    "launch_app with reason FOREGROUND_NOT_CONFIRMED must "
                    "have verification 'accessibility' and "
                    "foreground_confirmed false")
        elif reason == "VERIFICATION_UNAVAILABLE":
            if d["verification"] != "none" or fg is not None:
                raise ProtocolError(
                    "launch_app with reason VERIFICATION_UNAVAILABLE must "
                    "have verification 'none' and foreground_confirmed null")
        else:
            raise ProtocolError(
                f"launch_app.reason has unknown value: {reason!r}")
    else:
        # "unavailable" / "failed": no 200-body form in P0-4.
        raise ProtocolError(
            f"launch_app.status {status!r} has no 200-body form in P0-4 "
            "(failures stay HTTP 422/503 in the old format)")
    return dict(d)


def parse_repair_termux(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a POST /repair_termux 200 body (Decision 95,
    v2.5.0, TARMUX_KICKASS step 2) and return a copy. The
    core invariant: "ok" is True exactly when sshd answers
    locally after the attempt ("repaired" is True exactly
    then too, except ALREADY_RUNNING where ok is True and
    repaired is False). "reason" is one of ALREADY_RUNNING,
    REPAIRED or STILL_DOWN. "termux_sshd_local" is the same
    object parse_health validates, and "steps" lists the
    fixed commands that were sent (empty when nothing was
    sent). 400/403/409 stay HTTP errors, not 200 bodies."""
    d = _require_dict(data, "repair_termux")
    for key in ("ok", "repaired", "reason", "termux_sshd_local", "steps"):
        if key not in d:
            raise ProtocolError(
                f"repair_termux missing required field {key!r}")
    ok = _require_bool(d["ok"], "repair_termux.ok")
    repaired = _require_bool(d["repaired"], "repair_termux.repaired")
    reason = _require_str(d["reason"], "repair_termux.reason",
                          non_empty=True)
    if reason not in ("ALREADY_RUNNING", "REPAIRED", "STILL_DOWN"):
        raise ProtocolError(
            f"repair_termux.reason has unknown value: {reason!r}")
    if ok != (reason in ("ALREADY_RUNNING", "REPAIRED")):
        raise ProtocolError(
            "repair_termux.ok must be True exactly when reason is "
            "ALREADY_RUNNING or REPAIRED")
    if repaired != (reason == "REPAIRED"):
        raise ProtocolError(
            "repair_termux.repaired must be True exactly when "
            "reason is REPAIRED")
    local = _require_dict(d["termux_sshd_local"],
                          "repair_termux.termux_sshd_local")
    _require_bool(local.get("reachable"),
                  "repair_termux.termux_sshd_local.reachable")
    _require_str(local.get("banner"),
                 "repair_termux.termux_sshd_local.banner")
    _require_int(local.get("latency_ms"),
                 "repair_termux.termux_sshd_local.latency_ms",
                 min_value=0)
    _require_str(local.get("error"),
                 "repair_termux.termux_sshd_local.error")
    if local.get("reachable") != ok:
        raise ProtocolError(
            "repair_termux.termux_sshd_local.reachable must match ok")
    steps = d["steps"]
    if not isinstance(steps, list):
        raise ProtocolError("repair_termux.steps must be a list")
    for step in steps:
        _require_str(step, "repair_termux.steps[]")
    return dict(d)


def parse_state(data: dict[str, Any]) -> dict[str, Any]:
    d = _require_dict(data, "state")
    required_keys = (
        "ok",
        "app_version",
        "app_version_code",
        "width",
        "height",
        "package",
        "texts",
        "revision",
        "tree_hash",
        "nodes",
        "focused",
        "ime_visible",
        "event_seq",
        "truncated",
    )
    for key in required_keys:
        if key not in d:
            raise ProtocolError(f"state missing required field {key!r}")

    if _require_bool(d["ok"], "state.ok") is not True:
        raise ProtocolError("state.ok must be True")
    _require_str(d["app_version"], "state.app_version", non_empty=True)
    _require_int(d["app_version_code"], "state.app_version_code", min_value=0)
    _require_int(d["width"], "state.width", min_value=1)
    _require_int(d["height"], "state.height", min_value=1)
    _require_str(d["package"], "state.package")

    if not isinstance(d["texts"], list):
        raise ProtocolError("state.texts must be a list")
    for idx, text in enumerate(d["texts"]):
        _require_str(text, f"state.texts[{idx}]")

    _require_int(d["revision"], "state.revision", min_value=0)
    _require_int(d["tree_hash"], "state.tree_hash")

    if not isinstance(d["nodes"], list):
        raise ProtocolError("state.nodes must be a list")
    for idx, node in enumerate(d["nodes"]):
        _validate_node(node, idx)

    _require_optional_str(d["focused"], "state.focused")
    _require_bool(d["ime_visible"], "state.ime_visible")
    _require_int(d["event_seq"], "state.event_seq", min_value=0)
    _require_bool(d["truncated"], "state.truncated")
    return dict(d)


def _validate_event(event: Any, index: int) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ProtocolError(f"events[{index}] must be a dict")
    required_keys = (
        "seq",
        "t",
        "type",
        "package",
        "class",
        "text",
        "desc",
        "bounds",
        "coalesced",
    )
    for key in required_keys:
        if key not in event:
            raise ProtocolError(f"events[{index}] missing required field {key!r}")

    _require_int(event["seq"], f"events[{index}].seq", min_value=0)
    _require_int(event["t"], f"events[{index}].t", min_value=0)
    event_type = _require_str(event["type"], f"events[{index}].type", non_empty=True)
    if event_type not in VALID_EVENT_TYPES:
        raise ProtocolError(f"events[{index}] has unknown event type: {event_type!r}")
    _require_str(event["package"], f"events[{index}].package")
    _require_str(event["class"], f"events[{index}].class")
    _require_str(event["text"], f"events[{index}].text")
    _require_str(event["desc"], f"events[{index}].desc")
    _validate_bounds(event["bounds"], f"events[{index}].bounds")
    _require_int(event["coalesced"], f"events[{index}].coalesced", min_value=0)
    return dict(event)


def parse_events(data: dict[str, Any]) -> dict[str, Any]:
    d = _require_dict(data, "events_response")
    required_keys = (
        "ok",
        "events",
        "latest_seq",
        "dropped",
        "tree_hash",
        "revision",
    )
    for key in required_keys:
        if key not in d:
            raise ProtocolError(f"events_response missing required field {key!r}")

    if _require_bool(d["ok"], "events_response.ok") is not True:
        raise ProtocolError("events_response.ok must be True")

    if "limit" in d:
        _require_int(
            d["limit"],
            "events_response.limit",
            min_value=1,
            max_value=MAX_EVENTS_LIMIT,
        )

    if not isinstance(d["events"], list):
        raise ProtocolError("events_response.events must be a list")
    if len(d["events"]) > MAX_EVENTS_LIMIT:
        raise ProtocolError(
            f"events_response.events count ({len(d['events'])}) exceeds MAX_EVENTS_LIMIT ({MAX_EVENTS_LIMIT})"
        )
    for idx, ev in enumerate(d["events"]):
        _validate_event(ev, idx)

    _require_int(d["latest_seq"], "events_response.latest_seq", min_value=0)
    _require_bool(d["dropped"], "events_response.dropped")
    _require_int(d["tree_hash"], "events_response.tree_hash")
    _require_int(d["revision"], "events_response.revision", min_value=0)
    return dict(d)


def _validate_batch_result_item(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ProtocolError(f"results[{index}] must be a dict")
    required_keys = (
        "action_id",
        "status",
        "error",
        "hash_before",
        "hash_after",
    )
    for key in required_keys:
        if key not in item:
            raise ProtocolError(f"results[{index}] missing required field {key!r}")

    _require_str(item["action_id"], f"results[{index}].action_id", non_empty=True)
    status = _require_str(item["status"], f"results[{index}].status", non_empty=True)
    if status not in VALID_BATCH_RESULT_STATUSES:
        raise ProtocolError(f"results[{index}] has invalid status: {status!r}")
    _require_optional_str(item["error"], f"results[{index}].error")
    _require_int(item["hash_before"], f"results[{index}].hash_before")
    _require_int(item["hash_after"], f"results[{index}].hash_after")
    return dict(item)


def parse_batch_result(data: dict[str, Any]) -> dict[str, Any]:
    d = _require_dict(data, "batch_result")
    # C-01 (SPEC-BATCH-PERSIST-001 §2.10): an UNKNOWN response
    # carries no results array — only ok, batch_id, status,
    # replayed, and a message. It is validated on its own shape;
    # the full shape below still applies to every other status.
    if d.get("status") == "unknown":
        for key in ("ok", "batch_id", "replayed", "status", "message"):
            if key not in d:
                raise ProtocolError(f"batch_result missing required field {key!r}")
        if _require_bool(d["ok"], "batch_result.ok") is not True:
            raise ProtocolError("batch_result.ok must be True")
        _require_str(d["batch_id"], "batch_result.batch_id", non_empty=True)
        _require_bool(d["replayed"], "batch_result.replayed")
        _require_batch_outcome_status(d["status"], "batch_result.status")
        _require_str(d["message"], "batch_result.message", non_empty=True)
        return dict(d)
    required_keys = (
        "ok",
        "batch_id",
        "replayed",
        "results",
        "stopped_at",
        "stop_reason",
        # Step 4 (CONS-020 P0-02/P0-03): the A-4 server always
        # returns the batch-level status and the payload_hash, so a
        # response without them is malformed, not a value to tolerate.
        "status",
        "payload_hash",
    )
    for key in required_keys:
        if key not in d:
            raise ProtocolError(f"batch_result missing required field {key!r}")

    if _require_bool(d["ok"], "batch_result.ok") is not True:
        raise ProtocolError("batch_result.ok must be True")
    _require_str(d["batch_id"], "batch_result.batch_id", non_empty=True)
    _require_bool(d["replayed"], "batch_result.replayed")

    if not isinstance(d["results"], list):
        raise ProtocolError("batch_result.results must be a list")
    for idx, res in enumerate(d["results"]):
        _validate_batch_result_item(res, idx)

    _require_int(d["stopped_at"], "batch_result.stopped_at", min_value=-1)
    _require_optional_str(d["stop_reason"], "batch_result.stop_reason")
    _require_batch_outcome_status(d["status"], "batch_result.status")
    _require_payload_hash(d["payload_hash"], "batch_result.payload_hash")
    return dict(d)


def error_code_for_status(status: int) -> str:
    _require_int(status, "status")
    return _STATUS_TO_ERROR_CODE.get(status, "INTERNAL_ERROR")


def parse_error_body(data: dict[str, Any] | str | bytes) -> dict[str, Any]:
    if isinstance(data, (str, bytes)):
        try:
            parsed = json.loads(data)
        except Exception:
            return {
                "ok": False,
                "error": "invalid json",
                "code": "INVALID_JSON",
            }
        if not isinstance(parsed, dict):
            return {
                "ok": False,
                "error": "invalid json",
                "code": "INVALID_JSON",
            }
        data = parsed

    if not isinstance(data, dict):
        raise ProtocolError("error body must be a dict")
    if "ok" not in data or "error" not in data:
        raise ProtocolError("error body missing required field 'ok' or 'error'")
    if _require_bool(data["ok"], "error.ok") is not False:
        raise ProtocolError("error.ok must be False")
    err_msg = _require_str(data["error"], "error.error")

    if "status" in data and data["status"] is not None:
        status_val = _require_int(data["status"], "error.status")
        code = error_code_for_status(status_val)
    else:
        code = "INTERNAL_ERROR"

    return {
        "ok": False,
        "error": err_msg,
        "code": code,
    }


def _canon_str(name: str, value: Any) -> str:
    """Render one length-prefixed string field, as the server does.

    The server's appendCanon writes name=<len>:<value>, where <len>
    is Java's String.length() — the number of UTF-16 code units,
    not code points (an astral character counts as 2).
    """
    text = "" if value is None else str(value)
    utf16_len = len(text.encode("utf-16-le")) // 2
    return f"{name}={utf16_len}:{text}"


def _canonical_batch_payload(
    protocol_version: int, tree_hash: int, actions: list[dict[str, Any]]
) -> str:
    """Build the server's canonical batch payload string.

    Byte-identical to BridgeAccessibilityService.canonicalBatchPayload
    (A-4): "v{version}|tree={treeHash}|actions=[{...};{...}]" with a
    fixed per-action field order (id, type, node, x, y, x1, y1, x2,
    y2, dur, text, key, wait, exp), the server's defaults filled in
    for absent fields (action_id defaults to "a"+index, node/text/
    key to "", coordinates to 0, duration_ms to 300, wait_ms to 0,
    expect_change to false), and length-prefixed strings. The
    booleans render as "true"/"false" (Java's StringBuilder).
    """
    parts: list[str] = [f"v{protocol_version}", f"|tree={tree_hash}", "|actions=["]
    for i, action in enumerate(actions):
        if i > 0:
            parts.append(";")
        parts.append("{")
        parts.append(_canon_str("id", action.get("action_id", f"a{i}")))
        parts.append(",")
        parts.append(_canon_str("type", action.get("type", "")))
        parts.append(",")
        parts.append(_canon_str("node", action.get("node_id", "")))
        parts.append(",")
        parts.append(f"x={action.get('x', 0)}")
        parts.append(f",y={action.get('y', 0)}")
        parts.append(f",x1={action.get('x1', 0)}")
        parts.append(f",y1={action.get('y1', 0)}")
        parts.append(f",x2={action.get('x2', 0)}")
        parts.append(f",y2={action.get('y2', 0)}")
        parts.append(f",dur={action.get('duration_ms', 300)}")
        parts.append(",")
        parts.append(_canon_str("text", action.get("text", "")))
        parts.append(",")
        parts.append(_canon_str("key", action.get("key", "")))
        parts.append(f",wait={action.get('wait_ms', 0)}")
        exp = action.get("expect_change", False)
        parts.append(f",exp={'true' if exp else 'false'}")
        parts.append("}")
    parts.append("]")
    return "".join(parts)


class BatchRegistry:
    """Local idempotency registry for POST /batch requests.

    Step 4 (v1.2.0): the registry mirrors the server's A-4 rules
    (README decision 49). A record is live for BATCH_RESULT_TTL_S
    (10 min): the same batch_id with the same content replays, and
    with different content it conflicts (STALE_REVISION, the
    server's 409). Then it becomes a tombstone for another
    BATCH_TOMBSTONE_TTL_S (10 min): any submit with that batch_id
    raises BATCH_EXPIRED (the server's 410) — the caller must use a
    NEW batch_id, never re-use the expired one. After 20 minutes
    total the record is purged and the id is free again. The
    boundaries use ">" (not ">="), mirroring the server's `now >
    expiresAt` / `now > tombstoneUntil` — at exactly 600 s the
    batch is still live, at exactly 1200 s it is still a tombstone
    (CONS-020 P0-01). The registry also records the server's
    outcome for each batch (status incl. partial/running,
    payload_hash, completed/failed ids) and verifies the server's
    payload_hash matches the content the client sent. Memory only:
    no exactly-once across a process restart (a stated design
    constraint).
    """

    def __init__(self, clock: Any = None) -> None:
        # clock is a zero-arg callable returning a monotonic time
        # (default time.monotonic). It is injectable so tests can
        # move time forward without waiting.
        self._clock = clock if clock is not None else time.monotonic
        self._records: dict[str, dict[str, Any]] = {}

    def _purge_expired(self, now: float) -> None:
        # Step 4 (CONS-020 P0-01): the boundary is ">" (not ">="),
        # mirroring the server's `now > tombstoneUntil` — at exactly
        # 20 minutes the record is still a tombstone.
        lifetime = BATCH_RESULT_TTL_S + BATCH_TOMBSTONE_TTL_S
        stale = [
            batch_id
            for batch_id, record in self._records.items()
            if now - record["created_at"] > lifetime
        ]
        for batch_id in stale:
            del self._records[batch_id]

    @staticmethod
    def compute_content_hash(
        protocol_version: int, tree_hash: int, actions: list[dict[str, Any]]
    ) -> str:
        """Return a SHA-256 hash of the canonical batch payload.

        Step 4 (v1.2.0): the canonical form is now byte-identical
        to the live server's canonicalBatchPayload (A-4, README
        decision 49; BridgeAccessibilityService.java) — a fixed
        field order with defaults filled in and length-prefixed
        strings — so the client's hash EQUALS the server's
        payload_hash and record_result can verify it. Earlier
        versions hashed a JSON object with sort_keys; the field set
        was the same ({protocol_version, tree_hash, actions}) but
        the serialisation differed, so the hashes never matched the
        server's. protocol_version is a required argument: the hash
        must never be computed without it. This is a pure function
        — it hashes whatever version it is given; the version policy
        (only 1 is accepted) is enforced at the entry points,
        build_batch and submit.
        """
        canonical = _canonical_batch_payload(protocol_version, tree_hash, actions)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def submit(
        self,
        batch_id: str,
        tree_hash: int,
        actions: list[dict[str, Any]],
        protocol_version: int = PROTOCOL_VERSION,
        supersedes: str | None = None,
        operation_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register a batch by its content hash.

        Replays (replayed: True) when the same batch_id was submitted
        with identical content (same protocol_version, tree_hash, and
        actions) while the record is live. Raises ProtocolError
        (code STALE_REVISION) when the same batch_id was submitted
        with different content while live, and ProtocolError (code
        BATCH_EXPIRED) when the batch_id is in its tombstone window
        — the caller must use a NEW batch_id. The version is
        validated (only 1 is accepted) because the live server
        rejects any other version with 400. The returned dict also
        carries the validated "body" (the build_batch output) so
        the caller sends exactly the payload that was registered.
        """
        now = self._clock()
        self._purge_expired(now)
        _require_int(protocol_version, "protocol_version", min_value=1, max_value=1)
        validated = build_batch(
            batch_id, tree_hash, actions, protocol_version, supersedes,
            operation_context,
        )
        content_hash = self.compute_content_hash(
            validated["protocol_version"],
            validated["tree_hash"],
            validated["actions"],
        )
        record = self._records.get(batch_id)
        if record is not None:
            age = now - record["created_at"]
            # Step 4 (CONS-020 P0-01): the boundary is ">" (not
            # ">="), mirroring the server's `now > expiresAt` — at
            # exactly 600 s the batch is still live.
            if age > BATCH_RESULT_TTL_S:
                raise ProtocolError(
                    f"batch_id {batch_id!r} expired: the server's result "
                    "has lapsed (410); use a NEW batch_id, never re-use "
                    "the expired one",
                    code="BATCH_EXPIRED",
                )
            if record["content_hash"] == content_hash:
                return {
                    "batch_id": batch_id,
                    "replayed": True,
                    "content_hash": content_hash,
                    "body": validated,
                }
            raise ProtocolError(
                f"batch_id conflict for {batch_id!r}: existing content_hash does not match",
                code="STALE_REVISION",
            )
        self._records[batch_id] = {
            "content_hash": content_hash,
            "created_at": now,
            "status": None,
            "payload_hash": None,
            "completed_action_ids": None,
            "failed_action_id": None,
            "stopped_at": None,
            "stop_reason": None,
            "mismatch": None,
        }
        return {
            "batch_id": batch_id,
            "replayed": False,
            "content_hash": content_hash,
            "body": validated,
        }

    def record_result(
        self, batch_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Record the server's outcome for a batch.

        result is a parsed batch response (the parse_batch_result
        output). The batch-level "status" and the "payload_hash"
        are required — the server always returns them (CONS-020
        P0-02/P0-03), so a response without them is malformed. The
        status must be one of the server's states
        (accepted/running/completed/partial/failed). When the
        server's payload_hash differs from the content_hash this
        registry holds, the outcome is stored as a "mismatch"
        diagnostic on the record and a ProtocolError (code
        PAYLOAD_HASH_MISMATCH) is raised — the server executed
        different content than the client sent (CONS-020 P1-03).
        The batch_id must have been submitted first (CONS-020
        P1-02): an unknown id raises ProtocolError (code
        UNKNOWN_BATCH_ID) instead of creating a record that would
        block the id for 20 minutes. Returns the stored record
        (see get).
        """
        if not isinstance(result, dict):
            raise ProtocolError("batch result must be a dict")
        now = self._clock()
        self._purge_expired(now)
        status = _require_batch_outcome_status(
            result.get("status"), "batch_result.status"
        )
        if status == "unknown":
            # C-01 (SPEC §2.10): an UNKNOWN response carries no
            # payload_hash and no results — the server has no result
            # to report. Record the unknown outcome without a hash
            # check.
            payload_hash = None
        else:
            payload_hash = _require_payload_hash(
                result.get("payload_hash"), "batch_result.payload_hash"
            )
        record = self._records.get(batch_id)
        if record is None:
            raise ProtocolError(
                f"unknown batch_id {batch_id!r}: submit it before "
                "recording a result",
                code="UNKNOWN_BATCH_ID",
            )
        outcome = {
            "status": status,
            "payload_hash": payload_hash,
            "completed_action_ids": result.get("completed_action_ids"),
            "failed_action_id": result.get("failed_action_id"),
            "stopped_at": result.get("stopped_at"),
            "stop_reason": result.get("stop_reason"),
        }
        if (
            payload_hash is not None
            and record["content_hash"] is not None
            and payload_hash != record["content_hash"]
        ):
            # Keep the server's outcome as a diagnostic (it is not
            # the normal outcome — the batch's content does not
            # match what was sent), then raise.
            record["mismatch"] = outcome
            raise ProtocolError(
                f"payload_hash mismatch for batch_id {batch_id!r}: the "
                "server executed different content than the client sent",
                code="PAYLOAD_HASH_MISMATCH",
            )
        record.update(outcome)
        return self.get(batch_id)  # type: ignore[return-value]

    def get(self, batch_id: str) -> dict[str, Any] | None:
        """Return the stored record for a batch_id, or None.

        The record carries the content_hash, the outcome (status,
        payload_hash, completed_action_ids, failed_action_id,
        stopped_at, stop_reason), the age in seconds ("age_s"), and
        "expired" (True while the id is in its tombstone window — a
        submit with it would raise BATCH_EXPIRED). A "mismatch"
        holds the server's outcome when its payload_hash did not
        match what was sent (diagnostic only, CONS-020 P1-03). A
        purged id (older than 20 minutes) returns None: it is free
        again.
        """
        now = self._clock()
        self._purge_expired(now)
        record = self._records.get(batch_id)
        if record is None:
            return None
        age = now - record["created_at"]
        return {
            "batch_id": batch_id,
            "content_hash": record["content_hash"],
            "status": record["status"],
            "payload_hash": record["payload_hash"],
            "completed_action_ids": record["completed_action_ids"],
            "failed_action_id": record["failed_action_id"],
            "stopped_at": record["stopped_at"],
            "stop_reason": record["stop_reason"],
            "mismatch": record["mismatch"],
            "age_s": int(age),
            # Step 4 (CONS-020 P0-01): ">" mirrors the server.
            "expired": age > BATCH_RESULT_TTL_S,
        }


# C-01 (SPEC-BATCH-PERSIST-001 §2.9): the VM-side half of the batch
# safety mechanism. The phone-side BatchRegistry cannot prevent a
# double execution if the VM forgets what it sent and re-issues the
# same logical batch under a new ID — so the VM keeps a persistent
# record (an outbox) of every logical batch it has sent, with each
# batch's identity, its batch_id, and its outcome state.
#
# Fixed send order (SPEC §2.9): (1) generate the logical batch's
# persistent ID; (2) register the logical batch and its batch_id in
# the outbox; (3) confirm the outbox persist succeeded; (4) only
# then allow the network send; (5) after receiving the result,
# persistently update the outbox state. If the outbox persist
# fails, the batch is NOT sent. If the outbox is corrupt, missing,
# or unreadable, sending stops (fail-closed) — the outbox is never
# treated as an empty queue.
#
# A durable commit (SPEC §2.9, P1-B) is a crash-recoverable write:
# atomic write (temp file + flush + fsync + rename) plus a directory
# fsync. A write that only reaches the OS buffer is NOT a
# successful persist.
OUTBOX_FORMAT_VERSION = 1
OUTBOX_FILE = "batch_outbox.json"
OUTBOX_MARKER_FILE = "batch_outbox_initialized"
OUTBOX_DURABILITY_FILE = "batch_outbox_durability_verified"
OUTBOX_LOCK_FILE = "batch_outbox.lock"
# The storage method this build uses (SPEC §2.9): the durability
# marker must name this method; if the method ever changes,
# re-verification is required before sending resumes.
OUTBOX_STORAGE_METHOD = "file_atomic_v1"
OUTBOX_MAX_RECORDS = 1000
OUTBOX_DETERMINED_RETENTION_S = 24 * 60 * 60
OUTBOX_STATES = ("registered", "result_received", "outcome_unknown", "not_executed")


def default_outbox_dir() -> str:
    """The outbox directory: $MUSE_BRIDGE_OUTBOX_DIR, else
    ~/.muse-bridge. Runtime state, deliberately outside the git
    working tree."""
    override = os.environ.get("MUSE_BRIDGE_OUTBOX_DIR")
    if override:
        return override
    return os.path.expanduser("~/.muse-bridge")


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """Write a JSON file as a crash-recoverable durable commit: temp
    file in the same directory, flush, fsync the file, rename over
    the live file, fsync the directory entry. Raises OSError on any
    failure (the caller treats that as a persist failure).

    The temp file has a unique name per call (CONS-152 P1-03): two
    threads writing the same live path must not share one .tmp file
    and race the rename. A failed write unlinks its own temp file.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    # The directory-entry fsync makes the rename itself durable.
    # It is best-effort on platforms that do not support it, but on
    # this VM (Linux) it is expected to work; a failure here is a
    # persist failure, not something to silently ignore.
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _locked(method: Any) -> Any:
    """Run a BatchOutbox method under the instance RLock AND the
    cross-process file lock (CONS-153 P1-03): the RLock serializes
    threads sharing one instance; the file lock (flock LOCK_EX on
    batch_outbox.lock, held for the whole method) serializes
    separate instances and separate processes, so two processes
    registering different batches cannot lose each other's record
    in the read-modify-write. The file lock is re-entrant per
    thread (a depth counter): a @_locked method calling another
    @_locked method on the same thread does not deadlock on its
    own lock. A lock that cannot be acquired is fail-closed
    (OUTBOX_LOCK_FAILED) — never silently proceed unlocked."""
    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self._acquire_file_lock()
            try:
                return method(self, *args, **kwargs)
            finally:
                self._release_file_lock()
    return wrapper


class BatchOutbox:
    """Persistent outbox of logical batches (SPEC §2.9).

    One record per logical batch: its persistent logical_id, the
    batch_id sent for it, the content hash, the payload (tree_hash,
    actions, supersedes), and the outcome state:
    "registered" (persisted before the send; no outcome yet — an
    ambiguous record, treated as undetermined), "result_received"
    (a determined outcome), "outcome_unknown" (the outcome could
    not be determined — a lost response, a timeout, or the server's
    "unknown"), "not_executed" (a definitive rejection before any
    execution: 400/409/410/422, or a 503 with a SPEC error id).

    A record whose outcome is undetermined ("registered" or
    "outcome_unknown") is NEVER automatically re-issued under a new
    ID; a human decides. Undetermined records are never purged.
    """

    def __init__(
        self,
        directory: str | None = None,
        clock: Any = None,
    ) -> None:
        # clock is a zero-arg callable returning epoch seconds
        # (default time.time). It is injectable so tests can move
        # time forward without waiting. Epoch seconds, not a
        # monotonic clock: the outbox persists across restarts.
        self._clock = clock if clock is not None else time.time
        self._directory = directory if directory is not None else default_outbox_dir()
        self._outbox_path = os.path.join(self._directory, OUTBOX_FILE)
        self._marker_path = os.path.join(self._directory, OUTBOX_MARKER_FILE)
        self._durability_path = os.path.join(self._directory, OUTBOX_DURABILITY_FILE)
        self._lock_path = os.path.join(self._directory, OUTBOX_LOCK_FILE)
        # CONS-152 P1-03: one RLock serializes every file mutation
        # (initialize / require_ready / register / _update). Reads
        # go through the atomic rename, so they always see a whole
        # old or new file, never a torn one.
        self._lock = threading.RLock()
        # CONS-153 P1-03: the cross-process file lock (flock) held
        # while a @_locked method runs. _file_lock_fd is the open
        # lock-file descriptor (None when unlocked); _file_lock_depth
        # makes the lock re-entrant on the same thread. Both fields
        # are only touched while self._lock is held, so no extra
        # synchronization is needed for them.
        self._file_lock_fd: int | None = None
        self._file_lock_depth = 0

    def _acquire_file_lock(self) -> None:
        """Acquire the exclusive cross-process lock for this outbox
        directory (CONS-153 P1-03). Blocking LOCK_EX: the holder
        runs a whole read-modify-write; waiters queue behind it.
        The lock is released automatically if the holder process
        dies (the fd closes), so there is no stale-lock state to
        recover. Raises ProtocolError (fail-closed) when the lock
        cannot be opened or acquired."""
        if self._file_lock_depth > 0:
            self._file_lock_depth += 1
            return
        if fcntl is None:
            raise ProtocolError(
                "batch outbox file locking is unavailable on this "
                "platform — sending stops (fail-closed)",
                code="OUTBOX_LOCK_UNAVAILABLE",
            )
        os.makedirs(self._directory, exist_ok=True)
        try:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ProtocolError(
                f"outbox lock file cannot be opened: {exc}",
                code="OUTBOX_LOCK_FAILED",
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise ProtocolError(
                f"outbox file lock cannot be acquired: {exc}",
                code="OUTBOX_LOCK_FAILED",
            ) from exc
        self._file_lock_fd = fd
        self._file_lock_depth = 1

    def _release_file_lock(self) -> None:
        """Release one level of the cross-process lock (CONS-153
        P1-03). The underlying flock is only released when the
        re-entrancy depth reaches zero."""
        if self._file_lock_depth > 1:
            self._file_lock_depth -= 1
            return
        if self._file_lock_depth == 0:
            return
        self._file_lock_depth = 0
        fd = self._file_lock_fd
        self._file_lock_fd = None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    # ---- initialization and the startup decision table (SPEC §2.9) ----

    @_locked
    def initialize(self, attestation: str) -> dict[str, Any]:
        """The explicit operator initialization (for the both-missing
        case). Before calling this, the operator must verify from
        external records — the phone's execution history, or the
        operator's own logs — that NO logical batch was sent from
        this client. If that cannot be verified, do NOT initialize
        as fresh; recover the outbox from a backup instead. The
        initialization is logged in the marker (who/when/basis).

        Order: (1) create the outbox file (empty, with a format
        header) via a durable commit; (2) create the marker file
        (with the creation time and format version) via a durable
        commit. Because the marker is created AFTER the outbox,
        "marker exists" implies "outbox was created".
        """
        if not isinstance(attestation, str) or not attestation.strip():
            raise ProtocolError(
                "outbox initialization needs an attestation "
                "(who, when, and the basis — e.g. 'fresh install' or "
                "'loss accepted after checking the phone history')",
                code="OUTBOX_ATTESTATION_REQUIRED",
            )
        marker = self._load_marker()
        if marker is not None:
            raise ProtocolError(
                "outbox is already initialized",
                code="OUTBOX_ALREADY_INITIALIZED",
            )
        records = self._load_records()
        if records:
            # Marker missing + a non-empty outbox: contradictory.
            # The marker was lost after sends happened. Fail-closed.
            raise ProtocolError(
                "outbox has records but no init marker — the marker "
                "was lost after sends happened; refusing to initialize "
                "as fresh (recover the outbox from a backup instead)",
                code="OUTBOX_CONTRADICTORY",
            )
        self._persist_records([])
        marker_data = {
            "format_version": OUTBOX_FORMAT_VERSION,
            "created_at": int(self._clock()),
            "attestation": attestation.strip(),
        }
        try:
            _atomic_write_json(self._marker_path, marker_data)
        except OSError as exc:
            raise ProtocolError(
                f"outbox init marker persist failed: {exc}",
                code="OUTBOX_PERSIST_FAILED",
            ) from exc
        return dict(marker_data)

    @_locked
    def require_ready(self) -> dict[str, Any]:
        """The startup decision table (SPEC §2.9). Returns a summary
        when sending is allowed; raises ProtocolError otherwise.

        marker + readable outbox -> normal operation.
        marker + missing/corrupt/unreadable outbox -> fail-closed.
        neither -> explicit operator initialization required
            (the client does NOT auto-initialize).
        marker missing + empty outbox -> complete initialization by
            creating the marker, then start with an empty outbox.
        marker missing + non-empty outbox -> fail-closed.

        On top of the table, the storage-durability gate (CONS-152
        P1-02): sending is allowed only after the operator records
        the durability verification (see record_durability_verified).
        The gate ships ON.
        """
        marker = self._load_marker()
        records = self._load_records()
        if marker is not None:
            if records is None:
                raise ProtocolError(
                    "outbox is initialized but the outbox file is "
                    "missing, corrupt, or unreadable — sending stops "
                    "(fail-closed); recover the outbox from a backup",
                    code="OUTBOX_CORRUPT",
                )
            self._require_durability_verified()
            return self._summary(records, initialized=True,
                                 durability_verified=True,
                                 durability_method=OUTBOX_STORAGE_METHOD)
        # No marker.
        if records is None:
            raise ProtocolError(
                "batch outbox is not initialized (no init marker and "
                "no outbox file) — sending is refused until the "
                "operator performs an explicit initialization "
                "(batch-outbox-init) after verifying from external "
                "records that no logical batch was sent from this "
                "client",
                code="OUTBOX_NOT_INITIALIZED",
            )
        if records:
            raise ProtocolError(
                "outbox has records but no init marker — the marker "
                "was lost after sends happened; sending stops "
                "(fail-closed); recover the outbox from a backup",
                code="OUTBOX_CONTRADICTORY",
            )
        # Marker missing + an empty outbox: initialization stopped
        # midway (the outbox was created but the marker was not).
        # No sends could have happened before initialization
        # completed, so completing it is safe.
        marker_data = {
            "format_version": OUTBOX_FORMAT_VERSION,
            "created_at": int(self._clock()),
            "attestation": "initialization completed at startup: "
            "outbox existed and was empty, so no batch could have been sent",
        }
        try:
            _atomic_write_json(self._marker_path, marker_data)
        except OSError as exc:
            raise ProtocolError(
                f"outbox init marker persist failed: {exc}",
                code="OUTBOX_PERSIST_FAILED",
            ) from exc
        self._require_durability_verified()
        return self._summary(records, initialized=True,
                             durability_verified=True,
                             durability_method=OUTBOX_STORAGE_METHOD)

    # ---- storage-durability gate (SPEC §2.9, CONS-152 P1-02) ----

    @_locked
    def record_durability_verified(self, method: str, note: str) -> dict[str, Any]:
        """Record that the outbox storage durability has been
        demonstrated (the C-01-15 crash test: kill the VM process
        after an outbox write and confirm the record survives, on
        this VM's filesystem). The marker records the storage
        method it was verified for; if the method changes,
        re-verification is required before sending resumes. The
        gate ships ON: no marker means no send."""
        if method != OUTBOX_STORAGE_METHOD:
            raise ProtocolError(
                f"unknown outbox storage method: {method!r} "
                f"(this build uses {OUTBOX_STORAGE_METHOD!r})",
                code="OUTBOX_UNKNOWN_STORAGE_METHOD",
            )
        marker_data = {
            "format_version": OUTBOX_FORMAT_VERSION,
            "method": method,
            "verified_at": int(self._clock()),
            "note": note,
        }
        try:
            _atomic_write_json(self._durability_path, marker_data)
        except OSError as exc:
            raise ProtocolError(
                f"outbox durability marker persist failed: {exc}",
                code="OUTBOX_PERSIST_FAILED",
            ) from exc
        return dict(marker_data)

    def is_durability_verified(self) -> bool:
        """True when a valid durability marker names this build's
        storage method. A missing, unreadable, or invalid marker
        means unverified (fail-closed)."""
        marker = self._load_durability_marker()
        return marker is not None and marker.get("method") == OUTBOX_STORAGE_METHOD

    def _require_durability_verified(self) -> None:
        if not self.is_durability_verified():
            raise ProtocolError(
                "batch outbox storage durability is not verified — "
                "sending is refused until the operator records it "
                "(batch-outbox-durability-verify) after the C-01-15 "
                "crash-durability check",
                code="OUTBOX_DURABILITY_UNVERIFIED",
            )

    def _load_durability_marker(self) -> dict[str, Any] | None:
        """Read the durability marker. Returns None when it is
        missing, unreadable, or invalid (all mean unverified).
        Missing is decided by the open itself (FileNotFoundError):
        os.path.exists() returns False both when the file does not
        exist AND when its existence cannot be determined, so it
        must never decide absence (CONS-153 P1-01, same class as
        the phone-side readFileText fix). An undeterminable status
        also lands here as None — unverified — which is the
        fail-closed direction for this marker."""
        try:
            with open(self._durability_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("format_version") != OUTBOX_FORMAT_VERSION:
            return None
        if not isinstance(data.get("method"), str):
            return None
        if not isinstance(data.get("verified_at"), int):
            return None
        return data

    # ---- the fixed send order (SPEC §2.9) ----

    @_locked
    def register(
        self,
        logical_id: str,
        batch_id: str,
        content_hash: str,
        tree_hash: int,
        actions: list[dict[str, Any]],
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        """Step (2) of the fixed send order: register the logical
        batch and its batch_id in the outbox and confirm the durable
        persist succeeded. Only then may the caller send (step 4).
        Raises on a persist failure (no send) and on a reused
        batch_id (the permanent reuse ban, SPEC §2.9). Refuses to
        register while the storage durability is unverified
        (CONS-152 P1-02)."""
        records = self._load_records()
        if records is None:
            raise ProtocolError(
                "outbox file is missing or unreadable — sending stops "
                "(fail-closed)",
                code="OUTBOX_CORRUPT",
            )
        self._require_durability_verified()
        for record in records:
            if record["batch_id"] == batch_id:
                raise ProtocolError(
                    f"batch_id {batch_id!r} was already sent; the VM "
                    "never reuses a batch_id",
                    code="BATCH_ID_REUSED",
                )
            if record["logical_id"] == logical_id:
                raise ProtocolError(
                    f"logical batch {logical_id!r} is already registered",
                    code="LOGICAL_BATCH_REUSED",
                )
        now = int(self._clock())
        record = {
            "logical_id": logical_id,
            "batch_id": batch_id,
            "content_hash": content_hash,
            "tree_hash": tree_hash,
            "actions": actions,
            "supersedes": supersedes,
            "state": "registered",
            "created_at": now,
            "sent_at": None,
            "result": None,
            "note": None,
        }
        records.append(record)
        self._purge_determined_old(records, now)
        self._persist_records(records)
        return dict(record)

    def mark_result(self, logical_id: str, result: dict[str, Any]) -> bool:
        """A determined outcome was received (completed / partial /
        failed). Returns False when the persist failed — the record
        stays undetermined, which is the safe state."""
        return self._update(
            logical_id,
            {"state": "result_received", "result": result, "sent_at": int(self._clock())},
        )

    def mark_unknown(self, logical_id: str, note: str | None = None) -> bool:
        """The outcome could not be determined (a lost response, a
        timeout, or the server's "unknown"). The logical batch stays
        undetermined and is never automatically re-issued."""
        update: dict[str, Any] = {"state": "outcome_unknown", "sent_at": int(self._clock())}
        if note is not None:
            update["note"] = note
        return self._update(logical_id, update)

    def mark_not_executed(self, logical_id: str, error_id: str | None = None) -> bool:
        """A definitive rejection before any execution (400/409/410/
        422, or a 503 with a SPEC error id). The batch is known not
        to have run."""
        update: dict[str, Any] = {"state": "not_executed", "sent_at": int(self._clock())}
        if error_id is not None:
            update["note"] = error_id
        return self._update(logical_id, update)

    # ---- reads (for the operator and for batch()) ----

    def get(self, logical_id: str) -> dict[str, Any] | None:
        """Return the outbox record for a logical batch, or None."""
        records = self._load_records()
        if records is None:
            return None
        for record in records:
            if record["logical_id"] == logical_id:
                return dict(record)
        return None

    def get_by_batch_id(self, batch_id: str) -> dict[str, Any] | None:
        """Return the outbox record for a batch_id, or None."""
        records = self._load_records()
        if records is None:
            return None
        for record in records:
            if record["batch_id"] == batch_id:
                return dict(record)
        return None

    def list_undetermined(self) -> list[dict[str, Any]]:
        """Records whose outcome is undetermined ("registered" or
        "outcome_unknown") — the ones a human must decide about.
        An empty outbox (or an unreadable one) returns []."""
        records = self._load_records()
        if records is None:
            return []
        return [
            dict(record)
            for record in records
            if record["state"] in ("registered", "outcome_unknown")
        ]

    def summary(self) -> dict[str, Any]:
        """Counts by state, for the operator. An unreadable outbox
        reports initialized=False and no counts. The durability
        gate state is included (it never raises here — status must
        stay readable even when sending is refused)."""
        marker = self._load_marker()
        records = self._load_records()
        durability = self._load_durability_marker()
        if records is None:
            return {
                "initialized": marker is not None,
                "directory": self._directory,
                "records": 0,
                "by_state": {},
                "undetermined": 0,
                "durability_verified": False,
                "durability_method": None,
            }
        return self._summary(
            records,
            initialized=marker is not None,
            durability_verified=durability is not None
            and durability.get("method") == OUTBOX_STORAGE_METHOD,
            durability_method=durability.get("method")
            if durability is not None else None,
        )

    # ---- internals ----

    def _summary(
        self, records: list[dict[str, Any]], initialized: bool,
        durability_verified: bool = False,
        durability_method: str | None = None,
    ) -> dict[str, Any]:
        by_state: dict[str, int] = {}
        for record in records:
            by_state[record["state"]] = by_state.get(record["state"], 0) + 1
        return {
            "initialized": initialized,
            "directory": self._directory,
            "records": len(records),
            "by_state": by_state,
            "undetermined": sum(
                1
                for record in records
                if record["state"] in ("registered", "outcome_unknown")
            ),
            "durability_verified": durability_verified,
            "durability_method": durability_method,
        }

    @_locked
    def _update(self, logical_id: str, update: dict[str, Any]) -> bool:
        """Apply an update to one record and persist it. Returns
        False (instead of raising) when the record is missing or
        the persist failed — the record stays in its previous state,
        which is the safe direction for an undetermined outcome."""
        records = self._load_records()
        if records is None:
            return False
        for record in records:
            if record["logical_id"] == logical_id:
                record.update(update)
                try:
                    self._persist_records(records)
                except ProtocolError:
                    return False
                return True
        return False

    def _purge_determined_old(
        self, records: list[dict[str, Any]], now: int
    ) -> None:
        """Capacity: keep the outbox bounded without ever deleting
        an undetermined record. When over the cap, drop determined
        records older than 24 h, oldest first."""
        if len(records) <= OUTBOX_MAX_RECORDS:
            return
        determined_old = [
            record
            for record in records
            if record["state"] in ("result_received", "not_executed")
            and now - record["created_at"] > OUTBOX_DETERMINED_RETENTION_S
        ]
        determined_old.sort(key=lambda r: r["created_at"])
        excess = len(records) - OUTBOX_MAX_RECORDS
        for record in determined_old[:excess]:
            records.remove(record)

    def _persist_records(self, records: list[dict[str, Any]]) -> None:
        """Write the whole outbox as a durable commit. Raises
        ProtocolError (code OUTBOX_PERSIST_FAILED) on failure —
        the caller must not send when this fails."""
        try:
            _atomic_write_json(
                self._outbox_path,
                {"format_version": OUTBOX_FORMAT_VERSION, "records": records},
            )
        except OSError as exc:
            raise ProtocolError(
                f"outbox persist failed: {exc}",
                code="OUTBOX_PERSIST_FAILED",
            ) from exc

    def _load_records(self) -> list[dict[str, Any]] | None:
        """Read and validate the outbox file. Returns None when the
        file is missing; raises ProtocolError (OUTBOX_CORRUPT) when
        it is unreadable or invalid. Unknown extra fields are
        ignored (forward compatibility). Missing is decided by the
        open itself (FileNotFoundError): os.path.exists() returns
        False both when the file does not exist AND when its
        existence cannot be determined, so it must never decide
        absence (CONS-153 P1-01, same class as the phone-side
        readFileText fix) — an undeterminable outbox must raise
        OUTBOX_CORRUPT, never be treated as an empty queue."""
        try:
            with open(self._outbox_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ProtocolError(
                f"outbox file is unreadable: {exc}",
                code="OUTBOX_CORRUPT",
            ) from exc
        if not isinstance(data, dict):
            raise ProtocolError("outbox file must be a JSON object", code="OUTBOX_CORRUPT")
        if data.get("format_version") != OUTBOX_FORMAT_VERSION:
            raise ProtocolError(
                f"unsupported outbox format_version: {data.get('format_version')!r}",
                code="OUTBOX_CORRUPT",
            )
        records = data.get("records")
        if not isinstance(records, list):
            raise ProtocolError("outbox records must be a list", code="OUTBOX_CORRUPT")
        seen_logical: set[str] = set()
        seen_batch: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ProtocolError("outbox record must be an object", code="OUTBOX_CORRUPT")
            for key in ("logical_id", "batch_id", "content_hash", "state", "created_at"):
                if key not in record:
                    raise ProtocolError(
                        f"outbox record missing required field {key!r}",
                        code="OUTBOX_CORRUPT",
                    )
            if record["state"] not in OUTBOX_STATES:
                raise ProtocolError(
                    f"outbox record has an invalid state: {record['state']!r}",
                    code="OUTBOX_CORRUPT",
                )
            if record["logical_id"] in seen_logical or record["batch_id"] in seen_batch:
                raise ProtocolError(
                    "outbox has a duplicate logical_id or batch_id",
                    code="OUTBOX_CORRUPT",
                )
            seen_logical.add(record["logical_id"])
            seen_batch.add(record["batch_id"])
        return records

    def _load_marker(self) -> dict[str, Any] | None:
        """Read the init marker. Returns None when it is missing;
        raises ProtocolError (OUTBOX_CORRUPT) when it is unreadable
        or invalid. Missing is decided by the open itself
        (FileNotFoundError) — see _load_records for why
        os.path.exists() must not decide absence (CONS-153 P1-01)."""
        try:
            with open(self._marker_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ProtocolError(
                f"outbox init marker is unreadable: {exc}",
                code="OUTBOX_CORRUPT",
            ) from exc
        if not isinstance(data, dict):
            raise ProtocolError(
                "outbox init marker must be a JSON object", code="OUTBOX_CORRUPT"
            )
        if data.get("format_version") != OUTBOX_FORMAT_VERSION:
            raise ProtocolError(
                f"unsupported outbox marker format_version: {data.get('format_version')!r}",
                code="OUTBOX_CORRUPT",
            )
        if not isinstance(data.get("attestation"), str) or not data["attestation"].strip():
            raise ProtocolError(
                "outbox init marker has no attestation", code="OUTBOX_CORRUPT"
            )
        return data
