"""Attendee event emitter (contract C3b).

Pushes real-time attendee events to Control Tower's ingest endpoint
(``POST /api/ingest/events``, contract C3a). Design constraints:

- **Never blocks the attendee path**: ``emit`` only appends to an in-memory
  bounded buffer; a background flusher does the HTTP work.
- **Fail-soft**: delivery failures keep events buffered for the next flush; the
  buffer is bounded (drop-oldest) so a long CT outage can't exhaust memory.
- **Idempotent**: every event carries an ``idempotency_key`` so a retried flush
  can't double-count on CT's side.
- **Disabled unless configured**: with no ingest URL/token (CONTROL_TOWER_
  INGEST_URL / _TOKEN), ``emit`` is a no-op — the poll-based stats harvest
  remains the reconciliation path.

The buffer/drain/idempotency logic is pure and unit-tested; the HTTP POST is
injected so tests don't touch the network.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1


class EventEmitter:
    def __init__(
        self,
        *,
        run_id: str,
        workspace_id: str,
        ingest_url: str,
        ingest_token: str,
        max_buffer: int = 1000,
    ) -> None:
        self.run_id = run_id
        self.workspace_id = workspace_id
        self.ingest_url = (ingest_url or "").rstrip("/")
        self.ingest_token = ingest_token or ""
        self._buf: deque[dict[str, Any]] = deque(maxlen=max_buffer)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        # run_id is required so CT can attribute the event; without it (or the
        # endpoint/token) we stay disabled and let the poll harvest reconcile.
        return bool(self.ingest_url and self.ingest_token and self.run_id)

    def emit(
        self,
        event_type: str,
        attendee: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        event = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "attendee": attendee,
            "type": event_type,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
            "idempotency_key": idempotency_key or uuid.uuid4().hex,
        }
        with self._lock:
            self._buf.append(event)

    def pending(self) -> int:
        with self._lock:
            return len(self._buf)

    def drain(self, post: Callable[[dict[str, Any]], bool]) -> int:
        """Deliver buffered events via ``post`` (returns True on success).

        Preserves order: stops at the first failure and leaves the rest buffered
        for the next flush. Returns the number delivered. Never raises.
        """
        with self._lock:
            batch = list(self._buf)
        delivered = 0
        for event in batch:
            try:
                ok = post(event)
            except Exception:  # noqa: BLE001 — fail-soft; retry next flush
                ok = False
            if not ok:
                break
            delivered += 1
        if delivered:
            with self._lock:
                for _ in range(min(delivered, len(self._buf))):
                    self._buf.popleft()
        return delivered


def build_default_emitter() -> "EventEmitter":
    """Construct the process emitter from config (disabled unless configured)."""
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


def flush_loop(emitter: "EventEmitter", stop: threading.Event, *, interval: float = 15.0) -> None:
    """Background flusher: drain the buffer to CT every ``interval`` seconds."""
    while not stop.wait(timeout=interval):
        if emitter.pending():
            emitter.drain(lambda ev: _http_post(emitter, ev))


# Process-wide emitter; emit() is a no-op until CT ingest is configured.
event_emitter = build_default_emitter()


__all__ = ["EventEmitter", "SCHEMA_VERSION", "event_emitter", "flush_loop", "build_default_emitter"]
