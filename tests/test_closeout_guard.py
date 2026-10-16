"""A close barrier survives independent journal instances and detached work."""

from __future__ import annotations

import copy

import pytest

from server.capture_journal import (
    CaptureHeldError, CaptureJournal, CaptureStorageError, MemoryCaptureStorage,
)
from server.closeout import CloseoutManager
from server.closeout_guard import (
    CaptureClosedError, admitted_writer, check_open, control_transaction, finalizer_scope,
)
from server.event_emitter import EventEmitter

BINDING = {"run_id": "guard-run", "unit_id": "guard-unit", "workspace_id": "123",
           "owner": "alice@example.com"}


def source(storage=None, binding=None):
    binding = binding or BINDING
    journal = CaptureJournal(storage if storage is not None else MemoryCaptureStorage(), binding, {})
    return EventEmitter(run_id=binding["run_id"], workspace_id=binding["workspace_id"],
                        ingest_url="", ingest_token="", journal=journal)


def closing(emitter):
    manager = CloseoutManager(emitter,
        drainer=lambda _timeout: {"complete": True, "issues": [], "outcomes": []},
        finalizer=lambda *_args: {"complete": True, "issues": [], "outputs": {}})
    manager.begin(capture_id="guard-capture", stream_id=emitter.stream_id, source=emitter.journal.binding)
    return manager


@pytest.fixture(autouse=True)
def capture_enabled(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")


def test_detached_ticket_outlives_http_request_and_is_reserved_before_dispatch():
    emitter = source()
    with admitted_writer(emitter.journal, "http"):
        background = admitted_writer(emitter.journal, "background", detached=True)
        assert len(emitter.journal.read()["closeout"]["writers"]) == 2
    assert len(emitter.journal.read()["closeout"]["writers"]) == 1
    manager = closing(emitter)
    assert manager.advance()["phase"] == "CLOSING"
    with background:
        emitter.emit("identity.resolved", BINDING["owner"], {"completed": True})
    assert emitter.journal.read()["closeout"]["writers"] == {}
    assert manager.advance()["complete"] is True


def test_nested_synchronous_work_uses_request_ticket_without_finishing_it_early():
    emitter = source()
    with admitted_writer(emitter.journal, "http"):
        with admitted_writer(emitter.journal, "nested"):
            assert len(emitter.journal.read()["closeout"]["writers"]) == 1
        assert len(emitter.journal.read()["closeout"]["writers"]) == 1
        manager = closing(emitter)
        emitter.emit("discovery.record", BINDING["owner"], {"admitted": True})
    assert manager.advance()["complete"] is True


def test_separate_journal_sees_barrier_and_late_refusal_does_not_poison_capture():
    storage = MemoryCaptureStorage()
    first, other_container = source(storage), source(storage)
    manager = closing(first)
    with pytest.raises(CaptureClosedError):
        check_open(other_container.journal)
    with pytest.raises(CaptureClosedError):
        admitted_writer(other_container.journal, "late-request")
    with pytest.raises(CaptureClosedError):
        other_container.emit("discovery.record", BINDING["owner"], {"late": True})
    assert other_container.journal.health()["held"] is False
    assert not other_container.journal.read().get("producer_issues")
    assert manager.advance()["complete"] is True


def test_ticket_authority_cannot_cross_source_binding():
    first = source()
    other = source(binding={**BINDING, "run_id": "other-run"})
    closing(other)
    with admitted_writer(first.journal, "request"):
        with pytest.raises(CaptureClosedError):
            other.emit("workshop.signal", BINDING["owner"], {"cross_source": True})
    assert other.journal.read()["insight"]["next_seq"] == 0


def test_finished_ticket_cannot_be_reentered_or_keep_write_authority():
    emitter = source()
    ticket = admitted_writer(emitter.journal, "request")
    ticket.__enter__()
    try:
        manager = closing(emitter)
        ticket.finish()
        with pytest.raises(CaptureClosedError):
            emitter.emit("workshop.signal", BINDING["owner"], {"after_finish": True})
    finally:
        ticket.__exit__(None, None, None)
    with pytest.raises(RuntimeError):
        ticket.__enter__()
    ticket.finish()  # Repeated completion is safe.
    assert manager.advance()["complete"] is True


def test_completion_write_failure_cannot_erase_failed_accepted_writer_on_retry(monkeypatch):
    emitter = source()
    ticket = admitted_writer(emitter.journal, "http:POST:/api/discovery")
    ticket.__enter__()
    create = emitter.journal.storage.create
    unavailable = [True]

    def flaky_create(name, content):
        if unavailable[0]:
            unavailable[0] = False
            raise OSError("one completion write failed")
        return create(name, content)

    monkeypatch.setattr(emitter.journal.storage, "create", flaky_create)
    try:
        with pytest.raises(CaptureStorageError):
            ticket.finish(failed=True)
    finally:
        # Middleware's context cleanup can retry after the write failure. The
        # failed outcome must survive even if this retry has no exception flag.
        ticket.__exit__(None, None, None)
    retained = emitter.journal.read()["closeout"]
    assert retained["writers"] == {}
    assert len(retained["writer_outcomes"]) == 1
    assert retained["writer_outcomes"][0]["status"] == "failed"
    assert closing(emitter).advance()["complete"] is False


@pytest.mark.parametrize("kind", ["", "x" * 101, None, 1])
def test_invalid_ticket_does_not_reserve_a_writer(kind):
    emitter = source()
    before = emitter.journal.read()
    with pytest.raises(ValueError):
        admitted_writer(emitter.journal, kind)
    assert emitter.journal.read() == before


def test_writer_admission_capacity_fails_visibly_without_evicting_accepted_work():
    emitter = source()
    tickets = [admitted_writer(emitter.journal, "background", detached=True) for _ in range(256)]
    before = emitter.journal.read()
    try:
        with pytest.raises(CaptureHeldError, match="capacity"):
            admitted_writer(emitter.journal, "overflow", detached=True)
        assert emitter.journal.read() == before
    finally:
        for ticket in tickets:
            ticket.finish()
    assert emitter.journal.read()["closeout"]["writers"] == {}


def test_control_scope_cannot_change_capture_content_or_be_forged_by_normal_transaction():
    emitter = source()
    before = emitter.journal.read()
    with pytest.raises(CaptureClosedError):
        control_transaction(emitter.journal, lambda state: state.update(untracked="content"))
    assert emitter.journal.read() == before
    closing(emitter)
    before = emitter.journal.read()
    with pytest.raises(CaptureClosedError):
        emitter.journal.transaction(lambda state: state["closeout"].update(phase="OPEN"))
    assert emitter.journal.read() == before


def test_ack_exception_cannot_smuggle_captured_content_changes_after_barrier():
    emitter = source()
    emitter.emit("workshop.signal", BINDING["owner"], {"original": True})
    manager = closing(emitter)
    original = copy.deepcopy(emitter.journal.read()["insight"]["events"])

    def disguised_ack(state):
        state["insight"]["acked_through"] = 1
        state["insight"]["events"]["1"]["payload"] = {"modified": True}

    with pytest.raises(CaptureClosedError):
        emitter.journal.transaction(disguised_ack)
    assert emitter.journal.read()["insight"]["events"] == original
    assert emitter.journal.read()["insight"]["acked_through"] == 0
    assert manager.advance()["complete"] is True


def test_unclaimed_finalizer_scope_and_stale_claim_cannot_append():
    emitter = source()
    manager = closing(emitter)
    for capture_id, claim in (("wrong", ""), ("guard-capture", ""), ("guard-capture", "stale")):
        with finalizer_scope(emitter.journal, capture_id, claim):
            with pytest.raises(CaptureClosedError):
                emitter.emit("workshop.signal", BINDING["owner"], {"unauthorized": True})
    assert manager.advance()["complete"] is True


def test_sealed_manifest_cannot_be_rewritten_even_with_control_scope():
    emitter = source()
    manager = closing(emitter)
    sealed = manager.advance()

    def reopen(state):
        state["closeout"]["phase"] = "OPEN"

    def replace_manifest(state):
        state["closeout"]["manifest"]["complete"] = False

    def change_generation(state):
        state["closeout"]["generation"] = "replacement-generation"

    for mutation in (reopen, replace_manifest, change_generation):
        with pytest.raises(CaptureClosedError):
            control_transaction(emitter.journal, mutation)
    assert manager.status() == sealed


@pytest.mark.parametrize("invalid", [
    {"schema_version": 99, "phase": "OPEN"},
    {"schema_version": 1, "phase": [], "writers": {}, "writer_outcomes": []},
])
def test_invalid_retained_closeout_schema_cannot_reopen_source(invalid):
    journal = CaptureJournal(MemoryCaptureStorage(), BINDING,
                             {"closeout": invalid})
    # A producer cannot silently downgrade or discard an unknown closeout state.
    with pytest.raises(CaptureHeldError):
        check_open(journal)
    with pytest.raises(CaptureHeldError):
        admitted_writer(journal, "request")
