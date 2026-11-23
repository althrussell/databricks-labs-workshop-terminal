"""Idempotent sealed closeout of existing WT capture, driven by admin requests.

This is producer evidence, never CT's permission to delete a workspace. CT-02
must independently commit and verify the advertised inventory in Lakebase.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime

from . import config
from .capture_journal import CaptureError, CaptureHeldError, canonical_json
from .closeout_guard import (
    CaptureClosedError, _only_ack_change, closeout_state, control_transaction, finalizer_scope,
    initial_state,
)
from .closeout_refusal_proof import HistoricalRefusalProofObserver

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
CAPABILITY = "sealed-closeout-v1"
_lock_creation = threading.Lock()
_FINALIZER_LEASE_SECONDS = 120
_FINALIZER_RENEW_SECONDS = 20
# Writer-drain budget for one worker pass; every stage shares it. The process
# census comes last; it keeps each process's HOME verdict between passes, so a
# pass that runs out of time still leaves the next one less to do. A sealing
# pass runs two drains, then finalization and the audit replay. None of this runs
# inside an HTTP request: the closeout worker thread drives it, and requests
# only persist the barrier and report progress.
_DRAIN_STEP_SECONDS = 5.0
# Persisted progress within CLOSING. Absent means "draining" (also for captures
# persisted before this field existed); INCOMPLETE and SEALED carry no stage.
_STAGES = frozenset({"draining", "finalizing", "auditing"})
_WORKER_BACKOFF_SECONDS = 1.0
_WORKER_FAILURE_BACKOFF_SECONDS = (1.0, 5.0, 30.0)
_INCOMPLETE_RETRY_SECONDS = 30.0
_FULL_PASS_INTERVAL_SECONDS = 30.0
_WORKER_IDLE_STOP_SECONDS = 300.0
_WORKER_STALE_SECONDS = 2 * _FINALIZER_LEASE_SECONDS
SUPPORTED_EVENTS = frozenset({
    "session.started", "session.switched", "session.exited", "build.commit",
    "resource.created", "milestone.completed", "agent.error", "cost.sample", "heartbeat",
    "credential.health", "entitlements.health", "operational.health", "bootstrap.phase",
    "mirror.fetch", "entitlement.reconcile", "model_policy.applied", "event_deadline.applied",
    "agent_control.changed", "app.deploy", "background.task_failed", "gateway.request_limited",
    "obo.health", "omnigent.host_health", "attendee.error_seen", "session.create_failed",
    "install.step_failed", "omnigent.error_detected", "app.error_detected", "selfheal.attempted",
    "identity.resolved", "workshop.signal", "discovery.record", "insight.summary", "attendee.identity",
    "app.manifest", "journey.status", "journey.phase", "journey.outcome", "agent.activity",
    "network.check", "omnigent.session_ready", "omnigent.first_message",
    "omnigent.first_message_missing", "omnigent.session_start_stalled",
    "omnigent.terminal_exited_early", "friction.event",
})
REQUIRED_CATEGORIES = ("workshop.signal", "insight.summary", "discovery.record",
                       "attendee.identity", "identity.resolved", "help")
# Help assistant v1 entries are retained with exactly these fields.
ASSISTANT_HELP_FIELDS = {
    "feedback": frozenset({"seq", "kind", "message_id", "value", "created_at"}),
    "handoff": frozenset({"seq", "kind", "handoff_id", "help_request_id", "created_at"}),
}
_UTC_SECOND = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class CloseoutConflict(ValueError):
    """A stale or different request cannot claim this source's closeout."""


# Request-level errors from a pass are raised once on the next status read, so CT
# sees the same 409 or 503 it saw before the worker existed.
_REQUEST_ERRORS = (CloseoutConflict, CaptureError)


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _utc_timestamp(value) -> bool:
    if not isinstance(value, str) or not _UTC_SECOND.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _canonical_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _malformed_assistant_help(entry: dict) -> bool:
    kind = entry["kind"]
    if set(entry) != ASSISTANT_HELP_FIELDS[kind] or not _utc_timestamp(entry["created_at"]):
        return True
    if kind == "feedback":
        return (not isinstance(entry["message_id"], str) or not entry["message_id"]
                or entry["value"] not in ("up", "down"))
    request_id = entry["help_request_id"]
    return (not _canonical_uuid(entry["handoff_id"])
            or (request_id is not None and not isinstance(request_id, str)))


def inventory(entries: list[dict]) -> dict:
    return {"first_seq": entries[0]["seq"] if entries else 0,
            "last_seq": entries[-1]["seq"] if entries else 0,
            "count": len(entries), "sha256": digest(entries),
            "entries": [{"seq": entry["seq"], "sha256": digest(entry)} for entry in entries]}


def _reconciled_drain(drained: dict, operation: dict) -> dict:
    """Carry the retained proof ledger through fresh/retried drain receipts."""
    receipts = operation.get("refusal_reconciliations", [])
    if not isinstance(receipts, list) or any(not isinstance(row, dict) for row in receipts):
        raise CaptureHeldError("retained refusal reconciliation ledger is invalid")
    value = copy.deepcopy(drained)
    outcomes = value.get("outcomes", [])
    if not isinstance(outcomes, list) or any(not isinstance(row, dict) for row in outcomes):
        raise CaptureHeldError("retained drain outcomes are invalid")
    previous = [row for row in outcomes if row.get("kind") == "pre_session_refusal_reconciliation"]
    if previous:
        row = previous[0]
        old_receipts = row.get("receipts")
        if (len(previous) != 1 or set(row) != {"kind", "complete", "issues", "receipts"}
                or row["complete"] is not True or row["issues"] != []
                or not isinstance(old_receipts, list) or not old_receipts
                or canonical_json(old_receipts) != canonical_json(receipts[:len(old_receipts)])):
            raise CaptureHeldError("retained drain reconciliation does not match its ledger")
    outcomes = [row for row in outcomes if row.get("kind") != "pre_session_refusal_reconciliation"]
    if receipts:
        outcomes.append({"kind": "pre_session_refusal_reconciliation", "complete": True,
                         "issues": [], "receipts": copy.deepcopy(receipts)})
    if receipts or previous:
        value["outcomes"] = outcomes
    return value


def manifest_for(state: dict, binding: dict, *, completed_at: float) -> dict:
    """Pure canonical inventory of retained producer bytes, independent of ACKs."""
    operation = closeout_state(state)
    if operation is None or operation["phase"] not in {"CLOSING", "INCOMPLETE"}:
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
        if not isinstance(kind, str) or kind not in {"message", "seen", *ASSISTANT_HELP_FIELDS}:
            issues.add("unsupported_required_output")
            continue
        if kind in ASSISTANT_HELP_FIELDS:
            if _malformed_assistant_help(entry):
                issues.add("malformed_required_help_output")
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
        "drain": _reconciled_drain(operation.get("drain", {}), operation),
    }


def _drain(timeout: float) -> dict:
    from . import closeout_writers, insight_summary
    started = time.monotonic()
    process = closeout_writers.drain(timeout)
    summary = insight_summary.drain_for_closeout(max(0, timeout - (time.monotonic() - started)))
    return {"complete": bool(process["complete"] and summary["complete"]),
            "issues": sorted(set(process.get("issues", []) + summary.get("issues", []))),
            "outcomes": process.get("outcomes", []) + summary.get("outcomes", [])}


class _CloseoutWorker:
    """Single-flight background driver of one emitter's closeout.

    One thread at a time. The thread repeats CloseoutManager.advance until the
    phase leaves CLOSING, then stops at SEALED or INCOMPLETE. It also stops when
    nobody has polled for _WORKER_IDLE_STOP_SECONDS; the next status read restarts
    it. A failing pass backs off; a pass with no heartbeat for
    _WORKER_STALE_SECONDS is stale and a status read starts a replacement. The
    replacement still needs the persisted claim lease to have expired, so two
    passes cannot both finalize. Everything here is in memory and resets on
    restart; only the persisted operation survives.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._guard = threading.Lock()
        self._thread = None
        self._generation = 0
        self._pending_error = None
        self._next_full_pass = None
        self.state = "idle"          # outcome of the last thread: idle | failed
        self.last_beat = None
        self.last_poll = None
        self.retry_after = None
        self.failures = 0
        self.last_error = None       # {"code", "detail"}; cleared by a successful pass

    def poll(self) -> None:
        self.last_poll = self.clock()

    def beat(self) -> None:
        self.last_beat = self.clock()

    def observed(self) -> str:
        """running, stale, idle or failed. Stale means no heartbeat for too long."""
        if self._thread is None:
            return self.state
        if self.last_beat is not None and self.clock() - self.last_beat > _WORKER_STALE_SECONDS:
            return "stale"
        return "running"

    def take_request_error(self):
        """A request-level error from the last pass, raised once on the next read."""
        error, self._pending_error = self._pending_error, None
        return error

    def allows_full_pass(self) -> bool:
        return self._next_full_pass is None or self.clock() >= self._next_full_pass

    def full_pass_started(self) -> None:
        self._next_full_pass = self.clock() + _FULL_PASS_INTERVAL_SECONDS

    def start(self, manager, capture_id: str) -> bool:
        now = self.clock()
        with self._guard:
            stale = self._thread is not None and self.observed() == "stale"
            if self._thread is not None and not stale:
                return False
            if self.retry_after is not None and now < self.retry_after:
                return False
            if stale:
                # The stale thread cannot be stopped. Its advance lock may never
                # release, so the replacement gets a fresh one. Writes stay fenced
                # by the claim lease, which the replacement must see expired.
                logger.warning("closeout worker stale; starting a replacement")
                manager.emitter._closeout_advance_lock = threading.Lock()
                manager._lock = manager.emitter._closeout_advance_lock
            self._generation += 1
            generation = self._generation
            self.state, self.last_beat = "running", now
            thread = threading.Thread(target=self._run, args=(manager, capture_id, generation),
                                      name="closeout-worker", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                self.last_error = {"code": "worker_start_failed", "detail": ""}
                self._back_off()
                self.state = "failed"
                manager._persist_worker_issue(capture_id, "worker_start_failed")
                return False
            self._thread = thread
            return True

    def _back_off(self) -> None:
        self.failures += 1
        step = min(self.failures, len(_WORKER_FAILURE_BACKOFF_SECONDS)) - 1
        self.retry_after = self.clock() + _WORKER_FAILURE_BACKOFF_SECONDS[step]

    def _idle_too_long(self) -> bool:
        return self.last_poll is not None and self.clock() - self.last_poll > _WORKER_IDLE_STOP_SECONDS

    def _run(self, manager, capture_id: str, generation: int) -> None:
        try:
            while True:
                if generation != self._generation:
                    return  # superseded by a replacement; it must not touch state
                self.beat()
                current = manager.advance(capture_id=capture_id, gate=self)
                if current["phase"] != "CLOSING" or self._idle_too_long():
                    break
                time.sleep(_WORKER_BACKOFF_SECONDS)
            if generation != self._generation:
                return  # a replacement took over while this pass ran
            self.state, self.failures, self.last_error = "idle", 0, None
            # An INCOMPLETE result waits before its next retry; a CLOSING stop
            # (idle) restarts on the next poll.
            self.retry_after = (self.clock() + _INCOMPLETE_RETRY_SECONDS
                                if current["phase"] == "INCOMPLETE" else None)
        except Exception as error:
            if generation == self._generation:
                self.state = "failed"
                self.last_error = {"code": type(error).__name__,
                                   "detail": str(error)[:200] if isinstance(error, _REQUEST_ERRORS) else ""}
                self._back_off()
                # A failed pass is paced by its backoff alone, not the full-pass gate.
                self._next_full_pass = None
                if isinstance(error, _REQUEST_ERRORS):
                    self._pending_error = error
                # Full traceback server-side; only the class name is persisted.
                logger.exception("closeout worker pass failed")
                manager._persist_worker_issue(capture_id, type(error).__name__)
        finally:
            with self._guard:
                if generation == self._generation:
                    self._thread = None


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
            if not hasattr(emitter, "_closeout_worker"):
                emitter._closeout_worker = _CloseoutWorker()
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
                         drain_deadline=now + drain_timeout_seconds, issues=[],
                         stage="draining")

        # Only the barrier is durable work here. Drains, finalization and the
        # audit run in the closeout worker, never inside this request.
        control_transaction(self.journal, request, durable=True)
        return self.status(capture_id=capture_id)

    def resume(self, *, capture_id: str = "") -> dict:
        """Start the closeout worker for an unsealed capture, then report status.

        Requests use this and never wait for drains, the finalizer or the audit.
        Every call records a poll, which keeps an idle worker alive. A running
        worker is left alone. A failed or INCOMPLETE capture restarts only after
        its backoff. A request-level error from the last pass is raised once
        here, so CT still gets the 409 or 503 it expects.
        """
        worker = self.emitter._closeout_worker
        worker.poll()
        current = self.status(capture_id=capture_id)
        if current["phase"] not in {"CLOSING", "INCOMPLETE"}:
            return current
        # Only errors from earlier passes: taken before this read starts one, so a
        # pass that fails at once cannot be raised on the request that started it.
        error = worker.take_request_error()
        worker.start(self, current["capture_id"])
        result = self.status(capture_id=capture_id)
        if error is not None:
            raise error
        return result

    def _progress(self, value: dict) -> dict | None:
        # Diagnostics only, never proof. "worker" and "last_error" are in memory;
        # "stage", "active_writers" and "drains_pending" are persisted or derived.
        if value["phase"] not in {"CLOSING", "INCOMPLETE"}:
            return None
        worker = self.emitter._closeout_worker
        worker_state = worker.observed()
        drain = value.get("drain")
        stage = value.get("stage") or ("incomplete" if value["phase"] == "INCOMPLETE" else "draining")
        progress = {"stage": stage, "active_writers": len(value["writers"]),
                    "drains_pending": bool(value["writers"]) or not bool(drain and drain.get("complete")),
                    "worker": worker_state}
        if value["phase"] == "INCOMPLETE":
            # INCOMPLETE with retrying: the last finished pass stands while a retry runs.
            progress["retrying"] = worker_state in {"running", "stale"}
        if worker.last_error is not None:
            progress["last_error"] = dict(worker.last_error)
        return progress

    def _persist_worker_issue(self, capture_id: str, code: str) -> None:
        """Retain that a pass failed, as an issue of the open operation. Best effort:
        if storage is down, the in-memory last_error still reports it."""
        def record(state):
            operation = closeout_state(state)
            if (operation is None or operation["phase"] not in {"CLOSING", "INCOMPLETE"}
                    or operation.get("capture_id") != capture_id):
                return
            operation["issues"] = sorted(set(operation.get("issues", [])) | {f"worker_failed:{code}"})
        try:
            control_transaction(self.journal, record, durable=True)
        except Exception:
            logger.warning("closeout worker failure could not be retained")

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
        if "stage" in value and (value["phase"] not in {"CLOSING", "INCOMPLETE"}
                                 or not isinstance(value["stage"], str)
                                 or value["stage"] not in _STAGES):
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
        progress = self._progress(value)
        return {"schema_version": SCHEMA_VERSION, "capability": CAPABILITY,
                "phase": value["phase"], "capture_id": value.get("capture_id"),
                "generation": value.get("generation"),
                "complete": value["phase"] == "SEALED" and bool(manifest and manifest["complete"]),
                "issues": copy.deepcopy(value.get("issues", [])),
                "active_writers": copy.deepcopy(value["writers"]),
                "writer_outcomes": copy.deepcopy(value["writer_outcomes"]),
                "manifest": copy.deepcopy(manifest), "manifest_sha256": value.get("manifest_sha256"),
                "final_outputs": copy.deepcopy(value.get("final_outputs", {})),
                # Diagnostics only, never proof, and only while unsealed: the last
                # drain's per-stage outcomes (completion, issue codes, counts and
                # stage_elapsed_ms; no paths or contents), so CT's retained status
                # shows why a seat is still CLOSING. A sealed status is unchanged.
                **({"drain": copy.deepcopy(value["drain"])}
                   if value["phase"] in {"CLOSING", "INCOMPLETE"} and value.get("drain") is not None
                   else {}),
                # Additive progress while unsealed, so CT can see the worker moving.
                # A sealed status is unchanged.
                **({"progress": progress} if progress is not None else {}),
                "ct_verification_required": True}

    def advance(self, *, capture_id: str = "", wait_seconds: float = _DRAIN_STEP_SECONDS,
                gate=None) -> dict:
        """One synchronous pass of the closeout state machine.

        The closeout worker calls this with itself as ``gate``, which paces
        finalize-and-audit passes. Tests call it directly with no gate. HTTP
        requests never call it; they use ``resume``.

        A retry of an INCOMPLETE capture keeps phase INCOMPLETE and its last
        manifest visible until the pass seals or is incomplete again. Its stage
        and progress say it is retrying.
        """
        current = self.status(capture_id=capture_id)
        step = max(0.0, min(wait_seconds, _DRAIN_STEP_SECONDS))
        if current["phase"] not in {"CLOSING", "INCOMPLETE"} or not self._lock.acquire(blocking=False):
            return current
        claim_id = ""
        worker = self.emitter._closeout_worker
        try:
            if current["phase"] == "INCOMPLETE":
                def retry(state):
                    value = closeout_state(state)
                    if value["phase"] == "INCOMPLETE":
                        value["stage"] = "draining"
                control_transaction(self.journal, retry, durable=True)
            drained = self.drainer(step)
            worker.beat()
            value = closeout_state(self.journal.read())
            issues = list(drained.get("issues", []))
            if value["writers"]:
                issues.append("writers_not_drained")
                if self.clock() >= value["drain_deadline"]:
                    issues.append("writer_drain_deadline_exceeded")

            def record_drain(state):
                operation = closeout_state(state)
                if operation["phase"] in {"CLOSING", "INCOMPLETE"}:
                    operation["drain"] = _reconciled_drain(drained, operation)
                    operation["issues"] = sorted(set(issues))
                    if (operation["phase"] == "CLOSING" and (operation["writers"] or not drained["complete"])
                            and self.clock() >= operation["drain_deadline"]):
                        operation["phase"] = "INCOMPLETE"
                        operation.pop("stage", None)
            control_transaction(self.journal, record_drain, durable=True)
            if value["writers"] or not drained["complete"]:
                return self.status(capture_id=capture_id)
            # Stop a process which an already-admitted spawn finished creating
            # during the first drain; no new admission can start after begin.
            # It runs the census again, so it needs the same budget.
            drained = self.drainer(step)
            worker.beat()
            if not drained["complete"]:
                def record_racing_writer(state):
                    operation = closeout_state(state)
                    if operation["phase"] in {"CLOSING", "INCOMPLETE"}:
                        operation["drain"] = _reconciled_drain(drained, operation)
                        operation["issues"] = sorted(set(drained.get("issues", []) or ["writer_drain_unverified"]))
                        if operation["phase"] == "CLOSING" and self.clock() >= operation["drain_deadline"]:
                            operation["phase"] = "INCOMPLETE"
                            operation.pop("stage", None)
                control_transaction(self.journal, record_racing_writer, durable=True)
                return self.status(capture_id=capture_id)
            # Finalize-and-audit is the expensive part. A gate caps how often it
            # runs; drains above still ran, so the operation stays current.
            if gate is not None and not gate.allows_full_pass():
                return self.status(capture_id=capture_id)
            proposed_claim = uuid.uuid4().hex

            def claim(state):
                now = self.clock()
                operation = closeout_state(state)
                if operation["phase"] not in {"CLOSING", "INCOMPLETE"} or operation["writers"]:
                    return False
                previous = operation.get("finalizer")
                if previous and previous["expires_at"] > now:
                    return False
                operation["finalizer"] = {"claim_id": proposed_claim, "expires_at": now + _FINALIZER_LEASE_SECONDS}
                operation["issues"] = []
                operation["drain"] = _reconciled_drain(drained, operation)
                operation["stage"] = "finalizing"
                return True
            if not control_transaction(self.journal, claim, durable=True):
                return self.status(capture_id=capture_id)
            claim_id = proposed_claim
            if gate is not None:
                gate.full_pass_started()
            renewed_at = self.clock()

            def renew():
                nonlocal renewed_at
                if self.clock() - renewed_at < _FINALIZER_RENEW_SECONDS:
                    return

                def extend(state):
                    now = self.clock()
                    operation = closeout_state(state)
                    previous = operation.get("finalizer", {})
                    if (operation["phase"] not in {"CLOSING", "INCOMPLETE"} or operation["writers"]
                            or operation.get("capture_id") != current["capture_id"]
                            or operation.get("generation") != current["generation"]
                            or previous.get("claim_id") != claim_id
                            or previous.get("expires_at", 0) <= now):
                        raise CaptureClosedError("closeout finalizer ownership changed")
                    previous["expires_at"] = now + _FINALIZER_LEASE_SECONDS
                control_transaction(self.journal, extend, durable=True)
                renewed_at = self.clock()

            with finalizer_scope(self.journal, current["capture_id"], claim_id, clock=self.clock):
                result = self.finalizer(self.emitter, current["capture_id"])
            worker.beat()
            # Preserve pending rejected-output accounting and the emitter's
            # semantic validation before inspecting a private replay.
            self.emitter._state()
            # Full replay must not hold the live journal lock for its entire
            # network history or run inside one unrenewed 120-second lease.
            # The private replay still checks every prefix byte; renewals and
            # normal ACK/status requests use the unchanged live journal cache.
            refusal_proofs = HistoricalRefusalProofObserver(self.journal.binding)

            def auditing(state):
                # The finalizer may have run for a long time. Only the current,
                # unexpired claim may enter the audit, and entering renews it.
                now = self.clock()
                operation = closeout_state(state)
                lease = operation.get("finalizer") or {}
                if (operation["phase"] not in {"CLOSING", "INCOMPLETE"} or operation["writers"]
                        or lease.get("claim_id") != claim_id or lease.get("expires_at", 0) <= now):
                    raise CloseoutConflict("closeout finalizer ownership changed")
                lease["expires_at"] = now + _FINALIZER_LEASE_SECONDS
                operation["stage"] = "auditing"
            control_transaction(self.journal, auditing, durable=True)
            worker.beat()
            audited = self.journal.audit_snapshot(progress=renew, observer=refusal_proofs.observe)
            audited_state = audited.read()  # Validate any new ACK/renewal suffix.
            try:
                self.emitter._validate_state(audited_state)
            except CaptureHeldError as error:
                self.journal.mark_held(error)
                raise
            audited_operation = closeout_state(audited_state)
            if (audited_operation is None or audited_operation["phase"] not in {"CLOSING", "INCOMPLETE"}
                    or audited_operation["writers"]
                    or audited_operation.get("capture_id") != current["capture_id"]
                    or audited_operation.get("generation") != current["generation"]
                    or audited_operation.get("finalizer", {}).get("claim_id") != claim_id):
                raise CaptureClosedError("audited closeout finalizer ownership changed")
            refusal_proofs.validate_existing_receipts(
                audited_operation.get("refusal_reconciliations", []))
            reconciliations = refusal_proofs.proofs_for(audited_operation["writer_outcomes"])
            renew()
            worker.beat()

            def seal(state):
                completed_at = self.clock()
                operation = closeout_state(state)
                if (operation["phase"] not in {"CLOSING", "INCOMPLETE"} or operation["writers"]
                        or operation.get("capture_id") != current["capture_id"]
                        or operation.get("generation") != current["generation"]
                        or operation.get("finalizer", {}).get("claim_id") != claim_id
                        or operation["finalizer"]["expires_at"] <= completed_at):
                    raise CloseoutConflict("closeout finalizer ownership changed")
                # Only a renewal of this exact lease and legal monotonically
                # advancing transport ACKs can follow the fully audited state.
                # Never inventory an unverified live-cache producer mutation.
                expected = copy.deepcopy(audited_state)
                expected["closeout"]["finalizer"]["expires_at"] = operation["finalizer"]["expires_at"]
                if not _only_ack_change(expected, state):
                    raise CaptureHeldError("capture changed after its full audit")
                if reconciliations:
                    unresolved = operation["writer_outcomes"]
                    ledger = operation.setdefault("refusal_reconciliations", [])
                    known_writers = {row["writer_outcome"]["writer_id"] for row in ledger}
                    for receipt in reconciliations:
                        original = receipt["writer_outcome"]
                        matches = [index for index, row in enumerate(unresolved)
                                   if canonical_json(row) == canonical_json(original)]
                        event = state["insight"]["events"].get(str(receipt["event"]["seq"]))
                        if (len(matches) != 1 or original["writer_id"] in known_writers
                                or digest(event) != receipt["event"]["sha256"]):
                            raise CaptureHeldError("historical refusal proof no longer matches retained output")
                        # The original failed outcome remains byte-for-byte in
                        # this durable ledger and in its immutable historical
                        # operation. Only exact audited no-input refusals leave
                        # the unresolved-failure collection.
                        ledger.append(copy.deepcopy(receipt))
                        known_writers.add(original["writer_id"])
                        unresolved.pop(matches[0])
                    operation["drain"] = _reconciled_drain(operation.get("drain", {}), operation)
                operation["final_outputs"] = copy.deepcopy(result.get("outputs", {}))
                operation["outputs_complete"] = result.get("complete") is True
                operation["issues"] = sorted(set(result.get("issues", [])))
                manifest = manifest_for(state, self.journal.binding, completed_at=completed_at)
                if self.journal.health().get("operation_capacity_exhausted"):
                    raise CaptureHeldError("capture operation capacity is exhausted")
                if operation["finalizer"]["expires_at"] <= self.clock():
                    raise CloseoutConflict("closeout finalizer ownership changed")
                if not manifest["complete"] and completed_at < operation["drain_deadline"]:
                    operation["issues"] = copy.deepcopy(manifest["issues"])
                    operation.pop("finalizer", None)
                    operation["stage"] = "draining"
                    return
                operation["manifest"], operation["manifest_sha256"] = manifest, digest(manifest)
                operation["issues"] = copy.deepcopy(manifest["issues"])
                operation["phase"] = "SEALED" if manifest["complete"] else "INCOMPLETE"
                operation.pop("finalizer", None)
                operation.pop("stage", None)
            control_transaction(self.journal, seal, durable=True)
            return self.status(capture_id=capture_id)
        finally:
            try:
                if claim_id:
                    def release(state):
                        value = closeout_state(state)
                        if (value["phase"] in {"CLOSING", "INCOMPLETE"}
                                and value.get("finalizer", {}).get("claim_id") == claim_id):
                            value.pop("finalizer", None)
                            value["stage"] = "draining"
                    control_transaction(self.journal, release, durable=True)
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
