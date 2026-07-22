"""Owner-bound PTY sessions with reattach, scrollback replay, and reaping.

PTY fds and the session registry are process-local — the app must run with
exactly one uvicorn worker. PTYs keep running while the browser is away;
reattaching replays the scrollback deque before streaming live output.
"""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import logging
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable

from . import config
from .users import User

logger = logging.getLogger(__name__)

GRACEFUL_SHUTDOWN_WAIT = 1.0
REAPER_INTERVAL = 60
SCROLLBACK_MAX_BYTES = 2 * 1024 * 1024
TERMINAL_SUBSCRIBER_QUEUE_SIZE = 128


def _utf8_tail(value: str | bytes, max_bytes: int) -> bytes:
    """Return a valid UTF-8 suffix no larger than max_bytes."""
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) <= max_bytes:
        return raw
    return raw[-max_bytes:].decode("utf-8", errors="ignore").encode("utf-8")


class ByteScrollback:
    """UTF-8-safe terminal replay bounded by encoded byte size."""

    def __init__(self, max_bytes: int = SCROLLBACK_MAX_BYTES):
        self.max_bytes = max(1, max_bytes)
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def append(self, text: str) -> None:
        chunk = _utf8_tail(text, self.max_bytes)
        if not chunk:
            return
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size > self.max_bytes and self._chunks:
            excess = self._size - self.max_bytes
            first = self._chunks.popleft()
            self._size -= len(first)
            if len(first) > excess:
                kept = _utf8_tail(first, len(first) - excess)
                if kept:
                    self._chunks.appendleft(kept)
                    self._size += len(kept)

    def text(self, max_bytes: int | None = None) -> str:
        raw = b"".join(self._chunks)
        if max_bytes is not None:
            raw = _utf8_tail(raw, max_bytes)
        return raw.decode("utf-8")

    def __len__(self) -> int:
        return self._size


class Utf8StreamDecoder:
    """Incrementally decode PTY bytes without corrupting split code points."""

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def decode(self, chunk: bytes) -> str:
        return self._decoder.decode(chunk, final=False)

    def flush(self) -> str:
        return self._decoder.decode(b"", final=True)


class TerminalSubscriberQueue(asyncio.Queue):
    """Bounded queue that forces reconnect on overflow.

    Terminal bytes cannot use a drop policy without corrupting terminal
    semantics. On overflow, pending frames are replaced by an overflow signal;
    the WebSocket closes and its reconnect receives the bounded replay.
    """

    def __init__(
        self,
        maxsize: int,
        on_overflow: Callable[[], None] | None = None,
    ):
        super().__init__(maxsize=maxsize)
        self.overflowed = False
        self.overflow_count = 0
        self.max_depth = 0
        self._on_overflow = on_overflow

    def put_nowait(self, item) -> None:
        if self.overflowed:
            return
        try:
            super().put_nowait(item)
            self.max_depth = max(self.max_depth, self.qsize())
        except asyncio.QueueFull:
            self.overflowed = True
            self.overflow_count += 1
            if self._on_overflow is not None:
                self._on_overflow()
            while not self.empty():
                self.get_nowait()
            super().put_nowait({"t": "overflow"})


class Session:
    def __init__(self, owner_email: str, agent_id: str, label: str,
                 master_fd: int, pid: int):
        self.id = str(uuid.uuid4())
        self.owner_email = owner_email
        self.agent_id = agent_id
        self.label = label
        self.master_fd = master_fd
        self.pid = pid
        self.scrollback = ByteScrollback()
        self.lock = threading.Lock()
        self.created_at = time.time()
        self.last_activity = time.time()
        self.exited = False
        # Live websocket subscribers: asyncio queues drained by ws handlers.
        self.subscribers: set[asyncio.Queue] = set()

    def touch(self) -> None:
        with self.lock:
            self.last_activity = time.time()

    def write_input(self, data: str) -> None:
        os.write(self.master_fd, data.encode())
        self.touch()

    def resize(self, cols: int, rows: int) -> None:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError as e:
            logger.warning("resize failed for %s: %s", self.id, e)

    def replay_text(self) -> str:
        with self.lock:
            return self.scrollback.text()

    def metadata(self) -> dict:
        """Persistable metadata for restart recovery (P1-11).

        Excludes the un-persistable live PTY handles (fd, pid); keeps what's
        needed to tell an attendee which terminal they had. Terminal output is
        intentionally in-memory only and is never written to the journal.
        """
        with self.lock:
            return {
                "id": self.id,
                "owner_email": self.owner_email,
                "agent_id": self.agent_id,
                "label": self.label,
                "created_at": self.created_at,
                "last_activity": self.last_activity,
                "exited": self.exited,
            }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "label": self.label,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "exited": self.exited,
        }


class SessionManager:
    def __init__(self, store=None):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reaper_started = False
        # Optional observer fed each output chunk (topic spotting). Must be
        # fast and must never raise into the reader thread.
        self.output_observer = None
        # Optional metadata journal (P1-11). When set, live sessions are
        # persisted so a restart can surface them; prior ghosts loaded once.
        self._store = store
        self._prior: dict[str, list[dict]] = {}
        self._queue_lock = threading.Lock()
        self._terminal_queue_overflows = 0
        self._terminal_queue_max_depth = 0

    def configure_store(self, store) -> None:
        """Attach a metadata journal and load any pre-restart sessions as ghosts."""
        self._store = store
        self.load_prior()

    def load_prior(self) -> None:
        """Read the journal's pre-restart live sessions into per-owner ghosts."""
        self._prior = {}
        if self._store is None:
            return
        for ghost in self._store.prior_live_sessions():
            ghost = dict(ghost)
            # Older journals may contain raw terminal output. Never retain or
            # return it after loading into the metadata-only ghost model.
            ghost.pop("scrollback_tail", None)
            owner = ghost.get("owner_email")
            if owner:
                self._prior.setdefault(owner, []).append(ghost)
        # The journal is now stale (those PTYs are gone); fresh sessions will
        # repopulate it. Clearing avoids re-surfacing the same ghosts forever.
        self._store.clear()

    def prior_for(self, owner_email: str) -> list[dict]:
        """Ended-on-restart ghost sessions for an owner (newest first)."""
        with self._lock:
            ghosts = list(self._prior.get(owner_email, []))
        ghosts.sort(key=lambda g: g.get("last_activity", 0), reverse=True)
        return ghosts

    def acknowledge_prior(self, owner_email: str, session_id: str) -> bool:
        """Remove one restart ghost only when it belongs to this owner."""
        with self._lock:
            ghosts = self._prior.get(owner_email, [])
            remaining = [ghost for ghost in ghosts if ghost.get("id") != session_id]
            if len(remaining) == len(ghosts):
                return False
            if remaining:
                self._prior[owner_email] = remaining
            else:
                self._prior.pop(owner_email, None)
            return True

    def _new_subscriber(self, max_queue: int) -> TerminalSubscriberQueue:
        def record_overflow() -> None:
            with self._queue_lock:
                self._terminal_queue_overflows += 1

        return TerminalSubscriberQueue(max_queue, record_overflow)

    def subscribe(
        self,
        session: Session,
        *,
        max_queue: int = TERMINAL_SUBSCRIBER_QUEUE_SIZE,
    ) -> TerminalSubscriberQueue:
        queue = self._new_subscriber(max_queue)
        with session.lock:
            session.subscribers.add(queue)
        return queue

    def attach_with_replay(
        self,
        session: Session,
        *,
        max_queue: int = TERMINAL_SUBSCRIBER_QUEUE_SIZE,
    ) -> tuple[str, TerminalSubscriberQueue, bool]:
        """Atomically snapshot replay state and register the live subscriber."""
        queue = self._new_subscriber(max_queue)
        with session.lock:
            replay = session.scrollback.text()
            exited = session.exited
            session.subscribers.add(queue)
        return replay, queue, exited

    def unsubscribe(self, session: Session, queue: TerminalSubscriberQueue) -> None:
        with session.lock:
            session.subscribers.discard(queue)
        with self._queue_lock:
            self._terminal_queue_max_depth = max(
                self._terminal_queue_max_depth,
                queue.max_depth,
            )

    def queue_metrics(self) -> dict:
        with self._lock:
            sessions = list(self._sessions.values())
        with self._queue_lock:
            overflows = self._terminal_queue_overflows
            observed_max = self._terminal_queue_max_depth
        subscribers: list[TerminalSubscriberQueue] = []
        for session in sessions:
            with session.lock:
                subscribers.extend(session.subscribers)
        current_depth = sum(queue.qsize() for queue in subscribers)
        max_depth = max([observed_max, *(queue.max_depth for queue in subscribers)], default=0)
        return {
            "terminal": {
                "subscribers": len(subscribers),
                "queue_capacity": TERMINAL_SUBSCRIBER_QUEUE_SIZE,
                "current_depth": current_depth,
                "max_depth": max_depth,
                "overflows": overflows,
                "policy": "disconnect_and_replay",
            }
        }

    def _persist(self, session: Session) -> None:
        if self._store is None:
            return
        try:
            self._store.upsert(session.metadata())
        except Exception:  # noqa: BLE001 — journaling must never break a session
            logger.warning("session journal upsert failed for %s", session.id[:8])

    def _persist_exit(self, session_id: str, reason: str) -> None:
        if self._store is None:
            return
        try:
            self._store.mark_exited(session_id, reason)
        except Exception:  # noqa: BLE001
            logger.warning("session journal exit mark failed for %s", session_id[:8])

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if not self._reaper_started:
            self._reaper_started = True
            threading.Thread(target=self._reaper_loop, daemon=True, name="session-reaper").start()

    # -- lookups (always owner-scoped: wrong owner is indistinguishable from absent) --

    def get(self, session_id: str, owner_email: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.owner_email != owner_email:
            return None
        return session

    def list_for(self, owner_email: str) -> list[Session]:
        with self._lock:
            return [s for s in self._sessions.values() if s.owner_email == owner_email]

    def count_for(self, owner_email: str) -> int:
        return len(self.list_for(owner_email))

    def count_all(self) -> int:
        with self._lock:
            return len(self._sessions)

    def snapshot(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    # -- lifecycle --

    def create(self, user: User, agent_id: str, command: list[str], label: str) -> Session:
        """Spawn a PTY for `user`. Caps re-checked under the registry lock."""
        per_user = config.max_sessions_per_user()
        global_cap = config.max_sessions_global()
        with self._lock:
            if sum(1 for s in self._sessions.values() if s.owner_email == user.email) >= per_user:
                raise SessionLimitError(f"You already have {per_user} open terminals — close one first.")
            if len(self._sessions) >= global_cap:
                raise SessionLimitError("The workshop instance is at capacity — try again shortly.")

        master_fd, slave_fd = pty.openpty()
        env = user.shell_env()
        cwd = os.path.join(user.home, "projects")
        os.makedirs(cwd, exist_ok=True)
        try:
            pid = subprocess.Popen(
                command,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                preexec_fn=os.setsid, env=env, cwd=cwd,
            ).pid
        finally:
            os.close(slave_fd)

        session = Session(user.email, agent_id, label, master_fd, pid)
        with self._lock:
            # Authoritative re-check under the same lock as insertion (TOCTOU).
            owned = sum(1 for s in self._sessions.values() if s.owner_email == user.email)
            if owned >= per_user or len(self._sessions) >= global_cap:
                os.close(master_fd)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                raise SessionLimitError("Terminal limit reached — close one first.")
            self._sessions[session.id] = session

        self._persist(session)
        threading.Thread(
            target=self._read_pty, args=(session,), daemon=True,
            name=f"pty-reader-{session.id[:8]}",
        ).start()
        logger.info("session %s created (%s) for %s", session.id[:8], agent_id, user.email)
        return session

    def terminate(self, session: Session) -> None:
        """SIGHUP -> wait -> SIGKILL -> close fd -> drop from registry."""
        self._fanout(session, {"t": "exit"})
        try:
            os.kill(session.pid, signal.SIGHUP)
            time.sleep(GRACEFUL_SHUTDOWN_WAIT)
            try:
                os.kill(session.pid, 0)
                os.kill(session.pid, signal.SIGKILL)
            except OSError:
                pass
            os.close(session.master_fd)
        except OSError:
            pass
        with self._lock:
            self._sessions.pop(session.id, None)
        # A deliberately-closed session is no longer a restart ghost: drop it
        # from the journal so it isn't surfaced after a future restart.
        if self._store is not None:
            try:
                self._store.remove(session.id)
            except Exception:  # noqa: BLE001
                logger.warning("session journal remove failed for %s", session.id[:8])
        logger.info("session %s terminated", session.id[:8])

    # -- internals --

    def _read_pty(self, session: Session) -> None:
        fd = session.master_fd
        decoder = Utf8StreamDecoder()

        def handle_output(decoded: str) -> None:
            if not decoded:
                return
            self.publish_output(session, decoded)
            if self.output_observer is not None:
                try:
                    self.output_observer(session, decoded)
                except Exception:  # noqa: BLE001 — never break the reader
                    pass

        while True:
            with self._lock:
                if session.id not in self._sessions:
                    return
            try:
                readable, _, errored = select.select([fd], [], [fd], 0.05)
                if readable or errored:
                    output = os.read(fd, 65536)
                    if not output:
                        break  # EOF — process exited
                    handle_output(decoder.decode(output))
                else:
                    try:
                        if os.waitpid(session.pid, os.WNOHANG)[0] != 0:
                            break
                    except ChildProcessError:
                        break
            except OSError:
                break

        handle_output(decoder.flush())
        session.exited = True
        self._fanout(session, {"t": "exit"})
        self.terminate(session)

    def _fanout(self, session: Session, message: dict) -> None:
        loop = self._loop
        if loop is None:
            return
        with session.lock:
            subscribers = list(session.subscribers)
        self._dispatch(subscribers, message)

    def publish_output(self, session: Session, decoded: str) -> None:
        """Append output and select recipients in one lock transaction."""
        loop = self._loop
        with session.lock:
            session.scrollback.append(decoded)
            session.last_activity = time.time()
            subscribers = list(session.subscribers)
        if loop is not None:
            self._dispatch(subscribers, {"t": "output", "data": decoded})

    def _dispatch(
        self,
        subscribers: list[TerminalSubscriberQueue],
        message: dict,
    ) -> None:
        loop = self._loop
        if loop is None:
            return
        for queue in subscribers:
            loop.call_soon_threadsafe(self._put_terminal_message, queue, message)

    def _put_terminal_message(
        self,
        queue: TerminalSubscriberQueue,
        message: dict,
    ) -> None:
        queue.put_nowait(message)
        with self._queue_lock:
            self._terminal_queue_max_depth = max(
                self._terminal_queue_max_depth,
                queue.max_depth,
            )

    def _reaper_loop(self) -> None:
        while True:
            time.sleep(REAPER_INTERVAL)
            timeout = config.session_idle_timeout()
            now = time.time()
            live = self.snapshot()
            stale = [s for s in live if now - s.last_activity > timeout]
            for session in stale:
                logger.info("reaping idle session %s (owner=%s)", session.id[:8], session.owner_email)
                self.terminate(session)
            # Refresh the journal so a restart replays reasonably recent
            # scrollback/activity (within one reaper interval), not create-time.
            if self._store is not None:
                for session in live:
                    if not session.exited:
                        self._persist(session)


class SessionLimitError(Exception):
    pass


session_manager = SessionManager()
