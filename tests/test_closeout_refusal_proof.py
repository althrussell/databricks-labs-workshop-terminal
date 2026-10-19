"""Historical refusal proofs require exact, independently audited operations."""

from __future__ import annotations

import copy
import hashlib

import pytest

from server.capture_journal import (
    CaptureHeldError, CaptureJournal, MemoryCaptureStorage, _apply, _delta, canonical_json,
)
from server.closeout_refusal_proof import HistoricalRefusalProofObserver

BINDING = {"run_id": "refusal-run", "unit_id": "refusal-unit", "workspace_id": "123",
           "owner": "alice@example.com"}
WRITER_ID, PROCESS_ID = "1" * 32, "2" * 32
WRITER = {"kind": "http:POST:/api/sessions", "process_id": PROCESS_ID, "started_at": 123.25}


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def initial():
    return {"closeout": {"schema_version": 1, "phase": "OPEN", "writers": {}, "writer_outcomes": []},
            "insight": {"stream_id": "stream-one", "next_seq": 0, "acked_through": 0, "events": {}, "keys": {}},
            "producer_issues": [], "attendee_content": {"keep": ["all", "bytes"]}}


def event(*, key="3" * 32, agent="claude"):
    return {"schema_version": 1, "type": "session.create_failed", "run_id": BINDING["run_id"],
            "workspace_id": BINDING["workspace_id"], "attendee": BINDING["owner"],
            "occurred_at": "2026-10-17T23:02:45.531505+00:00", "idempotency_key": key,
            "payload": {"agent": agent, "code": "model_policy_pending", "raw_code": "model_policy_pending",
                        "detail": "", "outcome": "refused"}}


def admitted(state, *, writer_id=WRITER_ID, writer=None):
    result = copy.deepcopy(state)
    result.setdefault("closeout", {"schema_version": 1, "phase": "OPEN", "writers": {}, "writer_outcomes": []})
    result["closeout"]["writers"][writer_id] = copy.deepcopy(WRITER if writer is None else writer)
    return result


def refused(state, *, value=None):
    result, value = copy.deepcopy(state), copy.deepcopy(event() if value is None else value)
    seq = result["insight"]["next_seq"] + 1
    result["insight"]["events"][str(seq)] = value
    result["insight"]["keys"][value["idempotency_key"]] = seq
    result["insight"]["next_seq"] = seq
    return result


def completed(state, *, writer_id=WRITER_ID):
    result = copy.deepcopy(state)
    writer = result["closeout"]["writers"].pop(writer_id)
    result["closeout"]["writer_outcomes"].append({**writer, "writer_id": writer_id, "status": "failed"})
    return result


class AuditedReplay:
    """Mirror the production audit boundary, including all canonical checks."""

    def __init__(self, state=None):
        self.validator = CaptureJournal(MemoryCaptureStorage(), BINDING, {})
        self.observer = HistoricalRefusalProofObserver(BINDING)
        self.state, self.head, self.revision = {}, "", -1
        self.operations = []
        self.append(initial() if state is None else state)

    def append(self, after, *, corrupt=None):
        revision = self.revision + 1
        body = {"schema_version": 1, "revision": revision, "binding": BINDING,
                "previous_hash": self.head, "operation_id": f"{revision:032x}",
                "patch": _delta(self.state, after), "state_sha256": digest(after)}
        document = {**body, "hash": digest(body)}
        if corrupt is not None:
            corrupt(document)
        validated = self.validator._document(canonical_json(document), name=f"{revision:020d}.json")
        self.validator._validate_operation(validated, revision)
        if validated["previous_hash"] != self.head:
            raise CaptureHeldError("operation chain mismatch")
        candidate = _apply(self.state, validated["patch"])
        if digest(candidate) != validated["state_sha256"]:
            raise CaptureHeldError("operation state digest mismatch")
        self.observer.observe(revision=revision, operation_hash=validated["hash"], before=self.state, after=candidate)
        self.state, self.head, self.revision = candidate, validated["hash"], revision
        self.operations.append(validated)

    def triple(self, *, value=None, writer_id=WRITER_ID, writer=None):
        self.append(admitted(self.state, writer_id=writer_id, writer=writer))
        self.append(refused(self.state, value=value))
        self.append(completed(self.state, writer_id=writer_id))
        return self

    def receipts(self):
        return self.observer.proofs_for(self.state["closeout"]["writer_outcomes"])


@pytest.mark.parametrize("agent", ["claude", "codex", "omnigent"])
def test_exact_three_operations_prove_only_the_known_pre_session_refusal(agent):
    replay = AuditedReplay().triple(value=event(agent=agent))
    before = canonical_json(replay.state)
    receipt, = replay.receipts()
    assert receipt == {"schema_version": 1, "kind": "historical_session_refusal", "source": BINDING,
                       "writer_outcome": {**WRITER, "writer_id": WRITER_ID, "status": "failed"},
                       "event": {"seq": 1, "sha256": digest(event(agent=agent))},
                       "operations": [{"revision": revision, "hash": replay.operations[revision]["hash"]}
                                      for revision in [1, 2, 3]],
                       "audit": {"revision": 3, "hash": replay.head}}
    assert canonical_json(replay.state) == before
    receipt["writer_outcome"]["status"] = "changed"
    assert replay.receipts()[0]["writer_outcome"]["status"] == "failed"


def test_first_admission_can_create_exact_initial_closeout_state():
    state = initial()
    del state["closeout"]
    replay = AuditedReplay(state).triple()
    assert len(replay.receipts()) == 1


def test_unrelated_existing_failure_is_not_reclassified_or_removed():
    state = initial()
    unknown = {**WRITER, "writer_id": "a" * 32, "kind": "terminal_lifetime", "status": "failed"}
    state["closeout"]["writer_outcomes"].append(unknown)
    replay = AuditedReplay(state).triple()
    assert replay.receipts()[0]["writer_outcome"]["writer_id"] == WRITER_ID
    assert replay.state["closeout"]["writer_outcomes"] == [unknown, replay.receipts()[0]["writer_outcome"]]


@pytest.mark.parametrize("stage", [0, 1, 2])
@pytest.mark.parametrize("mutation", ["content", "producer_issue", "other_writer", "ack", "closeout_field"])
def test_any_additional_mutation_in_any_of_the_three_operations_remains_unresolved(stage, mutation):
    replay = AuditedReplay()
    transforms = [admitted, refused, completed]
    for index, transform in enumerate(transforms):
        after = transform(replay.state)
        if index == stage:
            if mutation == "content":
                after["attendee_content"]["keep"].append("additional")
            elif mutation == "producer_issue":
                after["producer_issues"].append("unverified_output")
            elif mutation == "other_writer":
                after["closeout"]["writers"]["b" * 32] = {**WRITER, "kind": "background"}
            elif mutation == "ack":
                after["insight"]["acked_through"] = 1
            elif mutation == "closeout_field":
                after["closeout"]["unexpected"] = True
        replay.append(after)
    assert replay.receipts() == []


@pytest.mark.parametrize("stage", [0, 1])
def test_even_an_intervening_noop_operation_breaks_the_exact_triple(stage):
    replay = AuditedReplay()
    for index, transform in enumerate([admitted, refused, completed]):
        replay.append(transform(replay.state))
        if index == stage:
            replay.append(copy.deepcopy(replay.state))
    assert replay.receipts() == []


@pytest.mark.parametrize("steps", [0, 1, 2])
def test_partial_or_missing_writer_completion_is_not_proven(steps):
    replay = AuditedReplay()
    for transform in [admitted, refused, completed][:steps]:
        replay.append(transform(replay.state))
    assert replay.receipts() == []


@pytest.mark.parametrize("field,value", [
    ("type", "session.started"), ("attendee", "another@example.com"), ("run_id", "another-run"),
    ("workspace_id", "456"), ("schema_version", True), ("idempotency_key", "not-a-uuid"),
    ("occurred_at", "2026-10-17T23:02:45"),
])
def test_event_envelope_must_match_exact_schema_and_source(field, value):
    changed = event()
    changed[field] = value
    assert AuditedReplay().triple(value=changed).receipts() == []


@pytest.mark.parametrize("field,value", [
    ("agent", "terminal"), ("code", "credential_unavailable"), ("raw_code", "unknown"),
    ("detail", "unexpected detail"), ("outcome", "failed"), ("extra", "unrecognized"),
])
def test_other_or_partially_matching_typed_failures_are_not_proven(field, value):
    changed = event()
    changed["payload"][field] = value
    assert AuditedReplay().triple(value=changed).receipts() == []


@pytest.mark.parametrize("field,value", [
    ("kind", "http:POST:/api/discovery"), ("kind", "http:PUT:/api/sessions"),
    ("process_id", "unrecognized-process"), ("started_at", True), ("started_at", -1),
    ("started_at", 10 ** 500), ("extra", "unknown"),
])
def test_only_exact_original_legacy_session_writer_shape_is_eligible(field, value):
    changed = {**WRITER, field: value}
    assert AuditedReplay().triple(writer=changed).receipts() == []


def test_existing_concurrent_writer_even_if_unchanged_prevents_proof():
    state = initial()
    state["closeout"]["writers"]["b" * 32] = {**WRITER, "kind": "terminal_lifetime"}
    assert AuditedReplay(state).triple().receipts() == []


def test_unchanged_existing_paired_host_lifetime_does_not_obscure_unique_request():
    state = initial()
    background = {**WRITER, "kind": "paired_host_lifetime"}
    state["closeout"]["writers"]["b" * 32] = background
    replay = AuditedReplay(state).triple()
    assert len(replay.receipts()) == 1
    assert replay.state["closeout"]["writers"] == {"b" * 32: background}
    assert replay.receipts()[0]["writer_outcome"]["kind"] == "http:POST:/api/sessions"


@pytest.mark.parametrize("stage", [0, 1, 2])
@pytest.mark.parametrize("mutation", ["changed", "removed", "new_paired_host"])
def test_background_lifetime_must_be_exactly_unchanged_in_each_operation(stage, mutation):
    state = initial()
    state["closeout"]["writers"]["b" * 32] = {**WRITER, "kind": "paired_host_lifetime"}
    replay = AuditedReplay(state)
    for index, transform in enumerate([admitted, refused, completed]):
        after = transform(replay.state)
        if index == stage:
            if mutation == "changed":
                after["closeout"]["writers"]["b" * 32]["started_at"] += 1
            elif mutation == "removed":
                del after["closeout"]["writers"]["b" * 32]
            else:
                after["closeout"]["writers"]["c" * 32] = {**WRITER, "kind": "paired_host_lifetime"}
        replay.append(after)
    assert replay.receipts() == []


def test_another_existing_session_request_is_always_ambiguous():
    state = initial()
    state["closeout"]["writers"]["b" * 32] = WRITER
    assert AuditedReplay(state).triple().receipts() == []


def test_second_event_in_same_operation_prevents_proof():
    replay = AuditedReplay()
    replay.append(admitted(replay.state))
    replay.append(refused(refused(replay.state), value=event(key="4" * 32)))
    replay.append(completed(replay.state))
    assert replay.receipts() == []


def test_two_distinct_exact_triples_have_unique_receipts_and_audit_tail_anchor():
    replay = AuditedReplay().triple()
    replay.triple(writer_id="4" * 32, value=event(key="5" * 32))
    replay.append(copy.deepcopy(replay.state))
    receipts = replay.receipts()
    assert len(receipts) == 2
    assert len({row["writer_outcome"]["writer_id"] for row in receipts}) == 2
    assert [row["event"]["seq"] for row in receipts] == [1, 2]
    assert all(row["audit"] == {"revision": 7, "hash": replay.head} for row in receipts)


def test_reused_writer_id_is_ambiguous_even_when_both_events_are_known_refusals():
    replay = AuditedReplay().triple()
    replay.triple(value=event(key="4" * 32))
    assert replay.receipts() == []


@pytest.mark.parametrize("mutation", ["event", "stream", "outcome", "duplicate_outcome", "outcome_removed"])
def test_later_original_byte_or_identity_change_permanently_invalidates_proof(mutation):
    replay = AuditedReplay().triple()
    original = copy.deepcopy(replay.state)
    changed = copy.deepcopy(original)
    if mutation == "event":
        changed["insight"]["events"]["1"]["payload"]["code"] = "unknown"
    elif mutation == "stream":
        changed["insight"]["stream_id"] = "another-stream"
    elif mutation == "outcome":
        changed["closeout"]["writer_outcomes"][0]["started_at"] += 1
    elif mutation == "duplicate_outcome":
        changed["closeout"]["writer_outcomes"] *= 2
    elif mutation == "outcome_removed":
        changed["closeout"]["writer_outcomes"] = []
    replay.append(changed)
    replay.append(original)
    assert replay.receipts() == []


def test_callers_cannot_request_a_changed_outcome_or_duplicate_one():
    replay = AuditedReplay().triple()
    original = replay.state["closeout"]["writer_outcomes"]
    assert replay.observer.proofs_for([{**original[0], "started_at": 999}]) == []
    assert replay.observer.proofs_for(original * 2) == []


@pytest.mark.parametrize("corruption", ["hash", "state_digest", "chain", "source"])
def test_corrupt_operation_cannot_cross_the_verified_audit_boundary(corruption):
    replay = AuditedReplay()
    replay.append(admitted(replay.state))

    def corrupt(document):
        if corruption == "hash":
            document["hash"] = "0" * 64
            return
        if corruption == "state_digest":
            document["state_sha256"] = "0" * 64
        elif corruption == "chain":
            document["previous_hash"] = "0" * 64
        elif corruption == "source":
            document["binding"] = {**BINDING, "workspace_id": "another"}
        document["hash"] = digest({key: value for key, value in document.items() if key != "hash"})

    with pytest.raises(CaptureHeldError):
        replay.append(refused(replay.state), corrupt=corrupt)
    assert replay.observer.proofs_for([]) == []


@pytest.mark.parametrize("bad_context", ["checkpoint_start", "gap", "boolean_revision", "hash", "before_state"])
def test_incomplete_or_inconsistent_observer_context_is_held(bad_context):
    observer = HistoricalRefusalProofObserver(BINDING)
    observer.observe(revision=0, operation_hash="a" * 64, before={}, after=initial())
    kwargs = {"revision": 1, "operation_hash": "b" * 64, "before": initial(), "after": initial()}
    if bad_context in {"checkpoint_start", "gap"}:
        kwargs["revision"] = 128
    elif bad_context == "boolean_revision":
        kwargs["revision"] = True
    elif bad_context == "hash":
        kwargs["operation_hash"] = "not-a-canonical-hash"
    elif bad_context == "before_state":
        kwargs["before"]["attendee_content"]["keep"] = []
    with pytest.raises(CaptureHeldError):
        observer.observe(**kwargs)


def reconcile(replay, receipt=None):
    receipts = replay.receipts() if receipt is None else [receipt]
    state = copy.deepcopy(replay.state)
    state["closeout"]["refusal_reconciliations"] = receipts
    state["closeout"]["writer_outcomes"] = []
    replay.append(state)
    return receipts


def test_exact_durable_ledger_preserves_entire_original_failure_and_survives_full_replay():
    replay = AuditedReplay().triple()
    original = copy.deepcopy(replay.state["closeout"]["writer_outcomes"][0])
    receipts = reconcile(replay)
    assert receipts[0]["writer_outcome"] == original
    assert replay.observer.validate_existing_receipts(receipts) == receipts
    # Rebuild a fresh observer only from independently verified original objects.
    observer = HistoricalRefusalProofObserver(BINDING)
    state = {}
    for operation in replay.operations:
        after = _apply(state, operation["patch"])
        observer.observe(revision=operation["revision"], operation_hash=operation["hash"], before=state, after=after)
        state = after
    assert observer.validate_existing_receipts(receipts) == receipts


@pytest.mark.parametrize("mutation", ["source", "outcome", "event", "operation", "audit_hash", "future_anchor", "extra", "duplicate"])
def test_unproven_or_modified_ledger_never_excuses_original_outcome_removal(mutation):
    replay = AuditedReplay().triple()
    receipt = copy.deepcopy(replay.receipts()[0])
    if mutation == "source":
        receipt["source"]["workspace_id"] = "other"
    elif mutation == "outcome":
        receipt["writer_outcome"]["kind"] = "terminal_lifetime"
    elif mutation == "event":
        receipt["event"]["seq"] = 2
    elif mutation == "operation":
        receipt["operations"][1]["hash"] = "f" * 64
    elif mutation == "audit_hash":
        receipt["audit"]["hash"] = "f" * 64
    elif mutation == "future_anchor":
        receipt["audit"]["revision"] = 999
    elif mutation == "extra":
        receipt["unrecognized"] = True
    receipts = reconcile(replay, receipt)
    if mutation == "duplicate":
        receipts *= 2
    with pytest.raises(CaptureHeldError):
        replay.observer.validate_existing_receipts(receipts)
    assert replay.receipts() == []


def test_ledger_cannot_prove_itself_without_the_original_three_operations():
    replay = AuditedReplay().triple()
    receipts = replay.receipts()
    state = copy.deepcopy(replay.state)
    state["closeout"]["refusal_reconciliations"] = receipts
    state["closeout"]["writer_outcomes"] = []
    checkpoint_only = AuditedReplay(state)
    with pytest.raises(CaptureHeldError):
        checkpoint_only.observer.validate_existing_receipts(receipts)


def test_empty_ledger_is_valid_and_validator_returns_a_defensive_copy():
    replay = AuditedReplay().triple()
    assert replay.observer.validate_existing_receipts([]) == []
    receipts = reconcile(replay)
    result = replay.observer.validate_existing_receipts(receipts)
    result[0]["writer_outcome"]["status"] = "changed"
    assert replay.observer.validate_existing_receipts(receipts) == receipts
