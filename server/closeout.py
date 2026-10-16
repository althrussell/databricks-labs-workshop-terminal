"""Idempotent sealed closeout of existing WT capture, driven by admin requests.

This is producer evidence, never CT's permission to delete a workspace. CT-02
must independently commit and verify the advertised inventory in Lakebase.
"""

from __future__ import annotations

import copy
import hashlib
import math
import threading
import time
import uuid
from collections import defaultdict

from . import config
from .capture_journal import CaptureHeldError, canonical_json
from .closeout_guard import (
    closeout_state, control_transaction, finalizer_scope,
    initial_state,
)

SCHEMA_VERSION = 1
CAPABILITY = "sealed-closeout-v1"
_lock_creation = threading.Lock()
_FINALIZER_LEASE_SECONDS = 120
SUPPORTED_EVENTS = frozenset({
    "session.started", "session.switched", "session.exited", "build.commit",
    "resource.created", "milestone.completed", "agent.error", "cost.sample", "heartbeat",
    "credential.health", "entitlements.health", "operational.health", "bootstrap.phase",
    "mirror.fetch", "entitlement.reconcile", "model_policy.applied", "event_deadline.applied",
    "agent_control.changed", "app.deploy", "background.task_failed", "gateway.request_limited",
    "obo.health", "omnigent.host_health", "attendee.error_seen", "session.create_failed",
    "install.step_failed", "omnigent.error_detected", "app.error_detected", "selfheal.attempted",
    "identity.resolved", "workshop.signal", "discovery.record", "insight.summary", "attendee.identity",
})
REQUIRED_CATEGORIES = ("workshop.signal", "insight.summary", "discovery.record",
                       "attendee.identity", "identity.resolved", "help")


class CloseoutConflict(ValueError):
    """A stale or different request cannot claim this source's closeout."""


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def inventory(entries: list[dict]) -> dict:
    return {"first_seq": entries[0]["seq"] if entries else 0,
            "last_seq": entries[-1]["seq"] if entries else 0,
            "count": len(entries), "sha256": digest(entries),
            "entries": [{"seq": entry["seq"], "sha256": digest(entry)} for entry in entries]}


def manifest_for(state: dict, binding: dict, *, completed_at: float) -> dict:
    """Pure canonical inventory of retained producer bytes, independent of ACKs."""
    operation = closeout_state(state)
    if operation is None or operation["phase"] != "CLOSING":
        raise CloseoutConflict("source is not closing")
    insight = state["insight"]
    entries = [{"seq": seq, "event": insight["events"][str(seq)]}
               for seq in range(1, insight["next_seq"] + 1)]
    help_state = state.get("help") or {}
    if help_state:
        from .help import _validate_state
        _validate_state(help_state)
    help_entries = [help_state["history"][key] for key in sorted(
        help_state.get("history", {}), key=int)]
    groups = defaultdict(list)
    for entry in entries:
        groups[entry["event"]["type"]].append(entry)
    issues = set(operation.get("issues", [])) | set(state.get("producer_issues", []))
    if operation["writers"]:
        issues.add("writers_not_drained")
    if operation["writer_outcomes"]:
        issues.add("accepted_writer_failed")
    if operation.get("capture_enabled") is not True:
        issues.add("capture_disabled")
    if help_state.get("history_floor", 0):
        issues.add("legacy_help_history_unverifiable")
    if help_state and "history" not in help_state:
        issues.add("help_history_unavailable")
    if len(help_entries) != help_state.get("seq", 0):
        issues.add("help_history_incomplete")
    for entry in help_entries:
        kind = entry.get("kind")
        if not isinstance(kind, str) or kind not in {"message", "seen"}:
            issues.add("unsupported_required_output")
            continue
        required = ("message_id",) if kind == "seen" else (
            "message_id", "body", "created_at", "sender_role")
        malformed = any(not isinstance(entry.get(key), str) or not entry[key] for key in required)
        if kind == "message":
            malformed = (malformed or not isinstance(entry.get("sender_role"), str)
                         or entry["sender_role"] not in {"attendee", "operator"}
                         or not isinstance(entry.get("sender"), str)
                         or "help_request_id" not in entry
                         or (entry["help_request_id"] is not None and not isinstance(entry["help_request_id"], str)))
        if malformed:
            issues.add("malformed_required_help_output")
    if any(entry["event"].get("schema_version") != 1
           or entry["event"]["type"] not in SUPPORTED_EVENTS for entry in entries):
        issues.add("unsupported_required_output")
    final = operation.get("final_outputs") or {}
    if not operation.get("outputs_complete"):
        issues.add("final_outputs_incomplete")
    categories = []
    for name in sorted(set(groups) | set(REQUIRED_CATEGORIES)):
        value = help_entries if name == "help" else groups[name]
        categories.append({"type": name, "required": name in REQUIRED_CATEGORIES,
                           "count": len(value), "sha256": digest(value)})
    return {
        "schema_version": SCHEMA_VERSION, "capability": CAPABILITY,
        "capture_id": operation["capture_id"], "generation": operation["generation"],
        "source": {**binding, "stream_id": insight["stream_id"]},
        "requested_at": operation["requested_at"], "completed_at": completed_at,
        "complete": not issues, "issues": sorted(issues),
        "required_categories": list(REQUIRED_CATEGORIES), "categories": categories,
        "streams": {"insight": {"stream_id": insight["stream_id"], **inventory(entries)},
                    "help": {"stream_id": insight["stream_id"], **inventory(help_entries)}},
        "final_outputs_sha256": digest(final),
        "writer_outcomes": copy.deepcopy(operation["writer_outcomes"]),
        "drain": copy.deepcopy(operation.get("drain", {})),
    }


def _drain(timeout: float) -> dict:
    from . import closeout_writers, insight_summary
    started = time.monotonic()
    process = closeout_writers.drain(timeout)
    summary = insight_summary.drain_for_closeout(max(0, timeout - (time.monotonic() - started)))
    return {"complete": bool(process["complete"] and summary["complete"]),
            "issues": sorted(set(process.get("issues", []) + summary.get("issues", []))),
            "outcomes": process.get("outcomes", []) + summary.get("outcomes", [])}


class CloseoutManager:
    def __init__(self, emitter, *, drainer=None, finalizer=None, clock=None):
        self.emitter = emitter
        emitter._ensure_ready()
        self.journal = emitter.journal
        self.drainer = drainer or _drain
        if finalizer is None:
            from .closeout_outputs import finalize
            finalizer = finalize
        self.finalizer = finalizer
        self.clock = clock or time.time
        with _lock_creation:
            if not hasattr(emitter, "_closeout_advance_lock"):
                emitter._closeout_advance_lock = threading.Lock()
        self._lock = emitter._closeout_advance_lock

    def begin(self, *, capture_id: str, stream_id: str, source: dict,
              drain_timeout_seconds: int = 120) -> dict:
        if (not isinstance(capture_id, str) or not capture_id or len(capture_id) > 128
                or type(drain_timeout_seconds) is not int or not 1 <= drain_timeout_seconds <= 600):
            raise ValueError("invalid closeout request")
        if source != self.journal.binding or stream_id != self.emitter.stream_id:
            raise CloseoutConflict("closeout source does not match the retained binding")
        now, generation = self.clock(), uuid.uuid4().hex

        def request(state):
            value = closeout_state(state)
            if value is None:
                value = state.setdefault("closeout", initial_state())
            if value["phase"] != "OPEN":
                if (value.get("capture_id") != capture_id
                        or value.get("stream_id") != stream_id
                        or value.get("drain_timeout_seconds") != drain_timeout_seconds):
                    raise CloseoutConflict("another closeout request already owns this source")
                return
            value.update(phase="CLOSING", capture_id=capture_id, generation=generation,
                         stream_id=stream_id, requested_at=now,
                         capture_enabled=config.insight_capture_enabled(),
                         drain_timeout_seconds=drain_timeout_seconds,
                         drain_deadline=now + drain_timeout_seconds, issues=[])

        control_transaction(self.journal, request)
        return self.status(capture_id=capture_id)

    def status(self, *, capture_id: str = "") -> dict:
        state = self.journal.read()
        self.emitter._validate_state(state)
        value = closeout_state(state) or initial_state()
        # Only a complete persisted operation may drive retry/seal decisions.
        # The low-level barrier also supports minimal OPEN/CLOSING markers, so
        # validate coordinator metadata here before indexing it in advance.
        def valid_time(timestamp):
            if type(timestamp) not in {int, float} or timestamp < 0:
                return False
            try:
                return math.isfinite(timestamp)
            except OverflowError:
                return False

        invalid = "retained closeout operation is invalid"
        if value["phase"] != "OPEN":
            if (any(not isinstance(value.get(key), str) or not value[key]
                    for key in ("capture_id", "generation", "stream_id"))
                    or value["stream_id"] != state["insight"]["stream_id"]
                    or not valid_time(value.get("requested_at"))
                    or not valid_time(value.get("drain_deadline"))
                    or value["drain_deadline"] < value["requested_at"]
                    or type(value.get("drain_timeout_seconds")) is not int
                    or not 1 <= value["drain_timeout_seconds"] <= 600
                    or type(value.get("capture_enabled")) is not bool):
                raise CaptureHeldError(invalid)
        if (not isinstance(value.get("issues", []), list)
                or any(not isinstance(issue, str) or not issue for issue in value.get("issues", []))
                or not isinstance(value.get("final_outputs", {}), dict)
                or ("outputs_complete" in value and type(value["outputs_complete"]) is not bool)):
            raise CaptureHeldError(invalid)
        if "finalizer" in value:
            claim = value["finalizer"]
            if (not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str)
                    or not claim["claim_id"] or not valid_time(claim.get("expires_at"))):
                raise CaptureHeldError(invalid)
        if capture_id and value.get("capture_id") != capture_id:
            raise CloseoutConflict("capture identifier does not match this source")
        manifest = value.get("manifest")
        if value["phase"] == "SEALED" and manifest is None:
            raise CaptureHeldError(invalid)
        if manifest is not None:
            if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
                    or manifest["schema_version"] != SCHEMA_VERSION
                    or manifest.get("capability") != CAPABILITY
                    or manifest.get("capture_id") != value.get("capture_id")
                    or manifest.get("generation") != value.get("generation")
                    or manifest.get("source") != {**self.journal.binding, "stream_id": value.get("stream_id")}
                    or type(manifest.get("complete")) is not bool
                    or not valid_time(manifest.get("completed_at"))):
                raise CaptureHeldError(invalid)
        if manifest is not None and value.get("manifest_sha256") != digest(manifest):
            raise CaptureHeldError("retained closeout manifest digest does not match")
        if value["phase"] == "SEALED":
            # A source produced by incompatible old code cannot silently extend
            # a sealed generation, even if its journal chain is well formed.
            snapshot = copy.deepcopy(state)
            snapshot["closeout"]["phase"] = "CLOSING"
            rebuilt = manifest_for(snapshot, self.journal.binding,
                                   completed_at=manifest["completed_at"])
            if canonical_json(rebuilt) != canonical_json(manifest):
                raise CaptureHeldError("sealed capture inventory no longer matches retained content")
        return {"schema_version": SCHEMA_VERSION, "capability": CAPABILITY,
                "phase": value["phase"], "capture_id": value.get("capture_id"),
                "generation": value.get("generation"),
                "complete": value["phase"] == "SEALED" and bool(manifest and manifest["complete"]),
                "issues": copy.deepcopy(value.get("issues", [])),
                "active_writers": copy.deepcopy(value["writers"]),
                "writer_outcomes": copy.deepcopy(value["writer_outcomes"]),
                "manifest": copy.deepcopy(manifest), "manifest_sha256": value.get("manifest_sha256"),
                "final_outputs": copy.deepcopy(value.get("final_outputs", {})),
                "ct_verification_required": True}

    def advance(self, *, capture_id: str = "", wait_seconds: float = 0.25) -> dict:
        current = self.status(capture_id=capture_id)
        if current["phase"] not in {"CLOSING", "INCOMPLETE"} or not self._lock.acquire(blocking=False):
            return current
        claim_id = ""
        try:
            if current["phase"] == "INCOMPLETE":
                def resume(state):
                    value = closeout_state(state)
                    if value["phase"] == "INCOMPLETE":
                        value["phase"] = "CLOSING"
                        value.pop("manifest", None)
                        value.pop("manifest_sha256", None)
                control_transaction(self.journal, resume)
            drained = self.drainer(max(0.0, min(wait_seconds, 5.0)))
            value = closeout_state(self.journal.read())
            issues = list(drained.get("issues", []))
            if value["writers"]:
                issues.append("writers_not_drained")
                if self.clock() >= value["drain_deadline"]:
                    issues.append("writer_drain_deadline_exceeded")

            def record_drain(state):
                operation = closeout_state(state)
                if operation["phase"] == "CLOSING":
                    operation["drain"] = copy.deepcopy(drained)
                    operation["issues"] = sorted(set(issues))
                    if (operation["writers"] or not drained["complete"]) and self.clock() >= operation["drain_deadline"]:
                        operation["phase"] = "INCOMPLETE"
            control_transaction(self.journal, record_drain)
            if value["writers"] or not drained["complete"]:
                return self.status(capture_id=capture_id)
            # Stop a process which an already-admitted spawn finished creating
            # during the first drain; no new admission can start after begin.
            drained = self.drainer(0.25)
            if not drained["complete"]:
                def record_racing_writer(state):
                    operation = closeout_state(state)
                    if operation["phase"] == "CLOSING":
                        operation["drain"] = copy.deepcopy(drained)
                        operation["issues"] = sorted(set(drained.get("issues", []) or ["writer_drain_unverified"]))
                        if self.clock() >= operation["drain_deadline"]:
                            operation["phase"] = "INCOMPLETE"
                control_transaction(self.journal, record_racing_writer)
                return self.status(capture_id=capture_id)
            now, proposed_claim = self.clock(), uuid.uuid4().hex

            def claim(state):
                operation = closeout_state(state)
                if operation["phase"] != "CLOSING" or operation["writers"]:
                    return False
                previous = operation.get("finalizer")
                if previous and previous["expires_at"] > now:
                    return False
                operation["finalizer"] = {"claim_id": proposed_claim, "expires_at": now + _FINALIZER_LEASE_SECONDS}
                operation["issues"] = []
                operation["drain"] = copy.deepcopy(drained)
                return True
            if not control_transaction(self.journal, claim):
                return self.status(capture_id=capture_id)
            claim_id = proposed_claim
            with finalizer_scope(self.journal, current["capture_id"], claim_id, clock=self.clock):
                result = self.finalizer(self.emitter, current["capture_id"])
            # Refresh retained history, including all competing ACKs. The seal
            # callback inventories this same serializable candidate state.
            self.emitter._state()
            self.journal.audit()
            completed_at = self.clock()

            def seal(state):
                operation = closeout_state(state)
                if (operation["phase"] != "CLOSING" or operation["writers"]
                        or operation.get("finalizer", {}).get("claim_id") != claim_id
                        or operation["finalizer"]["expires_at"] <= completed_at):
                    raise CloseoutConflict("closeout finalizer ownership changed")
                operation["final_outputs"] = copy.deepcopy(result.get("outputs", {}))
                operation["outputs_complete"] = result.get("complete") is True
                operation["issues"] = sorted(set(result.get("issues", [])))
                manifest = manifest_for(state, self.journal.binding, completed_at=completed_at)
                if self.journal.health().get("operation_capacity_exhausted"):
                    raise CaptureHeldError("capture operation capacity is exhausted")
                if not manifest["complete"] and completed_at < operation["drain_deadline"]:
                    operation["issues"] = manifest["issues"]
                    operation.pop("finalizer", None)
                    return
                operation["manifest"], operation["manifest_sha256"] = manifest, digest(manifest)
                operation["issues"] = manifest["issues"]
                operation["phase"] = "SEALED" if manifest["complete"] else "INCOMPLETE"
                operation.pop("finalizer", None)
            control_transaction(self.journal, seal)
            return self.status(capture_id=capture_id)
        finally:
            try:
                if claim_id:
                    def release(state):
                        value = closeout_state(state)
                        if (value["phase"] == "CLOSING"
                                and value.get("finalizer", {}).get("claim_id") == claim_id):
                            value.pop("finalizer", None)
                    control_transaction(self.journal, release)
            finally:
                self._lock.release()

    def entries(self, *, capture_id: str, kind: str, after: int = 0, limit: int = 1000) -> dict:
        if kind not in {"insight", "help"} or type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid sealed page cursor")
        status = self.status(capture_id=capture_id)
        if status["phase"] != "SEALED":
            raise CloseoutConflict("capture is not sealed")
        state = self.journal.read()
        if kind == "insight":
            events = state["insight"]["events"]
            entries = [{"seq": int(key), "event": value} for key, value in events.items() if int(key) > after]
        else:
            entries = [entry for entry in state.get("help", {}).get("history", {}).values() if entry["seq"] > after]
        entries.sort(key=lambda entry: entry["seq"])
        total = len(entries)
        entries = entries[:limit]
        return {"schema_version": SCHEMA_VERSION, "capture_id": capture_id,
                "generation": status["generation"], "manifest_sha256": status["manifest_sha256"],
                "kind": kind, "entries": copy.deepcopy(entries),
                "high_water": entries[-1]["seq"] if entries else after,
                "remaining": total - len(entries)}


def manager() -> CloseoutManager:
    from .event_emitter import event_emitter
    return CloseoutManager(event_emitter)
