"""Retained attendee-event delivery over the existing authenticated CT pull path.

Version 2 GET is non-destructive. Exact envelopes, logical keys, the epoch and
ACK position share the capture journal with discovery/help/revision recovery.
Production attaches Workspace-backed storage before accepting capture; the
constructor's memory journal is for explicit standalone/local/test use.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .capture_journal import (
    CaptureCapacityError,
    CaptureError,
    CaptureHeldError,
    CaptureJournal,
    MemoryCaptureStorage,
)

SCHEMA_VERSION = 1
COLLECT_SCHEMA_VERSION = 2
DEFAULT_MAX_BUFFER = 5000
DEFAULT_COLLECT_LIMIT = 1000
_parse_timestamp = datetime.fromisoformat


class CaptureConflictError(ValueError):
    """A logical retry changed an already accepted event's content."""


class CaptureEncodingError(CaptureError):
    """A required output could not be represented as finite canonical JSON."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class EventEmitter:
    def __init__(
        self,
        *,
        run_id: str,
        workspace_id: str,
        ingest_url: str,
        ingest_token: str,
        max_buffer: int = DEFAULT_MAX_BUFFER,
        journal: CaptureJournal | None = None,
    ) -> None:
        if type(max_buffer) is not int or max_buffer < 1:
            raise ValueError("capture pending limit must be a positive integer")
        if not isinstance(run_id, str) or not isinstance(workspace_id, str):
            raise ValueError("capture run and workspace identifiers must be strings")
        self.run_id = run_id
        self.workspace_id = workspace_id
        self.ingest_url = (ingest_url or "").rstrip("/")
        self.ingest_token = ingest_token or ""
        self._max_buffer = max_buffer
        self._lock = threading.RLock()
        self._collections = 0
        self._last_collected_at: float | None = None
        self._initializer: Callable[[], None] | None = None
        self._initializing = False
        self._configuration_fault = ""
        self._pending_issues: set[str] = set()
        self.journal = journal or CaptureJournal(
            MemoryCaptureStorage(),
            {
                "run_id": run_id or "standalone",
                "unit_id": "standalone",
                "workspace_id": workspace_id or "standalone",
                "owner": "standalone",
            },
            {},
        )
        if journal is not None:
            self._check_binding(journal)
        self._initialize_stream()

    def _check_binding(self, journal: CaptureJournal) -> None:
        if (
            journal.binding["run_id"] != self.run_id
            or journal.binding["workspace_id"] != self.workspace_id
        ):
            raise CaptureHeldError(
                "capture storage does not match emitter source binding"
            )

    def _initialize_stream(self) -> None:
        epoch = uuid.uuid4().hex

        def initialize(state):
            state.setdefault(
                "insight",
                {
                    "stream_id": epoch,
                    "next_seq": 0,
                    "acked_through": 0,
                    "events": {},
                    "keys": {},
                },
            )
            self._validate_state(state)

        try:
            self.journal.transaction(initialize)
        except CaptureHeldError as error:
            self.journal.mark_held(error)
            raise

    @staticmethod
    def _insight(state: dict) -> dict:
        value = state.get("insight")
        if not isinstance(value, dict):
            raise CaptureHeldError("retained insight state is missing")
        seq, ack = value.get("next_seq"), value.get("acked_through")
        if (
            not isinstance(value.get("stream_id"), str)
            or not value["stream_id"]
            or type(seq) is not int
            or type(ack) is not int
            or not 0 <= ack <= seq
            or not isinstance(value.get("events"), dict)
            or not isinstance(value.get("keys"), dict)
            or len(value["events"]) != seq
        ):
            raise CaptureHeldError("retained insight state is inconsistent")
        return value

    def configure_journal(self, journal: CaptureJournal) -> None:
        with self._lock:
            previous = self.journal
            if previous is journal:
                return
            old_state = previous.read()
            old = self._insight(old_state)
            if (
                old["next_seq"]
                or self._pending_issues
                or any(key != "insight" for key in old_state)
            ):
                raise CaptureHeldError(
                    "cannot replace capture storage after accepting output"
                )
            self._check_binding(journal)
            self.journal = journal
            try:
                self._initialize_stream()
            except Exception:
                self.journal = previous
                raise

    def defer_initialization(
        self, initializer: Callable[[], None], reason: str
    ) -> None:
        with self._lock:
            self._initializer = initializer
            self._configuration_fault = reason

    def _ensure_ready(self) -> None:
        with self._lock:
            if self._initializing:
                return  # Reentrant producer attachment during this initialization.
            if self._initializer is not None and not self._initializing:
                self._initializing = True
                try:
                    self._initializer()
                except Exception as exc:
                    self._configuration_fault = type(exc).__name__
                    raise CaptureHeldError(
                        "capture persistence is not initialized"
                    ) from exc
                else:
                    self._initializer = None
                    self._configuration_fault = ""
                finally:
                    self._initializing = False
            if self._configuration_fault:
                raise CaptureHeldError("capture persistence is held")

    def _validate_state(self, state: dict) -> dict:
        insight = self._insight(state)
        # Every retained position and logical key must resolve to its exact
        # accepted envelope, including fields whose bool/int equality could
        # otherwise admit parseable but contradictory JSON state.
        fields = {
            "schema_version",
            "run_id",
            "workspace_id",
            "attendee",
            "type",
            "occurred_at",
            "payload",
            "idempotency_key",
        }
        for seq in range(1, insight["next_seq"] + 1):
            event = insight["events"].get(str(seq))
            if (
                not isinstance(event, dict)
                or set(event) != fields
                or type(event.get("schema_version")) is not int
                or event["schema_version"] != SCHEMA_VERSION
                or event.get("run_id") != self.run_id
                or event.get("workspace_id") != self.workspace_id
                or any(
                    not isinstance(event.get(name), str) or not event[name]
                    for name in ("attendee", "type", "occurred_at", "idempotency_key")
                )
                or not isinstance(event.get("payload"), dict)
            ):
                raise CaptureHeldError("retained insight envelope is inconsistent")
            try:
                timestamp = _parse_timestamp(event["occurred_at"])
            except ValueError as error:
                raise CaptureHeldError(
                    "retained insight observation time is invalid"
                ) from error
            if timestamp.tzinfo is None:
                raise CaptureHeldError(
                    "retained insight observation time has no timezone"
                )
            indexed = insight["keys"].get(event["idempotency_key"])
            if type(indexed) is not int or indexed != seq:
                raise CaptureHeldError("retained insight key index is inconsistent")
        if len(insight["keys"]) != insight["next_seq"]:
            raise CaptureHeldError("retained insight key index is inconsistent")
        issues = state.get("producer_issues", [])
        if not isinstance(issues, list) or any(
            not isinstance(issue, str) or not issue for issue in issues
        ):
            raise CaptureHeldError("retained capture issue accounting is invalid")
        return insight

    def _state(self) -> dict:
        self._ensure_ready()
        self._persist_issues()
        state = self.journal.read()
        try:
            self._validate_state(state)
        except CaptureHeldError as error:
            self.journal.mark_held(error)
            raise
        return state

    def _persist_issues(self) -> None:
        with self._lock:
            issues = sorted(self._pending_issues)
            if not issues:
                return

            def record(state):
                retained = state.setdefault("producer_issues", [])
                if not isinstance(retained, list):
                    raise CaptureHeldError(
                        "retained capture issue accounting is invalid"
                    )
                for issue in issues:
                    if issue not in retained:
                        retained.append(issue)

            self.journal.transaction(record)
            self._pending_issues.difference_update(issues)

    def _rejected(self, exc: Exception, key: str = "") -> None:
        # No payload/PII is put in diagnostics. A later healthy read must not
        # erase evidence of output that a producer tried but failed to retain.
        digest = hashlib.sha256(key.encode()).hexdigest()[:16] if key else "unknown"
        with self._lock:
            if len(self._pending_issues) < 64:
                self._pending_issues.add(
                    f"capture_rejected:{type(exc).__name__}:{digest}"
                )
            else:
                self._pending_issues.add("capture_rejected:additional_failures")
        try:
            self._persist_issues()
        except CaptureError:
            pass  # Retried before the next successful read/append/ACK.

    @property
    def stream_id(self) -> str:
        self._ensure_ready()
        return self._insight(self._state())["stream_id"]

    @property
    def can_push(self) -> bool:
        return bool(self.ingest_url and self.ingest_token and self.run_id)

    def emit_transaction(
        self,
        event_type: str,
        attendee: str,
        prepare: Callable[[dict[str, Any]], tuple[dict[str, Any], str] | None],
    ) -> int:
        """Serialize first acceptance against changing the configured store."""
        with self._lock:
            return self._emit_transaction(event_type, attendee, prepare)

    def _emit_transaction(
        self,
        event_type: str,
        attendee: str,
        prepare: Callable[[dict[str, Any]], tuple[dict[str, Any], str] | None],
    ) -> int:
        """Commit recovery metadata and an exact envelope as one journal entry.

        Prepare may be retried after contention and must have no external side
        effects. Repeating a logical key returns its original sequence only if
        its type, attendee and payload are unchanged, even after acknowledgement.
        """
        if (
            not isinstance(event_type, str)
            or not event_type
            or not isinstance(attendee, str)
            or not attendee
        ):
            raise ValueError("capture type and attendee must be nonempty strings")
        self._ensure_ready()
        self._persist_issues()
        attempted_key = ""
        occurred_at = datetime.now(timezone.utc).isoformat()

        def append(state):
            nonlocal attempted_key
            self._validate_state(state)
            # A skipped or duplicate retry cannot commit speculative recovery
            # metadata without a matching newly accepted event.
            candidate = copy.deepcopy(state)
            insight = self._insight(candidate)
            prepared = prepare(candidate)
            if prepared is None:
                return 0
            if not isinstance(prepared, tuple) or len(prepared) != 2:
                raise ValueError(
                    "capture preparation must return payload and logical key"
                )
            payload, key = prepared
            if not isinstance(payload, dict) or not isinstance(key, str) or not key:
                raise ValueError("capture payload and logical key are required")
            attempted_key = key
            event = {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "workspace_id": self.workspace_id,
                "attendee": attendee,
                "type": event_type,
                "occurred_at": occurred_at,
                "payload": payload,
                "idempotency_key": key,
            }
            try:
                _canonical(event)
            except (TypeError, ValueError, RecursionError) as error:
                raise CaptureEncodingError(
                    "capture output is not finite JSON"
                ) from error
            previous = insight["keys"].get(key)
            if previous is not None:
                old = insight["events"].get(str(previous))
                if not isinstance(old, dict):
                    raise CaptureHeldError("retained logical key has no event")
                identity = (
                    "schema_version",
                    "run_id",
                    "workspace_id",
                    "attendee",
                    "type",
                    "payload",
                    "idempotency_key",
                )
                if _canonical({name: old.get(name) for name in identity}) != _canonical(
                    {name: event[name] for name in identity}
                ):
                    raise CaptureConflictError(
                        "logical key already has different capture content"
                    )
                return previous
            if insight["next_seq"] - insight["acked_through"] >= self._max_buffer:
                raise CaptureCapacityError(
                    "capture backlog is full; existing records are retained"
                )
            seq = insight["next_seq"] + 1
            insight["events"][str(seq)] = event
            insight["keys"][key] = seq
            insight["next_seq"] = seq
            self._validate_state(candidate)
            state.clear()
            state.update(candidate)
            return seq

        try:
            return self.journal.transaction(append)
        except (CaptureError, CaptureConflictError) as exc:
            from .closeout_guard import CaptureClosedError

            if isinstance(exc, CaptureClosedError):
                raise  # Intentional close refusal must not poison a valid seal.
            if isinstance(exc, CaptureHeldError):
                self.journal.mark_held(exc)
            else:
                self._rejected(exc, attempted_key)
            raise

    def emit(
        self,
        event_type: str,
        attendee: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> int:
        key = uuid.uuid4().hex if idempotency_key is None else idempotency_key
        value = {} if payload is None else payload
        return self.emit_transaction(event_type, attendee, lambda _state: (value, key))

    def pending(self) -> int:
        self._ensure_ready()
        insight = self._insight(self._state())
        return insight["next_seq"] - insight["acked_through"]

    @property
    def dropped(self) -> int:
        self._ensure_ready()
        return 0

    def collect(
        self, *, after: int = 0, stream: str = "", limit: int = DEFAULT_COLLECT_LIMIT
    ) -> dict[str, Any]:
        self._ensure_ready()
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or limit < 1
            or not isinstance(stream, str)
        ):
            raise ValueError("capture cursor and page limit must be valid integers")
        state = self._state()
        insight = self._insight(state)
        reset = bool(after > 0 and stream != insight["stream_id"])
        if reset:
            after = 0
        if after > insight["next_seq"]:
            raise ValueError("capture cursor exceeds emitted sequence")
        floor = max(after, insight["acked_through"])
        end = min(insight["next_seq"], floor + min(limit, DEFAULT_COLLECT_LIMIT))
        entries = [
            {"seq": seq, "event": insight["events"][str(seq)]}
            for seq in range(floor + 1, end + 1)
        ]
        if not entries and (
            state.get("producer_issues")
            or self.journal.health().get("operation_capacity_exhausted")
        ):
            raise CaptureHeldError("capture history has unresolved delivery issues")
        with self._lock:
            self._collections += 1
            self._last_collected_at = time.time()
        return {
            "schema_version": COLLECT_SCHEMA_VERSION,
            "ack_mode": "explicit",
            "stream_id": insight["stream_id"],
            "events": entries,
            "high_water": entries[-1]["seq"] if entries else after,
            "pending": insight["next_seq"] - insight["acked_through"],
            "dropped": 0,
            "cursor_reset": reset,
            "delivery": "pull",
        }

    def acknowledge(
        self, *, schema_version: int, stream_id: str, through_seq: int
    ) -> dict[str, Any]:
        self._ensure_ready()
        self._persist_issues()
        if type(schema_version) is not int or schema_version != COLLECT_SCHEMA_VERSION:
            raise ValueError("unsupported capture acknowledgement version")
        if type(through_seq) is not int or through_seq < 0:
            raise ValueError("capture acknowledgement must be a nonnegative integer")

        def acknowledge(state):
            insight = self._validate_state(state)
            if stream_id != insight["stream_id"]:
                raise ValueError("capture acknowledgement epoch does not match")
            if through_seq > insight["next_seq"]:
                raise ValueError("capture acknowledgement exceeds emitted sequence")
            insight["acked_through"] = max(insight["acked_through"], through_seq)
            return {
                "schema_version": COLLECT_SCHEMA_VERSION,
                "stream_id": stream_id,
                "acked_through": insight["acked_through"],
            }

        try:
            return self.journal.transaction(acknowledge)
        except CaptureHeldError as error:
            self.journal.mark_held(error)
            raise

    def delivery_status(self) -> dict[str, Any]:
        try:
            self._ensure_ready()
            state = self._state()
            insight = self._insight(state)
            issues = list(state.get("producer_issues") or [])
            persistence = self.journal.health()
            if persistence.get("operation_capacity_exhausted"):
                # The last allowed operation may have been an ACK. A refused
                # output cannot then append its fault marker, so this persisted
                # boundary must remain visible in a fresh app process too.
                issues.append("capture_operation_capacity_exhausted")
            ready = (
                not issues
                and not self._configuration_fault
                and persistence.get("ok", False)
                and not persistence.get("held", False)
            )
            epoch = insight["stream_id"]
            pending = insight["next_seq"] - insight["acked_through"]
            ack = insight["acked_through"]
        except Exception as exc:
            persistence = {"state": "HELD", "error": type(exc).__name__}
            ready, epoch, pending, ack = False, "", None, None
            issues = ["capture_persistence_unavailable"]
        with self._lock:
            return {
                "delivery": "pull",
                "schema_version": COLLECT_SCHEMA_VERSION,
                "ack_mode": "explicit",
                "stream_id": epoch,
                "pending": pending,
                "dropped": 0,
                "acked_through": ack,
                "collections": self._collections,
                "last_collected_at": self._last_collected_at,
                "retention_ready": ready,
                "retention_issues": issues,
                "persistence": persistence,
            }

    def drain(self, post: Callable[[dict[str, Any]], bool]) -> int:
        """Keep the optional push adapter ordered and retain failed records."""
        delivered = 0
        try:
            page = self.collect()
            for entry in page["events"]:
                try:
                    ok = post(entry["event"])
                except Exception:
                    ok = False
                if not ok:
                    break
                self.acknowledge(
                    schema_version=COLLECT_SCHEMA_VERSION,
                    stream_id=page["stream_id"],
                    through_seq=entry["seq"],
                )
                delivered += 1
        except Exception:
            return delivered
        return delivered


def build_default_emitter() -> EventEmitter:
    from . import config

    return EventEmitter(
        run_id=config.workshop_run_id(),
        workspace_id=config.workspace_id(),
        ingest_url=config.control_tower_ingest_url(),
        ingest_token=config.control_tower_ingest_token(),
    )


def _http_post(emitter: "EventEmitter", event: dict[str, Any]) -> bool:
    """POST one event to CT's ingest endpoint. True on 2xx."""
    import requests

    try:
        resp = requests.post(
            f"{emitter.ingest_url}/api/ingest/events",
            json=event,
            headers={"X-Ingest-Token": emitter.ingest_token},
            timeout=10,
        )
        return 200 <= resp.status_code < 300
    except requests.RequestException:
        return False


def flush_loop(
    emitter: "EventEmitter", stop: threading.Event, *, interval: float = 15.0
) -> None:
    """Background flusher: drain the buffer to CT every ``interval`` seconds."""
    while not stop.wait(timeout=interval):
        if emitter.delivery_status().get("pending"):
            emitter.drain(lambda ev: _http_post(emitter, ev))


def flush_now(emitter: "EventEmitter | None" = None) -> int:
    """Drain the buffer on the calling thread. Returns events delivered.

    Push-only, and a no-op when push is unconfigured — which is the normal case.
    The pull equivalent of this last-chance flush is Control Tower collecting
    after its ``final=true`` harvest; the events stay buffered until it does.
    """
    if emitter is None:
        emitter = event_emitter
    if not emitter.can_push or not emitter.pending():
        return 0
    return emitter.drain(lambda ev: _http_post(emitter, ev))


# Process-wide emitter. Buffers from the first event; delivery is by collection
# unless CT ingest is configured for push.
event_emitter = build_default_emitter()


__all__ = [
    "COLLECT_SCHEMA_VERSION",
    "DEFAULT_COLLECT_LIMIT",
    "DEFAULT_MAX_BUFFER",
    "EventEmitter",
    "SCHEMA_VERSION",
    "build_default_emitter",
    "event_emitter",
    "flush_loop",
    "flush_now",
]
