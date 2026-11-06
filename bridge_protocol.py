"""muse-bridge HTTP communication protocol models and validators (SPEC-POC-001).

Pure standard-library implementation with no network or socket operations.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

DEFAULT_PORT = 8080
TOKEN_HEADER = "X-Bridge-Token"

MAX_BODY_BYTES = 262144
MAX_HEADER_BYTES = 8192
MAX_EVENTS_LIMIT = 200

HEADER_TIMEOUT_SEC = 5
BODY_TIMEOUT_SEC = 10
TOTAL_TIMEOUT_SEC = 45

MAX_WORKERS = 4
MAX_QUEUE = 8
MAX_CONNECTIONS = 16

EXPECT_CHANGE_MAX_WAIT_MS = 2000

VALID_KEYS = ("HOME", "BACK", "RECENTS", "POWER")
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


def _require_optional_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a str or None")
    return value


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


def build_click(x: int, y: int) -> dict[str, Any]:
    _require_int(x, "x", min_value=0)
    _require_int(y, "y", min_value=0)
    return validate_body_size({"x": x, "y": y})


def build_swipe(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int = 300,
) -> dict[str, Any]:
    _require_int(x1, "x1", min_value=0)
    _require_int(y1, "y1", min_value=0)
    _require_int(x2, "x2", min_value=0)
    _require_int(y2, "y2", min_value=0)
    _require_int(duration_ms, "duration_ms", min_value=0)
    return validate_body_size(
        {
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "duration_ms": duration_ms,
        }
    )


def build_type_text(text: str) -> dict[str, Any]:
    _require_str(text, "text")
    return validate_body_size({"text": text})


def build_press_key(key: str) -> dict[str, Any]:
    _require_str(key, "key", non_empty=True)
    if key not in VALID_KEYS:
        raise ProtocolError(f"invalid key: {key!r}")
    return validate_body_size({"key": key})


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
        out["x"] = _require_int(action["x"], f"actions[{index}].x", min_value=0)
        out["y"] = _require_int(action["y"], f"actions[{index}].y", min_value=0)
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
                action[coord], f"actions[{index}].{coord}", min_value=0
            )
        if "duration_ms" in action:
            out["duration_ms"] = _require_int(
                action["duration_ms"], f"actions[{index}].duration_ms", min_value=0
            )
    elif action_type == "wait":
        allowed_keys.add("wait_ms")
        if "wait_ms" not in action:
            raise ProtocolError(f"actions[{index}] missing required field 'wait_ms'")
        out["wait_ms"] = _require_int(
            action["wait_ms"], f"actions[{index}].wait_ms", min_value=0
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
) -> dict[str, Any]:
    _require_str(batch_id, "batch_id", non_empty=True)
    _require_int(tree_hash, "tree_hash")
    if not isinstance(actions, list) or len(actions) == 0:
        raise ProtocolError("actions must be a non-empty list")
    if len(actions) > 20:
        raise ProtocolError("actions must contain at most 20 actions")
    validated_actions = [
        _validate_batch_action(action, idx) for idx, action in enumerate(actions)
    ]
    return validate_body_size(
        {
            "batch_id": batch_id,
            "tree_hash": tree_hash,
            "actions": validated_actions,
        }
    )


def build_notify(title: str, text: str) -> dict[str, Any]:
    _require_str(title, "title")
    _require_str(text, "text")
    return validate_body_size({"title": title, "text": text})


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
    required_keys = (
        "ok",
        "batch_id",
        "replayed",
        "results",
        "stopped_at",
        "stop_reason",
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


class BatchRegistry:
    """Local idempotency registry for POST /batch requests."""

    def __init__(self) -> None:
        self._records: dict[str, str] = {}

    @staticmethod
    def compute_content_hash(tree_hash: int, actions: list[dict[str, Any]]) -> str:
        canonical_obj = {"tree_hash": tree_hash, "actions": actions}
        canonical_json = json.dumps(
            canonical_obj,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def submit(
        self,
        batch_id: str,
        tree_hash: int,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        validated = build_batch(batch_id, tree_hash, actions)
        content_hash = self.compute_content_hash(
            validated["tree_hash"], validated["actions"]
        )
        if batch_id not in self._records:
            self._records[batch_id] = content_hash
            return {
                "batch_id": batch_id,
                "replayed": False,
                "content_hash": content_hash,
            }
        if self._records[batch_id] == content_hash:
            return {
                "batch_id": batch_id,
                "replayed": True,
                "content_hash": content_hash,
            }
        raise ProtocolError(
            f"batch_id conflict for {batch_id!r}: existing content_hash does not match",
            code="STALE_REVISION",
        )
