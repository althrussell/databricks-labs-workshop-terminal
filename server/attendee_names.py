"""What the attendee calls themselves, and where each version came from.

Control Tower assigns a workspace to an email off the roster, and that binding
is the identity the rest of the system runs on. It is also, routinely, wrong:
people arrive on a workspace nobody assigned to them, share a laptop, sign in
with a personal address, or are simply late and take whatever is free. The
roster then says one thing and the room says another, and nothing in the system
can tell an operator which.

Two places already ask a human to type their own name — the opening wizard and
the certificate — and both used to throw the answer away. Kept, they are the
only first-hand evidence of who is actually sitting at a workspace.

Three rules, and every one of them exists because of how this data gets used:

1. **Append, never overwrite.** A second name is not a correction of the first;
   it is a second observation. "Priya Raman" from the wizard and "P. Raman" from
   the certificate corroborate each other. "Priya Raman" then "Tom Weller" is
   two people on one workspace, which is exactly the thing an operator needs to
   see and exactly the thing a last-write-wins field would hide.
2. **Every observation carries its source.** A name typed to get a certificate
   with their own name on it is stronger evidence than one typed into an
   optional wizard field, and an operator reconciling a roster should be able to
   weigh them differently.
3. **Nothing leaves the instance without capture consent.** A typed human name
   is new PII: the existing contract only ever promised pooled lab identities
   like ``labuser+17@``. Storage here is local and always allowed — the attendee
   is looking at their own certificate — but emission is gated in
   ``server/insight.py`` terms, on ``insight_capture_enabled``.

Storage is a small JSON file beside the wizard brief in the attendee's home,
for the same reason the brief lives there: this is per-attendee state on an
instance that holds exactly one attendee, and Control Tower's Lakebase is the
durable store.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from . import config

logger = logging.getLogger(__name__)

_NAMES_RELATIVE = os.path.join(".workshop", "names.json")

SOURCE_WIZARD = "wizard"
SOURCE_CERTIFICATE = "certificate"
KNOWN_SOURCES = frozenset({SOURCE_WIZARD, SOURCE_CERTIFICATE})

MAX_NAME_CHARS = 60
# Generous enough that a room's worth of retyping is all kept, small enough that
# a scripted certificate loop cannot turn this into an unbounded sink.
MAX_OBSERVATIONS = 20

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class NameObservation:
    """One name, once, from one place."""

    name: str
    source: str
    captured_at: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def clean_name(value: Any) -> str:
    """A display name, or empty.

    Whitespace is collapsed rather than merely stripped so that "Priya  Raman"
    and "Priya Raman" are one observation instead of two, which otherwise reads
    downstream as corroboration when it is a stray keystroke.
    """
    return " ".join(str(value or "").split())[:MAX_NAME_CHARS]


def names_path(home: str) -> str:
    return os.path.join(home, _NAMES_RELATIVE)


def read(home: str) -> list[NameObservation]:
    """Every name observed for this attendee, oldest first. Never raises."""
    try:
        with open(names_path(home), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 — an unreadable file must not 500
        logger.warning("attendee names unreadable at %s: %s", names_path(home), exc)
        return []
    if not isinstance(raw, list):
        return []
    out: list[NameObservation] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = clean_name(item.get("name"))
        source = str(item.get("source") or "").strip()[:32]
        if not name or not source:
            continue
        out.append(
            NameObservation(
                name=name,
                source=source,
                captured_at=str(item.get("captured_at") or ""),
            )
        )
    return out


def observe(home: str, name: str, source: str) -> list[NameObservation]:
    """Record one sighting of a name. Returns the full list after the append.

    The same name from the same source twice is one observation — someone
    regenerating their certificate is not new evidence of anything. The same
    name from a *different* source is kept, because that is corroboration and
    the whole point of tracking where each one came from.
    """
    cleaned = clean_name(name)
    source = str(source or "").strip()[:32]
    if not cleaned or not source:
        return read(home)

    with _lock:
        existing = read(home)
        for seen in existing:
            if seen.name.lower() == cleaned.lower() and seen.source == source:
                return existing
        if len(existing) >= MAX_OBSERVATIONS:
            logger.warning(
                "attendee name observations capped at %d; dropping %r from %s",
                MAX_OBSERVATIONS,
                cleaned,
                source,
            )
            return existing
        updated = existing + [
            NameObservation(name=cleaned, source=source, captured_at=_now())
        ]
        _write(home, updated)
    return updated


def _write(home: str, observations: list[NameObservation]) -> None:
    path = names_path(home)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([o.to_json() for o in observations], fh)
        os.replace(tmp, path)
    except OSError as exc:
        # Same posture as the discovery journal: durability is best-effort and
        # never worth failing the attendee's request over. The observation is
        # still emitted, so Control Tower gets it even when the volume does not.
        logger.warning("attendee names write failed at %s: %s", path, exc)


def payload(email: str, observations: list[NameObservation]) -> dict[str, Any]:
    """The ``attendee.identity`` event payload (contract C6).

    Carries the email binding source alongside the names, because the two
    questions an operator has are one question: *is the person on this
    workspace the person the roster says?* The names are the evidence and the
    binding source is what they are evidence about.
    """
    from . import attendee

    return {
        "attendee": email,
        "binding_source": attendee.binding_source(),
        "names": [o.to_json() for o in observations],
        "observed_at": _now(),
    }


def capture(email: str, home: str, name: str, source: str, emitter=None) -> bool:
    """Observe a name and push the attendee's identity picture to Control Tower.

    Storage and emission are one call for the same reason they are in
    ``discovery.record``: a name held locally but never pushed is lost at
    teardown, and one pushed but not held cannot be shown back.

    Returns whether anything new was recorded. Emission is skipped when nothing
    changed, so a re-download of a certificate does not burn an event.
    """
    if source not in KNOWN_SOURCES:
        logger.warning("refusing unknown name source %r", source)
        return False

    before = read(home)
    after = observe(home, name, source)
    if len(after) == len(before):
        return False

    if not config.insight_capture_enabled():
        # A typed human name is PII the existing contract does not promise. The
        # local record still helps the attendee's own certificate; nothing
        # leaves an instance whose event did not opt in.
        return True

    if emitter is None:
        from .event_emitter import event_emitter as emitter

    emitter.emit(
        "attendee.identity",
        email,
        payload(email, after),
        # Keyed on the observation count, so each new name is a new logical
        # event while a retried flush of the same picture is still a duplicate.
        idempotency_key=f"identity:{emitter.run_id}:{email}:{len(after)}",
    )
    return True


__all__ = [
    "KNOWN_SOURCES",
    "MAX_NAME_CHARS",
    "MAX_OBSERVATIONS",
    "NameObservation",
    "SOURCE_CERTIFICATE",
    "SOURCE_WIZARD",
    "capture",
    "clean_name",
    "names_path",
    "observe",
    "payload",
    "read",
]
