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

import copy
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from . import config
from .capture_journal import CaptureCapacityError, CaptureHeldError

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
_capture_emitter = None


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


def _read_local(home: str, *, strict: bool = False) -> list[NameObservation]:
    """Read the standalone/local mirror; retained capture is authoritative."""
    try:
        with open(names_path(home), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 — an unreadable file must not 500
        logger.warning("attendee names unreadable at %s: %s", names_path(home), exc)
        if strict:
            raise CaptureHeldError("legacy attendee names cannot be read") from exc
        return []
    if not isinstance(raw, list):
        if strict:
            raise CaptureHeldError("legacy attendee names are not a list")
        return []
    if strict and len(raw) > MAX_OBSERVATIONS:
        raise CaptureHeldError(
            "legacy attendee names exceed the supported observation limit"
        )
    seen: set[tuple[str, str]] = set()
    out: list[NameObservation] = []
    for item in raw:
        if not isinstance(item, dict):
            if strict:
                raise CaptureHeldError("legacy attendee name observation is invalid")
            continue
        if strict and (
            set(item) != {"name", "source", "captured_at"}
            or not all(isinstance(item.get(field), str) for field in item)
            or not item["name"]
            or clean_name(item["name"]) != item["name"]
            or item["source"] not in KNOWN_SOURCES
            or not item["captured_at"]
        ):
            raise CaptureHeldError("legacy attendee name observation is invalid")
        name = clean_name(item.get("name"))
        source = str(item.get("source") or "").strip()[:32]
        if not name or not source:
            continue
        identity = (name.lower(), source)
        if strict and identity in seen:
            raise CaptureHeldError("legacy attendee name observations are duplicated")
        seen.add(identity)
        out.append(
            NameObservation(
                name=name,
                source=source,
                captured_at=str(item.get("captured_at") or ""),
            )
        )
    return out


def _retained_names(state: dict, journal) -> dict:
    try:
        entries = state.get("attendee_names", {})
        if not isinstance(entries, dict):
            raise CaptureHeldError("retained attendee names are invalid")
        for email, entry in entries.items():
            if (
                not isinstance(email, str)
                or not email
                or not isinstance(entry, dict)
                or set(entry) != {"observations", "revision"}
                or type(entry["revision"]) is not int
                or entry["revision"] < 1
                or not isinstance(entry["observations"], list)
                or entry["revision"] != len(entry["observations"])
                or len(entry["observations"]) > MAX_OBSERVATIONS
            ):
                raise CaptureHeldError("retained attendee name revision is invalid")
            seen = set()
            for observation in entry["observations"]:
                if (
                    not isinstance(observation, dict)
                    or set(observation) != {"name", "source", "captured_at"}
                    or not isinstance(observation["name"], str)
                    or clean_name(observation["name"]) != observation["name"]
                    or not observation["name"]
                    or not isinstance(observation["source"], str)
                    or observation["source"] not in KNOWN_SOURCES
                    or not isinstance(observation["captured_at"], str)
                    or not observation["captured_at"]
                ):
                    raise CaptureHeldError(
                        "retained attendee name observation is invalid"
                    )
                key = (observation["name"].lower(), observation["source"])
                if key in seen:
                    raise CaptureHeldError(
                        "retained attendee name observations are duplicated"
                    )
                seen.add(key)
        return entries
    except CaptureHeldError as error:
        journal.mark_held(error)
        raise


def configure_capture(emitter) -> None:
    """Attach validated name/profile recovery before any attendee request."""
    global _capture_emitter
    entries = _retained_names(emitter.journal.read(), emitter.journal)
    owner = emitter.journal.binding["owner"]
    if config.insight_capture_enabled() and owner not in entries:
        from . import attendee
        from .users import email_slug

        home = os.path.join(config.users_root(), email_slug(owner))
        try:
            local = _read_local(home, strict=True)
        except CaptureHeldError as error:
            emitter.journal.mark_held(error)
            raise
        if local:
            observed_at = _now()
            binding_source = attendee.binding_source()
            observations = [value.to_json() for value in local]

            def import_current(state):
                current = _retained_names(state, emitter.journal)
                if owner in current:
                    return None
                state.setdefault("attendee_names", {})[owner] = {
                    "observations": observations,
                    "revision": len(observations),
                }
                # Existing local names establish current output, never proof of
                # delivery of every earlier envelope on an upgraded deployment.
                issues = state.setdefault("producer_issues", [])
                if "legacy_attendee_names_delivery_unverified" not in issues:
                    issues.append("legacy_attendee_names_delivery_unverified")
                return {
                    "attendee": owner,
                    "binding_source": binding_source,
                    "names": observations,
                    "observed_at": observed_at,
                }, f"identity:{emitter.run_id}:{owner}:{len(observations)}"

            emitter.emit_transaction("attendee.identity", owner, import_current)
    _capture_emitter = emitter


def reset_for_tests() -> None:
    global _capture_emitter
    _capture_emitter = None


def read(home: str) -> list[NameObservation]:
    """Every accepted name, including recovery after container replacement."""
    if _capture_emitter is not None and config.insight_capture_enabled():
        entries = _retained_names(
            _capture_emitter.journal.read(), _capture_emitter.journal
        )
        owner = _capture_emitter.journal.binding["owner"]
        entry = entries.get(owner)
        if entry is not None:
            return [NameObservation(**value) for value in entry["observations"]]
    return _read_local(home)


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
    """Commit one name observation and its exact event atomically.

    Consent-off names stay in the existing local certificate mirror. With
    consent on, local files are updated only after the retained transaction; a
    failed append leaves the previous accepted name/profile unchanged.
    """
    if source not in KNOWN_SOURCES:
        logger.warning("refusing unknown name source %r", source)
        return False
    cleaned = clean_name(name)
    if not cleaned:
        return False
    if not config.insight_capture_enabled():
        before = _read_local(home)
        after = observe(home, cleaned, source)
        return len(after) > len(before)
    if emitter is None:
        from .event_emitter import event_emitter

        emitter = _capture_emitter or event_emitter
    from . import attendee

    binding_source = attendee.binding_source()
    observed_at = _now()
    local_error = None
    try:
        local = _read_local(home, strict=_capture_emitter is not None)
    except CaptureHeldError as error:
        local, local_error = [], error

    def prepare(state):
        entries = _retained_names(state, emitter.journal)
        previous = entries.get(email)
        if previous is None and local_error is not None:
            emitter.journal.mark_held(local_error)
            raise local_error
        observations = (
            copy.deepcopy(previous["observations"])
            if previous
            else [value.to_json() for value in local]
        )
        if any(
            value["name"].lower() == cleaned.lower() and value["source"] == source
            for value in observations
        ):
            return None
        if len(observations) >= MAX_OBSERVATIONS:
            raise CaptureCapacityError(
                "attendee name observation capacity is exhausted"
            )
        observations.append(
            {"name": cleaned, "source": source, "captured_at": observed_at}
        )
        revision = len(observations)
        state.setdefault("attendee_names", {})[email] = {
            "observations": observations,
            "revision": revision,
        }
        if previous is None and local and _capture_emitter is not None:
            issues = state.setdefault("producer_issues", [])
            if "legacy_attendee_names_delivery_unverified" not in issues:
                issues.append("legacy_attendee_names_delivery_unverified")
        return {
            "attendee": email,
            "binding_source": binding_source,
            "names": observations,
            "observed_at": observed_at,
        }, f"identity:{emitter.run_id}:{email}:{revision}"

    sequence = emitter.emit_transaction("attendee.identity", email, prepare)
    if not sequence:
        return False
    entries = _retained_names(emitter.journal.read(), emitter.journal)
    _write(home, [NameObservation(**value) for value in entries[email]["observations"]])
    return True


__all__ = [
    "KNOWN_SOURCES",
    "MAX_NAME_CHARS",
    "MAX_OBSERVATIONS",
    "NameObservation",
    "SOURCE_CERTIFICATE",
    "SOURCE_WIZARD",
    "capture",
    "configure_capture",
    "reset_for_tests",
    "clean_name",
    "names_path",
    "observe",
    "payload",
    "read",
]
