"""Recognize one historical pre-session refusal in a fully verified audit.

The journal audit owns operation schema, canonical hash, chain and state-digest
validation. It must call ``observe`` only after those checks, from revision zero,
and use ``proofs_for`` only after the entire audit succeeds. This observer never
loads storage, edits retained state, or treats an HTTP status as proof.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from datetime import datetime

from .capture_journal import CaptureHeldError, canonical_json

_WRITER_KIND = "http:POST:/api/sessions"
_BINDING_KEYS = {"run_id", "unit_id", "workspace_id", "owner"}
_AGENTS = {"claude", "codex", "omnigent"}


def _same(left, right) -> bool:
    return canonical_json(left) == canonical_json(right)


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _hex(value, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _open(value) -> bool:
    return (isinstance(value, dict) and type(value.get("schema_version")) is int
            and value["schema_version"] == 1 and value.get("phase") == "OPEN"
            and isinstance(value.get("writers"), dict)
            and isinstance(value.get("writer_outcomes"), list))


def _writer(value, *, kind=_WRITER_KIND) -> bool:
    if (not isinstance(value, dict) or set(value) != {"kind", "process_id", "started_at"}
            or value["kind"] != kind or not _hex(value["process_id"], 32)
            or type(value["started_at"]) not in {int, float} or value["started_at"] < 0):
        return False
    try:
        return math.isfinite(value["started_at"])
    except OverflowError:
        return False


class HistoricalRefusalProofObserver:
    """An observer for exact admission/refusal/completion triples, and nothing else."""

    def __init__(self, binding: dict):
        if (not isinstance(binding, dict) or set(binding) != _BINDING_KEYS
                or any(not isinstance(value, str) or not value for value in binding.values())):
            raise CaptureHeldError("historical refusal proof binding is invalid")
        self.binding = copy.deepcopy(binding)
        self._revision, self._hash, self._state = -1, "", {}
        self._candidate = None
        self._proofs: dict[str, dict] = {}
        self._seen_writers: set[str] = set()
        self._ambiguous_writers: set[str] = set()
        self._operation_hashes: dict[int, str] = {}

    def observe(self, *, revision: int, operation_hash: str, before: dict, after: dict) -> None:
        """Consume one operation already fully validated by the journal auditor."""
        if (type(revision) is not int or revision != self._revision + 1
                or not _hex(operation_hash, 64) or not isinstance(before, dict)
                or not isinstance(after, dict)
                or (before is not self._state and not _same(before, self._state))):
            raise CaptureHeldError("historical refusal proof audit context is invalid")
        self._operation_hashes[revision] = operation_hash

        # Once an observed proof's original bytes disappear/change, restoration
        # later in the chain cannot silently make that proof eligible again.
        for writer_id, proof in list(self._proofs.items()):
            if not self._retained(proof, after):
                self._ambiguous_writers.add(writer_id)
                del self._proofs[writer_id]

        candidate = self._candidate
        self._candidate = None
        if candidate is not None:
            if candidate.get("event") is None:
                event = self._refusal(before, after, candidate)
                if event is not None:
                    candidate.update(event)
                    candidate["operations"].append({"revision": revision, "hash": operation_hash})
                    self._candidate = candidate
            else:
                outcome = self._completion(before, after, candidate)
                if outcome is not None:
                    candidate["writer_outcome"] = copy.deepcopy(outcome)
                    candidate["operations"].append({"revision": revision, "hash": operation_hash})
                    self._proofs[candidate["writer_id"]] = candidate

        # Any intervening operation ends a pending triple. A fresh admission can
        # still begin here, but never while another request writer was present.
        admission = self._admission(before, after)
        if admission is not None:
            writer_id, writer, background_writers = admission
            if writer_id in self._seen_writers:
                self._ambiguous_writers.add(writer_id)
                self._proofs.pop(writer_id, None)
            self._seen_writers.add(writer_id)
            if writer_id not in self._ambiguous_writers:
                self._candidate = {"writer_id": writer_id, "writer": copy.deepcopy(writer),
                                   "background_writers": copy.deepcopy(background_writers),
                                   "operations": [{"revision": revision, "hash": operation_hash}]}

        self._revision, self._hash, self._state = revision, operation_hash, after

    def _admission(self, before: dict, after: dict):
        old, new = before.get("closeout"), after.get("closeout")
        if old is None:
            old = {"schema_version": 1, "phase": "OPEN", "writers": {}, "writer_outcomes": []}
        if not _open(old) or not _open(new):
            return None
        # Only this already-running, independently drained host lifetime is a
        # supported background. It must remain byte-identical throughout all
        # three operations, and is never itself reconciled by this proof.
        if any(not _hex(key, 32) or not _writer(value, kind="paired_host_lifetime")
               for key, value in old["writers"].items()):
            return None
        added = new["writers"].keys() - old["writers"].keys()
        if len(added) != 1:
            return None
        writer_id = next(iter(added))
        writer = new["writers"][writer_id]
        if not _hex(writer_id, 32) or not _writer(writer):
            return None
        expected = copy.deepcopy(before)
        expected["closeout"] = copy.deepcopy(old)
        expected["closeout"]["writers"][writer_id] = copy.deepcopy(writer)
        return (writer_id, writer, old["writers"]) if _same(expected, after) else None

    def _refusal(self, before: dict, after: dict, candidate: dict):
        closeout = before.get("closeout")
        expected_writers = {**candidate["background_writers"], candidate["writer_id"]: candidate["writer"]}
        if not _open(closeout) or not _same(closeout["writers"], expected_writers):
            return None
        old, new = before.get("insight"), after.get("insight")
        if (not isinstance(old, dict) or not isinstance(new, dict)
                or type(old.get("next_seq")) is not int or old["next_seq"] < 0
                or not isinstance(old.get("events"), dict) or not isinstance(old.get("keys"), dict)
                or not isinstance(old.get("stream_id"), str) or not old["stream_id"]):
            return None
        seq = old["next_seq"] + 1
        event = new.get("events", {}).get(str(seq)) if isinstance(new.get("events"), dict) else None
        if not self._event(event) or str(seq) in old["events"] or event["idempotency_key"] in old["keys"]:
            return None
        expected = copy.deepcopy(before)
        expected["insight"]["events"][str(seq)] = copy.deepcopy(event)
        expected["insight"]["keys"][event["idempotency_key"]] = seq
        expected["insight"]["next_seq"] = seq
        if not _same(expected, after):
            return None
        return {"event": {"seq": seq, "sha256": _digest(event)},
                "event_value": copy.deepcopy(event), "stream_id": old["stream_id"]}

    def _event(self, value) -> bool:
        if (not isinstance(value, dict) or set(value) != {"schema_version", "type", "run_id", "workspace_id",
                                                        "attendee", "occurred_at", "idempotency_key", "payload"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["type"] != "session.create_failed"
                or value["run_id"] != self.binding["run_id"]
                or value["workspace_id"] != self.binding["workspace_id"]
                or value["attendee"] != self.binding["owner"]
                or not _hex(value["idempotency_key"], 32)):
            return False
        payload = value["payload"]
        if (not isinstance(payload, dict) or set(payload) != {"agent", "code", "raw_code", "detail", "outcome"}
                or not isinstance(payload["agent"], str) or payload["agent"] not in _AGENTS
                or payload["code"] != "model_policy_pending" or payload["raw_code"] != "model_policy_pending"
                or payload["detail"] != "" or payload["outcome"] != "refused"):
            return False
        try:
            return isinstance(value["occurred_at"], str) and datetime.fromisoformat(value["occurred_at"]).tzinfo is not None
        except ValueError:
            return False

    def _completion(self, before: dict, after: dict, candidate: dict):
        old = before.get("closeout")
        expected_writers = {**candidate["background_writers"], candidate["writer_id"]: candidate["writer"]}
        if not _open(old) or not _same(old["writers"], expected_writers):
            return None
        outcome = {**candidate["writer"], "writer_id": candidate["writer_id"], "status": "failed"}
        expected = copy.deepcopy(before)
        expected["closeout"]["writers"] = copy.deepcopy(candidate["background_writers"])
        expected["closeout"]["writer_outcomes"].append(outcome)
        return outcome if _same(expected, after) else None

    def _receipt_valid(self, receipt, proof: dict) -> bool:
        if (not isinstance(receipt, dict)
                or set(receipt) != {"schema_version", "kind", "source", "writer_outcome", "event", "operations", "audit"}
                or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
                or receipt["kind"] != "historical_session_refusal"
                or not _same(receipt["source"], self.binding)
                or not _same(receipt["writer_outcome"], proof["writer_outcome"])
                or not _same(receipt["event"], proof["event"])
                or not _same(receipt["operations"], proof["operations"])):
            return False
        anchor = receipt["audit"]
        return (isinstance(anchor, dict) and set(anchor) == {"revision", "hash"}
                and type(anchor["revision"]) is int
                and anchor["revision"] >= proof["operations"][-1]["revision"]
                and _hex(anchor["hash"], 64)
                and self._operation_hashes.get(anchor["revision"]) == anchor["hash"])

    def _retained(self, proof: dict, state: dict) -> bool:
        closeout, insight = state.get("closeout", {}), state.get("insight", {})
        if not isinstance(closeout, dict) or not isinstance(insight, dict):
            return False
        outcomes = closeout.get("writer_outcomes", [])
        same_writer = [item for item in outcomes if isinstance(item, dict) and item.get("writer_id") == proof["writer_id"]] if isinstance(outcomes, list) else []
        ledger = closeout.get("refusal_reconciliations", [])
        receipts = [item for item in ledger if isinstance(item, dict)
                    and isinstance(item.get("writer_outcome"), dict)
                    and item["writer_outcome"].get("writer_id") == proof["writer_id"]] if isinstance(ledger, list) else []
        original_retained = (len(same_writer) == 1 and _same(same_writer[0], proof["writer_outcome"]))
        ledger_retained = len(receipts) == 1 and self._receipt_valid(receipts[0], proof)
        return (isinstance(outcomes, list) and isinstance(ledger, list)
                and (original_retained or (not same_writer and ledger_retained))
                and insight.get("stream_id") == proof["stream_id"]
                and isinstance(insight.get("events"), dict)
                and _same(insight["events"].get(str(proof["event"]["seq"])), proof["event_value"]))

    def validate_existing_receipts(self, receipts: list[dict]) -> list[dict]:
        """Verify every retained ledger receipt from this same complete audit.

        A receipt cannot prove itself: the original three operations and its
        audit anchor must have been independently observed in this replay.
        """
        if not isinstance(receipts, list):
            raise CaptureHeldError("historical refusal reconciliation ledger is invalid")
        writers, sequences, revisions, hashes = set(), set(), set(), set()
        for receipt in receipts:
            original = receipt.get("writer_outcome") if isinstance(receipt, dict) else None
            writer_id = original.get("writer_id") if isinstance(original, dict) else None
            proof = self._proofs.get(writer_id) if isinstance(writer_id, str) else None
            if (proof is None or writer_id in self._ambiguous_writers
                    or not self._receipt_valid(receipt, proof) or not self._retained(proof, self._state)
                    or writer_id in writers or receipt["event"]["seq"] in sequences
                    or any(item["revision"] in revisions or item["hash"] in hashes for item in receipt["operations"])):
                raise CaptureHeldError("historical refusal reconciliation ledger is unproven")
            writers.add(writer_id)
            sequences.add(receipt["event"]["seq"])
            revisions.update(item["revision"] for item in receipt["operations"])
            hashes.update(item["hash"] for item in receipt["operations"])
        return copy.deepcopy(receipts)

    def proofs_for(self, outcomes: list[dict]) -> list[dict]:
        """Return exact original-outcome receipts only after full audit success.

        Calling this does not reconcile anything. The closeout owner must persist
        receipts under its current fence and leave every original failure intact.
        """
        if not isinstance(outcomes, list):
            return []
        receipts, seen_sequences = [], set()
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            writer_id = outcome.get("writer_id")
            if not isinstance(writer_id, str):
                continue
            proof = self._proofs.get(writer_id)
            if (proof is None or writer_id in self._ambiguous_writers
                    or sum(isinstance(item, dict) and item.get("writer_id") == writer_id for item in outcomes) != 1
                    or not _same(outcome, proof["writer_outcome"]) or not self._retained(proof, self._state)
                    or proof["event"]["seq"] in seen_sequences):
                continue
            seen_sequences.add(proof["event"]["seq"])
            receipts.append({"schema_version": 1, "kind": "historical_session_refusal",
                             "source": copy.deepcopy(self.binding), "writer_outcome": copy.deepcopy(outcome),
                             "event": copy.deepcopy(proof["event"]), "operations": copy.deepcopy(proof["operations"]),
                             "audit": {"revision": self._revision, "hash": self._hash}})
        return receipts
