"""Closeout runs in a background worker; requests only persist and report.

Control Tower abandons a request at 30 s, so begin and status must return fast
while the drains, finalizer and audit take as long as they take.
"""

from __future__ import annotations

import copy
import threading
import time

import pytest

from server.capture_journal import CaptureHeldError, CaptureJournal, MemoryCaptureStorage
from server.closeout import CloseoutConflict, CloseoutManager
from server.closeout_guard import control_transaction
from server.event_emitter import EventEmitter

BINDING = {"run_id": "worker-run", "unit_id": "worker-unit", "workspace_id": "123",
           "owner": "alice@example.com"}
CAPTURE = "capture-worker"
FAST = 1.0  # seconds: the bound for any request-path call


@pytest.fixture(autouse=True)
def capture_enabled(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")


def build_emitter(storage):
    journal = CaptureJournal(storage, BINDING, {})
    return EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                        ingest_url="", ingest_token="", journal=journal)


def drained(_timeout):
    return {"complete": True, "issues": [], "outcomes": []}


def finalizer(source, capture_id):
    sequence = source.emit("workshop.signal", BINDING["owner"], {"final": capture_id},
                           idempotency_key=f"final:{capture_id}")
    return {"complete": True, "issues": [], "outputs": {"final_signal_sequence": sequence}}


def begin(manager, timeout=60):
    return manager.begin(capture_id=CAPTURE, stream_id=manager.emitter.stream_id,
                         source=copy.deepcopy(BINDING), drain_timeout_seconds=timeout)


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def wait_sealed(manager, timeout=20.0):
    assert wait_until(lambda: manager.status(capture_id=CAPTURE)["phase"] == "SEALED", timeout), \
        manager.status(capture_id=CAPTURE)
    return manager.status(capture_id=CAPTURE)


def timed(call):
    started = time.monotonic()
    result = call()
    return result, time.monotonic() - started


def test_worker_seals_and_a_sealed_status_has_no_progress():
    manager = CloseoutManager(build_emitter(MemoryCaptureStorage()), drainer=drained, finalizer=finalizer)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    sealed = wait_sealed(manager)
    assert sealed["complete"] is True and "progress" not in sealed and "drain" not in sealed
    assert manager.status(capture_id=CAPTURE) == sealed


def test_begin_and_resume_return_at_once_while_drain_is_slow():
    gate = threading.Event()

    def slow_drain(_timeout):
        gate.wait(30)
        return drained(_timeout)

    manager = CloseoutManager(build_emitter(MemoryCaptureStorage()), drainer=slow_drain, finalizer=finalizer)
    status, elapsed = timed(lambda: begin(manager))
    assert elapsed < FAST and status["phase"] == "CLOSING"
    status, elapsed = timed(lambda: manager.resume(capture_id=CAPTURE))
    assert elapsed < FAST, elapsed
    assert status["phase"] == "CLOSING"
    assert status["progress"] == {"stage": "draining", "active_writers": 0,
                                  "drains_pending": True, "worker": "running"}
    gate.set()
    wait_sealed(manager)


def test_status_reads_stay_fast_while_finalizer_and_audit_run(monkeypatch):
    finalizer_gate, audit_gate = threading.Event(), threading.Event()
    stages = []

    def slow_finalizer(source, capture_id):
        stages.append("finalizer entered")
        finalizer_gate.wait(30)
        return finalizer(source, capture_id)

    emitter = build_emitter(MemoryCaptureStorage())
    real_audit = emitter.journal.audit_snapshot

    def slow_audit(**kwargs):
        audit_gate.wait(30)
        return real_audit(**kwargs)

    monkeypatch.setattr(emitter.journal, "audit_snapshot", slow_audit)
    manager = CloseoutManager(emitter, drainer=drained, finalizer=slow_finalizer)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    assert wait_until(lambda: stages == ["finalizer entered"])

    status, elapsed = timed(lambda: manager.status(capture_id=CAPTURE))
    assert elapsed < FAST and status["progress"]["stage"] == "finalizing"
    status, elapsed = timed(lambda: manager.resume(capture_id=CAPTURE))
    assert elapsed < FAST and status["phase"] == "CLOSING"
    finalizer_gate.set()

    assert wait_until(lambda: manager.status(capture_id=CAPTURE)["progress"]["stage"] == "auditing")
    status, elapsed = timed(lambda: manager.status(capture_id=CAPTURE))
    assert elapsed < FAST and status["phase"] == "CLOSING"
    audit_gate.set()
    assert wait_sealed(manager)["complete"] is True


def test_a_failed_finalizer_is_retried_by_the_next_status_read_and_seals():
    attempts = []

    def flaky_finalizer(source, capture_id):
        attempts.append(capture_id)
        if len(attempts) == 1:
            raise RuntimeError("summary store unavailable")
        return finalizer(source, capture_id)

    manager = CloseoutManager(build_emitter(MemoryCaptureStorage()), drainer=drained, finalizer=flaky_finalizer)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    worker = manager.emitter._closeout_worker
    assert wait_until(lambda: worker.state == "failed")
    status = manager.status(capture_id=CAPTURE)
    assert status["phase"] == "CLOSING" and status["progress"]["worker"] == "failed"
    assert status["progress"]["stage"] == "draining"  # the claim was released
    manager.resume(capture_id=CAPTURE)
    assert wait_sealed(manager)["complete"] is True
    assert len(attempts) == 2


def test_restart_mid_finalize_resumes_from_persisted_state_and_seals():
    storage = MemoryCaptureStorage()
    gate = threading.Event()

    def hanging_finalizer(source, capture_id):
        gate.wait(60)  # the process "dies" here; this thread is abandoned
        return finalizer(source, capture_id)

    first = build_emitter(storage)
    old = CloseoutManager(first, drainer=drained, finalizer=hanging_finalizer)
    begin(old)
    old.resume(capture_id=CAPTURE)
    assert wait_until(lambda: old.status(capture_id=CAPTURE)["progress"]["stage"] == "finalizing")

    # A replacement process on the same retained storage. Its clock is past the
    # dead worker's finalizer lease, so it may claim the closeout again.
    replacement_emitter = build_emitter(storage)
    replacement = CloseoutManager(replacement_emitter, drainer=drained, finalizer=finalizer,
                                  clock=lambda: time.time() + 1000)
    persisted = replacement.status(capture_id=CAPTURE)
    assert persisted["phase"] == "CLOSING" and persisted["progress"]["stage"] == "finalizing"
    assert persisted["progress"]["worker"] == "idle"
    replacement.resume(capture_id=CAPTURE)
    sealed = wait_sealed(replacement)
    assert sealed["complete"] is True

    gate.set()  # the abandoned finalizer returns; it must not write anything
    assert wait_until(lambda: first._closeout_worker.state != "running")
    assert replacement.status(capture_id=CAPTURE) == sealed


def test_repeat_begin_is_idempotent_and_a_different_request_conflicts():
    manager = CloseoutManager(build_emitter(MemoryCaptureStorage()), drainer=drained, finalizer=finalizer)
    first = begin(manager)
    again, elapsed = timed(lambda: begin(manager))
    assert elapsed < FAST
    assert (again["generation"], again["capture_id"]) == (first["generation"], first["capture_id"])
    manager.resume(capture_id=CAPTURE)
    sealed = wait_sealed(manager)
    assert begin(manager) == sealed
    with pytest.raises(CloseoutConflict):
        manager.begin(capture_id="other", stream_id=manager.emitter.stream_id,
                      source=copy.deepcopy(BINDING), drain_timeout_seconds=60)


def test_resume_never_starts_a_second_worker_while_one_is_running():
    gate = threading.Event()
    calls = []

    def counting_drain(_timeout):
        calls.append(1)
        gate.wait(30)
        return drained(_timeout)

    manager = CloseoutManager(build_emitter(MemoryCaptureStorage()), drainer=counting_drain, finalizer=finalizer)
    begin(manager)
    for _ in range(5):
        manager.resume(capture_id=CAPTURE)
    assert wait_until(lambda: len(calls) >= 1)
    time.sleep(0.2)
    assert len(calls) == 1
    gate.set()
    wait_sealed(manager)


def test_invalid_persisted_stage_is_held_before_any_status_is_reported():
    manager = CloseoutManager(build_emitter(MemoryCaptureStorage()), drainer=drained, finalizer=finalizer)
    begin(manager)

    def corrupt(state):
        state["closeout"]["stage"] = ["not", "a", "stage"]

    control_transaction(manager.journal, corrupt, durable=True)
    with pytest.raises(CaptureHeldError):
        manager.status(capture_id=CAPTURE)
