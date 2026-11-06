"""Unit tests for muse-bridge-poc protocol module (SPEC-POC-001)."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import bridge_protocol as protocol
from bridge_protocol import (
     MAX_BODY_BYTES,
    MAX_EVENTS_LIMIT,
    MAX_HEADER_BYTES,
    BatchRegistry,
    ProtocolError,
    build_batch,
    build_capture_screen_query,
    build_click,
    build_click_node,
    build_events_query,
    build_notify,
    build_press_key,
    build_swipe,
    build_type_node,
    build_type_text,
    error_code_for_status,
    parse_batch_result,
    parse_error_body,
    parse_events,
    parse_health,
    parse_state,
    validate_body_size,
    validate_headers_size,
)

DUMMY_TOKEN = "test-only-token"


def _sample_node() -> dict:
    return {
        "id": "n1",
        "parent": None,
        "class": "android.widget.EditText",
        "text": "hello",
        "desc": "input field",
        "bounds": {"left": 0, "top": 100, "right": 500, "bottom": 200},
        "clickable": True,
        "focusable": True,
        "focused": True,
        "editable": True,
        "scrollable": False,
        "enabled": True,
        "selected": False,
        "password": False,
        "input_type": 1,
        "resource_id": "com.example:id/input",
        "actions": ["ACTION_CLICK", "ACTION_SET_TEXT"],
    }


def _sample_event(event_type: str = "TYPE_VIEW_CLICKED") -> dict:
    return {
        "seq": 10,
        "t": 1700000000000,
        "type": event_type,
        "package": "com.example.app",
        "class": "android.widget.Button",
        "text": "Send",
        "desc": "send button",
        "bounds": {"left": 10, "top": 20, "right": 110, "bottom": 70},
        "coalesced": 1,
    }


class TestProtocolConstantsAndLimits(unittest.TestCase):
    def test_constants_values(self) -> None:
        self.assertEqual(MAX_BODY_BYTES, 262144)
        self.assertEqual(MAX_HEADER_BYTES, 8192)
        self.assertEqual(MAX_EVENTS_LIMIT, 200)
        self.assertTrue(issubclass(ProtocolError, ValueError))

    def test_body_size_limit_enforced(self) -> None:
        oversized_text = "a" * (MAX_BODY_BYTES + 10)
        with self.assertRaises(ValueError):
            build_type_text(oversized_text)

    def test_header_size_limit_enforced(self) -> None:
        valid_headers = {protocol.TOKEN_HEADER: DUMMY_TOKEN}
        self.assertEqual(validate_headers_size(valid_headers), valid_headers)
        oversized_headers = {
            protocol.TOKEN_HEADER: DUMMY_TOKEN,
            "X-Extra-Padding": "x" * (MAX_HEADER_BYTES + 10),
        }
        with self.assertRaises(ValueError):
            validate_headers_size(oversized_headers)


class TestBuildFunctionsValid(unittest.TestCase):
    def test_build_click_valid(self) -> None:
        self.assertEqual(build_click(0, 0), {"x": 0, "y": 0})
        self.assertEqual(build_click(540, 1200), {"x": 540, "y": 1200})

    def test_build_swipe_valid(self) -> None:
        self.assertEqual(
            build_swipe(10, 20, 30, 40),
            {"x1": 10, "y1": 20, "x2": 30, "y2": 40, "duration_ms": 300},
        )
        self.assertEqual(
            build_swipe(0, 0, 100, 200, duration_ms=500),
            {"x1": 0, "y1": 0, "x2": 100, "y2": 200, "duration_ms": 500},
        )

    def test_build_type_text_valid(self) -> None:
        self.assertEqual(build_type_text("こんにちは"), {"text": "こんにちは"})

    def test_build_press_key_valid(self) -> None:
        for key in ("HOME", "BACK", "RECENTS", "POWER"):
            self.assertEqual(build_press_key(key), {"key": key})

    def test_build_click_node_valid(self) -> None:
        self.assertEqual(
            build_click_node("node_42", 123456),
            {"node_id": "node_42", "tree_hash": 123456},
        )

    def test_build_type_node_valid(self) -> None:
        self.assertEqual(
            build_type_node("node_42", -98765, "sample"),
            {"node_id": "node_42", "tree_hash": -98765, "text": "sample"},
        )

    def test_build_batch_valid_all_7_action_types(self) -> None:
        actions = [
            {"action_id": "a1", "type": "click_node", "node_id": "n1", "expect_change": True},
            {"action_id": "a2", "type": "tap", "x": 100, "y": 200},
            {"action_id": "a3", "type": "type_node", "node_id": "n2", "text": "abc"},
            {"action_id": "a4", "type": "type", "text": "def"},
            {"action_id": "a5", "type": "key", "key": "BACK"},
            {"action_id": "a6", "type": "swipe", "x1": 10, "y1": 20, "x2": 30, "y2": 40, "duration_ms": 250},
            {"action_id": "a7", "type": "wait", "wait_ms": 500},
        ]
        built = build_batch("batch-001", 999, actions)
        self.assertEqual(built["batch_id"], "batch-001")
        self.assertEqual(built["tree_hash"], 999)
        self.assertEqual(len(built["actions"]), 7)
        self.assertEqual(built["actions"], actions)

    def test_build_notify_valid(self) -> None:
        self.assertEqual(
            build_notify("Status", "Completed"),
            {"title": "Status", "text": "Completed"},
        )

    def test_build_capture_screen_query_valid(self) -> None:
        self.assertEqual(
            build_capture_screen_query(),
            "scale=0.5&format=jpeg&quality=80",
        )
        self.assertEqual(
            build_capture_screen_query(scale=1.0, format="png", quality=100),
            "scale=1.0&format=png&quality=100",
        )
        self.assertEqual(
            build_capture_screen_query(scale=0.1, format="jpg", quality=10),
            "scale=0.1&format=jpg&quality=10",
        )

    def test_build_events_query_valid(self) -> None:
        self.assertEqual(build_events_query(), "since=0&limit=100")
        self.assertEqual(build_events_query(since=42, limit=200), "since=42&limit=200")
        self.assertEqual(build_events_query(since=0, limit=1), "since=0&limit=1")


class TestBuildFunctionsInvalid(unittest.TestCase):
    def test_build_click_negative_or_wrong_type(self) -> None:
        with self.assertRaises(ValueError):
            build_click(-1, 10)
        with self.assertRaises(ValueError):
            build_click(10, -5)
        with self.assertRaises(ValueError):
            build_click(True, 10)  # bool must be rejected as int
        with self.assertRaises(ValueError):
            build_click(10.5, 10)  # type: ignore[arg-type]

    def test_build_swipe_negative_coordinates_or_duration(self) -> None:
        with self.assertRaises(ValueError):
            build_swipe(-1, 0, 10, 10)
        with self.assertRaises(ValueError):
            build_swipe(0, -1, 10, 10)
        with self.assertRaises(ValueError):
            build_swipe(0, 0, -1, 10)
        with self.assertRaises(ValueError):
            build_swipe(0, 0, 10, -1)
        with self.assertRaises(ValueError):
            build_swipe(0, 0, 10, 10, duration_ms=-10)
        with self.assertRaises(ValueError):
            build_swipe(0, 0, 10, 10, duration_ms=False)  # bool

    def test_build_type_text_wrong_type(self) -> None:
        with self.assertRaises(ValueError):
            build_type_text(None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            build_type_text(123)  # type: ignore[arg-type]

    def test_build_press_key_unknown_or_wrong_type(self) -> None:
        with self.assertRaises(ValueError):
            build_press_key("VOLUME_UP")
        with self.assertRaises(ValueError):
            build_press_key("")
        with self.assertRaises(ValueError):
            build_press_key(123)  # type: ignore[arg-type]

    def test_build_click_node_empty_node_id_or_bad_hash(self) -> None:
        with self.assertRaises(ValueError):
            build_click_node("", 123)
        with self.assertRaises(ValueError):
            build_click_node("   ", 123)
        with self.assertRaises(ValueError):
            build_click_node("n1", "not-an-int")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            build_click_node("n1", True)

    def test_build_type_node_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            build_type_node("", 123, "hello")
        with self.assertRaises(ValueError):
            build_type_node("n1", "123", "hello")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            build_type_node("n1", 123, None)  # type: ignore[arg-type]

    def test_build_batch_empty_batch_id_or_empty_actions(self) -> None:
        valid_action = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
        with self.assertRaises(ValueError):
            build_batch("", 100, valid_action)
        with self.assertRaises(ValueError):
            build_batch("   ", 100, valid_action)
        with self.assertRaises(ValueError):
            build_batch("b1", 100, [])
        with self.assertRaises(ValueError):
            build_batch("b1", "not-int", valid_action)  # type: ignore[arg-type]

    def test_build_batch_unknown_action_type_and_missing_fields(self) -> None:
        # Unknown action type
        with self.assertRaises(ValueError):
            build_batch("b1", 1, [{"action_id": "a1", "type": "double_tap", "x": 1, "y": 2}])
        # Missing required field for tap (missing y)
        with self.assertRaises(ValueError):
            build_batch("b1", 1, [{"action_id": "a1", "type": "tap", "x": 10}])
        # Empty node_id in click_node
        with self.assertRaises(ValueError):
            build_batch("b1", 1, [{"action_id": "a1", "type": "click_node", "node_id": ""}])
        # Negative coordinate in tap
        with self.assertRaises(ValueError):
            build_batch("b1", 1, [{"action_id": "a1", "type": "tap", "x": -1, "y": 0}])
        # Negative wait_ms in wait
        with self.assertRaises(ValueError):
            build_batch("b1", 1, [{"action_id": "a1", "type": "wait", "wait_ms": -5}])
        # Negative duration_ms in swipe
        with self.assertRaises(ValueError):
            build_batch(
                "b1",
                1,
                [{"action_id": "a1", "type": "swipe", "x1": 0, "y1": 0, "x2": 10, "y2": 10, "duration_ms": -1}],
            )
        # Unknown key in batch key action
        with self.assertRaises(ValueError):
            build_batch("b1", 1, [{"action_id": "a1", "type": "key", "key": "ENTER"}])

    def test_build_notify_invalid_types(self) -> None:
        with self.assertRaises(ValueError):
            build_notify(123, "text")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            build_notify("title", None)  # type: ignore[arg-type]

    def test_build_capture_screen_query_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            build_capture_screen_query(scale=0.05)
        with self.assertRaises(ValueError):
            build_capture_screen_query(scale=1.1)
        with self.assertRaises(ValueError):
            build_capture_screen_query(format="webp")
        with self.assertRaises(ValueError):
            build_capture_screen_query(quality=9)
        with self.assertRaises(ValueError):
            build_capture_screen_query(quality=101)
        with self.assertRaises(ValueError):
            build_capture_screen_query(scale=True)  # type: ignore[arg-type]

    def test_build_events_query_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            build_events_query(since=-1, limit=100)
        with self.assertRaises(ValueError):
            build_events_query(since=0, limit=0)
        with self.assertRaises(ValueError):
            build_events_query(since=0, limit=201)
        with self.assertRaises(ValueError):
            build_events_query(since=True, limit=100)


class TestParseFunctionsValid(unittest.TestCase):
    def test_parse_health_valid(self) -> None:
        raw = {
            "ok": True,
            "service": "muse-bridge",
            "version": 1,
            "app_version": "1.9.0",
            "app_version_code": 19,
            "accessibility": True,
        }
        self.assertEqual(parse_health(raw), raw)

    def test_parse_state_valid(self) -> None:
        raw = {
            "ok": True,
            "app_version": "1.9.0",
            "app_version_code": 19,
            "width": 1080,
            "height": 2400,
            "package": "com.example.app",
            "texts": ["hello", "world"],
            "revision": 12,
            "tree_hash": -1029384756,
            "nodes": [_sample_node()],
            "focused": "n1",
            "ime_visible": True,
            "event_seq": 45,
            "truncated": False,
        }
        self.assertEqual(parse_state(raw), raw)

    def test_parse_events_valid_all_7_types(self) -> None:
        events = [_sample_event(t) for t in protocol.VALID_EVENT_TYPES]
        raw = {
            "ok": True,
            "events": events,
            "latest_seq": 16,
            "dropped": False,
            "tree_hash": 555666,
            "revision": 8,
        }
        self.assertEqual(parse_events(raw), raw)

    def test_parse_batch_result_valid(self) -> None:
        raw = {
            "ok": True,
            "batch_id": "b-100",
            "replayed": False,
            "results": [
                {
                    "action_id": "a1",
                    "status": "executed",
                    "error": None,
                    "hash_before": 111,
                    "hash_after": 222,
                },
                {
                    "action_id": "a2",
                    "status": "failed",
                    "error": "node not found",
                    "hash_before": 222,
                    "hash_after": 222,
                },
            ],
            "stopped_at": 1,
            "stop_reason": "action_failed",
        }
        self.assertEqual(parse_batch_result(raw), raw)


class TestParseFunctionsInvalid(unittest.TestCase):
    def test_parse_health_missing_or_wrong_type(self) -> None:
        base = {
            "ok": True,
            "service": "muse-bridge",
            "version": 1,
            "app_version": "1.9.0",
            "app_version_code": 19,
            "accessibility": True,
        }
        # Missing field
        missing = dict(base)
        del missing["accessibility"]
        with self.assertRaises(ValueError):
            parse_health(missing)
        # Wrong service
        wrong_service = dict(base, service="other-service")
        with self.assertRaises(ValueError):
            parse_health(wrong_service)
        # Wrong type for version
        wrong_ver = dict(base, version="1")
        with self.assertRaises(ValueError):
            parse_health(wrong_ver)

    def test_parse_state_missing_field_or_invalid_node(self) -> None:
        base = {
            "ok": True,
            "app_version": "1.9.0",
            "app_version_code": 19,
            "width": 1080,
            "height": 2400,
            "package": "com.example.app",
            "texts": ["hello"],
            "revision": 12,
            "tree_hash": 123,
            "nodes": [_sample_node()],
            "focused": None,
            "ime_visible": False,
            "event_seq": 1,
            "truncated": False,
        }
        # Missing node field
        bad_node = _sample_node()
        del bad_node["resource_id"]
        with self.assertRaises(ValueError):
            parse_state(dict(base, nodes=[bad_node]))
        # Wrong type in node bounds
        bad_bounds_node = _sample_node()
        bad_bounds_node["bounds"] = {"left": "0", "top": 0, "right": 10, "bottom": 10}
        with self.assertRaises(ValueError):
            parse_state(dict(base, nodes=[bad_bounds_node]))

    def test_parse_events_unknown_type_and_limit_exceeded(self) -> None:
        base = {
            "ok": True,
            "events": [_sample_event("TYPE_UNKNOWN_EVENT")],
            "latest_seq": 10,
            "dropped": False,
            "tree_hash": 123,
            "revision": 1,
        }
        with self.assertRaises(ValueError):
            parse_events(base)

        # Exceeding MAX_EVENTS_LIMIT (200)
        too_many = dict(
            base,
            events=[_sample_event() for _ in range(MAX_EVENTS_LIMIT + 1)],
        )
        with self.assertRaises(ValueError):
            parse_events(too_many)

        # Out-of-range limit field on response
        bad_limit = dict(
            base,
            events=[_sample_event()],
            limit=201,
        )
        with self.assertRaises(ValueError):
            parse_events(bad_limit)

    def test_parse_batch_result_unknown_status_or_missing_field(self) -> None:
        bad_status = {
            "ok": True,
            "batch_id": "b-100",
            "replayed": False,
            "results": [
                {
                    "action_id": "a1",
                    "status": "skipped",  # invalid status
                    "error": None,
                    "hash_before": 111,
                    "hash_after": 222,
                }
            ],
            "stopped_at": -1,
            "stop_reason": None,
        }
        with self.assertRaises(ValueError):
            parse_batch_result(bad_status)

        missing_field = dict(bad_status)
        del missing_field["stopped_at"]
        with self.assertRaises(ValueError):
            parse_batch_result(missing_field)


class TestErrorCodesAndErrorBody(unittest.TestCase):
    def test_error_code_for_status_all_mappings(self) -> None:
        expected = {
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
            599: "INTERNAL_ERROR",
        }
        for status, code in expected.items():
            with self.subTest(status=status):
                self.assertEqual(error_code_for_status(status), code)

    def test_parse_error_body_with_and_without_status_and_invalid_json(self) -> None:
        self.assertEqual(
            parse_error_body({"ok": False, "error": "stale tree_hash", "status": 409}),
            {"ok": False, "error": "stale tree_hash", "code": "STALE_REVISION"},
        )
        self.assertEqual(
            parse_error_body({"ok": False, "error": "unexpected error"}),
            {"ok": False, "error": "unexpected error", "code": "INTERNAL_ERROR"},
        )
        invalid_json_res = parse_error_body("{invalid json")
        self.assertIs(invalid_json_res["ok"], False)
        self.assertEqual(invalid_json_res["code"], "INVALID_JSON")


class TestBatchRegistry(unittest.TestCase):
    def test_batch_registry_first_submit_replay_and_conflict(self) -> None:
        registry = BatchRegistry()
        actions_v1 = [
            {"action_id": "a1", "type": "tap", "x": 100, "y": 200},
            {"action_id": "a2", "type": "wait", "wait_ms": 200},
        ]
        expected_canonical = json.dumps(
            {"tree_hash": 777, "actions": actions_v1},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        expected_hash = hashlib.sha256(expected_canonical.encode("utf-8")).hexdigest()

        # 1. Initial submission -> replayed: False
        first = registry.submit("batch-xyz", 777, actions_v1)
        self.assertEqual(
            first,
            {
                "batch_id": "batch-xyz",
                "replayed": False,
                "content_hash": expected_hash,
            },
        )

        # 2. Identical re-submission -> replayed: True
        second = registry.submit("batch-xyz", 777, actions_v1)
        self.assertEqual(
            second,
            {
                "batch_id": "batch-xyz",
                "replayed": True,
                "content_hash": expected_hash,
            },
        )

        # 3. Different content with same batch_id -> ValueError with 'conflict'
        actions_v2 = [
            {"action_id": "a1", "type": "tap", "x": 100, "y": 201},
            {"action_id": "a2", "type": "wait", "wait_ms": 200},
        ]
        with self.assertRaises(ValueError) as ctx:
            registry.submit("batch-xyz", 777, actions_v2)
        self.assertIn("conflict", str(ctx.exception).lower())

        # Also different tree_hash with same batch_id -> ValueError with 'conflict'
        with self.assertRaises(ValueError) as ctx2:
            registry.submit("batch-xyz", 778, actions_v1)
        self.assertIn("conflict", str(ctx2.exception).lower())


class TestStep1Additions(unittest.TestCase):
    """MERGE_PLAN_A Step 1 additions: version==1, max 20 actions, extra fields tolerated."""

    def _health_base(self, version):
        return {
            "ok": True,
            "service": "muse-bridge",
            "version": version,
            "app_version": "1.9.0",
            "app_version_code": 19,
            "accessibility": True,
        }

    def test_parse_health_version_2_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_health(self._health_base(2))

    def test_parse_health_version_boundaries(self) -> None:
        # Only version 1 is accepted: 0 and -1 are below the range, True is
        # a bool (not an int), 1.0 is a float, "1" is a string.
        for bad in (0, -1, True, False, 1.0, "1"):
            with self.subTest(version=bad):
                with self.assertRaises(ProtocolError):
                    parse_health(self._health_base(bad))
        self.assertEqual(
            parse_health(self._health_base(1)), self._health_base(1)
        )

    def _tap_actions(self, count):
        action = {"action_id": "a1", "type": "tap", "x": 10, "y": 20}
        return [dict(action, action_id=f"a{i}") for i in range(count)]

    def test_build_batch_21_actions_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            build_batch("b1", 100, self._tap_actions(21))

    def test_build_batch_boundaries(self) -> None:
        # 0 actions rejected; 1 and exactly 20 accepted; 21 rejected.
        with self.assertRaises(ProtocolError):
            build_batch("b1", 100, [])
        self.assertEqual(len(build_batch("b1", 100, self._tap_actions(1))["actions"]), 1)
        self.assertEqual(
            len(build_batch("b1", 100, self._tap_actions(20))["actions"]), 20
        )
        with self.assertRaises(ProtocolError):
            build_batch("b1", 100, self._tap_actions(21))

    def test_parse_state_extra_fields_tolerated(self) -> None:
        raw = {
            "ok": True,
            "app_version": "1.9.0",
            "app_version_code": 19,
            "width": 1080,
            "height": 2400,
            "package": "com.example.app",
            "texts": ["hello"],
            "revision": 12,
            "tree_hash": 123,
            "nodes": [],
            "focused": None,
            "ime_visible": False,
            "event_seq": 1,
            "truncated": False,
            "snapshot_id": "snap-1",
            "snapshot_valid": True,
            "snapshot_stable": True,
        }
        self.assertEqual(parse_state(raw), raw)

    def test_parse_state_extra_fields_abnormal_values(self) -> None:
        # Extra fields are tolerated regardless of value: a False snapshot
        # flag and a None snapshot flag still pass through unchanged.
        raw = {
            "ok": True,
            "app_version": "1.9.0",
            "app_version_code": 19,
            "width": 1080,
            "height": 2400,
            "package": "com.example.app",
            "texts": ["hello"],
            "revision": 12,
            "tree_hash": 123,
            "nodes": [],
            "focused": None,
            "ime_visible": False,
            "event_seq": 1,
            "truncated": False,
            "snapshot_id": "snap-1",
            "snapshot_valid": False,
            "snapshot_stable": None,
        }
        self.assertEqual(parse_state(raw), raw)


if __name__ == "__main__":
    unittest.main()
