"""Retained producer recovery: no regenerated envelopes or unacknowledged eviction."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from server import artifacts, discovery, help as help_module, insight_summary
from server.artifacts import Harvest
from server.capture_journal import (
    CaptureHeldError, CaptureJournal, CaptureStorageError, LocalCaptureStorage,
    MemoryCaptureStorage,
)
from server.event_emitter import EventEmitter

BINDING = {"run_id": "run-one", "unit_id": "unit-one", "workspace_id": "123",
           "owner": "alice@example.com"}


def _journal(storage):
    return CaptureJournal(storage, BINDING, {})


def _emitter(journal):
    return EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                        ingest_url="", ingest_token="", journal=journal)


@pytest.fixture(autouse=True)
def isolated_producers(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.delenv("CONTROL_TOWER_URL", raising=False)
    monkeypatch.setattr(discovery, "discovery_store", discovery.DiscoveryStore())
    help_module.reset_for_tests()
    insight_summary.stamps.clear()
    yield
    help_module.reset_for_tests()
    insight_summary.stamps.clear()


def test_help_retains_all_pages_and_stable_receipts_across_fresh_process(tmp_path):
    path = tmp_path / "retained"
    journal = _journal(LocalCaptureStorage(path))
    help_module.configure_capture(journal)
    for index in range(65):
        help_module.post_attendee_message(f"message-{index}", sender=BINDING["owner"])
    help_module.acknowledge_message("operator-message")
    original = help_module.outbox_snapshot()
    raised = help_module.snapshot()
    help_module.ack_outbox(20)
    expected = help_module.outbox_snapshot()
    code = """
import json, sys
from server import help as help_module
from server.capture_journal import CaptureJournal, LocalCaptureStorage
journal = CaptureJournal(LocalCaptureStorage(sys.argv[1]), json.loads(sys.argv[2]), {})
help_module.configure_capture(journal)
print(json.dumps({'outbox': help_module.outbox_snapshot(), 'state': help_module.snapshot(),
                  'presence': help_module.presence_fields()}))
"""
    result = subprocess.run([sys.executable, "-c", code, str(path), json.dumps(BINDING)],
                            cwd=os.getcwd(), capture_output=True, text=True, check=True)
    recovered = json.loads(result.stdout)
    assert recovered["outbox"] == expected == original[20:]
    assert recovered["state"] == raised
    assert len(recovered["presence"]["help_outbox"]) == 20
    assert recovered["presence"]["help_pending"] == 46
    assert original[-1] == {"seq": 66, "kind": "seen", "message_id": "operator-message"}


def test_help_ack_is_monotonic_range_checked_and_survives_lost_response():
    storage = MemoryCaptureStorage()
    help_module.configure_capture(_journal(storage))
    for index in range(3):
        help_module.post_attendee_message(str(index))
    assert help_module.ack_outbox(2) == {"acked_through": 2, "pending": 1}
    help_module.reset_for_tests()
    help_module.configure_capture(_journal(storage))
    assert help_module.ack_outbox(2) == {"acked_through": 2, "pending": 1}
    assert help_module.ack_outbox(1) == {"acked_through": 2, "pending": 1}
    for invalid in (4, -1, True, 1.5):
        with pytest.raises(ValueError):
            help_module.ack_outbox(invalid)
    assert [entry["body"] for entry in help_module.outbox_snapshot()] == ["2"]
    assert help_module._capture_journal.health()["ok"] is True
    assert help_module._capture_journal.health()["held"] is False
    assert help_module.post_attendee_message("new")["message"]["message_id"]
    assert help_module.outbox_snapshot()[-1]["seq"] == 4


def test_help_failed_acceptance_and_ack_keep_prior_state(monkeypatch):
    storage = MemoryCaptureStorage()
    help_module.configure_capture(_journal(storage))
    help_module.post_attendee_message("accepted")
    expected = help_module.thread_snapshot()
    outbox = help_module.outbox_snapshot()
    original_create = storage.create
    def fail(*_args):
        raise OSError("retained storage is unavailable")
    monkeypatch.setattr(storage, "create", fail)
    with pytest.raises(CaptureStorageError):
        help_module.post_attendee_message("not accepted")
    with pytest.raises(CaptureStorageError):
        help_module.ack_outbox(1)
    monkeypatch.setattr(storage, "create", original_create)
    assert help_module.thread_snapshot() == expected
    assert help_module.outbox_snapshot() == outbox


def test_help_push_response_thread_id_is_retained_across_restart(monkeypatch):
    storage = MemoryCaptureStorage()
    help_module.configure_capture(_journal(storage))
    monkeypatch.setattr(help_module, "app_identity_bearer", lambda: "token")
    monkeypatch.setattr(help_module.requests, "post", lambda *_args, **_kwargs: SimpleNamespace(
        status_code=200, json=lambda: {"help_request_id": "ct-help-one"}))
    assert help_module._ct_post("https://ct.example/help", {}) is True
    help_module.reset_for_tests()
    help_module.configure_capture(_journal(storage))
    assert help_module.snapshot()["help_request_id"] == "ct-help-one"


def test_help_corrupt_nested_sequence_does_not_recover_as_empty():
    journal = _journal(MemoryCaptureStorage())
    help_module.configure_capture(journal)
    help_module.post_attendee_message("held")
    journal.transaction(lambda state: state["help"].update(seq=2))
    help_module.reset_for_tests()
    with pytest.raises(CaptureHeldError, match="tail"):
        help_module.configure_capture(journal)
    assert journal.health()["held"] is True
    with pytest.raises(CaptureHeldError):
        journal.read()


def test_discovery_exact_replay_and_tombstone_survive_ack_and_restart():
    storage = MemoryCaptureStorage()
    emitter = _emitter(_journal(storage))
    discovery.discovery_store.configure_capture(emitter)
    first = discovery.record(BINDING["owner"], {"record_id": "need-one", "goal": "reduce delays"}, emitter)
    assert first is not None and first.revision == 1
    original_page = emitter.collect()
    replay = _emitter(_journal(storage))
    assert replay.collect()["events"] == original_page["events"]
    replay.acknowledge(schema_version=2, stream_id=replay.stream_id, through_seq=1)
    discovery.discovery_store = discovery.DiscoveryStore()
    discovery.discovery_store.configure_capture(replay)
    second = discovery.record(BINDING["owner"], {"record_id": "need-one", "industry": "retail"}, replay)
    assert second is not None and second.revision == 2 and second.goal == "reduce delays"
    assert discovery.withdraw(BINDING["owner"], "need-one", replay)
    final = _emitter(_journal(storage))
    discovery.discovery_store = discovery.DiscoveryStore()
    discovery.discovery_store.configure_capture(final)
    assert discovery.discovery_store.for_attendee(BINDING["owner"]) == []
    assert discovery.record(BINDING["owner"], {"record_id": "need-one", "goal": "resurrect"}, final) is None
    entries = final.collect()["events"]
    assert [entry["event"]["payload"]["revision"] for entry in entries] == [2, 3]
    assert entries[-1]["event"]["payload"]["redacted_by_attendee"] is True


def test_discovery_failed_envelope_append_rolls_back_record_and_revision(monkeypatch):
    storage = MemoryCaptureStorage()
    emitter = _emitter(_journal(storage))
    discovery.discovery_store.configure_capture(emitter)
    discovery.record(BINDING["owner"], {"record_id": "need-one", "goal": "accepted"}, emitter)
    original_create = storage.create
    def fail(*_args):
        raise OSError("unavailable")
    monkeypatch.setattr(storage, "create", fail)
    with pytest.raises(CaptureStorageError):
        discovery.record(BINDING["owner"], {"record_id": "need-one", "goal": "uncommitted"}, emitter)
    with pytest.raises(CaptureStorageError):
        discovery.withdraw(BINDING["owner"], "need-one", emitter)
    monkeypatch.setattr(storage, "create", original_create)
    recovered = discovery.discovery_store.for_attendee(BINDING["owner"])[0]
    assert recovered.revision == 1 and recovered.goal == "accepted"
    assert emitter.pending() == 1


def test_legacy_discovery_import_preserves_rows_but_does_not_claim_verified_delivery(tmp_path):
    local = discovery.DiscoveryStore(str(tmp_path / "discovery.json"))
    local.put(discovery.build_record(BINDING["owner"], {"record_id": "legacy", "goal": "kept"}))
    original = (tmp_path / "discovery.json").read_bytes()
    emitter = _emitter(_journal(MemoryCaptureStorage()))
    local.configure_capture(emitter)
    assert local.for_attendee(BINDING["owner"])[0].goal == "kept"
    assert emitter.journal.read()["producer_issues"] == ["legacy_discovery_delivery_unverified"]
    assert (tmp_path / "discovery.json").read_bytes() == original


def test_unreadable_legacy_journal_remains_a_visible_migration_gap(tmp_path):
    path = tmp_path / "discovery.json"
    path.write_text("not-json")
    local = discovery.DiscoveryStore(str(path))
    assert local.load() == 0
    emitter = _emitter(_journal(MemoryCaptureStorage()))
    local.configure_capture(emitter)
    assert "legacy_discovery_journal_unreadable" in emitter.journal.read()["producer_issues"]
    assert path.read_text() == "not-json"


def test_summary_revision_and_generator_survive_ack_and_restart(monkeypatch):
    storage = MemoryCaptureStorage()
    emitter = _emitter(_journal(storage))
    insight_summary.stamps.configure_capture(emitter.journal)
    harvest = Harvest(prompts=["build a queue"], prompt_count=1)
    monkeypatch.setattr(artifacts, "harvest_user", lambda *_args, **_kwargs: harvest)
    monkeypatch.setattr(insight_summary, "_ask_model", lambda *_args: ({"headline": "First"}, "model"))
    user = SimpleNamespace(email=BINDING["owner"], home="/unused")
    first = insight_summary.summarise_user(user, phase="build", emitter=emitter, force=True)
    assert first is not None and first["revision"] == 1
    accepted = emitter.collect()["events"][0]["event"]["payload"]
    assert first == accepted
    emitter.acknowledge(schema_version=2, stream_id=emitter.stream_id, through_seq=1)
    recovered = _emitter(_journal(storage))
    insight_summary.stamps.clear()
    insight_summary.stamps.configure_capture(recovered.journal)
    assert insight_summary.stamps.revision_for(user.email) == 1
    assert insight_summary.summarise_user(user, phase="build", emitter=recovered, force=True) is None
    assert insight_summary.summarise_user(user, phase="wrap", emitter=recovered, force=True, allow_llm=False) is None
    harvest.prompts.append("add tracking")
    harvest.prompt_count += 1
    second = insight_summary.summarise_user(user, phase="wrap", emitter=recovered, force=True)
    assert second is not None and second["revision"] == 2
    assert recovered.collect()["events"][0]["event"]["payload"] == second


def test_summary_failed_append_does_not_advance_revision_or_fingerprint(monkeypatch):
    storage = MemoryCaptureStorage()
    emitter = _emitter(_journal(storage))
    insight_summary.stamps.configure_capture(emitter.journal)
    harvest = Harvest(prompts=["build a queue"], prompt_count=1)
    monkeypatch.setattr(artifacts, "harvest_user", lambda *_args, **_kwargs: harvest)
    monkeypatch.setattr(insight_summary, "_ask_model", lambda *_args: ({"headline": "First"}, "model"))
    original_create = storage.create
    def fail(*_args):
        raise OSError("unavailable")
    monkeypatch.setattr(storage, "create", fail)
    user = SimpleNamespace(email=BINDING["owner"], home="/unused")
    with pytest.raises(CaptureStorageError):
        insight_summary.summarise_user(user, phase="build", emitter=emitter, force=True)
    monkeypatch.setattr(storage, "create", original_create)
    assert emitter.journal.read()["summaries"] == {}
    assert emitter.pending() == 0
    accepted = insight_summary.summarise_user(user, phase="build", emitter=emitter, force=True)
    assert accepted is not None and accepted["revision"] == 1


def test_production_help_guard_refuses_memory_fallback_after_initialization_failure():
    def held():
        raise CaptureHeldError("retained capture did not initialize")
    help_module.require_capture_ready(held)
    with pytest.raises(CaptureHeldError):
        help_module.post_attendee_message("must not be accepted in memory")
    with pytest.raises(CaptureHeldError):
        help_module.presence_fields()
    help_module.require_capture_ready(None)
    assert help_module.outbox_snapshot() == []


@pytest.mark.parametrize("operation", ["read", "mutation"])
def test_help_readiness_guard_cannot_invert_emitter_and_help_locks(monkeypatch, operation):
    """A concurrent help request must not hold help while waiting for recovery."""
    # A bounded lock makes the pre-fix inversion fail without leaving daemon
    # threads holding the global lock and hanging later fixture cleanup.
    class TimedLock:
        def __init__(self):
            self.lock = threading.RLock()

        def __enter__(self):
            if not self.lock.acquire(timeout=1):
                raise RuntimeError("help lock acquisition timed out")
            return self

        def __exit__(self, *_args):
            self.lock.release()

    monkeypatch.setattr(help_module, "_lock", TimedLock())
    emitter = _emitter(_journal(MemoryCaptureStorage()))
    initialization_entered = threading.Event()
    concurrent_guard_entered = threading.Event()
    errors, results = [], []

    def initialize():
        initialization_entered.set()
        if not concurrent_guard_entered.wait(timeout=2):
            raise RuntimeError("concurrent help request did not reach its guard")
        help_module.configure_capture(emitter.journal)

    def guard():
        if threading.current_thread().name == "concurrent-help":
            concurrent_guard_entered.set()
        emitter._ensure_ready()

    def run(callback):
        try:
            results.append(callback())
        except Exception as error:
            errors.append(error)

    emitter.defer_initialization(initialize, "test lazy recovery")
    help_module.require_capture_ready(guard)
    initializer = threading.Thread(target=run, args=(emitter._ensure_ready,), daemon=True)
    callback = help_module.snapshot if operation == "read" else lambda: help_module.post_attendee_message("retained")
    request = threading.Thread(target=run, args=(callback,), name="concurrent-help", daemon=True)
    initializer.start()
    assert initialization_entered.wait(timeout=2)
    request.start()
    initializer.join(timeout=3)
    request.join(timeout=3)
    assert not initializer.is_alive() and not request.is_alive(), "capture/help lock inversion"
    assert errors == []
    assert len(results) == 2
    assert help_module._capture_journal is emitter.journal
    if operation == "mutation":
        assert help_module.outbox_snapshot()[0]["body"] == "retained"


def test_corrupt_retained_discovery_is_visible_instead_of_skipped():
    emitter = _emitter(_journal(MemoryCaptureStorage()))
    discovery.discovery_store.configure_capture(emitter)
    emitter.journal.transaction(lambda state: state["discovery"].update(records=[{"record_id": "broken"}]))
    restored = discovery.DiscoveryStore()
    with pytest.raises(CaptureHeldError, match="invalid"):
        restored.configure_capture(emitter)
    assert emitter.journal.health()["held"] is True


def test_corrupt_retained_summary_is_visible_instead_of_reset():
    journal = _journal(MemoryCaptureStorage())
    journal.transaction(lambda state: state.update(summaries={BINDING["owner"]: {"revision": True,
                        "generator": "llm", "fingerprint": "old"}}))
    with pytest.raises(CaptureHeldError, match="revision"):
        insight_summary.stamps.configure_capture(journal)
    assert journal.health()["held"] is True


def test_discovery_stale_process_merges_against_latest_committed_revision():
    storage = MemoryCaptureStorage()
    emitter_one = _emitter(_journal(storage))
    emitter_two = _emitter(_journal(storage))
    first = discovery.DiscoveryStore()
    second = discovery.DiscoveryStore()
    first.configure_capture(emitter_one)
    first.capture(discovery.build_record(BINDING["owner"], {"record_id": "same", "goal": "queue"}),
                  provided_fields={"record_id", "goal"}, emitter=emitter_one)
    second.configure_capture(emitter_two)
    a = first.capture(discovery.build_record(BINDING["owner"], {"record_id": "same", "industry": "retail"}),
                      provided_fields={"record_id", "industry"}, emitter=emitter_one)
    b = second.capture(discovery.build_record(BINDING["owner"], {"record_id": "same", "blockers": ["access"]}),
                       provided_fields={"record_id", "blockers"}, emitter=emitter_two)
    assert a is not None and b is not None
    assert (a.revision, b.revision) == (2, 3)
    assert b.goal == "queue" and b.industry == "retail" and b.blockers == ["access"]
    assert first.for_attendee(BINDING["owner"])[0].to_json() == b.to_json()


@pytest.mark.parametrize("section", ["help", "discovery", "summaries"])
def test_nested_corruption_seen_by_a_live_reader_marks_shared_capture_held(section):
    emitter = _emitter(_journal(MemoryCaptureStorage()))
    discovery.discovery_store.configure_capture(emitter)
    insight_summary.stamps.configure_capture(emitter.journal)
    help_module.configure_capture(emitter.journal)
    emitter.journal.transaction(lambda state: state.update({section: None}))
    read = {"help": help_module.presence_fields,
            "discovery": discovery.discovery_store.counts,
            "summaries": lambda: insight_summary.stamps.revision_for(BINDING["owner"])}[section]
    with pytest.raises(CaptureHeldError):
        read()
    assert emitter.journal.health()["held"] is True
    assert emitter.journal.health()["ok"] is False
    with pytest.raises(CaptureHeldError):
        emitter.collect()
