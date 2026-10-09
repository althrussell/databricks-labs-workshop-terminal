"""Durable attendee-owned state, serialized across threads and app processes."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import weakref
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

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
