"""Durable session-metadata journal for reconnect-after-restart (P1-11).

A Workshop Terminal session owns a live PTY (an fd + child process) that cannot
survive the server process restarting. What *can* survive is its **metadata** —
who owned it, which agent, its label, and timing. Raw terminal output stays in
memory only. This store journals metadata to JSON so that after a restart the
server can tell each attendee which terminals they had (now ended), instead of
showing a silent blank.

Design:
- One JSON object keyed by session_id; written atomically (temp file + rename)
  with mode 0600 so a crash mid-write can't corrupt it or expose metadata.
- Every method is **fail-soft**: persistence runs on the session hot path, so a
  bad path or a disk error is logged and swallowed — it must never take down a
  terminal.
- Entries still marked live (``exited`` False) when the file is read at startup
  were live when the previous process died; ``prior_live_sessions`` returns
  those as the "ended on restart" ghosts.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any

logger = logging.getLogger("workshop.session_store")


class SessionMetadataStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    # -- writes (fail-soft) --

    def upsert(self, meta: dict[str, Any]) -> None:
        """Insert or replace one session's metadata by ``id``."""
        sid = meta.get("id")
        if not sid:
            return
        with self._lock:
            data = self._read_unlocked()
            data[sid] = meta
            self._write_unlocked(data)

    def mark_exited(self, session_id: str, reason: str) -> None:
        with self._lock:
            data = self._read_unlocked()
            entry = data.get(session_id)
            if entry is None:
                return
            entry["exited"] = True
            entry["exit_reason"] = reason
            self._write_unlocked(data)

    def remove(self, session_id: str) -> None:
        with self._lock:
            data = self._read_unlocked()
            if data.pop(session_id, None) is not None:
                self._write_unlocked(data)

    # -- reads --

    def load(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return self._read_unlocked()

    def prior_live_sessions(self) -> list[dict[str, Any]]:
        """Sessions that were still live when the prior process died.

        Returned as exited ghosts (``exited`` True, ``exit_reason``
        ``"server_restarted"``) so a client renders them as ended-on-restart
        rather than reattachable.
        """
        ghosts: list[dict[str, Any]] = []
        for entry in self.load().values():
            if entry.get("exited"):
                continue
            ghost = dict(entry)
            ghost["exited"] = True
            ghost["exit_reason"] = "server_restarted"
            ghosts.append(ghost)
        return ghosts

    def clear(self) -> None:
        """Drop the journal — e.g. once priors have been surfaced/acked."""
        with self._lock:
            self._write_unlocked({})

    # -- internals --

    def _read_unlocked(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001 — corrupt/unreadable journal is non-fatal
            logger.warning("session journal unreadable (%s): %s", self.path, exc)
            return {}

    def _write_unlocked(self, data: dict[str, dict[str, Any]]) -> None:
        try:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".session-journal-")
            os.fchmod(fd, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, self.path)  # atomic
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except Exception as exc:  # noqa: BLE001 — persistence must never break a session
            logger.warning("session journal write failed (%s): %s", self.path, exc)
