"""Write-behind capture: attendee paths never wait on storage; nothing is lost.

Production capture (server/capture.py) runs this mode. A transaction commits
to process memory; one flusher appends the accumulated change as a single
operation in the unchanged retained format. CT collection, closeout and
shutdown flush first, so nothing published outside the process can be lost.
"""
from __future__ import annotations

import fcntl
import os
import time

import pytest

from server.capture_journal import (
    CaptureCapacityError,
    CaptureJournal,
    CaptureStorageError,
    MemoryCaptureStorage,
)

BINDING = {"run_id": "run", "unit_id": "unit", "workspace_id": "123", "owner": "alice@example.com"}


class CountingStorage(MemoryCaptureStorage):
    def __init__(self, *, delay: float = 0.0):
        super().__init__()
        self.calls = 0
        self.delay = delay
        self.fail = False

    def _touch(self):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise OSError("workspace storage unavailable")

    def list_names(self):
        self._touch()
        return super().list_names()

    def read(self, name):
        self._touch()
        return super().read(name)

    def create(self, name, content):
        self._touch()
        return super().create(name, content)


def journal(storage, **kwargs):
    kwargs.setdefault("flush_interval", 60.0)  # tests flush explicitly
    return CaptureJournal(storage, BINDING, {}, write_behind=True, **kwargs)


def operations(storage):
    return sorted(name for name in MemoryCaptureStorage.list_names(storage) if not name.startswith("checkpoint"))


def test_transactions_never_touch_storage_and_flush_one_operation():
    storage = CountingStorage()
    live = journal(storage)
    live.read()  # loading retained history once is the only startup I/O
    storage.calls = 0
    for n in range(500):
        live.transaction(lambda state, n=n: state.update(count=n))
    assert storage.calls == 0
    assert live.health()["pending_operations"] == 500
    live.flush()
    assert live.health()["pending_operations"] == 0
    # Genesis plus exactly one coalesced operation for 500 accepted changes.
    assert len(operations(storage)) == 2
    assert journal(storage).read() == {"count": 499}


def test_slow_storage_does_not_slow_producers():
    storage = CountingStorage(delay=0.05)
    live = journal(storage)
    live.read()
    started = time.monotonic()
    for n in range(2000):
        live.transaction(lambda state, n=n: state.update(last=n))
    assert time.monotonic() - started < 1.0  # 2000 x 3 round trips would be ~5 minutes


def test_flush_failure_never_reaches_producers_and_recovers():
    storage = CountingStorage()
    live = journal(storage)
    live.transaction(lambda state: state.update(kept="before"))
    storage.fail = True
    live.transaction(lambda state: state.update(kept="during outage"))  # accepted
    with pytest.raises(CaptureStorageError):
        live.flush()
    assert live.read() == {"kept": "during outage"}
    assert live.health()["pending_operations"] == 2
    storage.fail = False
    live.flush()
    assert live.health()["pending_operations"] == 0
    assert journal(storage).read() == {"kept": "during outage"}


def test_durable_transaction_retains_before_returning_and_is_atomic():
    storage = CountingStorage()
    live = journal(storage)
    live.transaction(lambda state: state.update(order=["pending"]))
    live.transaction(lambda state: state["order"].append("policy"), durable=True)
    # Earlier accepted changes are retained first, in order, then the barrier.
    assert journal(storage).read() == {"order": ["pending", "policy"]}
    storage.fail = True
    with pytest.raises(CaptureStorageError):
        live.transaction(lambda state: state["order"].append("refused"), durable=True)
    assert live.read() == {"order": ["pending", "policy"]}
    assert live.health()["pending_operations"] == 0


def test_foreign_append_is_rebased_without_losing_either_change():
    storage = CountingStorage()
    first = journal(storage)
    first.transaction(lambda state: state.update(first=1))
    first.flush()
    second = journal(storage)
    second.transaction(lambda state: state.update(second=2), durable=True)
    first.transaction(lambda state: state.update(first_again=3))
    first.flush()  # detects the foreign head and replays its own change on top
    assert journal(storage).read() == {"first": 1, "second": 2, "first_again": 3}


def test_audit_and_snapshot_see_everything_accepted():
    storage = CountingStorage()
    live = journal(storage)
    live.transaction(lambda state: state.update(events=[1, 2, 3]))
    assert live.audit() == {"events": [1, 2, 3]}
    live.transaction(lambda state: state["events"].append(4))
    snapshot = live.audit_snapshot(progress=lambda: None)
    assert snapshot.read() == {"events": [1, 2, 3, 4]}


def test_background_flusher_retains_without_any_caller():
    storage = CountingStorage()
    live = journal(storage, flush_interval=0.05)
    live.transaction(lambda state: state.update(heartbeat=True))
    deadline = time.monotonic() + 3
    while live.health()["pending_operations"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert live.health()["pending_operations"] == 0
    assert journal(storage).read() == {"heartbeat": True}
    live.close()


def test_close_retains_everything_and_stops_the_flusher():
    storage = CountingStorage()
    live = journal(storage, flush_interval=0.05)
    for n in range(10):
        live.transaction(lambda state, n=n: state.update(n=n))
    live.close()
    assert journal(storage).read() == {"n": 9}
    flusher = live._flusher
    if flusher is not None:
        flusher.join(1)
        assert not flusher.is_alive()


def test_large_burst_is_retained_change_by_change():
    storage = CountingStorage()
    live = journal(storage, max_record_bytes=4096)
    for n in range(8):
        live.transaction(lambda state, n=n: state.update({f"k{n}": "x" * 900}))
    live.flush()
    assert len(operations(storage)) > 2  # one combined operation would exceed the limit
    assert journal(storage, max_record_bytes=4096).read() == {f"k{n}": "x" * 900 for n in range(8)}


def test_a_single_oversized_change_is_still_refused_to_its_producer():
    live = journal(CountingStorage(), max_record_bytes=1024)
    with pytest.raises(CaptureCapacityError):
        live.transaction(lambda state: state.update(blob="x" * 4096))
    assert live.read() == {}


def test_flush_holds_the_process_file_lock(tmp_path, monkeypatch):
    # Since #132 _process_file_lock referenced ``config`` without importing it;
    # the NameError was swallowed by its fail-open except and the lock was never
    # taken. Assert the lock file exists and is held exclusively during flush.
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    lock_path = tmp_path / ".capture.lock"
    observed: dict[str, object] = {}

    class LockProbingStorage(MemoryCaptureStorage):
        def create(self, name, content):
            observed["lock_exists"] = lock_path.exists()
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed["held_during_flush"] = True
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                observed["held_during_flush"] = False
            finally:
                os.close(fd)
            return super().create(name, content)

    storage = LockProbingStorage()
    live = journal(storage)
    live.read()
    live.transaction(lambda state: state.update(k="v"))
    live.flush()

    assert observed == {"lock_exists": True, "held_during_flush": True}
    # Released after flush: the probe acquires it without contention.
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_help_routes_flush_before_return(monkeypatch):
    from server import help as help_module

    help_module.reset_for_tests()
    monkeypatch.setattr(help_module, "_push_control_tower_message", lambda *a, **k: False)
    monkeypatch.setattr(help_module, "_push_control_tower", lambda *a, **k: False)

    storage = CountingStorage()
    live = journal(storage, flush_interval=3600)
    help_module.configure_capture(live)

    accepted = help_module.post_attendee_message("crash-window-help")
    assert live.health()["pending_operations"] == 0
    # Model a sudden container crash without calling close() or flush().
    live._closed = True
    live._flush_wake.set()

    help_module.reset_for_tests()
    successor = journal(storage, flush_interval=3600)
    help_module.configure_capture(successor)
    offered = help_module.presence_fields()["help_outbox"]
    assert [e["message_id"] for e in offered if e["kind"] == "message"] == [
        accepted["message"]["message_id"]
    ]

    ack_res = help_module.ack_outbox(offered[-1]["seq"])
    assert ack_res["pending"] == 0
    assert successor.health()["pending_operations"] == 0
    successor._closed = True
    successor._flush_wake.set()

    help_module.reset_for_tests()
    after_ack = journal(storage, flush_interval=3600)
    help_module.configure_capture(after_ack)
    assert help_module.presence_fields()["help_outbox"] == []
    after_ack.close()


def test_flush_barrier_is_bounded_and_fails_open(monkeypatch):
    from server import help as help_module

    help_module.reset_for_tests()
    monkeypatch.setattr(help_module, "_push_control_tower_message", lambda *a, **k: False)
    monkeypatch.setattr(help_module, "_push_control_tower", lambda *a, **k: False)

    storage = CountingStorage()
    live = journal(storage, flush_interval=3600)
    help_module.configure_capture(live)

    storage.fail = True
    accepted = help_module.post_attendee_message("degraded-storage-help")
    assert accepted["raised"] is True
    assert help_module.flush_help_persistence(timeout=0.05) is False

    storage.fail = False
    storage.delay = 0.25
    live.transaction(lambda state: state["help"].update(note="slow-storage-help"))
    started = time.monotonic()
    assert help_module.flush_help_persistence(timeout=0.02) is False
    assert (time.monotonic() - started) < 0.15

    storage.delay = 0.0
    assert help_module.flush_help_persistence(timeout=1.0) is True
    assert live.health()["pending_operations"] == 0
    live.close()
