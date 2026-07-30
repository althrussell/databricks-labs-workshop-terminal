"""Attendee event emitter (contract C3b).

Buffers attendee events for Control Tower. There are two delivery paths, and the
distinction matters because only one of them works on the Databricks Apps
platform today:

- **Pull (the live path)**: Control Tower collects buffered events from
  ``GET /api/admin/insight-events`` on the same authenticated harvest it already
  makes to ``/api/admin/stats``. Needs no configuration on this instance at all —
  not even ``WORKSHOP_RUN_ID``, because the collector knows which unit it is
  polling and attributes on receipt.
- **Push (kept, currently unreachable)**: ``POST`` to CT's ingest endpoint with a
  shared ``X-Ingest-Token``. Every Databricks App sits behind a proxy that
  requires a Databricks identity, so a request carrying only that header never
  reaches CT's application code. Making this work needs an OAuth bearer minted
  for CT's workspace and ``CAN USE`` granted to this app's service principal —
  see ``docs/workshop-insight-contract.md``. Until then ``can_push`` is False in
  production and the buffer is drained by the collector instead.

Design constraints, unchanged by either path:

- **Never blocks the attendee path**: ``emit`` only appends to an in-memory
  bounded buffer.
- **Fail-soft**: delivery failures keep events buffered; the buffer is bounded
  (drop-oldest) so a long CT outage can't exhaust memory. ``dropped`` counts what
  eviction cost so the loss is reportable rather than invisible.
- **Idempotent**: every event carries an ``idempotency_key`` so a retried flush
  or a re-collected batch can't double-count on CT's side.

The buffer/collect/idempotency logic is pure and unit-tested; the HTTP POST is
injected so tests don't touch the network.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

# Transport schema for the collect response (distinct from SCHEMA_VERSION, which
# versions the event envelope CT ingests).
COLLECT_SCHEMA_VERSION = 1

# Sized for a full day with nothing acknowledging: operational.health every 60s
# is ~480 events over an 8-hour event, plus one workshop.signal per attendee per
# harvest (~250) and a handful of discovery records and summaries. ~5x headroom
# on that, because the events at the *end* of the day — the wrap-phase summaries
# — are the ones worth the most, and drop-oldest is only a safe policy while the
# ceiling is comfortably out of reach.
DEFAULT_MAX_BUFFER = 5000

# Cap on one collect response, so a CT that has fallen a long way behind gets a
# bounded page rather than a multi-megabyte body. The remainder stays buffered
# and comes back on the next harvest.
DEFAULT_COLLECT_LIMIT = 1000


class EventEmitter:
    def __init__(
        self,
        *,
        run_id: str,
        workspace_id: str,
        ingest_url: str,
        ingest_token: str,
        max_buffer: int = DEFAULT_MAX_BUFFER,
    ) -> None:
        self.run_id = run_id
        self.workspace_id = workspace_id
        self.ingest_url = (ingest_url or "").rstrip("/")
        self.ingest_token = ingest_token or ""
        # Identifies this process's event stream. A restart empties the buffer and
        # restarts the sequence, so a collector holding a cursor has to be able to
        # tell "nothing new since 400" from "a new process whose 400 is different
        # work". Without this, a restart mid-event would silently skip everything
        # up to the old cursor.
        self.stream_id = uuid.uuid4().hex
        self._buf: deque[tuple[int, dict[str, Any]]] = deque(maxlen=max_buffer)
        self._seq = 0
        self._dropped = 0
        self._collections = 0
        self._last_collected_at: float | None = None
        self._lock = threading.Lock()

    @property
    def can_push(self) -> bool:
        # run_id is required for push because the event body carries it and CT's
        # ingest validates it. The pull path has no such requirement — the
        # collector fills it in.
        return bool(self.ingest_url and self.ingest_token and self.run_id)

    def emit(
        self,
        event_type: str,
        attendee: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        """Buffer one event. Always records: the collector is the delivery path,
        and it needs no local configuration to exist.

        Whether an event is *worth* recording is the caller's decision, made
        upstream — insight events are gated on ``WORKSHOP_INSIGHT_CAPTURE``, so
        attendee-authored content only reaches this buffer when capture is on.
        """
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
            if self._buf.maxlen is not None and len(self._buf) == self._buf.maxlen:
                self._dropped += 1
            self._seq += 1
            self._buf.append((self._seq, event))

    def pending(self) -> int:
        with self._lock:
            return len(self._buf)

    @property
    def dropped(self) -> int:
        """Events evicted by the buffer ceiling, ever. Non-zero means real loss."""
        with self._lock:
            return self._dropped

    def collect(
        self,
        *,
        after: int = 0,
        stream: str = "",
        limit: int = DEFAULT_COLLECT_LIMIT,
    ) -> dict[str, Any]:
        """Hand buffered events to a pulling collector, oldest first.

        ``after`` is both a cursor and an acknowledgement: a collector only
        advances it once it has committed the previous batch, so anything at or
        below it is safe to discard. That keeps the buffer flat over a long event
        instead of drifting up towards the ceiling where drop-oldest starts
        costing us the newest events.

        A cursor is only honoured for the stream that issued it. Sequence numbers
        restart with the process, so a collector replaying ``after=400`` across a
        restart would otherwise discard the whole of a fresh buffer without ever
        reading it — turning a restart into silent data loss instead of a
        re-collection. ``cursor_reset`` tells the collector that happened.

        Events are returned wrapped — ``{"seq": n, "event": {...}}`` — because the
        envelope inside is exactly the C3a shape CT ingests, and ``seq`` is
        transport state that has no business inside it.
        """
        reset = bool(after > 0 and stream != self.stream_id)
        if reset:
            after = 0
        with self._lock:
            if after > 0:
                while self._buf and self._buf[0][0] <= after:
                    self._buf.popleft()
            entries = [
                {"seq": seq, "event": event}
                for seq, event in self._buf
                if seq > after
            ][: max(1, limit)]
            self._collections += 1
            self._last_collected_at = time.time()
            return {
                "schema_version": COLLECT_SCHEMA_VERSION,
                "stream_id": self.stream_id,
                "events": entries,
                "high_water": entries[-1]["seq"] if entries else after,
                "pending": len(self._buf),
                "dropped": self._dropped,
                "cursor_reset": reset,
                "delivery": "push" if self.can_push else "pull",
            }

    def delivery_status(self) -> dict[str, Any]:
        """Secret-free view of how (and whether) events are reaching CT.

        ``collections`` is the one fact that can't be inferred from configuration:
        under pull there is always a delivery path, so the only evidence that it
        is being used is that somebody used it.
        """
        with self._lock:
            return {
                "delivery": "push" if self.can_push else "pull",
                "stream_id": self.stream_id,
                "pending": len(self._buf),
                "dropped": self._dropped,
                "collections": self._collections,
                "last_collected_at": self._last_collected_at,
            }

    def drain(self, post: Callable[[dict[str, Any]], bool]) -> int:
        """Deliver buffered events via ``post`` (returns True on success).

        Preserves order: stops at the first failure and leaves the rest buffered
        for the next flush. Returns the number delivered. Never raises.
        """
        with self._lock:
            batch = [event for _, event in self._buf]
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
    """Construct the process emitter from config."""
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
