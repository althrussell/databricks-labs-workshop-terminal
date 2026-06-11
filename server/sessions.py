"""Owner-bound PTY sessions with reattach, scrollback replay, and reaping.

PTY fds and the session registry are process-local — the app must run with
exactly one uvicorn worker. PTYs keep running while the browser is away;
reattaching replays the scrollback deque before streaming live output.
"""

from __future__ import annotations

import asyncio
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

from . import config
from .users import User

logger = logging.getLogger(__name__)

GRACEFUL_SHUTDOWN_WAIT = 1.0
REAPER_INTERVAL = 60
SCROLLBACK_LINES = 2000


class Session:
    def __init__(self, owner_email: str, agent_id: str, label: str,
                 master_fd: int, pid: int):
        self.id = str(uuid.uuid4())
        self.owner_email = owner_email
        self.agent_id = agent_id
        self.label = label
        self.master_fd = master_fd
        self.pid = pid
        self.scrollback: deque[str] = deque(maxlen=SCROLLBACK_LINES)
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
            return "".join(self.scrollback)

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
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reaper_started = False

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
        logger.info("session %s terminated", session.id[:8])

    # -- internals --

    def _read_pty(self, session: Session) -> None:
        fd = session.master_fd
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
                    decoded = output.decode(errors="replace")
                    with session.lock:
                        session.scrollback.append(decoded)
                        session.last_activity = time.time()
                    self._fanout(session, {"t": "output", "data": decoded})
                else:
                    try:
                        if os.waitpid(session.pid, os.WNOHANG)[0] != 0:
                            break
                    except ChildProcessError:
                        break
            except OSError:
                break

        session.exited = True
        self._fanout(session, {"t": "exit"})
        self.terminate(session)

    def _fanout(self, session: Session, message: dict) -> None:
        loop = self._loop
        if loop is None:
            return
        with session.lock:
            subscribers = list(session.subscribers)
        for queue in subscribers:
            loop.call_soon_threadsafe(queue.put_nowait, message)

    def _reaper_loop(self) -> None:
        while True:
            time.sleep(REAPER_INTERVAL)
            timeout = config.session_idle_timeout()
            now = time.time()
            stale = [s for s in self.snapshot() if now - s.last_activity > timeout]
            for session in stale:
                logger.info("reaping idle session %s (owner=%s)", session.id[:8], session.owner_email)
                self.terminate(session)


class SessionLimitError(Exception):
    pass


session_manager = SessionManager()
