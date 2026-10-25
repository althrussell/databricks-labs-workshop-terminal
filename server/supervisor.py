"""Keep the Workshop Terminal serving when something inside it tries to stop it.

Attendee agents run as the same Unix user as the terminal, so nothing stops
them signalling it. On 25 Oct 2026 (test1025, seat 2) an agent freed port 8000
for its own test server and took the attendee's terminal down. The platform
showed the app as crashed, and getting it back needed an operator.

This parent process never imports the app. It has two jobs:

- **Restart.** Uvicorn runs as its child. If the child exits when nobody asked
  it to, it is started again within seconds, a bounded number of times. The
  new child records ``server.restarted``.
- **Kill guard.** Stop signals are blocked and read with ``sigtimedwait``, so
  the sender is known. A signal from inside the app's own process tree (an
  attendee shell, an agent, a test server) is ignored and recorded as
  ``server.kill_blocked``. Any other sender is the platform, and the terminal
  shuts down exactly as before.

Uvicorn itself can't be guarded this way: a blocked signal mask is inherited by
every process it spawns, which would leave attendee shells deaf to Ctrl-C. A
direct kill of Uvicorn is handled by the restart.

The parent keeps the platform's OAuth secret in its environment so each
restart can authenticate. It therefore only runs once it is non-dumpable;
otherwise the launcher falls back to exec'ing Uvicorn directly.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

logger = logging.getLogger("workshop.supervisor")

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
# First restart within a second; repeated quick deaths back off so a test
# server briefly squatting on the port can be outlasted.
RESTART_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)
MAX_RESTARTS = 5
RESTART_WINDOW = 600.0
# A child that served this long resets the backoff.
HEALTHY_AFTER = 60.0
# Apps sends SIGKILL 15 s after SIGTERM; the child's own drain fits inside this.
SHUTDOWN_GRACE = 14.0
KILL_BLOCKED_INTERVAL = 5.0

SUPERVISOR_PID_ENV = "WORKSHOP_SUPERVISOR_PID"
EVENTS_FD_ENV = "WORKSHOP_SUPERVISOR_EVENTS_FD"
DISABLE_ENV = "WORKSHOP_SUPERVISOR"
RELAYED_EVENTS = frozenset({"server.restarted", "server.kill_blocked"})

_PR_SET_PDEATHSIG = 1
_PR_SET_DUMPABLE = 4
_PR_SET_CHILD_SUBREAPER = 36
_MAX_TREE_DEPTH = 64
_libc = None


def enabled(env: Mapping[str, str]) -> bool:
    if env.get(DISABLE_ENV, "").strip().lower() in {"0", "false", "off", "no"}:
        return False
    return sys.platform.startswith("linux") and hasattr(signal, "sigtimedwait")


def _prctl(option: int, value: int) -> bool:
    global _libc
    try:
        if _libc is None:
            import ctypes

            _libc = ctypes.CDLL(None, use_errno=True)
        return _libc.prctl(option, value, 0, 0, 0) == 0
    except (AttributeError, OSError):
        return False


def harden() -> bool:
    """Make this process safe to hold the app's secret, or report that it isn't.

    Non-dumpable blocks same-user reads of ``/proc/<pid>/environ`` and
    ptrace. As a subreaper, processes orphaned inside the app reparent here
    rather than to init, so they still count as inside the tree.
    """
    if not _prctl(_PR_SET_DUMPABLE, 0):
        return False
    _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    return True


def _parent_pid(pid: int, proc: Path) -> int:
    # comm can contain spaces and parentheses; the fields after its final
    # closing parenthesis begin with state, then ppid.
    return int((proc / str(pid) / "stat").read_text().rpartition(") ")[2].split()[1])


def classify_sender(pid: int, root: int, *, proc: Path = Path("/proc")) -> str:
    """``platform`` for a sender outside the app; otherwise why it's refused.

    pid 0 is the kernel or a sender in another PID namespace (a container
    stop). A sender that can no longer be read is refused: the platform's
    signaller outlives its SIGTERM, a one-shot ``kill`` run by an agent doesn't.
    """
    if pid in (0, 1, root):
        return "platform"
    current = pid
    for _ in range(_MAX_TREE_DEPTH):
        try:
            parent = _parent_pid(current, proc)
        except (OSError, ValueError, IndexError):
            return "vanished"
        if parent == root:
            return "descendant"
        if parent <= 1:
            return "platform"
        current = parent
    return "vanished"


class _Events:
    """Hands supervisor events to the running child, which owns telemetry."""

    def __init__(self) -> None:
        self._write_fd: int | None = None
        self._blocked_at: dict[str, float] = {}
        self._blocked_since: collections.Counter[str] = collections.Counter()

    def pipe_for_child(self) -> int:
        """A fresh channel for the next child; returns the end it reads."""
        self.close()
        read_fd, self._write_fd = os.pipe()
        os.set_blocking(self._write_fd, False)
        return read_fd

    def close(self) -> None:
        if self._write_fd is not None:
            try:
                os.close(self._write_fd)
            except OSError:
                pass
            self._write_fd = None

    def send(self, name: str, payload: Mapping[str, object]) -> None:
        if self._write_fd is None:
            return
        line = json.dumps({"event": name, **payload}, separators=(",", ":")) + "\n"
        try:
            os.write(self._write_fd, line.encode())
        except OSError:
            # A full or closed pipe loses one event, never the terminal.
            pass

    def kill_blocked(self, signal_name: str, reason: str, sender: int, now: float) -> None:
        self._blocked_since[signal_name] += 1
        # An agent retrying in a loop is reported once per interval.
        if now - self._blocked_at.get(signal_name, float("-inf")) < KILL_BLOCKED_INTERVAL:
            return
        self._blocked_at[signal_name] = now
        attempts = self._blocked_since.pop(signal_name)
        logger.warning(
            "server.kill_blocked signal=%s sender_pid=%d reason=%s attempts=%d",
            signal_name, sender, reason, attempts,
        )
        self.send("server.kill_blocked", {
            "signal": signal_name, "code": reason, "attempts": attempts,
            "outcome": "blocked", "source": "supervisor",
        })


def _reap() -> list[tuple[int, int]]:
    reaped = []
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        if pid == 0:
            return reaped
        reaped.append((pid, status))


def _describe_exit(status: int) -> dict[str, object]:
    if os.WIFSIGNALED(status):
        number = os.WTERMSIG(status)
        try:
            name = signal.Signals(number).name
        except ValueError:
            name = str(number)
        return {"exit_code": -number, "signal": name}
    return {"exit_code": os.WEXITSTATUS(status), "signal": ""}


def _spawn(command: Sequence[str], env: Mapping[str, str], events: _Events,
           mask: set[signal.Signals], root: int) -> subprocess.Popen:
    read_fd = events.pipe_for_child()

    def prepare() -> None:
        # Runs in the child between fork and exec. Attendee shells inherit
        # whatever mask Uvicorn has, so it must be the original one.
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        # If this parent dies, the server drains and stops rather than running
        # on unsupervised, matching what the platform saw before.
        _prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
        if os.getppid() != root:
            os._exit(1)

    child_env = {**env, SUPERVISOR_PID_ENV: str(root), EVENTS_FD_ENV: str(read_fd)}
    try:
        return subprocess.Popen(
            list(command), env=child_env, pass_fds=(read_fd,), preexec_fn=prepare,
        )
    finally:
        os.close(read_fd)


def supervise(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    delays: Sequence[float] = RESTART_DELAYS,
    max_restarts: int = MAX_RESTARTS,
    window: float = RESTART_WINDOW,
    healthy_after: float = HEALTHY_AFTER,
    grace: float = SHUTDOWN_GRACE,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run ``command`` until the platform stops it; returns the exit status."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, stream=sys.stderr,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    env = dict(os.environ if env is None else env)
    root = os.getpid()
    waited = {*STOP_SIGNALS, signal.SIGCHLD}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, waited)
    events = _Events()
    restarts: collections.deque[float] = collections.deque()
    quick_deaths = 0
    child: subprocess.Popen | None = None
    started = 0.0
    restart_at: float | None = clock()
    last_exit: dict[str, object] | None = None
    deadline: float | None = None
    killed = False

    try:
        while True:
            now = clock()
            if child is None and deadline is None and restart_at is not None and now >= restart_at:
                child, started, restart_at = _spawn(command, env, events, mask, root), now, None
                if last_exit is not None:
                    logger.warning("server.restarted attempt=%d pid=%d", len(restarts), child.pid)
                    events.send("server.restarted", {
                        **last_exit, "attempts": len(restarts), "outcome": "restarted",
                        "source": "supervisor",
                    })
            wake = [moment for moment in (restart_at, deadline) if moment is not None]
            timeout = max(0.0, min(wake) - now) if wake else 60.0
            info = signal.sigtimedwait(waited, timeout)
            now = clock()

            if info is None:
                if deadline is not None and now >= deadline:
                    if child is None:
                        return 0
                    if killed:
                        logger.error("server did not exit after SIGKILL")
                        return 1
                    logger.error("server did not stop within %.0fs; killing it", grace)
                    os.kill(child.pid, signal.SIGKILL)
                    killed, deadline = True, now + 1.0
                continue

            if info.si_signo == signal.SIGCHLD:
                for pid, status in _reap():
                    if child is None or pid != child.pid:
                        continue  # an orphan reparented to this subreaper
                    child.returncode = os.waitstatus_to_exitcode(status)
                    child = None
                    if deadline is not None:
                        return 0
                    uptime = now - started
                    last_exit = {**_describe_exit(status), "duration_s": round(uptime, 1)}
                    logger.warning(
                        "server exited unexpectedly (exit_code=%s signal=%s after %.1fs)",
                        last_exit["exit_code"], last_exit["signal"] or "-", uptime,
                    )
                    quick_deaths = 0 if uptime >= healthy_after else quick_deaths + 1
                    while restarts and now - restarts[0] > window:
                        restarts.popleft()
                    if len(restarts) >= max_restarts:
                        logger.error(
                            "server.restart_exhausted: %d restarts in %.0fs; giving up",
                            len(restarts), window,
                        )
                        return 1
                    restarts.append(now)
                    restart_at = now + delays[min(max(quick_deaths - 1, 0), len(delays) - 1)]
                continue

            name = signal.Signals(info.si_signo).name
            reason = classify_sender(info.si_pid, root)
            if reason != "platform":
                events.kill_blocked(name, reason, info.si_pid, now)
                continue
            if deadline is not None:
                continue
            logger.info("%s from pid %d; stopping the server", name, info.si_pid)
            deadline = now + grace
            if child is None:
                return 0
            # Always SIGTERM: a second SIGINT makes Uvicorn skip its drain.
            os.kill(child.pid, signal.SIGTERM)
    finally:
        events.close()
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def supervisor_pid() -> int | None:
    """In the server: the supervisor that started it, if one did."""
    parent = os.getppid()
    return parent if os.environ.get(SUPERVISOR_PID_ENV) == str(parent) else None


def relay_events(record: Callable[[str, Mapping[str, object]], None]) -> threading.Thread | None:
    """In the server: record the supervisor's events, since telemetry lives here."""
    raw = os.environ.get(EVENTS_FD_ENV, "")
    if not raw.isdigit() or supervisor_pid() is None:
        return None
    fd = int(raw)
    try:
        os.set_inheritable(fd, False)
        stream = os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except OSError:
        return None

    def run() -> None:
        with stream:
            for line in stream:
                try:
                    payload = json.loads(line)
                    name = payload.pop("event")
                except (ValueError, KeyError, AttributeError):
                    continue
                if name not in RELAYED_EVENTS:
                    continue
                # The supervisor already logged it to the app log.
                try:
                    record(name, payload)
                except Exception:  # noqa: BLE001 - telemetry never stops the relay
                    pass

    thread = threading.Thread(target=run, name="supervisor-events", daemon=True)
    thread.start()
    return thread
