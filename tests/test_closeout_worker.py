"""Closeout runs in a background worker; requests only persist and report.

Control Tower abandons a request at 30 s, so begin and status must return while
the drains, finalizer and audit are still running. Tests use events for every
blocking step, a fake clock for every timer, and release every gate in teardown.
The only real-time bound is the one a request must meet (5 s).
"""

from __future__ import annotations

import copy
import threading
import time

import pytest

from server import closeout
from server.capture_journal import CaptureHeldError, CaptureJournal, MemoryCaptureStorage
from server.closeout import CloseoutConflict, CloseoutManager
from server.closeout_guard import control_transaction
from server.event_emitter import EventEmitter

BINDING = {"run_id": "worker-run", "unit_id": "worker-unit", "workspace_id": "123",
           "owner": "alice@example.com"}
CAPTURE = "capture-worker"
REQUEST_BOUND = 5.0  # the only real-time bound: a request must return within this


class Clock:
    """Fake clock shared by the manager (persisted times) and the worker (timers)."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def capture_enabled(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")


@pytest.fixture
def env(monkeypatch):
    """Per-test harness: gates to release, managers to join, a fake clock."""
    monkeypatch.setattr(closeout, "_WORKER_BACKOFF_SECONDS", 0.001)
    harness = type("Harness", (), {})()
    harness.clock = Clock()
    harness.gates = []
    harness.managers = []
    yield harness
    for gate_event in harness.gates:
        gate_event.set()
    for manager in harness.managers:
        thread = manager.emitter._closeout_worker._thread
        if thread is not None:
            thread.join(timeout=REQUEST_BOUND * 2)
        assert thread is None or not thread.is_alive(), "closeout worker leaked past its test"


def gate(harness) -> threading.Event:
    event = threading.Event()
    harness.gates.append(event)
    return event


def build_emitter(storage):
    journal = CaptureJournal(storage, BINDING, {})
    return EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                        ingest_url="", ingest_token="", journal=journal)


def drained(_timeout):
    return {"complete": True, "issues": [], "outcomes": []}


def complete_finalizer(source, capture_id):
    sequence = source.emit("workshop.signal", BINDING["owner"], {"final": capture_id},
                           idempotency_key=f"final:{capture_id}")
    return {"complete": True, "issues": [], "outputs": {"final_signal_sequence": sequence}}


def make(harness, *, storage=None, drainer=None, finalizer=None):
    emitter = build_emitter(storage if storage is not None else MemoryCaptureStorage())
    manager = CloseoutManager(emitter, drainer=drainer or drained,
                              finalizer=finalizer or complete_finalizer, clock=harness.clock)
    emitter._closeout_worker.clock = harness.clock
    harness.managers.append(manager)
    return manager


def begin(manager, timeout=600):
    return manager.begin(capture_id=CAPTURE, stream_id=manager.emitter.stream_id,
                         source=copy.deepcopy(BINDING), drain_timeout_seconds=timeout)


def wait_until(predicate, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def sealed(manager) -> dict:
    assert wait_until(lambda: manager.status(capture_id=CAPTURE)["phase"] == "SEALED"), \
        manager.status(capture_id=CAPTURE)
    return manager.status(capture_id=CAPTURE)


def within_bound(call):
    """Run a request-path call on its own thread; fail if it does not return in time."""
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as error:  # re-raised on the test thread below
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(REQUEST_BOUND)
    assert not thread.is_alive(), f"request-path call did not return within {REQUEST_BOUND} s"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def worker(manager):
    return manager.emitter._closeout_worker


def test_worker_seals_and_a_sealed_status_has_no_progress(env):
    manager = make(env)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    status = sealed(manager)
    assert status["complete"] is True and "progress" not in status and "drain" not in status
    assert manager.status(capture_id=CAPTURE) == status


def test_begin_and_resume_return_while_a_drain_is_blocked(env):
    release, entered = gate(env), threading.Event()

    def blocked_drain(_timeout):
        entered.set()
        release.wait(REQUEST_BOUND * 4)
        return drained(_timeout)

    manager = make(env, drainer=blocked_drain)
    assert within_bound(lambda: begin(manager))["phase"] == "CLOSING"
    status = within_bound(lambda: manager.resume(capture_id=CAPTURE))
    assert entered.wait(REQUEST_BOUND)
    assert status["phase"] == "CLOSING"
    assert status["progress"] == {"stage": "draining", "active_writers": 0,
                                  "drains_pending": True, "worker": "running"}
    release.set()
    sealed(manager)


def test_status_reads_return_while_the_finalizer_and_the_audit_run(env, monkeypatch):
    release_finalizer, release_audit = gate(env), gate(env)
    finalizer_entered = threading.Event()

    def blocked_finalizer(source, capture_id):
        finalizer_entered.set()
        release_finalizer.wait(REQUEST_BOUND * 4)
        return complete_finalizer(source, capture_id)

    manager = make(env, finalizer=blocked_finalizer)
    real_audit = manager.emitter.journal.audit_snapshot

    def blocked_audit(**kwargs):
        release_audit.wait(REQUEST_BOUND * 4)
        return real_audit(**kwargs)

    monkeypatch.setattr(manager.emitter.journal, "audit_snapshot", blocked_audit)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    assert finalizer_entered.wait(REQUEST_BOUND)
    status = within_bound(lambda: manager.status(capture_id=CAPTURE))
    assert status["progress"]["stage"] == "finalizing"
    release_finalizer.set()
    assert wait_until(lambda: manager.status(capture_id=CAPTURE)["progress"]["stage"] == "auditing")
    status = within_bound(lambda: manager.resume(capture_id=CAPTURE))
    assert status["phase"] == "CLOSING" and status["progress"]["stage"] == "auditing"
    release_audit.set()
    assert sealed(manager)["complete"] is True


def test_a_failure_is_reported_persisted_and_retried_after_its_backoff(env):
    calls = []

    def flaky(source, capture_id):
        calls.append(capture_id)
        if len(calls) == 1:
            raise RuntimeError("summary store unavailable")
        return complete_finalizer(source, capture_id)

    manager = make(env, finalizer=flaky)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    assert wait_until(lambda: worker(manager).state == "failed" and worker(manager)._thread is None)
    status = manager.status(capture_id=CAPTURE)
    assert status["phase"] == "CLOSING" and status["progress"]["worker"] == "failed"
    assert status["progress"]["last_error"] == {"code": "RuntimeError", "detail": ""}
    assert status["progress"]["stage"] == "draining"  # the claim was released
    assert "worker_failed:RuntimeError" in manager.journal.read()["closeout"]["issues"]

    generation = worker(manager)._generation
    manager.resume(capture_id=CAPTURE)  # inside the 1 s backoff: no new pass
    assert worker(manager)._generation == generation

    env.clock.advance(1)
    manager.resume(capture_id=CAPTURE)
    assert sealed(manager)["complete"] is True
    assert len(calls) == 2


def test_a_request_level_conflict_is_raised_once_on_the_next_read(env):
    calls = []

    def conflicting(source, capture_id):
        calls.append(capture_id)
        if len(calls) == 1:
            raise CloseoutConflict("closeout finalizer ownership changed")
        return complete_finalizer(source, capture_id)

    manager = make(env, finalizer=conflicting)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    assert wait_until(lambda: worker(manager).state == "failed" and worker(manager)._thread is None)
    env.clock.advance(1)
    with pytest.raises(CloseoutConflict):
        manager.resume(capture_id=CAPTURE)  # the 409 CT expects, once
    assert sealed(manager)["complete"] is True


def test_repeated_failures_back_off_one_five_thirty_seconds(env):
    calls = []

    def always_failing(source, capture_id):
        calls.append(capture_id)
        raise RuntimeError("boom")

    manager = make(env, finalizer=always_failing)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    for count, delay in [(1, 1.0), (2, 5.0), (3, 30.0)]:
        assert wait_until(lambda: len(calls) == count and worker(manager)._thread is None)
        assert worker(manager).retry_after - env.clock.now == pytest.approx(delay)
        if count < 3:
            env.clock.advance(delay - 0.5)
            manager.resume(capture_id=CAPTURE)  # still backing off: no pass
            assert len(calls) == count
            env.clock.advance(0.5)
            manager.resume(capture_id=CAPTURE)  # backoff over: one new pass
    assert len(calls) == 3


def test_incomplete_keeps_its_manifest_and_a_retry_is_visible(env):
    release, entered = gate(env), threading.Event()
    calls = []

    def finalizer(source, capture_id):
        calls.append(capture_id)
        if len(calls) == 1:
            return {"complete": False, "issues": ["final_outputs_incomplete"], "outputs": {}}
        entered.set()
        release.wait(REQUEST_BOUND * 4)
        return complete_finalizer(source, capture_id)

    manager = make(env, finalizer=finalizer)
    begin(manager, timeout=1)
    env.clock.advance(2)  # past the drain deadline, so an incomplete seal is INCOMPLETE
    manager.resume(capture_id=CAPTURE)
    assert wait_until(lambda: manager.status(capture_id=CAPTURE)["phase"] == "INCOMPLETE"
                      and worker(manager)._thread is None)
    held = manager.status(capture_id=CAPTURE)
    assert held["manifest"]["complete"] is False
    assert held["progress"]["worker"] == "idle" and held["progress"]["stage"] == "incomplete"

    generation = worker(manager)._generation
    assert within_bound(lambda: manager.resume(capture_id=CAPTURE))["manifest"] == held["manifest"]
    assert worker(manager)._generation == generation  # still backing off

    env.clock.advance(30)
    during = within_bound(lambda: manager.resume(capture_id=CAPTURE))
    assert entered.wait(REQUEST_BOUND)
    assert during["phase"] == "INCOMPLETE" and during["manifest"] == held["manifest"]
    assert during["progress"]["retrying"] is True
    release.set()
    done = sealed(manager)
    assert done["complete"] is True and "progress" not in done


def test_closing_passes_are_paced_while_drains_keep_running(env):
    drain_calls, final_calls = [], []
    finish = {"value": False}

    def counting_drain(_timeout):
        drain_calls.append(1)
        return drained(_timeout)

    def incomplete_until_told(source, capture_id):
        final_calls.append(capture_id)
        if finish["value"]:
            return complete_finalizer(source, capture_id)
        return {"complete": False, "issues": ["final_outputs_incomplete"], "outputs": {}}

    manager = make(env, drainer=counting_drain, finalizer=incomplete_until_told)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    assert wait_until(lambda: len(final_calls) == 1)
    assert wait_until(lambda: len(drain_calls) >= 10)  # many drain passes, no finalize
    assert len(final_calls) == 1
    finish["value"] = True
    env.clock.advance(30)  # the full-pass interval has passed
    assert sealed(manager)["complete"] is True
    assert len(final_calls) == 2


def test_an_idle_worker_stops_and_the_next_read_restarts_it(env):
    open_drain = {"value": False}

    def drain(_timeout):
        if open_drain["value"]:
            return drained(_timeout)
        return {"complete": False, "issues": ["writer_drain_pending"], "outcomes": []}

    manager = make(env, drainer=drain)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    assert wait_until(lambda: manager.status(capture_id=CAPTURE)["progress"]["drains_pending"])
    env.clock.advance(301)  # nobody has polled for more than 5 minutes
    assert wait_until(lambda: worker(manager)._thread is None)
    status = manager.status(capture_id=CAPTURE)
    assert status["phase"] == "CLOSING" and status["progress"]["worker"] == "idle"

    open_drain["value"] = True
    manager.resume(capture_id=CAPTURE)  # the next read restarts it
    assert sealed(manager)["complete"] is True


def test_a_stale_worker_is_replaced_and_the_claim_fence_holds(env):
    release, entered = gate(env), threading.Event()
    calls = []

    def hung_once(_timeout):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            release.wait(REQUEST_BOUND * 4)
        return drained(_timeout)

    manager = make(env, drainer=hung_once)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    assert entered.wait(REQUEST_BOUND)
    first = worker(manager)._generation
    env.clock.advance(241)  # no heartbeat for more than 2 x the finalizer lease
    assert manager.status(capture_id=CAPTURE)["progress"]["worker"] == "stale"
    manager.resume(capture_id=CAPTURE)
    assert worker(manager)._generation == first + 1
    assert sealed(manager)["complete"] is True

    release.set()  # the superseded thread wakes and must not change the outcome
    assert wait_until(lambda: worker(manager)._thread is None)
    assert manager.status(capture_id=CAPTURE)["phase"] == "SEALED"


def test_restart_mid_finalize_resumes_from_persisted_state_and_seals(env):
    storage = MemoryCaptureStorage()
    release, entered = gate(env), threading.Event()

    def hanging_finalizer(source, capture_id):
        entered.set()
        release.wait(REQUEST_BOUND * 4)  # the process "dies" here; this thread is abandoned
        return complete_finalizer(source, capture_id)

    old = make(env, storage=storage, finalizer=hanging_finalizer)
    begin(old)
    old.resume(capture_id=CAPTURE)
    assert entered.wait(REQUEST_BOUND)
    assert old.status(capture_id=CAPTURE)["progress"]["stage"] == "finalizing"

    # A replacement process on the same retained storage. The dead worker's
    # finalizer lease is past, so the replacement may claim the closeout again.
    env.clock.advance(121)
    replacement = make(env, storage=storage)
    persisted = replacement.status(capture_id=CAPTURE)
    assert persisted["phase"] == "CLOSING" and persisted["progress"]["stage"] == "finalizing"
    assert persisted["progress"]["worker"] == "idle"
    replacement.resume(capture_id=CAPTURE)
    result = sealed(replacement)
    assert result["complete"] is True

    release.set()  # the abandoned finalizer returns; it must not write anything
    assert wait_until(lambda: worker(old)._thread is None)
    assert replacement.status(capture_id=CAPTURE) == result


def test_an_idempotent_begin_and_a_different_request_conflict(env):
    manager = make(env)
    first = begin(manager)
    again = within_bound(lambda: begin(manager))
    assert (again["generation"], again["capture_id"]) == (first["generation"], first["capture_id"])
    manager.resume(capture_id=CAPTURE)
    result = sealed(manager)
    assert begin(manager) == result
    with pytest.raises(CloseoutConflict):
        manager.begin(capture_id="other", stream_id=manager.emitter.stream_id,
                      source=copy.deepcopy(BINDING), drain_timeout_seconds=600)


def test_resume_never_starts_a_second_worker_while_one_runs(env):
    release, entered = gate(env), threading.Event()

    def blocked(_timeout):
        entered.set()
        release.wait(REQUEST_BOUND * 4)
        return drained(_timeout)

    manager = make(env, drainer=blocked)
    begin(manager)
    manager.resume(capture_id=CAPTURE)
    assert entered.wait(REQUEST_BOUND)
    generation = worker(manager)._generation
    for _ in range(5):
        within_bound(lambda: manager.resume(capture_id=CAPTURE))
    assert worker(manager)._generation == generation
    release.set()
    sealed(manager)


def test_a_worker_that_cannot_start_is_reported_and_retried(env, monkeypatch):
    manager = make(env)
    begin(manager)

    def cannot_start(self):
        raise RuntimeError("can't start new thread")

    # Patched for this call only; no helper thread is created while it is in place.
    monkeypatch.setattr(closeout.threading.Thread, "start", cannot_start)
    status = manager.resume(capture_id=CAPTURE)
    monkeypatch.undo()
    assert status["phase"] == "CLOSING" and status["progress"]["worker"] == "failed"
    assert status["progress"]["last_error"]["code"] == "worker_start_failed"
    assert "worker_failed:worker_start_failed" in manager.journal.read()["closeout"]["issues"]

    env.clock.advance(1)
    manager.resume(capture_id=CAPTURE)
    assert sealed(manager)["complete"] is True


def test_invalid_persisted_stage_is_held(env):
    manager = make(env)
    begin(manager)

    def corrupt(state):
        state["closeout"]["stage"] = ["not", "a", "stage"]

    control_transaction(manager.journal, corrupt, durable=True)
    with pytest.raises(CaptureHeldError):
        manager.status(capture_id=CAPTURE)
