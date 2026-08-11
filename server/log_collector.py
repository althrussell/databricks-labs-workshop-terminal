"""Sweep Omnigent's process logs so an operator never needs the attendee's box.

Omnigent writes real diagnostics — the traceback behind ``spec_resolver_failed``,
the auth error behind ``native_terminal_start_failed`` — into
``~/.omnigent/logs/<destination>/*.log`` inside a per-attendee home. Everything an
operator was missing during the incident was already on disk. Nothing read it,
nothing kept it, and the instance was recycled.

This module reads it. Every few seconds it tails each attendee's process logs
from a remembered offset, reassembles multi-line records (a traceback is not a
log line), classifies what it finds, redacts it, and writes it to a bounded
journal that survives a restart of this app.

Three deliberate choices:

- **Classify on shape, not on literal strings.** A record is interesting because
  of its level, or because it carries a traceback, or because its message
  contains a ``*_failed``-shaped code — never because it matched a hardcoded
  list. A code Omnigent adds next month is captured on the day it first fires.
- **De-duplicate by ``(attendee, code, traceback fingerprint)``.** A crash loop
  emits the same failure hundreds of times. It should cost one journal entry and
  a counter, not evict every other failure in the room.
- **Never touch PTY scrollback.** Process logs are operator-visible by the same
  reasoning that makes them useful; what the attendee typed is not, and this
  sweep does not read it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .diagnostics import Journal, clip, redact

logger = logging.getLogger(__name__)

# ``LEVEL MM-DD HH:MM:SS.mmm  source  function | message`` — Omnigent's
# TerminalLogFormatter. Anything that does not match is a continuation of the
# record above it, which is how a traceback stays attached to its exception.
_HEADER = re.compile(
    r"^(?P<level>[A-Z]{4,8})\s+"
    r"(?P<timestamp>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<logger>\S+)\s+(?P<function>\S+)\s+\|\s?(?P<message>.*)$"
)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TRACEBACK = re.compile(r"^\s*Traceback \(most recent call last\)")
_EXCEPTION_LINE = re.compile(r"^(?P<type>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Exit))\b")
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
# Codes shaped like the ones attendees see: spec_resolver_failed,
# native_terminal_start_failed, token_expired.
_CODE = re.compile(
    r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)*_"
    r"(?:failed|failure|error|denied|expired|timeout|unavailable|unauthorized|refused))\b"
)
_SESSION = re.compile(r"(?i)\bsession[_ -]?(?:id)?[=: ]+['\"]?([A-Za-z0-9][A-Za-z0-9_-]{5,})")
_VOLATILE = re.compile(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b|\b\d+\b")

_SEVERE = frozenset({"ERROR", "CRIT", "CRITICAL", "FATAL"})

# Only ever used to render an exception into the same text shape the sweep above
# parses out of Omnigent's log files.
_FORMATTER = logging.Formatter()

# Per file, per sweep. A file that has fallen further behind than this catches up
# over the following sweeps rather than being read into memory in one go.
_MAX_READ = 512 * 1024
# Records held for reassembly across this many sweeps are flushed regardless: a
# traceback split by a sweep boundary should still be journalled promptly.
_MAX_HOLD = 2


@dataclass
class _Record:
    level: str
    logger_name: str
    function: str
    timestamp: str
    message: str
    lines: list[str] = field(default_factory=list)
    holds: int = 0

    @property
    def traceback(self) -> str:
        return "\n".join(self.lines)

    @property
    def has_traceback(self) -> bool:
        return any(_TRACEBACK.match(line) for line in self.lines)

    @property
    def looks_complete(self) -> bool:
        """Whether this record can be journalled without waiting for more input.

        A Python traceback ends on an unindented exception line, so a record
        whose last line is still an indented frame is probably mid-write. Holding
        that one for a sweep keeps a traceback split by a sweep boundary in one
        journal entry; flushing everything else keeps the common case immediate.
        """
        if not self.lines:
            return True
        return not self.lines[-1][:1].isspace()


@dataclass
class _Cursor:
    offset: int = 0
    inode: int = 0
    pending: _Record | None = None


def _classify(record: _Record) -> str | None:
    """The error code for ``record``, or None if it is not worth journalling."""
    severe = record.level.strip().upper() in _SEVERE
    if not (severe or record.has_traceback):
        match = _CODE.search(record.message)
        return match.group(1) if match else None
    match = _CODE.search(record.message)
    if match:
        return match.group(1)
    for line in reversed(record.lines):
        exception = _EXCEPTION_LINE.match(line.strip())
        if exception:
            return exception.group("type")
    # Nothing self-describing: the logger that raised it is the next best handle.
    return record.logger_name.strip(". ") or "unclassified"


def _fingerprint(record: _Record, code: str) -> str:
    """Stable identity for "the same failure", ignoring what varies per attempt.

    Built from the deepest stack frame and the exception type when there is a
    traceback, and from the message with its numbers and hex blanked when there
    is not — so two attempts at the same broken thing collapse into one entry
    while two genuinely different failures stay apart.
    """
    parts = [record.logger_name.strip(), code]
    frames = [_FRAME.match(line) for line in record.lines]
    deepest = [match for match in frames if match]
    if deepest:
        last = deepest[-1]
        parts.append(f"{os.path.basename(last.group('file'))}:{last.group('line')}")
    else:
        parts.append(_VOLATILE.sub("#", record.message)[:200])
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:12]


def _session_of(record: _Record) -> str:
    match = _SESSION.search(record.message)
    return match.group(1) if match else ""


class LogCollector:
    """Tails every bound attendee's Omnigent process logs into a journal."""

    def __init__(
        self,
        *,
        journal: Journal | None = None,
        users=None,
        emitter=None,
        interval: float | None = None,
    ) -> None:
        self.journal = journal if journal is not None else _default_journal()
        self._users = users
        self._emitter = emitter
        self.interval = interval if interval is not None else config.log_collector_interval()
        self._cursors: dict[str, _Cursor] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sweeps = 0
        self._captured = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="omnigent-log-collector"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(timeout=self.interval):
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 — a blind operator is the bug here
                logger.warning("log collector sweep failed", exc_info=True)

    # -- sweeping ----------------------------------------------------------

    def status(self) -> dict:
        return {
            "sweeps": self._sweeps,
            "captured": self._captured,
            "files": len(self._cursors),
            "journal": len(self.journal.recent(limit=10_000)),
            "running": self._thread is not None and self._thread.is_alive(),
        }

    def sweep(self) -> int:
        """Read what is new in every attendee's logs. Returns entries captured."""
        captured = 0
        for attendee, home in self._homes():
            for path in log_files(home):
                captured += self._sweep_file(attendee, path)
        self._sweeps += 1
        self._captured += captured
        return captured

    def _homes(self) -> list[tuple[str, str]]:
        users = self._users
        if users is None:
            from .users import user_manager as users
        try:
            return [(user.email, user.home) for user in users.all()]
        except Exception:  # noqa: BLE001 — never let the sweep die on a roster read
            logger.warning("log collector could not list attendees", exc_info=True)
            return []

    def _sweep_file(self, attendee: str, path: Path) -> int:
        key = str(path)
        cursor = self._cursors.setdefault(key, _Cursor())
        try:
            stat = path.stat()
        except OSError:
            return 0
        # Truncation or rotation: the file under this name is a different file,
        # so re-reading it from the top is correct rather than wasteful.
        if stat.st_ino != cursor.inode or stat.st_size < cursor.offset:
            cursor.inode = stat.st_ino
            cursor.offset = 0
            cursor.pending = None
        if stat.st_size == cursor.offset:
            return self._flush_stale(attendee, path, cursor)
        try:
            with open(path, "rb") as handle:
                handle.seek(cursor.offset)
                raw = handle.read(_MAX_READ)
        except OSError:
            return 0
        # Stop at the last newline: a half-written line is not a record yet.
        end = raw.rfind(b"\n")
        if end == -1:
            return 0
        cursor.offset += end + 1
        text = raw[: end + 1].decode("utf-8", "replace")
        return self._ingest(attendee, path, cursor, text)

    def _flush_stale(self, attendee: str, path: Path, cursor: _Cursor) -> int:
        """Journal a held record once the file has gone quiet."""
        if cursor.pending is None:
            return 0
        cursor.pending.holds += 1
        if cursor.pending.holds < _MAX_HOLD:
            return 0
        record, cursor.pending = cursor.pending, None
        return 1 if self._capture(attendee, path, record) else 0

    def _ingest(self, attendee: str, path: Path, cursor: _Cursor, text: str) -> int:
        captured = 0
        pending = cursor.pending
        for raw_line in text.splitlines():
            line = _ANSI.sub("", raw_line).rstrip()
            if not line:
                continue
            header = _HEADER.match(line)
            if header:
                if pending is not None and self._capture(attendee, path, pending):
                    captured += 1
                pending = _Record(
                    level=header.group("level"),
                    logger_name=header.group("logger"),
                    function=header.group("function"),
                    timestamp=header.group("timestamp"),
                    message=header.group("message"),
                )
                continue
            if pending is not None:
                if len(pending.lines) < 200:
                    pending.lines.append(line)
                continue
            if _TRACEBACK.match(line):
                # Bare stderr from a process that died before logging was up —
                # exactly the shape of a failed host start.
                pending = _Record(
                    level="ERROR",
                    logger_name="stdio",
                    function="-",
                    timestamp="",
                    message="uncaught exception",
                    lines=[line],
                )
        if pending is not None and pending.looks_complete:
            if self._capture(attendee, path, pending):
                captured += 1
            pending = None
        cursor.pending = pending
        return captured

    # -- journalling -------------------------------------------------------

    def _capture(self, attendee: str, path: Path, record: _Record) -> bool:
        code = _classify(record)
        if code is None:
            return False
        fingerprint = _fingerprint(record, code)
        session = _session_of(record)
        entry = {
            "attendee": attendee,
            "source": path.parent.name,
            "code": code,
            "level": record.level.strip(),
            "logger": record.logger_name.strip(),
            "function": record.function.strip(),
            "session": session,
            "message": clip(redact(record.message), 1024),
            "detail": clip(redact(record.traceback), 4096),
            "fingerprint": fingerprint,
        }
        stored = self.journal.record(f"{attendee}|{session}|{code}|{fingerprint}", entry)
        if stored.get("count") == 1:
            self._emit(entry)
        # Recovery runs on every occurrence, not just the first: a crash loop is
        # the case that most needs a fresh credential, and the self-healer has
        # its own cooldown. Deliberately after journalling — a recovery attempt
        # must never cost us the record of what went wrong.
        self._heal(entry)
        return True

    def _heal(self, entry: dict) -> None:
        try:
            from .selfheal import on_omnigent_error

            on_omnigent_error(entry["attendee"], entry["code"], entry["message"])
        except Exception:  # noqa: BLE001 — a sweep never dies for a side effect
            logger.debug("self-heal hook failed", exc_info=True)

    def _emit(self, entry: dict) -> None:
        emitter = self._emitter
        if emitter is None:
            from .event_emitter import event_emitter as emitter
        logger.warning(
            "omnigent error captured for %s: %s (%s/%s)",
            entry["attendee"],
            entry["code"],
            entry["source"],
            entry["logger"],
        )
        try:
            emitter.emit(
                "omnigent.error_detected",
                entry["attendee"],
                {
                    "code": entry["code"],
                    "source": entry["source"],
                    "level": entry["level"],
                    "logger": entry["logger"],
                    "session": entry["session"],
                    "message": entry["message"][:400],
                    "fingerprint": entry["fingerprint"],
                },
            )
        except Exception:  # noqa: BLE001 — telemetry is never load-bearing
            pass


class AppErrorJournal(logging.Handler):
    """Put this app's own failures in the same journal as Omnigent's.

    The sweep above exists because Omnigent's failures land in files nothing
    read. This app's own failures land on stdout, which is the platform log page
    — reachable only by someone with the workspace open, and invisible to every
    operator tool built for the room. A 500 on session create is precisely the
    error an attendee reports, so the operator has to be able to read it from the
    same place they read everything else.

    Measured, not hypothetical: a ``UnicodeDecodeError`` in home bootstrap once
    made every session create, host readiness probe and diagnostics call on a
    deployed instance return 500, and the journal had nothing in it.

    Two properties this must keep. It never raises — a diagnostics sink that can
    fail is worse than none. And it never logs, because the journal it writes to
    is fed by logging: the thread-local guard is what stops one error becoming a
    loop.
    """

    def __init__(self, journal: Journal | None = None, *, emitter=None) -> None:
        super().__init__(level=logging.ERROR)
        self._journal_override = journal
        self._emitter = emitter
        self._guard = threading.local()

    @property
    def journal(self) -> Journal:
        # Resolved per record rather than captured at install time: the handler
        # outlives any one journal, and binding early means a journal swapped
        # afterwards silently receives nothing.
        if self._journal_override is not None:
            return self._journal_override
        return log_collector.journal

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._guard, "busy", False):
            return
        self._guard.busy = True
        try:
            self._journal(record)
        except Exception:  # noqa: BLE001 — see the docstring: never raise
            pass
        finally:
            self._guard.busy = False

    def _journal(self, record: logging.LogRecord) -> None:
        lines: list[str] = []
        if record.exc_info:
            lines = _FORMATTER.formatException(record.exc_info).splitlines()[:200]
        parsed = _Record(
            level=record.levelname,
            logger_name=record.name,
            function=record.funcName or "-",
            timestamp="",
            message=record.getMessage(),
            lines=lines,
        )
        code = _classify(parsed) or "app_error"
        fingerprint = _fingerprint(parsed, code)
        entry = {
            "attendee": "",
            "source": "app",
            "code": code,
            "level": parsed.level,
            "logger": parsed.logger_name,
            "function": parsed.function,
            "session": _session_of(parsed),
            "message": clip(redact(parsed.message), 1024),
            "detail": clip(redact(parsed.traceback), 4096),
            "fingerprint": fingerprint,
        }
        stored = self.journal.record(f"app|{code}|{fingerprint}", entry)
        if stored.get("count") == 1:
            self._notify(entry)

    def _notify(self, entry: dict) -> None:
        emitter = self._emitter
        if emitter is None:
            from .event_emitter import event_emitter as emitter
        try:
            emitter.emit(
                "app.error_detected",
                "system",
                {
                    "code": entry["code"],
                    "level": entry["level"],
                    "logger": entry["logger"],
                    "function": entry["function"],
                    "message": entry["message"][:400],
                    "fingerprint": entry["fingerprint"],
                },
            )
        except Exception:  # noqa: BLE001 — telemetry is never load-bearing
            pass


def install_app_error_journal(journal: Journal | None = None) -> AppErrorJournal:
    """Attach the app-error sink to the root logger, once."""
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, AppErrorJournal):
            return handler
    handler = AppErrorJournal(journal)
    root.addHandler(handler)
    return handler


def log_files(home: str) -> list[Path]:
    """Every Omnigent process log under one attendee's home.

    Globs destinations rather than naming ``host`` and ``runner``, so a
    destination Omnigent adds later is swept without a change here.
    """
    root = Path(home) / ".omnigent" / "logs"
    try:
        return sorted(path for path in root.glob("*/*.log") if path.is_file())
    except OSError:
        return []


def journal_path() -> str:
    """Beside the session journal on the app volume, or "" for memory only."""
    session_path = config.session_state_path()
    if not session_path:
        return ""
    return os.path.join(os.path.dirname(session_path) or ".", "diagnostics.json")


def _default_journal() -> Journal:
    return Journal(journal_path() or None, capacity=config.log_journal_capacity())


log_collector = LogCollector()


__all__ = [
    "AppErrorJournal",
    "LogCollector",
    "install_app_error_journal",
    "journal_path",
    "log_collector",
    "log_files",
]
