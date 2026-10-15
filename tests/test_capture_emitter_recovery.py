"""Actual producer delivery: commit-safe ACK, exact replay and visible faults."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from server.capture_journal import (
    CaptureCapacityError,
    CaptureHeldError,
    CaptureJournal,
    CaptureStorageError,
    LocalCaptureStorage,
    MemoryCaptureStorage,
)
from server.event_emitter import (
    CaptureConflictError,
    CaptureEncodingError,
    EventEmitter,
)

BINDING = {
    "run_id": "run-emitter",
    "unit_id": "unit-emitter",
    "workspace_id": "123",
    "owner": "alice@example.com",
}


def make(storage=None, **kwargs):
    store = storage if storage is not None else MemoryCaptureStorage()
    retained = CaptureJournal(store, BINDING, {})
    return EventEmitter(
        run_id=BINDING["run_id"],
        workspace_id=BINDING["workspace_id"],
        ingest_url="",
        ingest_token="",
        journal=retained,
        **kwargs,
    )


def append(emitter, count):
    for index in range(count):
        emitter.emit(
            "workshop.signal",
            BINDING["owner"],
            {"value": index},
            idempotency_key=f"signal-{index}",
        )


def ack(emitter, position):
    return emitter.acknowledge(
        schema_version=2, stream_id=emitter.stream_id, through_seq=position
    )


def test_get_cursor_never_acknowledges_and_fresh_journal_replays_exact_bytes(tmp_path):
    original = make(LocalCaptureStorage(tmp_path))
    append(original, 5)
    first = original.collect(limit=2)
    assert first["schema_version"] == 2 and first["ack_mode"] == "explicit"
    assert first["high_water"] == 2 and first["pending"] == 5
    second = original.collect(after=2, stream=first["stream_id"], limit=2)
    assert [row["seq"] for row in second["events"]] == [3, 4]
    assert original.pending() == 5
    replacement = make(LocalCaptureStorage(tmp_path))
    assert replacement.stream_id == first["stream_id"]
    assert replacement.collect(limit=2)["events"] == first["events"]
    ack(replacement, 2)
    assert replacement.pending() == 3
    assert original.pending() == 3
    assert (
        original.journal.read()["insight"]["events"]["1"] == first["events"][0]["event"]
    )


def test_lost_ack_response_repeats_idempotently_after_restart(tmp_path):
    original = make(LocalCaptureStorage(tmp_path))
    append(original, 3)
    epoch = original.stream_id
    # The source commits this ACK, but its HTTP reply is lost to CT.
    ack(original, 2)
    replacement = make(LocalCaptureStorage(tmp_path))
    assert replacement.acknowledge(
        schema_version=2, stream_id=epoch, through_seq=2
    ) == {"schema_version": 2, "stream_id": epoch, "acked_through": 2}
    assert (
        replacement.acknowledge(schema_version=2, stream_id=epoch, through_seq=1)[
            "acked_through"
        ]
        == 2
    )
    assert [
        entry["seq"] for entry in replacement.collect(after=2, stream=epoch)["events"]
    ] == [3]


def test_actual_emitter_drains_more_than_one_page_with_explicit_acks(tmp_path):
    original = make(LocalCaptureStorage(tmp_path), max_buffer=2000)
    append(original, 1003)
    first = original.collect(limit=5000)
    assert len(first["events"]) == 1000
    assert first["high_water"] == 1000 and first["pending"] == 1003
    # A read cursor alone retains both the first page and the tail.
    tail = original.collect(after=1000, stream=first["stream_id"], limit=1000)
    assert [entry["seq"] for entry in tail["events"]] == [1001, 1002, 1003]
    assert original.pending() == 1003
    ack(original, 1000)
    replacement = make(LocalCaptureStorage(tmp_path), max_buffer=2000)
    assert (
        replacement.collect(after=1000, stream=replacement.stream_id)["events"]
        == tail["events"]
    )
    ack(replacement, 1003)
    assert replacement.collect(after=1003, stream=replacement.stream_id) == {
        "schema_version": 2,
        "ack_mode": "explicit",
        "stream_id": first["stream_id"],
        "events": [],
        "high_water": 1003,
        "pending": 0,
        "dropped": 0,
        "cursor_reset": False,
        "delivery": "pull",
    }


def test_capacity_preserves_prefix_and_permanent_rejection_after_ack_and_restart():
    store = MemoryCaptureStorage()
    original = make(store, max_buffer=2)
    append(original, 2)
    accepted = original.collect()["events"]
    with pytest.raises(CaptureCapacityError):
        original.emit(
            "workshop.signal",
            BINDING["owner"],
            {"value": 2},
            idempotency_key="rejected",
        )
    assert original.collect()["events"] == accepted
    assert original.delivery_status()["retention_ready"] is False
    assert original.delivery_status()["retention_issues"][0].startswith(
        "capture_rejected:CaptureCapacityError:"
    )
    ack(original, 2)
    replacement = make(store)
    assert replacement.pending() == 0
    assert replacement.delivery_status()["retention_ready"] is False
    with pytest.raises(CaptureHeldError, match="unresolved delivery"):
        replacement.collect(after=2, stream=replacement.stream_id)
    # Retrying the refused output can retain it, but cannot erase the recorded
    # earlier loss assertion merely by draining newer records.
    replacement.emit(
        "workshop.signal", BINDING["owner"], {"value": 2}, idempotency_key="rejected"
    )
    assert [
        entry["seq"]
        for entry in replacement.collect(after=2, stream=replacement.stream_id)[
            "events"
        ]
    ] == [3]
    ack(replacement, 3)
    with pytest.raises(CaptureHeldError):
        replacement.collect(after=3, stream=replacement.stream_id)


def test_offline_rejection_becomes_durable_on_first_healthy_access(monkeypatch):
    store = MemoryCaptureStorage()
    original = make(store)
    append(original, 1)
    original_create = store.create
    monkeypatch.setattr(
        store, "create", lambda *_: (_ for _ in ()).throw(OSError("offline"))
    )
    with pytest.raises(CaptureStorageError):
        original.emit(
            "workshop.signal",
            BINDING["owner"],
            {"value": 1},
            idempotency_key="rejected",
        )
    assert original._pending_issues
    monkeypatch.setattr(store, "create", original_create)
    assert original.pending() == 1
    assert not original._pending_issues
    replacement = make(store)
    assert replacement.delivery_status()["retention_ready"] is False
    assert replacement.journal.read()["insight"]["next_seq"] == 1
    assert replacement.collect()["events"][0]["event"]["idempotency_key"] == "signal-0"


def test_identical_logical_retry_preserves_original_envelope_even_after_ack():
    original = make()
    assert (
        original.emit("event", BINDING["owner"], {"value": 1}, idempotency_key="same")
        == 1
    )
    before = copy.deepcopy(original.journal.read())
    ack(original, 1)
    assert (
        original.emit("event", BINDING["owner"], {"value": 1}, idempotency_key="same")
        == 1
    )
    after = original.journal.read()
    assert after["insight"]["next_seq"] == 1
    assert after["insight"]["events"] == before["insight"]["events"]
    assert original.pending() == 0


def test_conflicting_retry_does_not_overwrite_original_and_issue_survives_restart():
    store = MemoryCaptureStorage()
    original = make(store)
    original.emit("event", BINDING["owner"], {"value": 1}, idempotency_key="same")
    before = original.journal.read()["insight"]["events"]
    with pytest.raises(CaptureConflictError):
        original.emit("event", BINDING["owner"], {"value": 2}, idempotency_key="same")
    replacement = make(store)
    assert replacement.journal.read()["insight"]["events"] == before
    assert replacement.delivery_status()["retention_issues"][0].startswith(
        "capture_rejected:CaptureConflictError:"
    )


@pytest.mark.parametrize("kind", ["skipped", "duplicate"])
def test_skipped_or_duplicate_preparation_cannot_change_recovery_metadata(kind):
    original = make()
    original.emit("event", BINDING["owner"], {"value": 1}, idempotency_key="same")
    before = original.journal.read()

    def speculative(state):
        state["current_profile"] = {"value": "must not commit without a new event"}
        return None if kind == "skipped" else ({"value": 1}, "same")

    result = original.emit_transaction("event", BINDING["owner"], speculative)
    assert result == (0 if kind == "skipped" else 1)
    assert original.journal.read() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("attendee", ""),
        ("type", False),
        ("occurred_at", "yesterday"),
        ("occurred_at", "2026-10-16T12:00:00"),
        ("payload", []),
    ],
)
def test_semantically_invalid_retained_envelope_cannot_be_acknowledged(field, value):
    original = make()
    append(original, 1)
    original.journal.transaction(
        lambda state: state["insight"]["events"]["1"].update({field: value})
    )
    with pytest.raises(CaptureHeldError):
        original.acknowledge(
            schema_version=2, stream_id="wrong-would-also-refuse", through_seq=1
        )
    assert original.journal.health()["held"] is True


def test_bool_logical_sequence_index_cannot_masquerade_as_one():
    original = make()
    append(original, 1)
    original.journal.transaction(
        lambda state: state["insight"]["keys"].update({"signal-0": True})
    )
    assert original.delivery_status()["retention_ready"] is False
    assert original.journal.health()["held"] is True


@pytest.mark.parametrize(
    "argument,value",
    [
        ("schema_version", True),
        ("schema_version", 1),
        ("through_seq", True),
        ("through_seq", -1),
        ("through_seq", 2),
        ("stream_id", "other"),
    ],
)
def test_invalid_ack_never_changes_retained_prefix(argument, value):
    original = make()
    append(original, 1)
    body = {"schema_version": 2, "stream_id": original.stream_id, "through_seq": 1}
    body[argument] = value
    with pytest.raises(ValueError):
        original.acknowledge(**body)
    assert original.journal.read()["insight"]["acked_through"] == 0


@pytest.mark.parametrize(
    "field,value",
    [("after", True), ("after", -1), ("limit", True), ("limit", 0), ("stream", 1)],
)
def test_invalid_page_inputs_never_mutate_state(field, value):
    original = make()
    append(original, 1)
    before = original.journal.read()
    with pytest.raises(ValueError):
        original.collect(**{field: value})
    assert original.journal.read() == before


@pytest.mark.parametrize(
    "argument,value",
    [
        ("event_type", ""),
        ("event_type", True),
        ("attendee", ""),
        ("payload", []),
        ("idempotency_key", ""),
        ("idempotency_key", False),
    ],
)
def test_invalid_new_output_is_not_silently_coerced(argument, value):
    original = make()
    body = {
        "event_type": "event",
        "attendee": BINDING["owner"],
        "payload": {},
        "idempotency_key": "valid",
    }
    body[argument] = value
    with pytest.raises(ValueError):
        original.emit(**body)
    assert original.pending() == 0


@pytest.mark.parametrize(
    "field,value", [("run_id", "other-run"), ("workspace_id", "other-workspace")]
)
def test_supplied_journal_cannot_bind_to_different_source(field, value):
    store = MemoryCaptureStorage()
    retained = CaptureJournal(store, BINDING, {})
    body = {
        "run_id": BINDING["run_id"],
        "workspace_id": BINDING["workspace_id"],
        "ingest_url": "",
        "ingest_token": "",
        "journal": retained,
    }
    body[field] = value
    with pytest.raises(CaptureHeldError, match="source binding"):
        EventEmitter(**body)
    assert store.objects == {}


@pytest.mark.parametrize(
    "domain_state",
    [
        {"help": {"outbox": [{"seq": 1}]}},
        {"producer_issues": ["earlier loss"]},
        {"discovery": {"records": [{"record_id": "retained"}]}},
    ],
)
def test_configure_cannot_abandon_non_insight_accepted_state(domain_state):
    original = EventEmitter(
        run_id=BINDING["run_id"],
        workspace_id=BINDING["workspace_id"],
        ingest_url="",
        ingest_token="",
    )
    original.journal.transaction(lambda state: state.update(domain_state))
    old = original.journal
    replacement = CaptureJournal(MemoryCaptureStorage(), BINDING, {})
    with pytest.raises(CaptureHeldError, match="after accepting output"):
        original.configure_journal(replacement)
    assert original.journal is old
    assert replacement.storage.objects == {}


def test_first_emit_and_store_reconfiguration_cannot_race_to_abandon_output():
    original = EventEmitter(
        run_id=BINDING["run_id"],
        workspace_id=BINDING["workspace_id"],
        ingest_url="",
        ingest_token="",
    )
    old = original.journal
    started, release = threading.Event(), threading.Event()
    replacement = CaptureJournal(MemoryCaptureStorage(), BINDING, {})

    def prepare(_state):
        started.set()
        assert release.wait(timeout=5)
        return {"value": 1}, "first"

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = pool.submit(
            original.emit_transaction, "event", BINDING["owner"], prepare
        )
        assert started.wait(timeout=5)
        changed = pool.submit(original.configure_journal, replacement)
        release.set()
        assert accepted.result(timeout=5) == 1
        with pytest.raises(CaptureHeldError, match="after accepting output"):
            changed.result(timeout=5)
    assert original.journal is old
    assert original.pending() == 1
    assert replacement.storage.objects == {}


@pytest.mark.parametrize(
    "before,after", [(1, True), (0, False), (1, 1.0), ([1], [True])]
)
def test_changed_json_scalar_type_is_a_logical_conflict_not_a_duplicate(before, after):
    original = make()
    original.emit("event", BINDING["owner"], {"value": before}, idempotency_key="same")
    with pytest.raises(CaptureConflictError):
        original.emit(
            "event", BINDING["owner"], {"value": after}, idempotency_key="same"
        )
    assert original.journal.read()["insight"]["next_seq"] == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"not-json"}])
def test_unencodable_required_output_is_refused_with_durable_visible_fault(value):
    store = MemoryCaptureStorage()
    original = make(store)
    with pytest.raises(CaptureEncodingError):
        original.emit(
            "event", BINDING["owner"], {"value": value}, idempotency_key="invalid"
        )
    replacement = make(store)
    assert replacement.pending() == 0
    assert replacement.delivery_status()["retention_ready"] is False
    assert replacement.delivery_status()["retention_issues"][0].startswith(
        "capture_rejected:CaptureEncodingError:"
    )
    with pytest.raises(CaptureHeldError, match="unresolved delivery"):
        replacement.collect()
