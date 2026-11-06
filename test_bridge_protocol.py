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
    build_operation_end,
    build_press_key,
    build_swipe,
    build_type_node,
    build_type_text,
    error_code_for_status,
    parse_batch_result,
    parse_error_body,
    parse_events,
    parse_health,
    parse_launch_app,
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


def _v2_health_body(**overrides) -> dict:
    """A complete, cross-field-consistent v2.0 /health body
    (FULL_ACCESS, a11y on). P1-5 (CONS-022): the all-or-none
    rule accepts it (all 5 extension fields present). Pass
    overrides to mutate one field per test case."""
    body = {
        "ok": True,
        "service": "muse-bridge",
        "version": 1,
        "app_version": "2.2.0",
        "app_version_code": 35,
        "accessibility": True,
        "notification_listener": True,
        "mode": "FULL_ACCESS",
        "overall_status": "HEALTHY",
        "modules": {"http": "HEALTHY", "tailscale": "HEALTHY",
                    "accessibility": "ENABLED",
                    "notification_access": "GRANTED",
                    "termux_ssh": "UNKNOWN"},
        "capabilities": {"screenshot": True, "tap": True,
                         "swipe": True, "text_input": True,
                         "notification": True, "notification_post": True,
                         "http_api": True, "launch_app": True,
                         "launch_app_guaranteed": False,
                         "foreground_verification": True,
                         "install_apk": True},
        "tailscale_ips": ["100.111.74.42"],
    }
    body.update(overrides)
    return body


def _v2_health_body_minimal(**overrides) -> dict:
    """A complete, cross-field-consistent v2.0 /health body
    (MINIMAL, a11y off). Same all-or-none acceptance as
    _v2_health_body, with the screen capabilities off."""
    body = _v2_health_body(
        accessibility=False,
        mode="MINIMAL",
        modules={"http": "HEALTHY", "tailscale": "HEALTHY",
                 "accessibility": "DISABLED",
                 "notification_access": "GRANTED",
                 "termux_ssh": "UNKNOWN"},
        capabilities={"screenshot": False, "tap": False,
                      "swipe": False, "text_input": False,
                      "notification": True, "notification_post": True,
                      "http_api": True, "launch_app": True,
                      "launch_app_guaranteed": False,
                      "foreground_verification": False,
                      "install_apk": True},
    )
    body.update(overrides)
    return body


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
        # Step 3 (v1.1.0): the DPAD keys are in VALID_KEYS too,
        # matching the server's pressKey (v1.9.9).
        for key in ("HOME", "BACK", "RECENTS", "POWER",
                    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
                    "DPAD_CENTER"):
            self.assertEqual(build_press_key(key), {"key": key})

    def test_build_press_key_lowercase_normalised(self) -> None:
        # Step 3 (P1-01, CONS-019): the server compares keys
        # case-insensitively, so lowercase input is normalised to
        # the canonical uppercase form.
        self.assertEqual(build_press_key("back"), {"key": "BACK"})
        self.assertEqual(build_press_key("dpad_down"), {"key": "DPAD_DOWN"})

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

    def test_build_operation_end_valid(self) -> None:
        self.assertEqual(build_operation_end(), {})

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

    def test_parse_health_extended_v2(self) -> None:
        """P0-3 (v2.0): the extended /health body (mode,
        overall_status, modules, capabilities, tailscale_ips)
        parses and round-trips."""
        raw = {
            "ok": True,
            "service": "muse-bridge",
            "version": 1,
            "app_version": "1.9.0",
            "app_version_code": 19,
            "accessibility": False,
            "notification_listener": True,
            "mode": "MINIMAL",
            "overall_status": "HEALTHY",
            "modules": {"http": "HEALTHY", "tailscale": "HEALTHY",
                        "accessibility": "DISABLED",
                        "notification_access": "GRANTED",
                        "termux_ssh": "UNKNOWN"},
            "capabilities": {"screenshot": False, "tap": False,
                             "swipe": False, "text_input": False,
                             "notification": True, "notification_post": True,
                             "http_api": True, "launch_app": True,
                             "install_apk": True},
            "tailscale_ips": ["100.111.74.42"],
        }
        self.assertEqual(parse_health(raw), raw)

    def test_parse_health_old_server_no_extension(self) -> None:
        """P0-3 (v2.0): a body without the new fields (an old
        server) still parses — the extension is optional."""
        raw = {
            "ok": True,
            "service": "muse-bridge",
            "version": 1,
            "app_version": "1.9.0",
            "app_version_code": 19,
            "accessibility": True,
        }
        self.assertEqual(parse_health(raw), raw)

    def test_parse_health_p0_4_capabilities(self) -> None:
        """P0-4 (v2.0): /health carries launch_app_guaranteed
        (always false) and foreground_verification (true when
        a11y is on). Both are optional — an old server omits
        them — but when present they must be booleans.
        P1-5 (CONS-022): the body is a complete v2.0 body (all
        5 extension fields), so the all-or-none rule accepts
        it."""
        raw = {
            "ok": True,
            "service": "muse-bridge",
            "version": 1,
            "app_version": "2.2.0",
            "app_version_code": 35,
            "accessibility": True,
            "mode": "FULL_ACCESS",
            "overall_status": "HEALTHY",
            "modules": {"http": "HEALTHY", "tailscale": "HEALTHY",
                        "accessibility": "ENABLED",
                        "notification_access": "GRANTED",
                        "termux_ssh": "UNKNOWN"},
            "capabilities": {"screenshot": True, "tap": True,
                             "swipe": True, "text_input": True,
                             "notification": True,
                             "notification_post": True,
                             "http_api": True, "launch_app": True,
                             "launch_app_guaranteed": False,
                             "foreground_verification": True,
                             "install_apk": True},
            "tailscale_ips": ["100.111.74.42"],
        }
        self.assertEqual(parse_health(raw), raw)

    def test_parse_launch_app_launched(self) -> None:
        """P0-4 (v2.0): a confirmed launch parses and
        round-trips — ok is True exactly when the status is
        'launched'."""
        raw = {
            "ok": True, "status": "launched", "accepted": True,
            "package": "com.twitter.android",
            "launch_requested": True, "foreground_confirmed": True,
            "verification": "accessibility",
        }
        self.assertEqual(parse_launch_app(raw), raw)

    def test_parse_launch_app_unconfirmed(self) -> None:
        """P0-4 (v2.0): both unconfirmed reasons parse — the
        verified-but-not-confirmed case (foreground False) and
        the no-verification case (foreground None, a11y off)."""
        verified = {
            "ok": False, "status": "unconfirmed",
            "reason": "FOREGROUND_NOT_CONFIRMED", "accepted": True,
            "package": "com.twitter.android",
            "launch_requested": True, "foreground_confirmed": False,
            "verification": "accessibility",
        }
        self.assertEqual(parse_launch_app(verified), verified)
        unverified = {
            "ok": False, "status": "unconfirmed",
            "reason": "VERIFICATION_UNAVAILABLE", "accepted": True,
            "package": "com.twitter.android",
            "launch_requested": True, "foreground_confirmed": None,
            "verification": "none",
        }
        self.assertEqual(parse_launch_app(unverified), unverified)

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
            # Step 4 (CONS-020 P0-02/P0-03): the A-4 server always
            # returns the batch-level status and the payload_hash.
            "status": "partial",
            "payload_hash": "ab" * 32,
        }
        self.assertEqual(parse_batch_result(raw), raw)

    def test_parse_batch_result_extra_fields_tolerated(self) -> None:
        # Step 3 (P1-02, CONS-019): the live batch response carries
        # extra fields (snapshot_id, the batch-level status, the
        # payload hash, completed/failed action ids, transport).
        # They pass through unchanged. The batch-level status is
        # NOT validated (only each result item's status is) — a
        # "partial" or "failed" batch is still a well-formed
        # response, not a protocol error.
        for batch_status in ("completed", "partial", "failed"):
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
                ],
                "stopped_at": -1,
                "stop_reason": None,
                "snapshot_id": 7,
                "status": batch_status,
                "payload_hash": "ab" * 32,
                "completed_action_ids": ["a1"],
                "failed_action_id": None,
                "transport": "app",
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

    def test_parse_health_extension_invalid(self) -> None:
        """P0-3 (v2.0): a present-but-invalid extension field is
        a protocol error. P1-5 (CONS-022): each case uses a
        complete v2.0 body (all 5 extension fields) so the
        all-or-none rule passes and the invalid VALUE is what
        is rejected."""
        # Bad mode
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(mode="SUPERUSER"))
        # Bad overall_status
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(overall_status="FINE"))
        # Bad module value
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(modules={"http": "OK"}))
        # Bad capability type
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(capabilities={"screenshot": "yes"}))
        # Bad tailscale_ips type
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(tailscale_ips="100.111.74.42"))
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(tailscale_ips=[123]))
        # Unknown keys inside modules/capabilities are ignored.
        ok = _v2_health_body(
            modules={"http": "HEALTHY", "tailscale": "HEALTHY",
                     "accessibility": "ENABLED",
                     "notification_access": "GRANTED",
                     "termux_ssh": "UNKNOWN", "future_module": "X"},
            capabilities={"screenshot": True, "tap": True,
                          "swipe": True, "text_input": True,
                          "notification": True, "notification_post": True,
                          "http_api": True, "launch_app": True,
                          "launch_app_guaranteed": False,
                          "foreground_verification": True,
                          "install_apk": True, "future_cap": 1})
        self.assertEqual(parse_health(ok), ok)
        # P0-4: a present-but-non-boolean new capability is an error.
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(capabilities={
                "launch_app_guaranteed": "no"}))
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(capabilities={
                "foreground_verification": "yes"}))

    def test_parse_launch_app_invalid(self) -> None:
        """P0-4 (v2.0): the launch-report invariants are enforced
        — ok is True exactly when the status is 'launched', and
        foreground_confirmed is True exactly then too."""
        base = {
            "ok": True, "status": "launched", "accepted": True,
            "package": "com.twitter.android",
            "launch_requested": True, "foreground_confirmed": True,
            "verification": "accessibility",
        }
        # Missing field
        missing = dict(base)
        del missing["verification"]
        with self.assertRaises(ProtocolError):
            parse_launch_app(missing)
        # ok True with an unconfirmed status is the old false
        # positive — it must never parse.
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                base, status="unconfirmed",
                reason="FOREGROUND_NOT_CONFIRMED",
                foreground_confirmed=False))
        # ok False with a launched status is equally wrong.
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(base, ok=False))
        # Launched without a confirmed foreground.
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(base, foreground_confirmed=False))
        # Unconfirmed claiming a confirmed foreground.
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                base, ok=False, status="unconfirmed",
                reason="FOREGROUND_NOT_CONFIRMED"))
        # Unknown status / verification values.
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(base, status="maybe"))
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(base, verification="guesswork"))
        # Unconfirmed without a reason, or with an unknown one.
        no_reason = {
            "ok": False, "status": "unconfirmed", "accepted": True,
            "package": "com.twitter.android",
            "launch_requested": True, "foreground_confirmed": False,
            "verification": "accessibility",
        }
        with self.assertRaises(ProtocolError):
            parse_launch_app(no_reason)
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(no_reason, reason="BLOCKED_BY_MAGIC"))

    def test_parse_launch_app_combination(self) -> None:
        """P0-4 fix (CONS-027 P1 #2): only the three
        status/reason/verification/foreground combinations the
        server can send are accepted — launched (no reason,
        verification "accessibility", foreground True),
        unconfirmed / FOREGROUND_NOT_CONFIRMED (verification
        "accessibility", foreground False), and unconfirmed /
        VERIFICATION_UNAVAILABLE (verification "none", foreground
        null). Anything else is rejected."""
        launched = {
            "ok": True, "status": "launched", "accepted": True,
            "package": "com.twitter.android",
            "launch_requested": True, "foreground_confirmed": True,
            "verification": "accessibility",
        }
        # Launched verified by "none" cannot confirm a foreground.
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(launched, verification="none"))
        # Launched verified by UsageStats has no P0-4 form yet.
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(launched, verification="usage_stats"))
        # Launched must carry no reason.
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                launched, reason="FOREGROUND_NOT_CONFIRMED"))
        # Unconfirmed / FOREGROUND_NOT_CONFIRMED pairs with
        # verification "accessibility" and foreground False.
        not_confirmed = {
            "ok": False, "status": "unconfirmed", "accepted": True,
            "package": "com.twitter.android",
            "launch_requested": True, "foreground_confirmed": False,
            "verification": "accessibility",
            "reason": "FOREGROUND_NOT_CONFIRMED",
        }
        self.assertEqual(parse_launch_app(not_confirmed), not_confirmed)
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(not_confirmed, verification="none"))
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                not_confirmed, verification="usage_stats"))
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                not_confirmed, foreground_confirmed=None))
        # Unconfirmed / VERIFICATION_UNAVAILABLE pairs with
        # verification "none" and foreground null.
        unverified = {
            "ok": False, "status": "unconfirmed", "accepted": True,
            "package": "com.twitter.android",
            "launch_requested": True, "foreground_confirmed": None,
            "verification": "none",
            "reason": "VERIFICATION_UNAVAILABLE",
        }
        self.assertEqual(parse_launch_app(unverified), unverified)
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                unverified, verification="accessibility"))
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                unverified, foreground_confirmed=False))
        # The two reasons cannot swap verification methods.
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                not_confirmed, reason="VERIFICATION_UNAVAILABLE"))
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                unverified, reason="FOREGROUND_NOT_CONFIRMED"))
        # "unavailable" / "failed" have no 200-body form in P0-4.
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                launched, ok=False, status="unavailable",
                foreground_confirmed=False))
        with self.assertRaises(ProtocolError):
            parse_launch_app(dict(
                launched, ok=False, status="failed",
                foreground_confirmed=False))

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
            # Step 4 (CONS-020 P0-02/P0-03): the batch-level fields
            # are valid here so the test exercises the per-item check.
            "status": "completed",
            "payload_hash": "ab" * 32,
        }
        with self.assertRaises(ValueError):
            parse_batch_result(bad_status)

        missing_field = dict(bad_status)
        del missing_field["stopped_at"]
        with self.assertRaises(ValueError):
            parse_batch_result(missing_field)

        # Step 4 (CONS-020 P0-02/P0-03): a missing payload_hash or
        # a missing/invalid batch-level status is also rejected.
        missing_hash = dict(bad_status)
        missing_hash["results"] = [{
            "action_id": "a1", "status": "executed", "error": None,
            "hash_before": 111, "hash_after": 222,
        }]
        del missing_hash["payload_hash"]
        with self.assertRaises(ValueError):
            parse_batch_result(missing_hash)
        bad_batch_status = dict(missing_hash)
        bad_batch_status["payload_hash"] = "ab" * 32
        bad_batch_status["status"] = "done"
        with self.assertRaises(ValueError):
            parse_batch_result(bad_batch_status)
        bad_hash_format = dict(bad_batch_status)
        bad_hash_format["status"] = "completed"
        bad_hash_format["payload_hash"] = "not-a-hash"
        with self.assertRaises(ValueError):
            parse_batch_result(bad_hash_format)


class TestParseHealthP1(unittest.TestCase):
    """P1-5 (CONS-022): parse_health all-or-none for the 5
    extension fields, plus cross-field consistency checks."""

    def test_parse_health_all_present_valid(self) -> None:
        """All 5 extension fields present, cross-field
        consistent: accepted (FULL_ACCESS and MINIMAL)."""
        self.assertEqual(parse_health(_v2_health_body()),
                         _v2_health_body())
        self.assertEqual(parse_health(_v2_health_body_minimal()),
                         _v2_health_body_minimal())

    def test_parse_health_partial_extension_rejected(self) -> None:
        """Only SOME of the 5 extension fields present: an
        incomplete Health response (ProtocolError)."""
        full = _v2_health_body()
        ext = ("mode", "overall_status", "modules",
               "capabilities", "tailscale_ips")
        base = {k: v for k, v in full.items() if k not in ext}
        # Every non-empty proper subset is rejected.
        for n in range(1, len(ext)):
            partial = dict(base)
            partial.update({f: full[f] for f in ext[:n]})
            with self.assertRaises(ProtocolError, msg=f"n={n}"):
                parse_health(partial)
        # A single field alone is also rejected.
        with self.assertRaises(ProtocolError):
            parse_health(dict(base, capabilities=full["capabilities"]))
        with self.assertRaises(ProtocolError):
            parse_health(dict(base, tailscale_ips=full["tailscale_ips"]))

    def test_parse_health_full_access_requires_enabled(self) -> None:
        """mode FULL_ACCESS with modules.accessibility
        DISABLED: inconsistent."""
        bad = _v2_health_body(
            modules={"http": "HEALTHY", "tailscale": "HEALTHY",
                     "accessibility": "DISABLED",
                     "notification_access": "GRANTED",
                     "termux_ssh": "UNKNOWN"})
        with self.assertRaises(ProtocolError):
            parse_health(bad)

    def test_parse_health_minimal_requires_disabled(self) -> None:
        """mode MINIMAL with modules.accessibility ENABLED:
        inconsistent."""
        bad = _v2_health_body_minimal(
            modules={"http": "HEALTHY", "tailscale": "HEALTHY",
                     "accessibility": "ENABLED",
                     "notification_access": "GRANTED",
                     "termux_ssh": "UNKNOWN"})
        with self.assertRaises(ProtocolError):
            parse_health(bad)

    def test_parse_health_top_level_accessibility_must_agree(self) -> None:
        """The top-level accessibility boolean must agree with
        modules.accessibility."""
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body_minimal(accessibility=True))
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(accessibility=False))

    def test_parse_health_disabled_requires_no_screen_caps(self) -> None:
        """accessibility DISABLED with a screen capability true:
        inconsistent (each of screenshot/tap/swipe/text_input)."""
        for key in ("screenshot", "tap", "swipe", "text_input"):
            caps = dict(_v2_health_body_minimal()["capabilities"])
            caps[key] = True
            with self.assertRaises(ProtocolError, msg=key):
                parse_health(_v2_health_body_minimal(capabilities=caps))

    def test_parse_health_tailscale_unavailable_requires_empty_ips(self) -> None:
        """modules.tailscale UNAVAILABLE with a non-empty
        tailscale_ips: inconsistent."""
        bad = _v2_health_body_minimal(
            modules={"http": "HEALTHY", "tailscale": "UNAVAILABLE",
                     "accessibility": "DISABLED",
                     "notification_access": "GRANTED",
                     "termux_ssh": "UNKNOWN"})
        with self.assertRaises(ProtocolError):
            parse_health(bad)
        # UNAVAILABLE with an empty list is consistent.
        ok = _v2_health_body_minimal(
            modules={"http": "HEALTHY", "tailscale": "UNAVAILABLE",
                     "accessibility": "DISABLED",
                     "notification_access": "GRANTED",
                     "termux_ssh": "UNKNOWN"},
            tailscale_ips=[])
        self.assertEqual(parse_health(ok), ok)

    def test_parse_health_launch_app_guaranteed_must_be_false(self) -> None:
        """P0-4: launch_app_guaranteed is always false; a true
        value is inconsistent."""
        caps = dict(_v2_health_body()["capabilities"])
        caps["launch_app_guaranteed"] = True
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(capabilities=caps))

    def test_parse_health_foreground_verification_requires_a11y(self) -> None:
        """foreground_verification true with accessibility
        DISABLED: inconsistent."""
        caps = dict(_v2_health_body_minimal()["capabilities"])
        caps["foreground_verification"] = True
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body_minimal(capabilities=caps))

    def test_parse_health_tailscale_ips_bound_optional(self) -> None:
        """P1-3 (CONS-022): tailscale_ips_bound is an optional
        list of strings, NOT part of the all-or-none set — a
        full v2.0 body and an old-server body both accept it,
        and it may be absent."""
        full = _v2_health_body(tailscale_ips_bound=["100.111.74.42"])
        self.assertEqual(parse_health(full), full)
        old = {
            "ok": True, "service": "muse-bridge", "version": 1,
            "app_version": "1.9.0", "app_version_code": 19,
            "accessibility": False,
            "tailscale_ips_bound": ["100.85.138.44"],
        }
        self.assertEqual(parse_health(old), old)
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(tailscale_ips_bound="100.111.74.42"))
        with self.assertRaises(ProtocolError):
            parse_health(_v2_health_body(tailscale_ips_bound=[123]))


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
        # Step 4 (v1.2.0): the expected hash is the server's canonical
        # form (fixed field order, defaults filled, length-prefixed
        # strings), written out literally — not recomputed via the
        # function under test.
        canonical = (
            "v1|tree=777|actions=["
            "{id=2:a1,type=3:tap,node=0:,x=100,y=200,x1=0,y1=0,x2=0,y2=0,"
            "dur=300,text=0:,key=0:,wait=0,exp=false};"
            "{id=2:a2,type=4:wait,node=0:,x=0,y=0,x1=0,y1=0,x2=0,y2=0,"
            "dur=300,text=0:,key=0:,wait=200,exp=false}]"
        )
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        # 1. Initial submission -> replayed: False. Step 4 (v1.2.0):
        # the return also carries the validated "body" (the exact
        # payload to send).
        first = registry.submit("batch-xyz", 777, actions_v1)
        self.assertEqual(
            first,
            {
                "batch_id": "batch-xyz",
                "replayed": False,
                "content_hash": expected_hash,
                "body": {
                    "batch_id": "batch-xyz",
                    "protocol_version": 1,
                    "tree_hash": 777,
                    "actions": actions_v1,
                },
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
                "body": {
                    "batch_id": "batch-xyz",
                    "protocol_version": 1,
                    "tree_hash": 777,
                    "actions": actions_v1,
                },
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


class TestBatchRegistryStep4(unittest.TestCase):
    """Step 4 (v1.2.0): BatchRegistry mirrors the A-4 server rules
    — 10-minute result, 10-minute tombstone, partial/running
    outcomes, payload_hash verification."""

    def _actions(self):
        return [{"action_id": "a1", "type": "wait", "wait_ms": 500}]

    def _clock(self, start=1000.0):
        # A fake monotonic clock the test can move forward.
        state = {"now": start}

        def clock():
            return state["now"]

        return state, clock

    def _result(self, batch_id, status="completed", payload_hash=None):
        return {
            "ok": True,
            "batch_id": batch_id,
            "replayed": False,
            "results": [
                {
                    "action_id": "a1",
                    "status": "executed",
                    "error": None,
                    "hash_before": 1,
                    "hash_after": 1,
                }
            ],
            "stopped_at": -1,
            "stop_reason": None,
            "snapshot_id": 7,
            "status": status,
            "payload_hash": payload_hash,
            "completed_action_ids": ["a1"],
            "failed_action_id": None,
        }

    def test_tombstone_then_free_again(self) -> None:
        # Live (0-10 min): same content replays, different content
        # conflicts. Tombstone (10-20 min): any submit raises
        # BATCH_EXPIRED. After 20 min: the id is free again.
        # Step 4 (CONS-020 P0-01): the boundaries are ">" (not
        # ">="), mirroring the server — at exactly 600 s the batch
        # is still live, at exactly 1200 s it is still a tombstone.
        state, clock = self._clock()
        registry = BatchRegistry(clock=clock)
        actions = self._actions()
        registry.submit("b-exp", 777, actions)
        self.assertTrue(registry.submit("b-exp", 777, actions)["replayed"])
        state["now"] += 599.999
        self.assertTrue(registry.submit("b-exp", 777, actions)["replayed"])
        state["now"] += 0.001  # exactly 600 s: still live
        self.assertTrue(registry.submit("b-exp", 777, actions)["replayed"])
        with self.assertRaises(ProtocolError) as ctx:
            registry.submit("b-exp", 777, [{"action_id": "a9", "type": "wait", "wait_ms": 100}])
        self.assertEqual(ctx.exception.code, "STALE_REVISION")
        state["now"] += 0.001  # 600.001 s: tombstone
        with self.assertRaises(ProtocolError) as ctx:
            registry.submit("b-exp", 777, actions)
        self.assertEqual(ctx.exception.code, "BATCH_EXPIRED")
        self.assertIn("410", str(ctx.exception))
        # A different payload in the tombstone window is still
        # expired (not a conflict) — the id itself is dead.
        with self.assertRaises(ProtocolError) as ctx2:
            registry.submit("b-exp", 777, [{"action_id": "a9", "type": "wait", "wait_ms": 100}])
        self.assertEqual(ctx2.exception.code, "BATCH_EXPIRED")
        # The record reports itself as expired while tombstoned.
        self.assertTrue(registry.get("b-exp")["expired"])
        state["now"] = 1000.0 + 1199.999
        self.assertTrue(registry.get("b-exp")["expired"])
        state["now"] = 1000.0 + 1200.0  # exactly 1200 s: still a tombstone
        with self.assertRaises(ProtocolError) as ctx3:
            registry.submit("b-exp", 777, actions)
        self.assertEqual(ctx3.exception.code, "BATCH_EXPIRED")
        state["now"] = 1000.0 + 1200.001  # past 20 minutes total
        self.assertIsNone(registry.get("b-exp"))
        fresh = registry.submit("b-exp", 777, actions)
        self.assertFalse(fresh["replayed"])

    def test_record_result_partial_and_running(self) -> None:
        # "partial" (>=1 success then stop) and "running" are valid
        # outcomes the registry stores; the status is readable via get.
        state, clock = self._clock()
        registry = BatchRegistry(clock=clock)
        actions = self._actions()
        first = registry.submit("b-part", 777, actions)
        recorded = registry.record_result(
            "b-part", self._result("b-part", status="partial",
                                   payload_hash=first["content_hash"]))
        self.assertEqual(recorded["status"], "partial")
        self.assertEqual(recorded["payload_hash"], first["content_hash"])
        self.assertEqual(recorded["completed_action_ids"], ["a1"])
        self.assertIsNone(recorded["failed_action_id"])
        self.assertFalse(recorded["expired"])
        self.assertEqual(recorded["age_s"], 0)
        # A later "running" outcome (a re-send while running)
        # overwrites the stored status.
        registry.record_result(
            "b-part", self._result("b-part", status="running",
                                   payload_hash=first["content_hash"]))
        self.assertEqual(registry.get("b-part")["status"], "running")

    def test_record_result_rejects_bad_status_and_hash_mismatch(self) -> None:
        registry = BatchRegistry()
        actions = self._actions()
        first = registry.submit("b-bad", 777, actions)
        with self.assertRaises(ProtocolError):
            registry.record_result(
                "b-bad", self._result("b-bad", status="done",
                                      payload_hash=first["content_hash"]))
        with self.assertRaises(ProtocolError) as ctx:
            registry.record_result(
                "b-bad", self._result("b-bad", status="completed",
                                      payload_hash="00" * 32))
        self.assertEqual(ctx.exception.code, "PAYLOAD_HASH_MISMATCH")

    def test_record_result_unknown_id_rejected_and_mismatch_kept(self) -> None:
        # Step 4 (CONS-020 P1-02): an id that was never submitted
        # raises instead of creating a record that would block the
        # id for 20 minutes.
        registry = BatchRegistry()
        with self.assertRaises(ProtocolError) as ctx:
            registry.record_result(
                "never-submitted",
                self._result("never-submitted", status="completed",
                             payload_hash="ab" * 32))
        self.assertEqual(ctx.exception.code, "UNKNOWN_BATCH_ID")
        # Step 4 (CONS-020 P1-03): on a hash mismatch the server's
        # outcome is kept as a diagnostic on the record.
        actions = self._actions()
        first = registry.submit("b-mis", 777, actions)
        mismatched = self._result("b-mis", status="partial",
                                  payload_hash="00" * 32)
        with self.assertRaises(ProtocolError):
            registry.record_result("b-mis", mismatched)
        record = registry.get("b-mis")
        self.assertEqual(record["mismatch"]["status"], "partial")
        self.assertEqual(record["mismatch"]["payload_hash"], "00" * 32)
        # The client's hash is stored; no server hash is verified
        # yet (the mismatch is only a diagnostic).
        self.assertEqual(record["content_hash"], first["content_hash"])
        self.assertIsNone(record["payload_hash"])
        self.assertIsNone(record["status"])

    def test_integer_ranges_match_java(self) -> None:
        # Step 4 (CONS-020 P1-01): tree_hash is a Java int, coords
        # are Java ints, duration/wait are Java longs.
        with self.assertRaises(ProtocolError):
            build_batch("b-r", 2 ** 31, [{"action_id": "a1", "type": "wait", "wait_ms": 100}])
        with self.assertRaises(ProtocolError):
            build_batch("b-r", -(2 ** 31) - 1, [{"action_id": "a1", "type": "wait", "wait_ms": 100}])
        build_batch("b-r", 2 ** 31 - 1, [{"action_id": "a1", "type": "wait", "wait_ms": 100}])
        with self.assertRaises(ProtocolError):
            build_batch("b-r", 100, [{"action_id": "a1", "type": "tap", "x": 2 ** 31, "y": 0}])
        with self.assertRaises(ProtocolError):
            build_batch("b-r", 100, [{"action_id": "a1", "type": "wait", "wait_ms": 2 ** 63}])
        build_batch("b-r", 100, [{"action_id": "a1", "type": "wait", "wait_ms": 2 ** 63 - 1}])

    def test_get_unknown_returns_none(self) -> None:
        self.assertIsNone(BatchRegistry().get("no-such-batch"))

    def test_canonical_form_matches_server(self) -> None:
        # The canonical string is byte-identical to the server's
        # canonicalBatchPayload: fixed field order, defaults filled,
        # length-prefixed strings (Java UTF-16 length — an astral
        # character counts as 2), booleans as "true"/"false".
        from bridge_protocol import _canonical_batch_payload

        self.assertEqual(
            _canonical_batch_payload(
                1, 777, [{"action_id": "a1", "type": "wait", "wait_ms": 500}]
            ),
            "v1|tree=777|actions=[{id=2:a1,type=4:wait,node=0:,x=0,y=0,"
            "x1=0,y1=0,x2=0,y2=0,dur=300,text=0:,key=0:,wait=500,exp=false}]",
        )
        # No action_id -> the server defaults to "a"+index.
        self.assertEqual(
            _canonical_batch_payload(
                1, 100, [{"type": "tap", "x": 1, "y": 2}]
            ),
            "v1|tree=100|actions=[{id=2:a0,type=3:tap,node=0:,x=1,y=2,"
            "x1=0,y1=0,x2=0,y2=0,dur=300,text=0:,key=0:,wait=0,exp=false}]",
        )
        # A Japanese string: 3 code points, 3 UTF-16 units. An emoji
        # (astral): 1 code point, 2 UTF-16 units.
        self.assertEqual(
            _canonical_batch_payload(
                1, 100,
                [{"action_id": "a1", "type": "type", "text": "あいう"}],
            ),
            "v1|tree=100|actions=[{id=2:a1,type=4:type,node=0:,x=0,y=0,"
            "x1=0,y1=0,x2=0,y2=0,dur=300,text=3:あいう,key=0:,wait=0,exp=false}]",
        )
        self.assertEqual(
            _canonical_batch_payload(
                1, 100,
                [{"action_id": "a1", "type": "type", "text": "😀"}],
            ),
            "v1|tree=100|actions=[{id=2:a1,type=4:type,node=0:,x=0,y=0,"
            "x1=0,y1=0,x2=0,y2=0,dur=300,text=2:😀,key=0:,wait=0,exp=false}]",
        )
        # Step 4 (CONS-020 P1-04): expect_change=True renders as
        # "true", and several actions without action_id default to
        # "a0", "a1", ... by index.
        self.assertEqual(
            _canonical_batch_payload(
                1, 100,
                [{"type": "tap", "x": 1, "y": 2, "expect_change": True},
                 {"type": "wait", "wait_ms": 50}],
            ),
            "v1|tree=100|actions=[{id=2:a0,type=3:tap,node=0:,x=1,y=2,"
            "x1=0,y1=0,x2=0,y2=0,dur=300,text=0:,key=0:,wait=0,exp=true};"
            "{id=2:a1,type=4:wait,node=0:,x=0,y=0,x1=0,y1=0,x2=0,y2=0,"
            "dur=300,text=0:,key=0:,wait=50,exp=false}]",
        )


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


class TestStep2HashVersion(unittest.TestCase):
    """MERGE_PLAN_A Step 2: protocol_version is explicit in
    build_batch and in the BatchRegistry content hash."""

    def test_build_batch_includes_protocol_version_1(self) -> None:
        built = build_batch(
            "b1", 100, [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
        )
        self.assertEqual(built["protocol_version"], 1)

    def test_build_batch_other_protocol_version_rejected(self) -> None:
        actions = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
        with self.assertRaises(ProtocolError):
            build_batch("b1", 100, actions, protocol_version=2)
        with self.assertRaises(ProtocolError):
            build_batch("b1", 100, actions, protocol_version=0)

    def test_hash_includes_protocol_version(self) -> None:
        actions = [{"action_id": "a1", "type": "tap", "x": 10, "y": 20}]
        hash_v1 = BatchRegistry.compute_content_hash(1, 777, actions)
        # Same content, same version -> same hash.
        self.assertEqual(
            BatchRegistry.compute_content_hash(1, 777, actions), hash_v1
        )
        # Same content, different version -> different hash.
        self.assertNotEqual(
            BatchRegistry.compute_content_hash(2, 777, actions), hash_v1
        )

    def test_hash_ignores_dict_key_order(self) -> None:
        actions = [{"action_id": "a1", "type": "tap", "x": 10, "y": 20}]
        reordered = [{"y": 20, "x": 10, "type": "tap", "action_id": "a1"}]
        self.assertEqual(
            BatchRegistry.compute_content_hash(1, 777, actions),
            BatchRegistry.compute_content_hash(1, 777, reordered),
        )

    def test_hash_differs_when_actions_differ(self) -> None:
        actions = [{"action_id": "a1", "type": "tap", "x": 10, "y": 20}]
        changed = [{"action_id": "a1", "type": "tap", "x": 11, "y": 20}]
        self.assertNotEqual(
            BatchRegistry.compute_content_hash(1, 777, actions),
            BatchRegistry.compute_content_hash(1, 777, changed),
        )

    def test_submit_other_protocol_version_rejected(self) -> None:
        registry = BatchRegistry()
        actions = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
        with self.assertRaises(ProtocolError):
            registry.submit("b1", 100, actions, protocol_version=2)

    def test_submit_same_batch_same_version_replays(self) -> None:
        registry = BatchRegistry()
        actions = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
        first = registry.submit("b1", 100, actions, protocol_version=1)
        second = registry.submit("b1", 100, actions, protocol_version=1)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["content_hash"], second["content_hash"])


if __name__ == "__main__":
    unittest.main()
