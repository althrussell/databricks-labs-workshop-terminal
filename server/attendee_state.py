"""Durable attendee-owned state, serialized across threads and app processes."""

from __future__ import annotations

import fcntl
import copy
import json
import logging
import os
import tempfile
import threading
import weakref
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

from . import config
from .capture_journal import CaptureHeldError, canonical_json

logger = logging.getLogger(__name__)
_SECTION = "attendee_product_state"
_NAMES = {"profile", "brief"}

_registry_lock = threading.Lock()
_threads: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


@contextmanager
def locked(path: str) -> Iterator[None]:
    """Lock the read/modify/write transaction, not just its final replacement."""
    key = os.path.abspath(path)
    with _registry_lock:
        mutex = _threads.get(key)
        if mutex is None:
            mutex = threading.RLock()
            _threads[key] = mutex
    with mutex:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_json(path: str, value: Any) -> None:
    """Caller owns the transaction lock. Never expose a partial JSON record."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(prefix=".attendee-state-", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def _journal(user):
    journal = getattr(user, "_capture_journal", None)
    if journal is None:
        if not config.local_dev():
            raise CaptureHeldError("attendee product persistence is not initialized")
        return None
    if journal.binding["owner"] != user.email:
        raise CaptureHeldError("attendee product owner does not match its retained binding")
    if not config.local_dev() and not journal.storage.durable:
        raise CaptureHeldError("attendee product persistence is not durable")
    return journal


def _validate(name: str, raw: dict) -> None:
    from . import attendee_profile, wizard

    if name == "profile":
        parsed = attendee_profile.from_json(raw).to_json()
    elif name == "brief":
        parsed = wizard.WizardBrief.from_json(raw).to_json()
    else:
        raise ValueError("unknown attendee product document")
    if canonical_json(parsed) != canonical_json(raw):
        raise ValueError("attendee product snapshot is not canonical")


def _documents(state: dict, journal) -> dict:
    from .closeout_guard import closeout_state

    closeout_state(state)  # An unknown barrier must not silently become OPEN.
    if _SECTION not in state:
        return {}
    section = state[_SECTION]
    try:
        if (not isinstance(section, dict) or set(section) != {"schema_version", "owner", "documents"}
                or type(section["schema_version"]) is not int or section["schema_version"] != 1
                or section["owner"] != journal.binding["owner"]
                or not isinstance(section["documents"], dict)
                or not set(section["documents"]) <= _NAMES):
            raise ValueError("invalid attendee product state")
        for name, raw in section["documents"].items():
            _validate(name, raw)
        return section["documents"]
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        held = CaptureHeldError("retained attendee product state is invalid")
        journal.mark_held(held)
        raise held from error


def configure_capture(journal) -> None:
    """Validate bound snapshots before accepting attendee input.

    HOME is a disposable cache without a source binding. A new run therefore
    starts with defaults rather than importing another run's local preferences
    or goal. Existing closed epochs are read without adding new state.
    """
    _documents(journal.read(), journal)


def document_matches(state: dict, journal, name: str, expected: dict) -> bool:
    """Fence an optional projection against the authoritative product snapshot.

    The caller must run this inside the same journal transaction that emits the
    projection, so another container cannot publish an older goal afterwards.
    """
    return _documents(state, journal).get(name) == expected


def read_document(user, name: str, *, default: dict, local_read, path: str) -> dict:
    journal = _journal(user)
    if journal is None:
        return local_read()
    raw = copy.deepcopy(_documents(journal.read(), journal).get(name, default))
    _cache(path, raw)
    return raw


def update_document(user, name: str, *, default: dict, local_read, path: str, update) -> dict:
    """The revision check and replacement share the remote commit transaction."""
    journal = _journal(user)
    if journal is None:
        with locked(path):
            updated = update(local_read())
            write_json(path, updated)
            return updated

    def mutate(state):
        documents = _documents(state, journal)
        updated = update(copy.deepcopy(documents.get(name, default)))
        _validate(name, updated)
        state.setdefault(_SECTION, {"schema_version": 1, "owner": user.email,
                                   "documents": {}})["documents"][name] = copy.deepcopy(updated)
        return copy.deepcopy(updated)

    updated = journal.transaction(mutate)
    _cache(path, updated)
    return updated


def _cache(path: str, raw: dict) -> None:
    try:
        with locked(path):
            write_json(path, raw)
    except OSError as error:
        # The accepted snapshot is already retained. A failed disposable cache
        # must not turn that acceptance into a false failed save.
        logger.warning("attendee product cache unavailable (%s)", type(error).__name__)
