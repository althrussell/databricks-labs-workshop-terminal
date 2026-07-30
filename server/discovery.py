"""Agent-elicited discovery records (contract C6, tier 2).

This is the highest-value and highest-risk data the app handles. An attendee
describing what their company is trying to build is exactly what a post-event
Account Manager Brief needs, and exactly what must not leak, be stored longer
than the event, or be captured without the event's terms covering it.

Design consequences of that:

- **Nothing is stored unless capture is on.** ``record`` is a no-op when
  ``WORKSHOP_INSIGHT_CAPTURE``/``DISCOVERY_ENABLED`` are off, so a deployment
  that never opted in holds no discovery data even if an agent tries to submit.
- **Redaction happens on the way in, not on the way out.** An attendee can paste
  a connection string while describing their stack. Redacting at emit time would
  leave the secret sitting in memory and in the journal in the meantime.
- **Partial records are normal.** Only ``record_id`` is required. Demanding
  completeness would push the agent into interrogating people, which produces
  worse data and a worse workshop.
- **Fail-soft, like the rest of the attendee path.** A journal write that fails
  degrades restart durability; it never fails the attendee's request.
- **Bounded.** Per-attendee and global caps keep a looping agent from turning
  discovery into an unbounded memory sink.

Durability is a journal on the app volume rather than a database: the app owns no
external state (teardown is ``apps.delete``), so the journal exists only to
survive a process restart mid-workshop. Control Tower's Lakebase is the durable
store, reached through the event emitter.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import config

logger = logging.getLogger("workshop.discovery")

# Free-text fields carry what the attendee said; list fields carry short names.
_TEXT_FIELDS = ("use_case_title", "use_case_summary", "goal", "timeline", "industry")
_LIST_FIELDS = (
    "current_stack", "databricks_products", "blockers", "interest_signals",
)
_CONFIDENCE = ("low", "medium", "high")

# Caps. A record is a few hundred bytes of prose; these are generous for a
# human-paced conversation and tight enough that a looping agent can't exhaust
# memory or produce a payload CT has to truncate.
MAX_RECORDS_PER_ATTENDEE = 50
MAX_TEXT_CHARS = 2000
MAX_LIST_ITEMS = 20
MAX_ITEM_CHARS = 200

_REDACTED = "[redacted]"

# Credential shapes. Ordered longest-match-first where patterns could overlap so
# a JWT isn't half-matched by the base64-run rule and reported as two hits.
_SECRET_PATTERNS = (
    # Databricks PATs. The distinctive prefix makes this the highest-confidence
    # rule, and the one most likely to actually fire in a Databricks workshop.
    re.compile(r"\bdapi[0-9a-f]{16,}(?:-\d+)?\b", re.IGNORECASE),
    # PEM private keys, including the body — a match must not leave the key
    # material behind on the next line.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # JWTs (three base64url segments).
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # Bearer/token headers.
    re.compile(r"\b(?:bearer|token)\s+[A-Za-z0-9._\-+/=]{16,}", re.IGNORECASE),
    # Secret-shaped key=value / key: value pairs. Quoted or bare.
    re.compile(
        r"\b[\w.\-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key"
        r"|client[_-]?secret|private[_-]?key|credential)[\w.\-]*"
        r"\s*[=:]\s*[\"']?[^\s\"',;]{6,}[\"']?",
        re.IGNORECASE,
    ),
    # Connection-string credentials: scheme://user:pass@host.
    re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE),
    # Long high-entropy runs. Last, and deliberately conservative — over-redaction
    # is its own failure, because shredded prose is worthless in a brief.
    re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE),
    # The base64 rule additionally requires both an uppercase letter and a digit
    # inside the run. Encoded random bytes have both; a long lowercase identifier
    # an attendee typed does not, and a length test alone would redact it.
    re.compile(
        r"\b(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[0-9])[A-Za-z0-9+/]{40,}={0,2}"
    ),
)


def redact(text: str) -> tuple[str, int]:
    """Strip credential-shaped values from ``text``. Returns (clean, count).

    A backstop, not the primary control — the primary control is that these
    fields hold what the attendee said about their business, not machine output.
    But a pasted connection string must not become a row in Lakebase.
    """
    if not text:
        return text, 0
    count = 0
    for pattern in _SECRET_PATTERNS:
        text, hits = pattern.subn(_REDACTED, text)
        count += hits
    return text, count


@dataclass
class DiscoveryRecord:
    """One agent-elicited discovery record for one attendee.

    Every field beyond ``record_id`` is optional by design; see the module
    docstring.
    """

    record_id: str
    attendee: str
    captured_at: str
    agent: str = ""
    confidence: str = ""
    use_case_title: str = ""
    use_case_summary: str = ""
    goal: str = ""
    timeline: str = ""
    industry: str = ""
    current_stack: list[str] = field(default_factory=list)
    databricks_products: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    interest_signals: list[str] = field(default_factory=list)
    redactions: int = 0
    # Attendee-initiated withdrawal. Kept as a tombstone rather than deleted so
    # a re-submission of the same record_id can't silently resurrect it.
    redacted_by_attendee: bool = False
    # Bumped on every change to this record_id, and carried in the idempotency
    # key. Without it a refinement and a retried flush are indistinguishable to
    # Control Tower, which de-dupes on that key — so the improved version of a
    # use case would be silently discarded as a duplicate.
    revision: int = 1

    def payload(self) -> dict[str, Any]:
        """The ``discovery.record`` event payload (contract C6).

        Empty optional fields are omitted rather than sent as ""/[]: on CT's side
        "the attendee didn't say" and "the attendee said nothing" are different
        facts, and only one of them should be renderable in a brief.
        """
        out: dict[str, Any] = {
            "record_id": self.record_id,
            "captured_at": self.captured_at,
            "revision": self.revision,
        }
        for name in ("agent", "confidence", *_TEXT_FIELDS):
            value = getattr(self, name)
            if value:
                out[name] = value
        for name in _LIST_FIELDS:
            value = getattr(self, name)
            if value:
                out[name] = list(value)
        if self.redactions:
            out["redactions"] = self.redactions
        if self.redacted_by_attendee:
            # A withdrawal is an event, not an absence. CT has to be told, or the
            # row the attendee revoked stays in the brief the account team reads.
            out["redacted_by_attendee"] = True
        return out

    def to_json(self) -> dict[str, Any]:
        """Journal form — includes the attendee and tombstone, unlike the payload."""
        return {
            "record_id": self.record_id,
            "attendee": self.attendee,
            "captured_at": self.captured_at,
            "agent": self.agent,
            "confidence": self.confidence,
            **{name: getattr(self, name) for name in _TEXT_FIELDS},
            **{name: list(getattr(self, name)) for name in _LIST_FIELDS},
            "redactions": self.redactions,
            "redacted_by_attendee": self.redacted_by_attendee,
            "revision": self.revision,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "DiscoveryRecord | None":
        record_id = str(raw.get("record_id") or "").strip()
        attendee = str(raw.get("attendee") or "").strip()
        if not record_id or not attendee:
            return None
        return cls(
            record_id=record_id,
            attendee=attendee,
            captured_at=str(raw.get("captured_at") or _now()),
            agent=str(raw.get("agent") or ""),
            confidence=str(raw.get("confidence") or ""),
            **{name: str(raw.get(name) or "") for name in _TEXT_FIELDS},
            **{
                name: [str(v) for v in (raw.get(name) or []) if str(v).strip()]
                for name in _LIST_FIELDS
            },
            redactions=int(raw.get("redactions") or 0),
            redacted_by_attendee=bool(raw.get("redacted_by_attendee")),
            revision=max(1, int(raw.get("revision") or 1)),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> tuple[str, int]:
    if value is None:
        return "", 0
    text = str(value).strip()[:MAX_TEXT_CHARS]
    return redact(text)


def _clean_list(value: Any) -> tuple[list[str], int]:
    if value is None:
        return [], 0
    if isinstance(value, str):
        # Agents reach for a comma-separated string as often as a list; rejecting
        # it would lose the record over a formatting detail.
        items = [part for part in (p.strip() for p in value.split(",")) if part]
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        return [], 0
    out: list[str] = []
    redactions = 0
    for item in items[:MAX_LIST_ITEMS]:
        clean, hits = redact(item[:MAX_ITEM_CHARS])
        redactions += hits
        if clean not in out:
            out.append(clean)
    return out, redactions


def build_record(attendee: str, raw: dict[str, Any]) -> DiscoveryRecord:
    """Validate, truncate and redact one submission into a record.

    Unknown keys are ignored rather than rejected: the agent writes this payload
    from an instruction file, and a hallucinated extra field should cost the
    field, not the whole record.
    """
    record_id = str(raw.get("record_id") or "").strip() or uuid.uuid4().hex
    confidence = str(raw.get("confidence") or "").strip().lower()
    if confidence not in _CONFIDENCE:
        # An unrecognised confidence must not read as high. Empty means unstated,
        # which downstream synthesis treats as the weakest claim.
        confidence = ""
    redactions = 0
    text: dict[str, str] = {}
    for name in _TEXT_FIELDS:
        text[name], hits = _clean_text(raw.get(name))
        redactions += hits
    lists: dict[str, list[str]] = {}
    for name in _LIST_FIELDS:
        lists[name], hits = _clean_list(raw.get(name))
        redactions += hits
    return DiscoveryRecord(
        record_id=record_id,
        attendee=attendee,
        captured_at=str(raw.get("captured_at") or "").strip() or _now(),
        agent=str(raw.get("agent") or "").strip()[:64],
        confidence=confidence,
        redactions=redactions,
        **text,
        **lists,
    )


class DiscoveryStore:
    """In-memory records for this instance, journalled for restart durability."""

    def __init__(self, path: str = ""):
        self.path = path
        self._lock = threading.Lock()
        # attendee -> record_id -> record. Insertion order is capture order.
        self._records: dict[str, dict[str, DiscoveryRecord]] = {}

    def configure_journal(self, path: str) -> int:
        """Attach a journal and restore anything a prior process left behind.

        Called from the app lifespan rather than resolved at import so the path
        follows Control Tower's in-place env edits, matching how the session
        journal is wired.
        """
        self.path = path
        return self.load()

    # -- writes --

    def put(self, record: DiscoveryRecord) -> DiscoveryRecord | None:
        """Store (or update) a record. Returns it, or None if it was dropped.

        Re-submitting a ``record_id`` is an update: the agent's understanding
        improves through a conversation, and forcing a new id per revision would
        make CT store three contradictory versions of one use case.
        """
        with self._lock:
            per_attendee = self._records.setdefault(record.attendee, {})
            existing = per_attendee.get(record.record_id)
            if existing is not None and existing.redacted_by_attendee:
                # The attendee withdrew this record. An agent that re-submits the
                # same id must not be able to undo that.
                return None
            if (
                existing is None
                and len(per_attendee) >= MAX_RECORDS_PER_ATTENDEE
            ):
                logger.warning(
                    "discovery cap reached for %s (%d records); dropping",
                    record.attendee,
                    len(per_attendee),
                )
                return None
            if existing is not None:
                record.revision = existing.revision + 1
            per_attendee[record.record_id] = record
        self._persist()
        return record

    def redact_record(self, attendee: str, record_id: str) -> DiscoveryRecord | None:
        """Attendee-initiated withdrawal. Blanks the content, keeps a tombstone.

        Returns the tombstone so the caller can push it to Control Tower. Local
        blanking alone would leave the attendee looking at an empty pane while the
        brief the account team reads still quotes them.
        """
        with self._lock:
            record = (self._records.get(attendee) or {}).get(record_id)
            if record is None:
                return None
            for name in _TEXT_FIELDS:
                setattr(record, name, "")
            for name in _LIST_FIELDS:
                setattr(record, name, [])
            record.confidence = ""
            record.redacted_by_attendee = True
            record.revision += 1
        self._persist()
        return record

    # -- reads --

    def for_attendee(self, attendee: str) -> list[DiscoveryRecord]:
        with self._lock:
            return [
                r for r in (self._records.get(attendee) or {}).values()
                if not r.redacted_by_attendee
            ]

    def counts(self) -> dict[str, int]:
        """Per-attendee record counts, for the stats harvest."""
        with self._lock:
            return {
                attendee: len([r for r in records.values() if not r.redacted_by_attendee])
                for attendee, records in self._records.items()
            }

    def count_for(self, attendee: str) -> int:
        return len(self.for_attendee(attendee))

    def all_records(self) -> list[DiscoveryRecord]:
        with self._lock:
            return [
                r
                for records in self._records.values()
                for r in records.values()
                if not r.redacted_by_attendee
            ]

    def clear(self) -> None:
        with self._lock:
            self._records = {}
        self._persist()

    # -- journal (fail-soft) --

    def load(self) -> int:
        """Restore records from the journal. Returns the number restored."""
        if not self.path:
            return 0
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return 0
        except Exception as exc:  # noqa: BLE001 — a corrupt journal is non-fatal
            logger.warning("discovery journal unreadable (%s): %s", self.path, exc)
            return 0
        entries = raw.get("records") if isinstance(raw, dict) else raw
        restored = 0
        with self._lock:
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                record = DiscoveryRecord.from_json(entry)
                if record is None:
                    continue
                self._records.setdefault(record.attendee, {})[
                    record.record_id
                ] = record
                restored += 1
        return restored

    def _persist(self) -> None:
        if not self.path:
            return
        with self._lock:
            payload = {
                "records": [
                    r.to_json()
                    for records in self._records.values()
                    for r in records.values()
                ]
            }
        try:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".discovery-journal-")
            # 0600 before any content is written: the journal holds attendee
            # narrative about their employer's plans, and the container's shell
            # is reachable by the attendee.
            os.fchmod(fd, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.replace(tmp, self.path)  # atomic
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except Exception as exc:  # noqa: BLE001 — durability is best-effort
            logger.warning("discovery journal write failed (%s): %s", self.path, exc)


def journal_path() -> str:
    """Journal beside the session journal on the app volume, or "" to disable."""
    session_path = config.session_state_path()
    if not session_path:
        return ""
    return os.path.join(os.path.dirname(session_path) or ".", "discovery.json")


def _emit(stored: DiscoveryRecord, emitter=None) -> None:
    """Queue one record state for Control Tower.

    The key carries the revision so a genuine update is a new logical event while
    a retried flush of the same state is still a duplicate. Without the revision,
    CT's de-dupe would silently discard every refinement after the first.
    """
    if emitter is None:
        from .event_emitter import event_emitter as emitter
    emitter.emit(
        "discovery.record",
        stored.attendee,
        stored.payload(),
        idempotency_key=(
            f"discovery:{emitter.run_id}:{stored.attendee}"
            f":{stored.record_id}:{stored.revision}"
        ),
    )


def record(attendee: str, raw: dict[str, Any], emitter=None) -> DiscoveryRecord | None:
    """Capture one submission and queue it for Control Tower.

    Returns None when discovery is disabled or the record was dropped. Storage
    and emission are deliberately one call: a record held locally but never
    pushed would be lost at teardown, and one pushed but not held couldn't be
    shown back to the attendee or counted in the harvest.
    """
    if not config.discovery_enabled():
        return None
    built = build_record(attendee, raw)
    stored = discovery_store.put(built)
    if stored is None:
        return None
    _emit(stored, emitter)
    return stored


def withdraw(attendee: str, record_id: str, emitter=None) -> bool:
    """Withdraw one of the attendee's own records, locally and downstream.

    The tombstone is pushed rather than just stored, because by the time an
    attendee asks to remove something the original has almost certainly already
    reached Control Tower. Deletion in Lakebase is CT's to perform — the terminal
    can only tell it the attendee revoked consent for that record.
    """
    tombstone = discovery_store.redact_record(attendee, record_id)
    if tombstone is None:
        return False
    _emit(tombstone, emitter)
    return True


# Journal-less until the app lifespan configures it (see configure_journal).
discovery_store = DiscoveryStore()


__all__ = [
    "DiscoveryRecord",
    "DiscoveryStore",
    "MAX_RECORDS_PER_ATTENDEE",
    "build_record",
    "discovery_store",
    "journal_path",
    "record",
    "redact",
    "withdraw",
]
