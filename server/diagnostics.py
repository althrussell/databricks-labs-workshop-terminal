"""Operator-visible diagnostics for failures an attendee can see.

The rule this module exists to enforce: **if an attendee can see an error, an
operator must be able to see it — the same code, with more detail, and without
the attendee's browser.** During the incident that motivated it, an attendee saw
``native_terminal_start_failed`` and nobody could say why: the host's stdout went
to ``/dev/null``, the runner's traceback stayed inside the container, and by the
time anyone looked, the instance was gone.

Three pieces:

- :func:`redact` — token shapes never leave the box, in a log line or an API
  response. Applied on the way in (as the host's output is captured) and again
  on the way out, because a log written before a redaction rule existed is still
  a log somebody will read.
- :class:`BoundedLog` — an append-only writer with a hard byte ceiling and one
  retained generation. A crash-looping process must not fill an attendee's disk,
  and a log that rotated away the failure is no better than no log.
- :class:`Journal` — a bounded, de-duplicated, disk-backed record of classified
  errors. It survives an app restart, because the interesting failures are the
  ones that also restarted something.

Privacy boundary, unchanged from the rest of the app: error codes, short
redacted messages, and the Omnigent *process* logs are operator-visible. Raw PTY
scrollback — what the attendee typed and what the agent replied — is not, and
nothing here reads it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

# Token shapes, most specific first. These are matched against process output,
# so they must be cheap and must never depend on surrounding context.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # JWTs — the OBO token, and anything else three base64url segments long.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+"), "<jwt>"),
    # Databricks PATs and OAuth secrets.
    (re.compile(r"\bdapi[0-9a-f]{16,}\b", re.IGNORECASE), "<pat>"),
    (re.compile(r"\bdose[0-9a-f]{16,}\b", re.IGNORECASE), "<oauth-secret>"),
    (re.compile(r"\bdkea[A-Za-z0-9._-]{16,}\b"), "<oauth-token>"),
    # Authorization headers, however the value is shaped.
    (
        re.compile(r"(?i)\b(authorization|bearer)\b(\s*[:=]\s*|\s+)(\S+)"),
        r"\1\2<redacted>",
    ),
    # Anything self-describing as a secret in a key=value or "key": "value" pair.
    (
        re.compile(
            r"(?i)(\"?\b[a-z_]*(?:token|secret|password|client_id)\b\"?\s*[:=]\s*\"?)"
            r"([^\s\",}]{8,})"
        ),
        r"\1<redacted>",
    ),
)

_MAX_LINE = 4096


def redact(text: str) -> str:
    """Mask credential shapes in ``text``.

    Deliberately over-eager: a masked stack frame costs an operator a little
    context, an unmasked attendee token costs the workshop its credential.
    """
    if not text:
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class BoundedLog:
    """Append-only text log with a byte ceiling and one retained generation.

    Rotation keeps ``<path>.1`` so the most recent failure survives the noise
    that a crash loop generates after it. Every write is redacted first: the
    ceiling protects the disk, redaction protects the credential, and neither is
    optional.
    """

    def __init__(self, path: str | Path, *, max_bytes: int = 2 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max(8 * 1024, int(max_bytes))
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        if not text:
            return
        payload = redact(text)
        if not payload.endswith("\n"):
            payload += "\n"
        data = payload.encode("utf-8", "replace")
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed(len(data))
                with open(self.path, "ab", buffering=0) as handle:
                    handle.write(data)
                os.chmod(self.path, 0o600)
            except OSError:
                # Diagnostics must never be the reason something else fails.
                pass

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size + incoming <= self.max_bytes:
            return
        backup = self.path.with_suffix(self.path.suffix + ".1")
        try:
            os.replace(self.path, backup)
        except OSError:
            try:
                self.path.unlink()
            except OSError:
                pass

    def tail(self, limit_bytes: int = 64 * 1024) -> str:
        """The most recent ``limit_bytes`` of the log, redacted again on read."""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - limit_bytes))
                raw = handle.read()
        except OSError:
            return ""
        text = raw.decode("utf-8", "replace")
        if size > limit_bytes:
            text = text.partition("\n")[2]
        return redact(text)


class Journal:
    """Bounded, de-duplicated record of classified errors that survives restart.

    Deduplication is by ``key`` — normally ``(session, code, traceback
    fingerprint)`` — and repeats increment a count rather than appending, so one
    crash loop cannot evict the twenty other things an operator needs to see.
    """

    def __init__(self, path: str | Path | None = None, *, capacity: int = 500) -> None:
        self.path = Path(path) if path else None
        self.capacity = max(1, capacity)
        self._entries: deque[dict] = deque(maxlen=self.capacity)
        self._by_key: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    def record(self, key: str, entry: dict) -> dict:
        now = time.time()
        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                existing["count"] += 1
                existing["last_seen"] = now
                existing.update(
                    {k: v for k, v in entry.items() if k not in ("count", "first_seen")}
                )
                record = dict(existing)
            else:
                record = {
                    **entry,
                    "key": key,
                    "count": 1,
                    "first_seen": now,
                    "last_seen": now,
                }
                if len(self._entries) == self._entries.maxlen:
                    evicted = self._entries[0]
                    self._by_key.pop(str(evicted.get("key")), None)
                self._entries.append(record)
                self._by_key[key] = record
            self._persist_locked()
            return dict(record)

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            entries = sorted(
                (dict(entry) for entry in self._entries),
                key=lambda entry: entry.get("last_seen", 0.0),
                reverse=True,
            )
        return entries[: max(0, limit)]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._by_key.clear()
            self._persist_locked()

    def _persist_locked(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(list(self._entries)))
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(raw, list):
            return
        for entry in raw[-self.capacity :]:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "")
            if not key:
                continue
            self._entries.append(entry)
            self._by_key[key] = entry


def clip(text: str, limit: int = _MAX_LINE) -> str:
    """Trim a captured line to something a log and an API response can carry."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"… (+{len(text) - limit} chars)"
