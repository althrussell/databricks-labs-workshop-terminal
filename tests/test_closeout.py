"""Producer closeout proves retained bytes, rather than a drained live queue."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys

import pytest

from server import help as help_module
from server.capture_journal import (
    CaptureHeldError, CaptureJournal, CaptureStorageError, LocalCaptureStorage,
    MemoryCaptureStorage,
)
from server.closeout import CloseoutConflict, CloseoutManager
from server.closeout_guard import CaptureClosedError, admitted_writer
from server.event_emitter import EventEmitter

BINDING = {"run_id": "close-run", "unit_id": "close-unit", "workspace_id": "123",
           "owner": "alice@example.com"}


def emitter(storage=None):
    retained = CaptureJournal(storage if storage is not None else MemoryCaptureStorage(), BINDING, {})
    return EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                        ingest_url="", ingest_token="", journal=retained)


def final_outputs(source, capture_id):
    sequence = source.emit("workshop.signal", BINDING["owner"], {"final": capture_id},
                           idempotency_key=f"final:{capture_id}")
    return {"complete": True, "issues": [], "outputs": {"final_signal_sequence": sequence}}


def drained(_timeout):
    return {"complete": True, "issues": [], "outcomes": []}


def sha256(value):
    # Independently reconstruct the public canonical serialization contract.
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def begin(manager, capture_id="capture-one", timeout=5):
    return manager.begin(capture_id=capture_id, stream_id=manager.emitter.stream_id,
                         source=copy.deepcopy(BINDING), drain_timeout_seconds=timeout)


@pytest.fixture(autouse=True)
def capture_enabled(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")


def test_begin_is_idempotent_and_retains_original_generation_deadline_and_inventory():
    source = emitter()
    now = [100.0]
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs, clock=lambda: now[0])
    first = begin(manager)
    retained = copy.deepcopy(source.journal.read())
    now[0] += 1
    assert begin(manager) == first
    assert source.journal.read() == retained
    assert first["phase"] == "CLOSING" and first["complete"] is False
    sealed = manager.advance(capture_id="capture-one")
    assert sealed["phase"] == "SEALED" and sealed["complete"] is True
    assert begin(manager) == sealed
    assert manager.advance(capture_id="capture-one") == sealed


@pytest.mark.parametrize("changed", [
    {"capture_id": "other"}, {"stream_id": "other"},
    {"source": {**BINDING, "owner": "bob@example.com"}},
    {"source": {**BINDING, "unit_id": "other"}}, {"drain_timeout_seconds": 6},
])
def test_different_request_cannot_claim_existing_capture(changed):
    manager = CloseoutManager(emitter(), drainer=drained, finalizer=final_outputs)
    begin(manager)
    request = {"capture_id": "capture-one", "stream_id": manager.emitter.stream_id,
               "source": BINDING, "drain_timeout_seconds": 5}
    with pytest.raises(CloseoutConflict):
        manager.begin(**{**request, **changed})
    with pytest.raises(CloseoutConflict):
        manager.status(capture_id="other")


@pytest.mark.parametrize("field,value", [
    ("capture_id", ""), ("capture_id", "x" * 129), ("capture_id", 12),
    ("drain_timeout_seconds", 0), ("drain_timeout_seconds", 601),
    ("drain_timeout_seconds", True), ("drain_timeout_seconds", 1.5),
])
def test_invalid_begin_never_changes_source(field, value):
    manager = CloseoutManager(emitter(), drainer=drained, finalizer=final_outputs)
    before = manager.journal.read()
    request = {"capture_id": "capture-one", "stream_id": manager.emitter.stream_id,
               "source": BINDING, "drain_timeout_seconds": 5, field: value}
    with pytest.raises(ValueError):
        manager.begin(**request)
    assert manager.journal.read() == before


def test_previously_accepted_writer_drains_before_final_outputs_and_new_work_is_refused():
    source = emitter()
    calls = []

    def finalize(source, capture_id):
        calls.append(capture_id)
        return final_outputs(source, capture_id)

    manager = CloseoutManager(source, drainer=drained, finalizer=finalize)
    with admitted_writer(source.journal, "accepted-request"):
        begin(manager)
        assert manager.advance()["complete"] is False
        assert not calls
        source.emit("discovery.record", BINDING["owner"], {"accepted": True})
        with pytest.raises(CaptureClosedError):
            admitted_writer(source.journal, "new-request", detached=True)
    sealed = manager.advance()
    assert sealed["complete"] is True and calls == ["capture-one"]
    assert sealed["manifest"]["streams"]["insight"]["count"] == 2


def test_writer_timeout_is_explicit_and_late_accepted_output_can_recover_same_generation():
    now = [100.0]
    source = emitter()
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs, clock=lambda: now[0])
    ticket = admitted_writer(source.journal, "background", detached=True)
    first = begin(manager, timeout=1)
    now[0] += 2
    timed_out = manager.advance()
    assert timed_out["phase"] == "INCOMPLETE" and timed_out["complete"] is False
    assert "writer_drain_deadline_exceeded" in timed_out["issues"]
    assert timed_out["manifest"] is None
    with ticket:
        source.emit("workshop.signal", BINDING["owner"], {"late_accepted": True})
    recovered = manager.advance()
    assert recovered["phase"] == "SEALED" and recovered["complete"] is True
    assert recovered["generation"] == first["generation"]
    assert recovered["manifest"]["streams"]["insight"]["count"] == 2


def test_failed_accepted_writer_can_never_be_silently_declared_complete():
    now = [100.0]
    source = emitter()
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs, clock=lambda: now[0])
    with pytest.raises(RuntimeError):
        with admitted_writer(source.journal, "accepted-request"):
            raise RuntimeError("private request details")
    begin(manager, timeout=1)
    now[0] += 2
    result = manager.advance()
    assert result["phase"] == "INCOMPLETE" and result["complete"] is False
    assert "accepted_writer_failed" in result["issues"]
    assert result["writer_outcomes"][0]["status"] == "failed"
    assert "private" not in json.dumps(result)


def test_process_drain_deadline_is_explicit_and_does_not_call_finalizer():
    now = [100.0]
    manager = CloseoutManager(emitter(), clock=lambda: now[0],
        drainer=lambda _timeout: {"complete": False, "issues": ["stdio_pending"], "outcomes": []},
        finalizer=lambda *_args: pytest.fail("finalized before process drain"))
    begin(manager, timeout=1)
    assert manager.advance()["phase"] == "CLOSING"
    now[0] += 2
    result = manager.advance()
    assert result["phase"] == "INCOMPLETE" and not result["complete"]
    assert "stdio_pending" in result["issues"] and result["manifest"] is None


def test_second_drain_reports_racing_spawn_and_evaluates_deadline():
    now = [100.0]
    calls = [0]

    def racing_drain(_timeout):
        calls[0] += 1
        if calls[0] == 1:
            return drained(0)
        return {"complete": False, "issues": ["terminal_process_pending"], "outcomes": []}

    manager = CloseoutManager(emitter(), clock=lambda: now[0], drainer=racing_drain,
                              finalizer=lambda *_args: pytest.fail("racing spawn was not drained"))
    begin(manager, timeout=1)
    now[0] += 2
    result = manager.advance()
    assert result["phase"] == "INCOMPLETE" and not result["complete"]
    assert "terminal_process_pending" in result["issues"]


def test_manifest_and_pages_inventory_all_acknowledged_and_pending_bytes_independently():
    source = emitter()
    help_module.configure_capture(source.journal)
    for n in range(5):
        source.emit("discovery.record", BINDING["owner"], {"revision": n + 1, "answer": "日本語"})
        help_module.post_attendee_message(f"message-{n}", sender=BINDING["owner"])
    source.acknowledge(schema_version=2, stream_id=source.stream_id, through_seq=3)
    help_module.ack_outbox(2)
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs)
    begin(manager)
    result = manager.advance()
    manifest = result["manifest"]
    assert result["manifest_sha256"] == sha256(manifest)
    assert manifest["source"] == {**BINDING, "stream_id": source.stream_id}
    assert result["ct_verification_required"] is True
    for kind, count in (("insight", 6), ("help", 5)):
        pages, after = [], 0
        while True:
            page = manager.entries(capture_id="capture-one", kind=kind, after=after, limit=2)
            assert page["manifest_sha256"] == result["manifest_sha256"]
            pages.extend(page["entries"])
            after = page["high_water"]
            if not page["remaining"]:
                break
        expected = manifest["streams"][kind]
        assert [entry["seq"] for entry in pages] == list(range(1, count + 1))
        assert expected["count"] == count and expected["first_seq"] == 1 and expected["last_seq"] == count
        assert expected["sha256"] == sha256(pages)
        assert expected["entries"] == [{"seq": entry["seq"], "sha256": sha256(entry)} for entry in pages]
    categories = {value["type"]: value for value in manifest["categories"]}
    assert categories["attendee.identity"]["count"] == 0  # explicit absence, not invented records
    assert categories["discovery.record"]["count"] == 5
    source.acknowledge(schema_version=2, stream_id=source.stream_id, through_seq=6)
    help_module.ack_outbox(5)
    assert source.pending() == 0 and help_module.outbox_snapshot() == []
    assert manager.status() == result
    replacement = CloseoutManager(emitter(source.journal.storage), drainer=drained,
                                  finalizer=lambda *_args: pytest.fail("sealed source finalized twice"))
    assert replacement.advance() == result
    assert replacement.entries(capture_id="capture-one", kind="help")["entries"] == pages


def test_sealed_inventory_remains_immutable_when_later_runtime_config_changes(monkeypatch):
    source = emitter()
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs)
    begin(manager)
    sealed = manager.advance()
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "false")
    replacement = CloseoutManager(emitter(source.journal.storage), drainer=drained,
                                  finalizer=lambda *_args: pytest.fail("prior seal was reopened"))
    assert replacement.status() == sealed
    assert replacement.advance() == sealed


def test_returned_status_and_page_mutations_cannot_rewrite_retained_evidence():
    manager = CloseoutManager(emitter(), drainer=drained, finalizer=final_outputs)
    begin(manager)
    sealed = manager.advance()
    response = manager.status()
    response["manifest"]["complete"] = False
    response["final_outputs"]["final_signal_sequence"] = 100
    page = manager.entries(capture_id="capture-one", kind="insight")
    page["entries"][0]["event"]["payload"].clear()
    assert manager.status() == sealed
    assert manager.entries(capture_id="capture-one", kind="insight")["entries"][0]["event"]["payload"]


@pytest.mark.parametrize("kwargs", [
    {"kind": "unknown"}, {"after": -1}, {"after": True}, {"after": 1.5},
    {"limit": 0}, {"limit": 1001}, {"limit": True},
])
def test_invalid_paging_never_changes_source(kwargs):
    manager = CloseoutManager(emitter(), drainer=drained, finalizer=final_outputs)
    begin(manager)
    manager.advance()
    before = manager.journal.read()
    with pytest.raises(ValueError):
        manager.entries(**{"capture_id": "capture-one", "kind": "insight", **kwargs})
    assert manager.journal.read() == before


def test_unsealed_or_wrong_capture_cannot_supply_final_pages():
    manager = CloseoutManager(emitter(), drainer=drained, finalizer=final_outputs)
    begin(manager)
    with pytest.raises(CloseoutConflict):
        manager.entries(capture_id="capture-one", kind="insight")
    manager.advance()
    with pytest.raises(CloseoutConflict):
        manager.entries(capture_id="wrong", kind="insight")


@pytest.mark.parametrize("problem", ["capture_disabled", "unsupported_required_output", "producer_gap"])
def test_required_output_problems_never_produce_success(problem, monkeypatch):
    now = [100.0]
    source = emitter()
    if problem == "capture_disabled":
        monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "false")
    elif problem == "unsupported_required_output":
        source.emit("future.output.v99", BINDING["owner"], {})
    else:
        source.journal.transaction(lambda state: state.update(producer_issues=["producer_gap"]))
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs, clock=lambda: now[0])
    begin(manager, timeout=1)
    now[0] += 2
    result = manager.advance()
    assert result["phase"] == "INCOMPLETE" and not result["complete"]
    assert problem in result["issues"] and result["manifest"]["complete"] is False


@pytest.mark.parametrize("damage", ["unsupported-schema", "sequence-gap"])
def test_invalid_retained_insight_cannot_begin_a_successful_close(damage):
    source = emitter()
    source.emit("workshop.signal", BINDING["owner"], {"retained": True})
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs)
    if damage == "unsupported-schema":
        source.journal.transaction(lambda state: state["insight"]["events"]["1"].update(schema_version=99))
    else:
        source.journal.transaction(lambda state: state["insight"]["events"].pop("1"))
    with pytest.raises(CaptureHeldError):
        begin(manager)
    assert source.journal.health()["held"] is True


def test_help_history_with_equal_count_but_wrong_sequences_never_seals():
    now = [100.0]
    source = emitter()
    help_module.configure_capture(source.journal)
    help_module.post_attendee_message("kept")
    source.journal.transaction(lambda state: state["help"].update(history={"2": {"seq": 2, "kind": "message"}}))
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs, clock=lambda: now[0])
    begin(manager, timeout=1)
    now[0] += 2
    try:
        result = manager.advance()
    except CaptureHeldError:
        return  # A held source is a valid fail-closed result, never a completed seal.
    assert result["complete"] is False
    assert result["phase"] != "SEALED"
    assert any("help" in issue for issue in result["issues"])


@pytest.mark.parametrize("kind,field,value,issue", [
    ("message", "kind", "future-help-output", "unsupported_required_output"),
    ("message", "kind", [], "unsupported_required_output"),
    ("message", "message_id", "", "malformed_required_help_output"),
    ("message", "body", None, "malformed_required_help_output"),
    ("message", "created_at", None, "malformed_required_help_output"),
    ("message", "sender_role", "unknown", "malformed_required_help_output"),
    ("message", "sender_role", [], "malformed_required_help_output"),
    ("message", "sender", None, "malformed_required_help_output"),
    ("message", "help_request_id", {}, "malformed_required_help_output"),
    ("seen", "message_id", None, "malformed_required_help_output"),
])
def test_unknown_or_malformed_retained_help_cannot_seal_complete(kind, field, value, issue):
    now = [100.0]
    source = emitter()
    help_module.configure_capture(source.journal)
    if kind == "seen":
        help_module.acknowledge_message("operator-message")
    else:
        help_module.post_attendee_message("synthetic test message", sender=BINDING["owner"])
    help_module.ack_outbox(1)
    source.journal.transaction(lambda state: state["help"]["history"]["1"].update({field: value}))
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs, clock=lambda: now[0])
    begin(manager, timeout=1)
    now[0] += 2
    result = manager.advance()
    assert result["phase"] == "INCOMPLETE" and result["complete"] is False
    assert issue in result["issues"]
    assert result["manifest"]["complete"] is False


ASSISTANT_ANSWER = {"message_id": "assistant-answer", "help_request_id": "help-thread",
                    "sender_role": "assistant", "sender": "Workshop Assistant",
                    "body": "Open the SQL editor from the sidebar."}


def test_assistant_feedback_and_handoff_are_supported_required_help_output():
    source = emitter()
    help_module.configure_capture(source.journal)
    help_module.ingest_operator_message(ASSISTANT_ANSWER)
    help_module.record_feedback("assistant-answer", "up")
    help_module.request_person()
    help_module.ack_outbox(1)
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs)
    begin(manager)
    result = manager.advance()
    assert result["phase"] == "SEALED" and result["complete"] is True, result["issues"]
    entries = manager.entries(capture_id="capture-one", kind="help")["entries"]
    assert [(entry["seq"], entry["kind"]) for entry in entries] == [(1, "feedback"), (2, "handoff")]
    assert entries[1]["help_request_id"] == "help-thread"
    assert result["manifest"]["streams"]["help"]["sha256"] == sha256(entries)


MISSING = object()


@pytest.mark.parametrize("kind,field,value", [
    ("feedback", "value", "sideways"),
    ("feedback", "value", None),
    ("feedback", "message_id", ""),
    ("feedback", "message_id", 7),
    ("feedback", "created_at", "yesterday"),
    ("feedback", "created_at", "2026-11-01T02:00:00+00:00"),
    ("feedback", "created_at", MISSING),
    ("feedback", "body", "an extra field"),
    ("handoff", "handoff_id", "not-a-uuid"),
    ("handoff", "handoff_id", None),
    ("handoff", "help_request_id", 12),
    ("handoff", "help_request_id", MISSING),
    ("handoff", "created_at", "2026-13-01T02:00:00Z"),
    ("handoff", "note", "an extra field"),
])
def test_malformed_assistant_help_output_cannot_seal_complete(kind, field, value):
    now = [100.0]
    source = emitter()
    help_module.configure_capture(source.journal)
    if kind == "feedback":
        help_module.ingest_operator_message(ASSISTANT_ANSWER)
        help_module.record_feedback("assistant-answer", "down")
    else:
        help_module.request_person()
    help_module.ack_outbox(1)

    def damage(state):
        entry = state["help"]["history"]["1"]
        if value is MISSING:
            entry.pop(field)
        else:
            entry[field] = value
    source.journal.transaction(damage)
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs, clock=lambda: now[0])
    begin(manager, timeout=1)
    now[0] += 2
    result = manager.advance()
    assert result["phase"] == "INCOMPLETE" and result["complete"] is False
    assert result["issues"] == ["malformed_required_help_output"]
    assert result["manifest"]["complete"] is False


def test_a_new_release_resumes_a_closing_capture_retained_without_feedback():
    """Replacing the app mid-closeout must not need a write the barrier refuses."""
    source = emitter()
    help_module.configure_capture(source.journal)
    help_module.post_attendee_message("retained before the upgrade", sender=BINDING["owner"])
    source.journal.transaction(lambda state: state["help"].pop("feedback"))
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs)
    begin(manager)
    help_module.reset_for_tests()
    help_module.configure_capture(source.journal)
    assert help_module.thread_snapshot()["feedback"] == {}
    result = manager.advance()
    assert result["phase"] == "SEALED" and result["complete"] is True, result["issues"]


@pytest.mark.parametrize("path,value,remove", [
    (("capture_id",), None, False), (("generation",), "", False),
    (("stream_id",), "wrong-source-epoch", False), (("requested_at",), "yesterday", False),
    (("requested_at",), True, False), (("drain_deadline",), -1, False),
    (("requested_at",), 10 ** 400, False),
    (("drain_timeout_seconds",), True, False), (("drain_timeout_seconds",), 601, False),
    (("capture_enabled",), "true", False), (("issues",), "unknown", False),
    (("final_outputs",), [], False), (("outputs_complete",), 1, False),
    (("finalizer",), {}, False), (("manifest",), None, False),
    (("manifest",), [], False), (("manifest", "completed_at"), "tomorrow", False),
    (("manifest", "completed_at"), None, True), (("manifest", "schema_version"), True, False),
    (("manifest", "capture_id"), "other-capture", False),
    (("manifest", "generation"), "other-generation", False),
    (("manifest", "source"), {}, False), (("manifest", "complete"), 1, False),
])
def test_malformed_persisted_operation_is_held_before_status_or_advance(path, value, remove):
    source = emitter()
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs)
    begin(manager)
    assert manager.advance()["complete"]
    damaged = source.journal.read()
    parent = damaged["closeout"]
    for segment in path[:-1]:
        parent = parent[segment]
    if remove:
        parent.pop(path[-1])
    else:
        parent[path[-1]] = value
    retained = CaptureJournal(MemoryCaptureStorage(), BINDING, damaged)
    replacement = EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                               ingest_url="", ingest_token="", journal=retained)
    resumed = CloseoutManager(replacement, drainer=drained, finalizer=final_outputs)
    with pytest.raises(CaptureHeldError, match="retained closeout"):
        resumed.status()
    with pytest.raises(CaptureHeldError, match="retained closeout"):
        resumed.advance()


def test_failed_final_output_retries_and_does_not_create_a_second_logical_event():
    source = emitter()
    calls = [0]

    def interrupted(source, capture_id):
        result = final_outputs(source, capture_id)
        calls[0] += 1
        if calls[0] == 1:
            raise CaptureStorageError("lost finalizer reply")
        return result

    manager = CloseoutManager(source, drainer=drained, finalizer=interrupted)
    first = begin(manager)
    with pytest.raises(CaptureStorageError):
        manager.advance()
    assert manager.status()["phase"] == "CLOSING" and not manager.status()["complete"]
    replacement = CloseoutManager(emitter(source.journal.storage), drainer=drained, finalizer=interrupted)
    result = replacement.advance()
    assert result["complete"] and result["generation"] == first["generation"]
    assert result["manifest"]["streams"]["insight"]["count"] == 1


def test_missing_final_output_can_retry_same_incomplete_generation_after_deadline():
    now = [100.0]
    ready = [False]

    def finalizer(source, capture_id):
        if not ready[0]:
            return {"complete": False, "issues": ["final_resource_observation_unavailable"], "outputs": {}}
        return final_outputs(source, capture_id)

    manager = CloseoutManager(emitter(), drainer=drained, finalizer=finalizer, clock=lambda: now[0])
    original = begin(manager, timeout=1)
    now[0] += 2
    incomplete = manager.advance()
    assert incomplete["phase"] == "INCOMPLETE" and incomplete["manifest"]["complete"] is False
    ready[0] = True
    completed = manager.advance()
    assert completed["complete"] is True and completed["generation"] == original["generation"]
    assert completed["manifest_sha256"] != incomplete["manifest_sha256"]
    assert completed["issues"] == []


def test_replacement_cannot_forget_an_orphaned_accepted_writer():
    now = [100.0]
    source = emitter()
    admitted_writer(source.journal, "lost-process", detached=True)
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs, clock=lambda: now[0])
    original = begin(manager, timeout=1)
    # No in-memory worker survives this replacement; its acceptance still does.
    replacement = CloseoutManager(emitter(source.journal.storage), drainer=drained,
        finalizer=lambda *_args: pytest.fail("orphaned acceptance silently discarded"), clock=lambda: now[0])
    now[0] += 2
    held = replacement.advance()
    assert held["phase"] == "INCOMPLETE" and held["generation"] == original["generation"]
    assert len(held["active_writers"]) == 1 and "writers_not_drained" in held["issues"]


def test_unavailable_journal_cannot_commit_a_false_success_and_restart_recovers(monkeypatch):
    source = emitter()
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs)
    first = begin(manager)
    create = source.journal.storage.create

    def unavailable(*_args):
        raise OSError("journal offline")

    monkeypatch.setattr(source.journal.storage, "create", unavailable)
    with pytest.raises(CaptureStorageError):
        manager.advance()
    monkeypatch.setattr(source.journal.storage, "create", create)
    replacement = CloseoutManager(emitter(source.journal.storage), drainer=drained, finalizer=final_outputs)
    assert replacement.status()["phase"] == "CLOSING"
    recovered = replacement.advance()
    assert recovered["generation"] == first["generation"] and recovered["complete"] is True


def test_local_store_seal_replays_unchanged_in_fresh_process(tmp_path):
    source = emitter(LocalCaptureStorage(tmp_path / "retained"))
    source.emit("attendee.identity", BINDING["owner"], {"name": "Attendee"})
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs)
    begin(manager)
    expected = manager.advance()
    code = """
import json, sys
from server.capture_journal import CaptureJournal, LocalCaptureStorage
from server.closeout import CloseoutManager
from server.event_emitter import EventEmitter
binding = json.loads(sys.argv[2])
source = EventEmitter(run_id=binding['run_id'], workspace_id=binding['workspace_id'],
                      ingest_url='', ingest_token='',
                      journal=CaptureJournal(LocalCaptureStorage(sys.argv[1]), binding, {}))
manager = CloseoutManager(source, finalizer=lambda *_args: None)
print(json.dumps(manager.advance(capture_id='capture-one')))
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "retained"), json.dumps(BINDING)],
                            cwd=os.getcwd(), capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("damage", ["missing-prefix", "corrupt-prefix"])
def test_damaged_journal_prefix_cannot_recover_as_a_valid_seal(damage):
    storage = MemoryCaptureStorage()
    source = emitter(storage)
    source.emit("attendee.identity", BINDING["owner"], {"name": "Attendee"})
    manager = CloseoutManager(source, drainer=drained, finalizer=final_outputs)
    begin(manager)
    assert manager.advance()["complete"]
    if damage == "missing-prefix":
        storage.objects.pop("00000000000000000001.json")
    else:
        storage.objects["00000000000000000001.json"] += b"corrupt"
    with pytest.raises(CaptureHeldError):
        CloseoutManager(emitter(storage), drainer=drained, finalizer=final_outputs).status()


def test_second_manager_cannot_finalize_until_lease_expires_and_stale_claim_cannot_write():
    now = [100.0]
    storage = MemoryCaptureStorage()
    first_source, second_source = emitter(storage), emitter(storage)
    second = CloseoutManager(second_source, drainer=drained, finalizer=final_outputs, clock=lambda: now[0])
    sealed = []

    def slow_finalizer(source, _capture_id):
        assert second.advance()["phase"] == "CLOSING"  # Current claim is still live.
        now[0] += 121
        sealed.append(second.advance())
        assert sealed[0]["complete"] is True
        source.emit("workshop.signal", BINDING["owner"], {"stale_finalizer": True})
        pytest.fail("expired finalizer wrote after another claimant sealed")

    first = CloseoutManager(first_source, drainer=drained, finalizer=slow_finalizer, clock=lambda: now[0])
    begin(first)
    with pytest.raises((CaptureClosedError, CloseoutConflict)):
        first.advance()
    assert first.status() == sealed[0]
    assert first.status()["manifest"]["streams"]["insight"]["count"] == 1


def test_finalizer_cannot_write_after_lease_expiry_without_a_replacement_claim():
    now = [100.0]
    source = emitter()

    def expired(source, capture_id):
        now[0] += 121
        return final_outputs(source, capture_id)

    manager = CloseoutManager(source, drainer=drained, finalizer=expired, clock=lambda: now[0])
    begin(manager)
    with pytest.raises(CaptureClosedError):
        manager.advance()
    assert source.journal.read()["insight"]["next_seq"] == 0
    replacement = CloseoutManager(emitter(source.journal.storage), drainer=drained,
                                  finalizer=final_outputs, clock=lambda: now[0])
    assert replacement.advance()["complete"] is True
