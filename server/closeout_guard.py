"""Durable capture admission and transaction barrier in the existing journal.

Tickets are reserved before dispatch, including background thread creation. A
context may finish accepted work while closing; new work cannot cross the
barrier. The transaction check also applies to independent app containers.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .capture_journal import CaptureError, CaptureHeldError, canonical_json


class CaptureClosedError(CaptureError):
    """Expected refusal of new input after closeout; not a capture loss."""


PROCESS_ID = uuid.uuid4().hex
_authority: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "workshop_capture_authority", default=None
)
_finalizer_clock = ContextVar("workshop_capture_finalizer_clock", default=time.time)


def _binding(journal) -> str:
    return canonical_json(journal.binding).decode()


def closeout_state(state: dict) -> dict | None:
    value = state.get("closeout")
    if value is None:
        return None
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(value.get("phase"), str)
            or value["phase"] not in {"OPEN", "CLOSING", "SEALED", "INCOMPLETE"}
            or not isinstance(value.get("writers"), dict)
            or not isinstance(value.get("writer_outcomes"), list)):
        raise CaptureHeldError("retained closeout state is invalid")
    return value


def initial_state() -> dict:
    return {"schema_version": 1, "phase": "OPEN", "writers": {}, "writer_outcomes": []}


@contextmanager
def control_scope(journal):
    token = _authority.set((_binding(journal), "control", ""))
    try:
        yield
    finally:
        _authority.reset(token)


@contextmanager
def finalizer_scope(journal, capture_id: str, claim_id: str = "", *, clock=None):
    token = _authority.set((_binding(journal), "finalizer", capture_id + ":" + claim_id))
    clock_token = _finalizer_clock.set(clock or time.time)
    try:
        yield
    finally:
        _finalizer_clock.reset(clock_token)
        _authority.reset(token)


def control_transaction(journal, mutator):
    with control_scope(journal):
        return journal.transaction(mutator)


def check_open(journal) -> None:
    value = closeout_state(journal.read())
    if value is not None and value["phase"] != "OPEN":
        raise CaptureClosedError("Workshop is closing; new capture input is disabled")


class WriterTicket:
    def __init__(self, journal, kind: str, *, detached: bool = False):
        self.journal, self.binding = journal, _binding(journal)
        current = _authority.get()
        self.inherited = bool(not detached and current and current[0] == self.binding and current[1] in {"writer", "finalizer"})
        self.authority = current if self.inherited else (self.binding, "writer", uuid.uuid4().hex)
        self._context_token = None
        self._finished = False
        self._failed = False
        self._finish_lock = threading.Lock()
        if not isinstance(kind, str) or not kind or len(kind) > 100:
            raise ValueError("capture writer kind is required")
        if not self.inherited:
            started = time.time()

            def reserve(state):
                value = closeout_state(state)
                if value is None:
                    value = state.setdefault("closeout", initial_state())
                if value["phase"] != "OPEN":
                    raise CaptureClosedError("Workshop is closing; new capture input is disabled")
                if len(value["writers"]) >= 256:
                    raise CaptureHeldError("capture writer admission capacity reached")
                value["writers"][self.authority[2]] = {
                    "kind": kind, "process_id": PROCESS_ID, "started_at": started,
                }

            control_transaction(journal, reserve)

    def __enter__(self):
        if self._finished or self._context_token is not None:
            raise RuntimeError("capture writer ticket cannot be reused")
        self._context_token = _authority.set(self.authority)
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.finish(failed=exc_type is not None)
        finally:
            if self._context_token is not None:
                _authority.reset(self._context_token)
                self._context_token = None

    @property
    def finished(self) -> bool:
        with self._finish_lock:
            return self._finished

    @contextmanager
    def scope(self):
        """Borrow already-reserved producer authority without ending its ticket.

        Process readers and their shutdown caller may run on different threads.
        Each scope still needs the durable writer receipt at commit time, and
        cannot admit a new independent writer after the closeout barrier.
        """
        with self._finish_lock:
            if self._finished:
                raise CaptureClosedError("capture writer has already finished")
        token = _authority.set(self.authority)
        try:
            yield
        finally:
            _authority.reset(token)

    def finish(self, *, failed: bool = False) -> None:
        with self._finish_lock:
            self._finish(failed=failed)

    def _finish(self, *, failed: bool = False) -> None:
        if self._finished:
            return
        # Completion can fail after a request failed. A retry (including the
        # context cleanup path) must never turn that failure into success.
        self._failed = self._failed or failed
        if not self.inherited:
            def complete(state):
                value = closeout_state(state)
                if value is None:
                    raise CaptureHeldError("capture writer admission disappeared")
                writer = value["writers"].pop(self.authority[2], None)
                if writer is None:
                    raise CaptureHeldError("capture writer admission disappeared")
                if self._failed:
                    # Only failures remain; successful request traffic need not
                    # grow an unbounded per-request audit collection.
                    value["writer_outcomes"].append({**writer, "writer_id": self.authority[2], "status": "failed"})
            control_transaction(self.journal, complete)
        self._finished = True


def admitted_writer(journal, kind: str, *, detached: bool = False) -> WriterTicket:
    """Reserve now; enter on the executing thread, then release after all work."""
    return WriterTicket(journal, kind, detached=detached)


def _without_control(state: dict) -> dict:
    value = copy.deepcopy(state)
    value.pop("closeout", None)
    return value


def _only_ack_change(before: dict, after: dict) -> bool:
    expected = copy.deepcopy(before)
    for key in ("insight", "help"):
        old, new = expected.get(key), after.get(key)
        if old is None and new is None:
            continue
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        ack = new.get("acked_through")
        upper = old.get("next_seq") if key == "insight" else old.get("seq")
        if type(ack) is not int or type(upper) is not int or not old.get("acked_through", 0) <= ack <= upper:
            return False
        old["acked_through"] = ack
        if key == "help":
            old["outbox"] = [entry for entry in old.get("outbox", []) if entry["seq"] > ack]
    return canonical_json(expected) == canonical_json(after)


def authorize_mutation(journal, before: dict[str, Any], after: dict[str, Any]) -> None:
    """Called for each candidate transaction, after replay and before commit."""
    old = closeout_state(before)
    new = closeout_state(after)
    current = _authority.get()
    role = current if current and current[0] == _binding(journal) else None
    if role and role[1] == "control":
        if canonical_json(_without_control(before)) != canonical_json(_without_control(after)):
            raise CaptureClosedError("closeout control cannot change captured content")
        if old and old["phase"] == "SEALED":
            if canonical_json(old) != canonical_json(new):
                raise CaptureClosedError("sealed capture is immutable")
        return
    if old != new:
        raise CaptureClosedError("closeout state requires control authority")
    if old is None or old["phase"] == "OPEN":
        return
    if _only_ack_change(before, after):
        return
    if old["phase"] in {"CLOSING", "INCOMPLETE"} and role:
        if role[1] == "writer" and role[2] in old["writers"]:
            return
        claim = old.get("finalizer") or {}
        expected = str(old.get("capture_id")) + ":" + str(claim.get("claim_id", ""))
        if (role[1] == "finalizer" and role[2] == expected and claim.get("claim_id")
                and claim.get("expires_at", 0) > _finalizer_clock.get()()):
            return
    raise CaptureClosedError("Workshop capture is closed to new output")
