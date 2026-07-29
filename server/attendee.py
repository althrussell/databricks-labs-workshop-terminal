"""Attendee identity binding for a single-attendee instance.

The security model is one disposable workspace per attendee (see
server/topology.py): the vended credential is instance-wide, HOMEs are not
uid-isolated, and remote Omnigent mirrors one attendee's OBO token under a
shared Unix uid. Enforcing that model needs a bound identity.

Control Tower injects ``WORKSHOP_ATTENDEE_EMAIL`` when it can, but that
injection is not dependable: it rides on the dedicated-Omnigent deploy path and
travels as per-deployment env, which a console redeploy drops. Treating the
absent value as fatal left attendees with a 403 on every request and no way
out, so the effective binding is resolved in order:

1. ``WORKSHOP_ATTENDEE_EMAIL`` -- Control Tower's hint, authoritative when set.
2. the binding persisted on the data volume, which survives restarts.
3. write-once self-bind on the first non-admin principal to authenticate.

Self-binding is sound because the workspace itself is provisioned per attendee,
so the first non-operator identity to arrive *is* the attendee. Once bound, the
enforcement in ``server/auth.py`` is unchanged: every other identity is refused.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time

from . import config

logger = logging.getLogger(__name__)

SOURCE_CONTROL_TOWER = "control-tower"
SOURCE_PERSISTED = "persisted"
SOURCE_SELF_BOUND = "self-bound"
SOURCE_UNBOUND = "unbound"

_lock = threading.Lock()


def binding_path() -> str:
    return os.path.join(config.shared_prefix(), "attendee.json")


def _read_binding() -> dict[str, str]:
    """Return the persisted binding, or an empty mapping when absent/invalid."""
    try:
        with open(binding_path(), encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as error:  # noqa: BLE001 - unreadable binding must not 500
        logger.warning("attendee binding unreadable (%s): %s", binding_path(), error)
        return {}
    if not isinstance(payload, dict):
        return {}
    email = str(payload.get("email") or "").strip().lower()
    if not config.valid_attendee_email(email):
        return {}
    source = str(payload.get("source") or SOURCE_PERSISTED)
    return {"email": email, "source": source}


def _write_binding(email: str) -> None:
    directory = os.path.dirname(binding_path()) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".attendee-")
    os.fchmod(fd, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "email": email,
                    "source": SOURCE_SELF_BOUND,
                    "bound_at": time.time(),
                },
                handle,
            )
        os.replace(temporary, binding_path())
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resolved_email() -> str:
    """Effective attendee identity, or empty when the instance is unbound."""
    configured = config.workshop_attendee_email()
    if configured:
        return configured
    return _read_binding().get("email", "")


def binding_source() -> str:
    """Where the effective binding came from, for readiness reporting."""
    if config.workshop_attendee_email():
        return SOURCE_CONTROL_TOWER
    binding = _read_binding()
    return binding.get("source", SOURCE_UNBOUND) if binding else SOURCE_UNBOUND


def binding() -> dict[str, str]:
    return {"email": resolved_email(), "source": binding_source()}


def bind(email: str) -> str:
    """Write-once bind this instance to ``email``; return the effective owner.

    Concurrent first requests race here, so the persisted binding always wins
    over the caller's candidate -- two attendees cannot both believe they own
    the instance. A write failure returns the candidate so a volume problem
    degrades to per-request enforcement rather than locking the attendee out.
    """
    candidate = email.strip().lower()
    if not config.valid_attendee_email(candidate):
        raise ValueError("attendee identity must be a valid email address")
    with _lock:
        existing = _read_binding().get("email", "")
        if existing:
            return existing
        try:
            _write_binding(candidate)
        except OSError as error:
            logger.warning("attendee binding write failed: %s", error)
            return candidate
    logger.info("bound this instance to attendee %s", candidate)
    return candidate


__all__ = [
    "SOURCE_CONTROL_TOWER",
    "SOURCE_PERSISTED",
    "SOURCE_SELF_BOUND",
    "SOURCE_UNBOUND",
    "bind",
    "binding",
    "binding_path",
    "binding_source",
    "resolved_email",
]
