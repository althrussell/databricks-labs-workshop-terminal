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
from contextlib import nullcontext

from . import config
from .users import User
from .codex_home import shell_home as codex_shell_home

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


class PartialInputError(OSError):
    """Input began reaching the PTY, so an automatic replay would duplicate it."""


class Session:
    def __init__(self, owner_email: str, agent_id: str, label: str,
                 master_fd: int, pid: int):
        self.id = str(uuid.uuid4())
        self.owner_email = owner_email
        self.agent_id = agent_id
        self.label = label
        self.project_name = ""
        self.master_fd = master_fd
        self.pid = pid
        self.scrollback = ByteScrollback()
        self.lock = threading.Lock()
        self.input_lock = threading.Lock()
        self.delivery_lock = threading.Lock()
        self.prompt_deliveries: dict[str, dict] = {}
        self.created_at = time.time()
        self.last_activity = time.time()
        self.exited = False
        self.exit_code: int | None = None
        self.exit_signal: int | None = None
        self.exit_reason: str | None = None
        self._process: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_done = threading.Event()
        self._master_closed = False
        # The reader and the shutdown caller share one pre-barrier producer.
        # It stays admitted until both terminal output and exit telemetry end.
        self._capture_ticket = None
        self._capture_lock = threading.Lock()
        self._capture_failed = False
        self._exit_reported = False
        # Live websocket subscribers: asyncio queues drained by ws handlers.
        self.subscribers: set[asyncio.Queue] = set()

    def touch(self) -> None:
        with self.lock:
            self.last_activity = time.time()

    def write_input(self, data: str) -> None:
        from .closeout_guard import admitted_writer
        from .event_emitter import event_emitter

        event_emitter._ensure_ready()
        with admitted_writer(event_emitter.journal, "terminal_input"):
            self._write_input(data)

    def _write_input(self, data: str) -> None:
        encoded = data.encode()
        with self.input_lock:
            if self.exited:
                raise OSError("Session is not accepting input")
            offset = 0
            while offset < len(encoded):
                try:
                    written = os.write(self.master_fd, encoded[offset:])
                except OSError as exc:
                    if offset:
                        raise PartialInputError("Prompt delivery was interrupted. Clear the visible input before retrying.") from exc
                    raise
                if written <= 0:
                    if offset:
                        raise PartialInputError("Prompt delivery was interrupted. Clear the visible input before retrying.")
                    raise OSError("PTY did not accept the complete input")
                offset += written
        self.touch()

    def deliver_prompt(self, text: str, delivery_id: str) -> dict:
        """Retry a completed unsent delivery without typing it a second time."""
        import hashlib

        acknowledgement = {"status": "ok", "typed_characters": len(text),
                           "typed_sha256": hashlib.sha256(text.encode()).hexdigest()}
        with self.delivery_lock:
            existing = self.prompt_deliveries.get(delivery_id)
            if existing:
                if existing.get("status") == "interrupted":
                    raise ValueError("Prompt delivery was interrupted. Open the agent to clear its input, then start a new session to load your saved goal.")
                if existing != acknowledgement:
                    raise ValueError("This delivery request was already used for a different prompt")
                return existing
            if len(self.prompt_deliveries) >= 128:
                self.prompt_deliveries.pop(next(iter(self.prompt_deliveries)))
            try:
                self.write_input(text)
            except PartialInputError:
                self.prompt_deliveries[delivery_id] = {"status": "interrupted"}
                raise ValueError("Prompt delivery was interrupted. Open the agent to clear its input, then start a new session to load your saved goal.") from None
            self.prompt_deliveries[delivery_id] = acknowledgement
        return acknowledgement

    def resize(self, cols: int, rows: int) -> None:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError as e:
            logger.warning("resize failed for %s: %s", self.id, e)

    def replay_text(self) -> str:
        with self.lock:
            return self.scrollback.text()

    def exit_message(self) -> dict:
        """Structured status for the attendee; raw output stays in the PTY."""
        with self.lock:
            return {
                "t": "exit",
                "reason": self.exit_reason or "exited",
                "exit_code": self.exit_code,
                "exit_signal": self.exit_signal,
            }

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
                "project_name": self.project_name,
                "created_at": self.created_at,
                "last_activity": self.last_activity,
                "exited": self.exited,
            }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "label": self.label,
            "project_name": self.project_name,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "exited": self.exited,
        }


class SessionManager:
    def __init__(self, store=None):
        self._sessions: dict[str, Session] = {}
        # Retain writers after registry removal until process and reader closure
        # can be verified. Removing an attendee-visible session is not a drain.
        self._writers: dict[str, Session] = {}
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

    def update_project(self, user: User, name: str) -> None:
        """Remember a build selected by the native helper during a live session."""
        with self._lock:
            active = next(iter(self._sessions.values()), None)
            if active is None or active.owner_email != user.email or active.project_name == name:
                return
            active.project_name = name
        self._persist(active)

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

    def active(self) -> Session | None:
        """The app's sole live session, if the slot is occupied."""
        with self._lock:
            return next(iter(self._sessions.values()), None)

    # -- lifecycle --

    def create(self, user: User, agent_id: str, command: list[str], label: str) -> Session:
        from .closeout_guard import admitted_writer
        from .event_emitter import event_emitter

        event_emitter._ensure_ready()
        with admitted_writer(event_emitter.journal, "terminal_spawn"):
            ticket = admitted_writer(event_emitter.journal, "terminal_lifetime", detached=True)
            try:
                return self._create(user, agent_id, command, label, capture_ticket=ticket)
            except BaseException:
                ticket.finish(failed=True)
                raise

    def _create(self, user: User, agent_id: str, command: list[str], label: str, *, capture_ticket=None) -> Session:
        """Atomically claim the app's sole PTY slot and spawn its process."""
        per_user = config.max_sessions_per_user()
        global_cap = config.max_sessions_global()
        if per_user != 1 or global_cap != 1:
            raise SessionConfigurationError(
                "Workshop Terminal requires MAX_SESSIONS_PER_USER=1 and "
                "MAX_SESSIONS_GLOBAL=1"
            )

        with self._lock:
            active = next(iter(self._sessions.values()), None)
            if active is not None:
                raise SessionConflictError(active)

            # Hold the registry lock through process creation and insertion.
            # A reservation flag would introduce a second state to recover from;
            # this short critical section guarantees a losing request never
            # creates a child process at all.
            from . import projects

            cwd = projects.launch_directory(user)
            project_name = projects.read(user)["active"]
            os.makedirs(cwd, exist_ok=True)
            env = user.shell_env()
            if project_name:
                env["WORKSHOP_PROJECT_NAME"] = project_name
            if agent_id == "codex":
                # Keep cwd, configs, credentials and native logs in the original
                # attendee home. Only Codex's lexical socket prefix is shortened.
                env["HOME"] = codex_shell_home(user.home)
            master_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    command,
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    preexec_fn=os.setsid, env=env, cwd=cwd,
                )
            except Exception:
                os.close(master_fd)
                raise
            finally:
                os.close(slave_fd)

            session = Session(user.email, agent_id, label, master_fd, process.pid)
            session._capture_ticket = capture_ticket
            session._process = process
            session.project_name = project_name
            self._sessions[session.id] = session
            self._writers[session.id] = session

        self._persist(session)
        session._reader_thread = threading.Thread(
            target=self._read_pty_until_done, args=(session,), daemon=True,
            name=f"pty-reader-{session.id[:8]}",
        )
        session._reader_thread.start()
        logger.info("session %s created (%s) for %s", session.id[:8], agent_id, user.email)
        return session

    def terminate(self, session: Session, reason: str = "closed", *, deadline: float | None = None) -> None:
        """SIGHUP -> wait -> SIGKILL -> close fd -> drop from registry.

        ``reason`` distinguishes the three ways a terminal ends — the attendee
        closed it, the agent exited, or the reaper took it — which is the
        difference between "they finished" and "it died on them".
        """
        # Claim the session by removing it from the registry, and only then tear
        # it down. Three callers race here — the attendee closing a tab, the PTY
        # reader seeing EOF, and the idle reaper — and the teardown is not
        # idempotent: two closes of the same master fd is a double close, and
        # the second one can land on a descriptor another thread has since been
        # given. Claiming first makes the loser a no-op instead of a hazard.
        with self._lock:
            claimed = self._sessions.pop(session.id, None) is not None
        if not claimed:
            return
        with session.lock:
            session.exited = True
            session.exit_reason = reason
        self._fanout(session, session.exit_message())
        try:
            # A captured wait status means the reader already reaped the agent;
            # signalling that PID again risks hitting a rapidly reused PID.
            if session.exit_code is None and session.exit_signal is None:
                os.killpg(session.pid, signal.SIGHUP)
                wait = GRACEFUL_SHUTDOWN_WAIT
                if deadline is not None:
                    wait = min(wait, max(0.0, (deadline - time.monotonic()) / 2))
                time.sleep(wait)
                try:
                    os.killpg(session.pid, 0)
                    os.killpg(session.pid, signal.SIGKILL)
                except OSError:
                    pass
            # Removing the public session must not stop its already-admitted
            # reader before the process's final output is observed.
            reader = session._reader_thread
            if reader is not None and reader is not threading.current_thread():
                remaining = GRACEFUL_SHUTDOWN_WAIT if deadline is None else max(0.0, deadline - time.monotonic())
                reader.join(timeout=remaining)
            if reader is None or reader is threading.current_thread() or not reader.is_alive():
                self._close_master(session)
        except OSError:
            pass
        # A deliberately-closed session is no longer a restart ghost: drop it
        # from the journal so it isn't surfaced after a future restart.
        if self._store is not None:
            try:
                self._store.remove(session.id)
            except Exception:  # noqa: BLE001
                logger.warning("session journal remove failed for %s", session.id[:8])
        logger.info("session %s terminated (%s)", session.id[:8], reason)
        from . import telemetry

        with session._capture_ticket.scope() if session._capture_ticket else nullcontext():
            captured = telemetry.session_exited(
                session.owner_email,
                session.agent_id,
                reason,
                session_id=session.id,
                duration_s=time.time() - session.created_at,
                exit_code=session.exit_code,
                process_signal=session.exit_signal,
            )
        if captured is False:
            session._capture_failed = True
        session._exit_reported = True
        self._finish_capture(session)

    @staticmethod
    def _finish_capture(session: Session) -> None:
        with session._capture_lock:
            if session._capture_ticket and session._reader_done.is_set() and session._exit_reported:
                session._capture_ticket.finish(failed=session._capture_failed)

    @staticmethod
    def _close_master(session: Session) -> None:
        # The reader owns a still-draining PTY after a bounded shutdown wait.
        # Close exactly once, including a later reader completion.
        with session.lock:
            if session._master_closed:
                return
            session._master_closed = True
        try:
            os.close(session.master_fd)
        except OSError:
            pass

    @staticmethod
    def _group_alive(session: Session) -> bool:
        from .closeout_writers import process_group_alive

        return process_group_alive(session.pid)

    def drain_for_closeout(self, *, deadline: float) -> dict:
        """Stop and verify groups and readers using one caller-owned deadline."""
        with self._lock:
            writers = dict(self._writers)
            writers.update(self._sessions)
        issues: list[str] = []
        outcomes: list[dict] = []
        for session in writers.values():
            local_issues: list[str] = []
            try:
                # A reaped leader can leave live PTY descendants. Stand those
                # down before terminate waits for the reader's true EOF.
                if ((session.exit_code is not None or session.exit_signal is not None)
                        and self._group_alive(session)):
                    os.killpg(session.pid, signal.SIGTERM)
                self.terminate(session, reason="workshop_closing", deadline=deadline)
                # A naturally exited leader can leave children alive, including
                # a child that ignored the terminal's hangup signal.
                if self._group_alive(session):
                    os.killpg(session.pid, signal.SIGTERM)
                    grace = min(0.25, max(0.0, (deadline - time.monotonic()) / 2))
                    if grace:
                        time.sleep(grace)
                    if self._group_alive(session):
                        os.killpg(session.pid, signal.SIGKILL)
                process = session._process
                if process is not None:
                    try:
                        process.wait(timeout=max(0.0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        local_issues.append("terminal_process_not_reaped")
                reader = session._reader_thread
                if reader is not None and reader is not threading.current_thread():
                    reader.join(timeout=max(0.0, deadline - time.monotonic()))
                if reader is not None and reader.is_alive():
                    local_issues.append("terminal_reader_pending")
                self._finish_capture(session)
                while self._group_alive(session) and time.monotonic() < deadline:
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                if self._group_alive(session):
                    local_issues.append("terminal_process_group_pending")
            except (OSError, subprocess.SubprocessError) as error:
                local_issues.append(f"terminal_shutdown_unverified:{type(error).__name__}")
            if not local_issues:
                with self._lock:
                    self._writers.pop(session.id, None)
            issues.extend(issue for issue in local_issues if issue not in issues)
            outcomes.append({"kind": "terminal", "complete": not local_issues, "issues": local_issues})
        return {"complete": not issues, "issues": issues, "writers": outcomes}

    def _read_pty_until_done(self, session: Session) -> None:
        try:
            with session._capture_ticket.scope() if session._capture_ticket else nullcontext():
                self._read_pty(session)
        except BaseException:
            session._capture_failed = True
            raise
        finally:
            self._close_master(session)
            session._reader_done.set()
            self._finish_capture(session)
            # Completed writers cannot later acquire a recycled process-group
            # ID. Preserve uncertain groups for explicit closeout verification.
            try:
                alive = self._group_alive(session)
            except OSError:
                alive = True
            if not alive:
                with self._lock:
                    self._writers.pop(session.id, None)

    # -- internals --

    def _read_pty(self, session: Session) -> None:
        fd = session.master_fd
        decoder = Utf8StreamDecoder()

        def capture_wait_status(status: int) -> None:
            exit_value = os.waitstatus_to_exitcode(status)
            if exit_value < 0:
                session.exit_signal = -exit_value
            else:
                session.exit_code = exit_value

        def handle_output(decoded: str) -> None:
            if not decoded:
                return
            self.publish_output(session, decoded)
            if self.output_observer is not None:
                try:
                    self.output_observer(session, decoded)
                except Exception:  # noqa: BLE001 — never break the reader
                    session._capture_failed = True

        while True:
            try:
                readable, _, errored = select.select([fd], [], [fd], 0.05)
                if readable or errored:
                    output = os.read(fd, 65536)
                    if not output:
                        break  # EOF — process exited
                    handle_output(decoder.decode(output))
                else:
                    try:
                        waited_pid, wait_status = os.waitpid(session.pid, os.WNOHANG)
                        if waited_pid != 0:
                            capture_wait_status(wait_status)
                    except ChildProcessError:
                        # A reaped leader can leave descendants holding the
                        # slave PTY. Only fd EOF/error ends their output stream.
                        pass
            except OSError:
                break

        handle_output(decoder.flush())
        if session.exit_code is None and session.exit_signal is None:
            try:
                waited_pid, wait_status = os.waitpid(session.pid, os.WNOHANG)
                if waited_pid != 0:
                    capture_wait_status(wait_status)
            except (ChildProcessError, OSError):
                pass
        reason = (
            "process_signal"
            if session.exit_signal is not None
            else "process_error"
            if session.exit_code not in (None, 0)
            else "exited"
        )
        self.terminate(session, reason=reason)

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
                self.terminate(session, reason="idle_reaped")
            # Refresh the journal so a restart replays reasonably recent
            # scrollback/activity (within one reaper interval), not create-time.
            if self._store is not None:
                for session in live:
                    if not session.exited:
                        self._persist(session)


class SessionConflictError(Exception):
    """A create lost the single-session admission race."""

    def __init__(self, active: Session):
        self.active_session = {
            "id": active.id,
            "agent_id": active.agent_id,
            "label": active.label,
        }
        super().__init__(f"{active.label} is already open — close it before switching agents")


class SessionConfigurationError(Exception):
    """The deployment attempted to override the fixed one-session invariant."""


session_manager = SessionManager()
