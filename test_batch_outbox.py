"""C-01 (SPEC-BATCH-PERSIST-001 §2.9): the VM-side persistent batch
outbox — the init-marker decision table, the fixed send order, the
reuse ban, the durable commit, and the UNKNOWN response shape.

The outbox is the VM half of the batch safety mechanism: the phone
Registry cannot prevent a double execution if the VM forgets what
it sent and re-issues the same logical batch under a new ID.
"""
import json
import multiprocessing
import os
import pathlib
import threading

import pytest

import android_bridge as ab
import bridge_protocol as bp


def _fresh_outbox(tmp_path):
    """An initialized, durability-verified outbox in a temp dir —
    the normal ready state (init + the C-01-15 durability check)."""
    outbox = bp.BatchOutbox(str(tmp_path / "outbox"))
    outbox.initialize("test — fresh outbox")
    outbox.record_durability_verified(
        "file_atomic_v1", "test — crash-durability check passed")
    return outbox


# ---- the init-marker decision table (SPEC §2.9) ----

def test_outbox_both_missing_needs_explicit_init(tmp_path):
    outbox = bp.BatchOutbox(str(tmp_path / "outbox"))
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.require_ready()
    assert ctx.value.code == "OUTBOX_NOT_INITIALIZED"


def test_outbox_initialize_requires_attestation(tmp_path):
    outbox = bp.BatchOutbox(str(tmp_path / "outbox"))
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.initialize("")
    assert ctx.value.code == "OUTBOX_ATTESTATION_REQUIRED"
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.initialize("   ")
    assert ctx.value.code == "OUTBOX_ATTESTATION_REQUIRED"


def test_outbox_initialize_creates_outbox_then_marker(tmp_path):
    outbox = bp.BatchOutbox(str(tmp_path / "outbox"))
    marker = outbox.initialize("test — fresh install")
    assert marker["format_version"] == 1
    assert marker["attestation"] == "test — fresh install"
    assert os.path.exists(str(tmp_path / "outbox" / "batch_outbox.json"))
    assert os.path.exists(str(tmp_path / "outbox" / "batch_outbox_initialized"))
    # The durability gate ships ON (CONS-152 P1-02): require_ready
    # refuses until the durability verification is recorded.
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.require_ready()
    assert ctx.value.code == "OUTBOX_DURABILITY_UNVERIFIED"
    outbox.record_durability_verified(
        "file_atomic_v1", "test — crash-durability check passed")
    summary = outbox.require_ready()
    assert summary["initialized"] is True
    assert summary["records"] == 0
    assert summary["durability_verified"] is True
    assert summary["durability_method"] == "file_atomic_v1"


def test_outbox_initialize_twice_is_refused(tmp_path):
    outbox = _fresh_outbox(tmp_path)
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.initialize("test — again")
    assert ctx.value.code == "OUTBOX_ALREADY_INITIALIZED"


def test_outbox_marker_missing_empty_outbox_completes_init(tmp_path):
    """Marker missing + an empty outbox: initialization stopped
    midway (the outbox was created but the marker was not). No sends
    could have happened before initialization completed, so
    completing it is safe."""
    outbox_dir = tmp_path / "outbox"
    outbox = bp.BatchOutbox(str(outbox_dir))
    outbox_dir.mkdir()
    with open(outbox_dir / "batch_outbox.json", "w") as f:
        json.dump({"format_version": 1, "records": []}, f)
    # The durability gate is checked after the init completion
    # (CONS-152 P1-02), so the verification is recorded first.
    outbox.record_durability_verified(
        "file_atomic_v1", "test — crash-durability check passed")
    summary = outbox.require_ready()
    assert summary["initialized"] is True
    assert summary["records"] == 0
    assert os.path.exists(outbox_dir / "batch_outbox_initialized")


def test_outbox_marker_missing_nonempty_is_contradictory(tmp_path):
    """Marker missing + a non-empty outbox: the marker was lost
    after sends happened. Fail-closed — never initialize as fresh."""
    outbox_dir = tmp_path / "outbox"
    outbox = bp.BatchOutbox(str(outbox_dir))
    outbox_dir.mkdir()
    record = {
        "logical_id": "l-1", "batch_id": "b-1",
        "content_hash": "a" * 64, "tree_hash": 1, "actions": [],
        "supersedes": None, "state": "result_received",
        "created_at": 1000, "sent_at": 1000, "result": {}, "note": None,
    }
    with open(outbox_dir / "batch_outbox.json", "w") as f:
        json.dump({"format_version": 1, "records": [record]}, f)
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.require_ready()
    assert ctx.value.code == "OUTBOX_CONTRADICTORY"
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.initialize("test — must not init as fresh")
    assert ctx.value.code == "OUTBOX_CONTRADICTORY"


def test_outbox_marker_present_missing_outbox_is_fail_closed(tmp_path):
    """Marker exists + the outbox file is missing: fail-closed,
    never treated as an empty queue."""
    outbox_dir = tmp_path / "outbox"
    outbox = _fresh_outbox(tmp_path)
    os.remove(outbox_dir / "batch_outbox.json")
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.require_ready()
    assert ctx.value.code == "OUTBOX_CORRUPT"


def test_outbox_corrupt_file_is_fail_closed(tmp_path):
    outbox_dir = tmp_path / "outbox"
    outbox = _fresh_outbox(tmp_path)
    with open(outbox_dir / "batch_outbox.json", "w") as f:
        f.write("{not json")
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.require_ready()
    assert ctx.value.code == "OUTBOX_CORRUPT"


def test_outbox_wrong_format_version_is_corrupt(tmp_path):
    outbox_dir = tmp_path / "outbox"
    outbox = _fresh_outbox(tmp_path)
    with open(outbox_dir / "batch_outbox.json", "w") as f:
        json.dump({"format_version": 99, "records": []}, f)
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.require_ready()
    assert ctx.value.code == "OUTBOX_CORRUPT"


# ---- register: the fixed send order and the reuse ban ----

def test_outbox_register_persists_before_send(tmp_path):
    """The fixed send order: the record is in the outbox (durable)
    before any network send happens. A fresh read of the file
    (a new BatchOutbox instance) sees it."""
    outbox = _fresh_outbox(tmp_path)
    record = outbox.register(
        "l-1", "b-1", "a" * 64, 123456,
        [{"action_id": "a1", "type": "wait", "wait_ms": 100}])
    assert record["state"] == "registered"
    assert record["logical_id"] == "l-1"
    reloaded = bp.BatchOutbox(str(tmp_path / "outbox")).get("l-1")
    assert reloaded is not None
    assert reloaded["batch_id"] == "b-1"
    assert reloaded["state"] == "registered"


def test_outbox_register_refuses_reused_batch_id(tmp_path):
    """The VM never reuses a batch_id — a second registration with
    the same batch_id is refused, whatever the content."""
    outbox = _fresh_outbox(tmp_path)
    actions = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
    outbox.register("l-1", "b-1", "a" * 64, 123456, actions)
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.register("l-2", "b-1", "b" * 64, 123456, actions)
    assert ctx.value.code == "BATCH_ID_REUSED"


def test_outbox_register_refuses_reused_logical_id(tmp_path):
    outbox = _fresh_outbox(tmp_path)
    actions = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
    outbox.register("l-1", "b-1", "a" * 64, 123456, actions)
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.register("l-1", "b-2", "a" * 64, 123456, actions)
    assert ctx.value.code == "LOGICAL_BATCH_REUSED"


def test_outbox_register_persist_failure_is_fail_closed(tmp_path, monkeypatch):
    """A persist failure means no send: register() raises
    OUTBOX_PERSIST_FAILED. (The atomic write is forced to fail
    with a monkeypatched _atomic_write_json — the temp file name
    is unique per call since CONS-152 P1-03, so blocking one
    fixed .tmp path no longer fails the write.)"""
    outbox = _fresh_outbox(tmp_path)

    def _boom(path, data):
        raise OSError("simulated persist failure")

    monkeypatch.setattr(bp, "_atomic_write_json", _boom)
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.register("l-1", "b-1", "a" * 64, 123456, [])
    assert ctx.value.code == "OUTBOX_PERSIST_FAILED"


def test_outbox_atomic_write_leaves_no_tmp(tmp_path):
    """The durable commit leaves no .tmp file behind and the live
    file is the complete new content."""
    outbox = _fresh_outbox(tmp_path)
    outbox.register("l-1", "b-1", "a" * 64, 123456, [])
    outbox_dir = tmp_path / "outbox"
    assert not list(outbox_dir.glob("*.tmp"))
    with open(outbox_dir / "batch_outbox.json") as f:
        data = json.load(f)
    assert data["format_version"] == 1
    assert len(data["records"]) == 1
    assert data["records"][0]["batch_id"] == "b-1"


# ---- outcome states ----

def test_outbox_state_transitions_and_undetermined(tmp_path):
    outbox = _fresh_outbox(tmp_path)
    actions = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
    outbox.register("l-1", "b-1", "a" * 64, 123456, actions)
    outbox.register("l-2", "b-2", "b" * 64, 123456, actions)
    outbox.register("l-3", "b-3", "c" * 64, 123456, actions)
    outbox.register("l-4", "b-4", "d" * 64, 123456, actions)
    assert outbox.mark_result("l-1", {"status": "completed"})
    assert outbox.mark_unknown("l-2", "lost response")
    assert outbox.mark_not_executed("l-3", "persist_failed")
    # l-4 stays "registered" (no outcome yet).
    undetermined = outbox.list_undetermined()
    assert {r["logical_id"] for r in undetermined} == {"l-2", "l-4"}
    summary = outbox.summary()
    assert summary["records"] == 4
    assert summary["by_state"] == {
        "result_received": 1, "outcome_unknown": 1,
        "not_executed": 1, "registered": 1,
    }
    assert summary["undetermined"] == 2
    # A mark on a missing logical id fails quietly (False).
    assert outbox.mark_result("l-missing", {}) is False


def test_outbox_capacity_never_purges_undetermined(tmp_path):
    """Over the cap, determined records older than 24 h are purged
    (oldest first); undetermined records are never purged."""
    outbox_dir = tmp_path / "outbox"
    outbox = _fresh_outbox(tmp_path)
    now = 10_000_000
    records = []
    for i in range(1000):
        records.append({
            "logical_id": f"l-old-{i}", "batch_id": f"b-old-{i}",
            "content_hash": "a" * 64, "tree_hash": 1, "actions": [],
            "supersedes": None, "state": "result_received",
            "created_at": now - 25 * 3600, "sent_at": now - 25 * 3600,
            "result": {"status": "completed"}, "note": None,
        })
    records.append({
        "logical_id": "l-undet", "batch_id": "b-undet",
        "content_hash": "a" * 64, "tree_hash": 1, "actions": [],
        "supersedes": None, "state": "outcome_unknown",
        "created_at": now - 25 * 3600, "sent_at": now - 25 * 3600,
        "result": None, "note": "lost response",
    })
    with open(outbox_dir / "batch_outbox.json", "w") as f:
        json.dump({"format_version": 1, "records": records}, f)
    clock = lambda: float(now)  # noqa: E731
    outbox2 = bp.BatchOutbox(str(outbox_dir), clock=clock)
    outbox2.register("l-new", "b-new", "a" * 64, 1, [])
    summary = outbox2.summary()
    assert summary["records"] <= bp.OUTBOX_MAX_RECORDS
    # The undetermined record survived the purge.
    assert outbox2.get("l-undet") is not None
    assert outbox2.get("l-undet")["state"] == "outcome_unknown"


# ---- the UNKNOWN response shape (SPEC §2.10) ----

def test_parse_batch_result_accepts_unknown_shape():
    parsed = bp.parse_batch_result({
        "ok": True, "batch_id": "b-1", "status": "unknown",
        "replayed": False,
        "message": "batch outcome is unknown — check the phone",
    })
    assert parsed["status"] == "unknown"
    assert "results" not in parsed


def test_parse_batch_result_still_rejects_missing_results():
    """A normal (non-unknown) response without results is still
    malformed — the unknown branch must not become a loophole."""
    with pytest.raises(bp.ProtocolError):
        bp.parse_batch_result({
            "ok": True, "batch_id": "b-1", "status": "completed",
            "replayed": False, "payload_hash": "a" * 64,
        })


def test_record_result_accepts_unknown_without_payload_hash():
    registry = bp.BatchRegistry()
    actions = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
    registry.submit("b-1", 123456, actions)
    record = registry.record_result("b-1", {
        "ok": True, "batch_id": "b-1", "status": "unknown",
        "replayed": False, "message": "outcome unknown",
    })
    assert record["status"] == "unknown"
    assert record["payload_hash"] is None


# ---- supersedes (SPEC §2.6) ----

def test_build_batch_supersedes_in_body_not_in_hash():
    actions = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
    body = bp.build_batch("b-new", 123456, actions, supersedes="b-old")
    assert body["supersedes"] == "b-old"
    # The content hash is unchanged by supersedes (the server hashes
    # protocol_version, tree_hash, and actions only).
    assert bp.BatchRegistry.compute_content_hash(1, 123456, actions) == \
        bp.BatchRegistry.compute_content_hash(1, 123456, body["actions"])
    body_no_sup = bp.build_batch("b-new", 123456, actions)
    assert "supersedes" not in body_no_sup


def test_build_batch_supersedes_validated():
    actions = [{"action_id": "a1", "type": "wait", "wait_ms": 100}]
    with pytest.raises(bp.ProtocolError):
        bp.build_batch("b-new", 123456, actions, supersedes="")
    with pytest.raises(bp.ProtocolError):
        bp.build_batch("b-new", 123456, actions, supersedes="x" * 129)


# ---- definitive vs undetermined (SPEC §2.9) ----

def test_is_definitive_not_executed():
    def err(status, error_id=None):
        return ab.TransportError(
            f"app returned HTTP {status} for POST /batch: {{}}",
            http_status=status, error_id=error_id)

    # Rejected before execution: definitive.
    assert ab._is_definitive_not_executed(err(400)) is True
    assert ab._is_definitive_not_executed(err(401)) is True
    assert ab._is_definitive_not_executed(err(403)) is True
    assert ab._is_definitive_not_executed(err(409)) is True
    assert ab._is_definitive_not_executed(err(410)) is True
    assert ab._is_definitive_not_executed(err(422)) is True
    assert ab._is_definitive_not_executed(err(503, "persist_failed")) is True
    assert ab._is_definitive_not_executed(
        err(503, "batch_store_recovery_pending")) is True
    assert ab._is_definitive_not_executed(
        err(503, "batch_store_durability_unverified")) is True
    assert ab._is_definitive_not_executed(
        ab.AccessibilityUnavailableError("a11y off")) is True
    # Undetermined: a lost response, a timeout, a bare overload 503,
    # or a 5xx.
    assert ab._is_definitive_not_executed(
        ab.TransportError("cannot reach the app")) is False
    assert ab._is_definitive_not_executed(err(408)) is False
    assert ab._is_definitive_not_executed(err(500)) is False
    assert ab._is_definitive_not_executed(err(503)) is False
    assert ab._is_definitive_not_executed(err(503, "overloaded")) is False

# ---- the storage-durability gate (SPEC §2.9, CONS-152 P1-02) ----

def test_outbox_durability_gate_ships_on(tmp_path):
    """The gate ships ON: an initialized outbox with no durability
    marker refuses both require_ready() and register() with
    OUTBOX_DURABILITY_UNVERIFIED — nothing is sent until the
    operator records the C-01-15 crash-durability check."""
    outbox = bp.BatchOutbox(str(tmp_path / "outbox"))
    outbox.initialize("test — fresh outbox")
    assert outbox.is_durability_verified() is False
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.require_ready()
    assert ctx.value.code == "OUTBOX_DURABILITY_UNVERIFIED"
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.register("l-1", "b-1", "a" * 64, 123456, [])
    assert ctx.value.code == "OUTBOX_DURABILITY_UNVERIFIED"
    # Nothing was registered (the gate fires before any mutation).
    assert outbox.list_undetermined() == []


def test_outbox_durability_verify_opens_gate(tmp_path):
    """After the operator records the verification, the gate opens
    and the marker content is what was recorded."""
    outbox = bp.BatchOutbox(str(tmp_path / "outbox"))
    outbox.initialize("test — fresh outbox")
    marker = outbox.record_durability_verified(
        "file_atomic_v1", "test — killed the VM after a write; record survived")
    assert marker["format_version"] == 1
    assert marker["method"] == "file_atomic_v1"
    assert marker["note"].startswith("test — killed the VM")
    assert isinstance(marker["verified_at"], int)
    assert os.path.exists(
        str(tmp_path / "outbox" / "batch_outbox_durability_verified"))
    assert outbox.is_durability_verified() is True
    summary = outbox.require_ready()
    assert summary["durability_verified"] is True
    assert summary["durability_method"] == "file_atomic_v1"
    record = outbox.register("l-1", "b-1", "a" * 64, 123456, [])
    assert record["state"] == "registered"
    # A fresh instance (a new process) sees the same marker.
    assert bp.BatchOutbox(str(tmp_path / "outbox")).is_durability_verified() is True


def test_outbox_durability_verify_rejects_unknown_method(tmp_path):
    """A marker for a storage method this build does not use is
    refused — a method change requires re-verification."""
    outbox = _fresh_outbox(tmp_path)
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.record_durability_verified("sqlite_full", "test")
    assert ctx.value.code == "OUTBOX_UNKNOWN_STORAGE_METHOD"
    # The valid marker is untouched by the refused recording.
    assert outbox.is_durability_verified() is True


def test_outbox_summary_reports_durability(tmp_path):
    """batch-outbox-status shows the gate state without raising,
    even when sending is refused."""
    outbox = bp.BatchOutbox(str(tmp_path / "outbox"))
    outbox.initialize("test — fresh outbox")
    summary = outbox.summary()
    assert summary["durability_verified"] is False
    assert summary["durability_method"] is None
    outbox.record_durability_verified("file_atomic_v1", "test")
    summary = outbox.summary()
    assert summary["durability_verified"] is True
    assert summary["durability_method"] == "file_atomic_v1"


# ---- concurrency (CONS-152 P1-03) ----

def _register_in_subprocess(directory: str, index: int, queue) -> None:
    """Module-level worker for the cross-process test below: a
    separate process opens its own BatchOutbox (its own RLock)
    on the same directory and registers one batch. Must stay at
    module level so multiprocessing (fork) can run it."""
    try:
        outbox = bp.BatchOutbox(directory)
        outbox.register(
            f"proc-l-{index}", f"proc-b-{index}", "a" * 64, 1, [])
        queue.put(None)
    except Exception as exc:  # noqa: BLE001 — collected, asserted below
        queue.put(repr(exc))


def test_outbox_concurrent_register_loses_nothing(tmp_path):
    """20 threads registering different logical batches at once:
    every registration succeeds and every record survives — no
    lost update from the read-modify-write, no shared-.tmp
    collision."""
    outbox = _fresh_outbox(tmp_path)
    errors: list = []

    def _worker(i):
        try:
            outbox.register(f"l-{i}", f"b-{i}", "a" * 64, 1, [])
        except Exception as exc:  # noqa: BLE001 — collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    summary = outbox.summary()
    assert summary["records"] == 20
    assert summary["by_state"] == {"registered": 20}
    # A fresh read of the file sees all 20 records.
    reloaded = bp.BatchOutbox(str(tmp_path / "outbox"))
    assert len(reloaded.list_undetermined()) == 20


def test_atomic_write_concurrent_same_path(tmp_path):
    """Concurrent durable commits to the same path: the final file
    is one complete write (valid JSON), and no .tmp file is left
    behind."""
    path = str(tmp_path / "shared.json")
    errors: list = []

    def _worker(i):
        try:
            bp._atomic_write_json(path, {"writer": i, "values": list(range(50))})
        except Exception as exc:  # noqa: BLE001 — collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with open(path) as f:
        data = json.load(f)
    assert data["values"] == list(range(50))
    assert isinstance(data["writer"], int)
    assert not list(tmp_path.glob("*.tmp"))


def test_outbox_concurrent_register_across_processes(tmp_path):
    """CONS-153 P1-03 (required test): 8 separate processes, each
    with its own BatchOutbox instance (its own RLock) on the same
    directory, registering different batches at once. Every
    registration succeeds and every record survives — the file
    lock (flock LOCK_EX) makes the whole read-modify-write atomic
    across processes, where the RLock alone cannot. Without the
    file lock, the last writer overwrites the earlier records."""
    _fresh_outbox(tmp_path)
    directory = str(tmp_path / "outbox")
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=_register_in_subprocess, args=(directory, i, queue))
        for i in range(8)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    assert all(p.exitcode == 0 for p in procs)
    errors = [queue.get(timeout=10) for _ in procs]
    assert [e for e in errors if e is not None] == []
    reloaded = bp.BatchOutbox(directory)
    assert len(reloaded.list_undetermined()) == 8
    assert reloaded.summary()["records"] == 8


def test_outbox_concurrent_register_across_instances(tmp_path):
    """CONS-153 P1-03: two BatchOutbox instances in the same
    process (different RLocks) on the same directory, registering
    from 10 threads each. No record is lost — the file lock, not
    the per-instance RLock, is what serializes them."""
    _fresh_outbox(tmp_path)
    directory = str(tmp_path / "outbox")
    errors: list = []

    def _worker(instance_no, i):
        try:
            outbox = bp.BatchOutbox(directory)
            outbox.register(
                f"inst{instance_no}-l-{i}", f"inst{instance_no}-b-{i}",
                "a" * 64, 1, [])
        except Exception as exc:  # noqa: BLE001 — collected, asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(n, i))
        for n in range(2) for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    reloaded = bp.BatchOutbox(directory)
    assert reloaded.summary()["records"] == 20


def test_outbox_file_lock_fields_initialized(tmp_path):
    """CONS-154 P1-03 (pinning test): a fresh BatchOutbox has
    both file-lock fields initialized in __init__ — depth 0, fd
    None. After a locked call (register) the lock is released
    again (depth 0, fd None), and a nested acquire/release pair
    (the re-entrancy path a @_locked method calling another
    @_locked method would take) also leaves depth 0 / fd None.
    This pins the initialization the CONS-154 draft excerpt cut
    off, so it is verified by execution, not by excerpt alone."""
    outbox = _fresh_outbox(tmp_path)
    assert outbox._file_lock_depth == 0
    assert outbox._file_lock_fd is None
    outbox.register("l-1", "b-1", "a" * 64, 1, [])
    assert outbox._file_lock_depth == 0
    assert outbox._file_lock_fd is None
    outbox._acquire_file_lock()
    outbox._acquire_file_lock()
    assert outbox._file_lock_depth == 2
    assert outbox._file_lock_fd is not None
    outbox._release_file_lock()
    outbox._release_file_lock()
    assert outbox._file_lock_depth == 0
    assert outbox._file_lock_fd is None


def test_outbox_lock_released_after_failure(tmp_path):
    """CONS-153 P1-03: a @_locked method that raises (reused
    batch_id) releases the file lock — the next register on the
    same instance, and on a fresh instance, still succeeds. A
    lock leaked by the failure path would deadlock both."""
    outbox = _fresh_outbox(tmp_path)
    outbox.register("l-1", "b-1", "a" * 64, 1, [])
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox.register("l-2", "b-1", "a" * 64, 1, [])
    assert ctx.value.code == "BATCH_ID_REUSED"
    outbox.register("l-2", "b-2", "a" * 64, 1, [])
    other = bp.BatchOutbox(str(tmp_path / "outbox"))
    other.register("l-3", "b-3", "a" * 64, 1, [])
    assert other.summary()["records"] == 3


def test_outbox_locked_covers_all_mutations():
    """CONS-153 P1-03 (reviewer note): the _locked definition
    alone proves nothing — every mutation method must carry it.
    initialize / require_ready / record_durability_verified /
    register / _update are all @_locked; mark_result /
    mark_unknown / mark_not_executed delegate to the locked
    _update; _persist_records has no lock of its own but is only
    called from locked methods; and _locked itself holds the file
    lock (flock) for the whole method."""
    src = pathlib.Path(bp.__file__).read_text(encoding="utf-8")
    for name in ("initialize", "require_ready",
                 "record_durability_verified", "register", "_update"):
        idx = src.index(f"    def {name}(")
        assert "@_locked" in src[max(0, idx - 200):idx], name
    for name in ("mark_result", "mark_unknown", "mark_not_executed"):
        start = src.index(f"    def {name}(")
        end = src.index("\n    def ", start + 10)
        assert "self._update(" in src[start:end], name
    persist = src.index("    def _persist_records(")
    assert "@_locked" not in src[max(0, persist - 200):persist]
    for call in ("self._persist_records([])",
                 "self._persist_records(records)"):
        assert call in src
    locked_start = src.index("def _locked(")
    locked_body = src[locked_start:src.index("\nclass BatchOutbox", locked_start)]
    assert "_acquire_file_lock()" in locked_body
    assert "_release_file_lock()" in locked_body
    assert "fcntl.flock(fd, fcntl.LOCK_EX)" in src
    assert 'OUTBOX_LOCK_FILE = "batch_outbox.lock"' in src


def test_outbox_load_missing_vs_unreadable(tmp_path):
    """CONS-153 P1-01 (VM side, same class as the phone fix):
    _load_records / _load_marker return None only when the file
    is missing; an unreadable file raises OUTBOX_CORRUPT and is
    never treated as an empty outbox. A directory in place of the
    file stands in for an unreadable file (root bypasses chmod 0,
    so permission bits cannot simulate it here)."""
    outbox = bp.BatchOutbox(str(tmp_path / "outbox"))
    assert outbox._load_records() is None
    assert outbox._load_marker() is None
    outbox_dir = tmp_path / "outbox"
    outbox_dir.mkdir()
    (outbox_dir / "batch_outbox.json").mkdir()
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox._load_records()
    assert ctx.value.code == "OUTBOX_CORRUPT"
    (outbox_dir / "batch_outbox_initialized").mkdir()
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox._load_marker()
    assert ctx.value.code == "OUTBOX_CORRUPT"


def test_outbox_load_permission_error_is_corrupt(tmp_path, monkeypatch):
    """CONS-153 P1-01 (VM side): an OSError other than
    FileNotFoundError while opening the outbox — e.g. a permission
    error — raises OUTBOX_CORRUPT, never returns None (empty)."""
    outbox = _fresh_outbox(tmp_path)
    real_open = open

    def _failing_open(path, *args, **kwargs):
        if str(path).endswith("batch_outbox.json"):
            raise PermissionError("permission denied (test)")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _failing_open)
    with pytest.raises(bp.ProtocolError) as ctx:
        outbox._load_records()
    assert ctx.value.code == "OUTBOX_CORRUPT"


# ---- the init CLI (CONS-152 P2-03) ----

def test_cli_outbox_init_requires_attestation(tmp_path, monkeypatch):
    """batch-outbox-init without --attestation refuses (exit 2)
    and creates nothing: the CLI must not auto-generate wording
    asserting the operator checked external records."""
    monkeypatch.setenv("MUSE_BRIDGE_OUTBOX_DIR", str(tmp_path / "outbox"))
    assert ab.main(["batch-outbox-init"]) == 2
    assert not os.path.exists(str(tmp_path / "outbox"))
    assert ab.main([
        "batch-outbox-init", "--attestation",
        "test — fresh install, verified by the operator",
    ]) == 0
    assert os.path.exists(str(tmp_path / "outbox" / "batch_outbox.json"))
    assert os.path.exists(str(tmp_path / "outbox" / "batch_outbox_initialized"))


def test_cli_outbox_durability_verify(tmp_path, monkeypatch):
    """batch-outbox-durability-verify records the marker; without
    --method it refuses (exit 2)."""
    monkeypatch.setenv("MUSE_BRIDGE_OUTBOX_DIR", str(tmp_path / "outbox"))
    assert ab.main(["batch-outbox-durability-verify"]) == 2
    assert ab.main([
        "batch-outbox-init", "--attestation", "test — fresh install",
    ]) == 0
    assert ab.main([
        "batch-outbox-durability-verify", "--method", "file_atomic_v1",
        "--note", "test — crash-durability check passed",
    ]) == 0
    outbox = bp.BatchOutbox(str(tmp_path / "outbox"))
    assert outbox.is_durability_verified() is True
    assert ab.main(["batch-outbox-status"]) == 0
