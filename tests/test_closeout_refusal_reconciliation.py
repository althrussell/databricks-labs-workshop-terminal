"""Historical no-input failures require a full audited proof and durable receipt."""

from __future__ import annotations

import copy
import json

import pytest

from server.capture_journal import (
    CaptureHeldError, CaptureJournal, CaptureStorageError, MemoryCaptureStorage,
)
from server.closeout import CloseoutConflict, CloseoutManager
from server.closeout_guard import CaptureClosedError, admitted_writer, control_transaction
from server.event_emitter import EventEmitter


BINDING = {"run_id": "legacy-run", "unit_id": "legacy-unit", "workspace_id": "123",
           "owner": "alice@example.com"}


def emitter(storage=None):
    return EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
        ingest_url="", ingest_token="", journal=CaptureJournal(
            storage if storage is not None else MemoryCaptureStorage(), BINDING, {},
            checkpoint_interval=4))


def legacy_failure(source, *, code="model_policy_pending", partial=False, diagnostic=True):
    ticket = admitted_writer(source.journal, "http:POST:/api/sessions")
    with ticket:
        if diagnostic:
            source.emit("session.create_failed", BINDING["owner"], {
                "agent": "claude", "code": code, "raw_code": code,
                "detail": "", "outcome": "refused"})
        if partial:
            source.emit("workshop.signal", BINDING["owner"], {"partial": True})
        ticket.finish(failed=True)
    return copy.deepcopy(source.journal.read()["closeout"]["writer_outcomes"][-1])


def final_outputs(source, capture_id):
    source.emit("workshop.signal", BINDING["owner"], {"final": capture_id},
                idempotency_key=f"final:{capture_id}")
    return {"complete": True, "issues": [], "outputs": {"final": True}}


def manager_for(source, now):
    return CloseoutManager(source, clock=lambda: now[0], finalizer=final_outputs,
        drainer=lambda _timeout: {"complete": True, "issues": [], "outcomes": []})


def begin(manager, now):
    result = manager.begin(capture_id="legacy-capture", stream_id=manager.emitter.stream_id,
                           source=BINDING, drain_timeout_seconds=1)
    now[0] += 2
    return result


@pytest.fixture(autouse=True)
def capture_enabled(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")


def test_exact_legacy_failures_move_to_audited_ledger_without_changing_original_bytes():
    source = emitter()
    originals = [legacy_failure(source) for _ in range(3)]
    original_objects = dict(source.journal.storage.objects)
    now = [100.0]
    manager = manager_for(source, now)
    started = begin(manager, now)
    sealed = manager.advance()
    assert sealed["complete"] is True and sealed["generation"] == started["generation"]
    assert sealed["writer_outcomes"] == []
    ledger = source.journal.read()["closeout"]["refusal_reconciliations"]
    assert [row["writer_outcome"] for row in ledger] == originals
    assert all(row["source"] == BINDING for row in ledger)
    assert all(source.journal.storage.objects[name] == raw for name, raw in original_objects.items())
    drain = sealed["manifest"]["drain"]["outcomes"]
    assert drain == [{"kind": "pre_session_refusal_reconciliation", "complete": True,
                      "issues": [], "receipts": ledger}]
    for receipt in ledger:
        for operation in receipt["operations"] + [receipt["audit"]]:
            stored = json.loads(source.journal.storage.objects[f'{operation["revision"]:020d}.json'])
            assert stored["hash"] == operation.get("hash")
    assert manager_for(emitter(source.journal.storage), now).status() == sealed


@pytest.mark.parametrize("mode", ["unknown", "partial", "missing_diagnostic"])
def test_unknown_and_partial_writer_failures_are_never_reconciled(mode):
    source = emitter()
    original = legacy_failure(source, code="credential_unavailable" if mode == "unknown" else "model_policy_pending",
                              partial=mode == "partial", diagnostic=mode != "missing_diagnostic")
    now = [100.0]
    manager = manager_for(source, now)
    begin(manager, now)
    result = manager.advance()
    assert result["complete"] is False and "accepted_writer_failed" in result["issues"]
    assert result["writer_outcomes"] == [original]
    assert not source.journal.read()["closeout"].get("refusal_reconciliations")


def test_mixed_failures_retain_unknown_failure_and_proof_ledger_survives_retry_restart():
    source = emitter()
    reconciled = legacy_failure(source)
    unresolved = legacy_failure(source, diagnostic=False)
    now = [100.0]
    manager = manager_for(source, now)
    begin(manager, now)
    first = manager.advance()
    assert first["phase"] == "INCOMPLETE" and first["writer_outcomes"] == [unresolved]
    ledger = copy.deepcopy(source.journal.read()["closeout"]["refusal_reconciliations"])
    assert [row["writer_outcome"] for row in ledger] == [reconciled]
    restarted = manager_for(emitter(source.journal.storage), now)
    second = restarted.advance()
    assert second["complete"] is False and second["writer_outcomes"] == [unresolved]
    assert source.journal.read()["closeout"]["refusal_reconciliations"] == ledger
    assert second["manifest"]["drain"]["outcomes"] == first["manifest"]["drain"]["outcomes"]


def test_unchanged_paired_host_lifetime_ticket_must_drain_before_proven_refusal_can_seal():
    source = emitter()
    lifetime = admitted_writer(source.journal, "paired_host_lifetime", detached=True)
    original = legacy_failure(source)
    now = [100.0]
    manager = manager_for(source, now)
    begin(manager, now)
    waiting = manager.advance()
    assert waiting["complete"] is False and waiting["active_writers"]
    assert waiting["writer_outcomes"] == [original]
    assert not source.journal.read()["closeout"].get("refusal_reconciliations")
    lifetime.finish()
    sealed = manager.advance()
    assert sealed["complete"] is True and sealed["active_writers"] == {}
    ledger = source.journal.read()["closeout"]["refusal_reconciliations"]
    assert [row["writer_outcome"] for row in ledger] == [original]


@pytest.mark.parametrize("background", ["unknown_background", "changed_lifetime"])
def test_unknown_or_changed_background_ticket_never_proves_a_session_refusal(background):
    source = emitter()
    kind = "paired_host_lifetime" if background == "changed_lifetime" else "unknown_background"
    lifetime = admitted_writer(source.journal, kind, detached=True)
    request = admitted_writer(source.journal, "http:POST:/api/sessions")
    with request:
        if background == "changed_lifetime":
            control_transaction(source.journal, lambda state: state["closeout"]["writers"][
                lifetime.authority[2]].update(started_at=0))
        source.emit("session.create_failed", BINDING["owner"], {
            "agent": "claude", "code": "model_policy_pending", "raw_code": "model_policy_pending",
            "detail": "", "outcome": "refused"})
        request.finish(failed=True)
    lifetime.finish()
    original = copy.deepcopy(source.journal.read()["closeout"]["writer_outcomes"])
    now = [100.0]
    manager = manager_for(source, now)
    begin(manager, now)
    result = manager.advance()
    assert result["complete"] is False and result["writer_outcomes"] == original
    assert not source.journal.read()["closeout"].get("refusal_reconciliations")


@pytest.mark.parametrize("problem", ["storage", "corrupt", "lease"])
def test_audit_failure_or_expired_lease_never_reconciles_an_outcome(problem, monkeypatch):
    source = emitter()
    original = legacy_failure(source)
    now = [100.0]
    manager = manager_for(source, now)
    begin(manager, now)
    storage = source.journal.storage
    read = storage.read
    triggered = [False]
    storage_failures = [0]

    def failed_prefix(name):
        if name == f"{0:020d}.json" and (
            not triggered[0] or problem == "storage" and storage_failures[0] < 3
        ):
            triggered[0] = True
            if problem == "lease":
                now[0] += 121
            elif problem == "corrupt":
                return b"{}"
            else:
                storage_failures[0] += 1
                raise TimeoutError("synthetic unavailable prefix")
        return read(name)

    monkeypatch.setattr(storage, "read", failed_prefix)
    with pytest.raises((CaptureStorageError, CaptureHeldError, CaptureClosedError)):
        manager.advance()
    # Inspect exact committed bytes independently even when the live cache is held.
    ledger_ops = [raw for raw in storage.objects.values() if b"refusal_reconciliations" in raw]
    assert ledger_ops == []
    state = CaptureJournal(storage, BINDING, {}).read()["closeout"]
    assert state["writer_outcomes"] == [original]
    assert state.get("refusal_reconciliations", []) == []


def test_lease_expiring_during_inventory_discards_the_entire_reconciliation(monkeypatch):
    from server import closeout

    source = emitter()
    original = legacy_failure(source)
    now = [100.0]
    manager = manager_for(source, now)
    begin(manager, now)
    manifest_for = closeout.manifest_for

    def delayed_inventory(*args, **kwargs):
        result = manifest_for(*args, **kwargs)
        now[0] += 121
        return result

    monkeypatch.setattr(closeout, "manifest_for", delayed_inventory)
    with pytest.raises(CloseoutConflict, match="ownership changed"):
        manager.advance()
    state = source.journal.read()["closeout"]
    assert state["writer_outcomes"] == [original]
    assert state.get("refusal_reconciliations", []) == []
    assert "manifest" not in state

