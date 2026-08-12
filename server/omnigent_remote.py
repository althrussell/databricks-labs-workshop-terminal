"""Per-attendee foreground Omnigent remote-host supervision."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import requests
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import attendee as attendee_binding
from . import cli_config
from . import config
from .users import User, email_slug

logger = logging.getLogger(__name__)

REMOTE_HOST_STATES = {
    "disabled",
    "waiting_for_token",
    "starting",
    "running",
    "backoff",
    "stopped",
    "error",
}

_HOST_ID_DOMAIN = b"databricks-workshop-terminal/omnigent-host-id/v1\0"
_HOST_NAME_MAX_LENGTH = 64


@dataclass(frozen=True)
class StableHostIdentity:
    host_id: str
    name: str


def stable_host_identity(user: User, server_url: str) -> StableHostIdentity:
    """Derive the non-secret stable identity for one attendee/server pair.

    Construction:
    ``SHA256(domain || normalized_server_url || NUL || normalized_email)[:32]``
    where ``domain`` is
    ``b"databricks-workshop-terminal/omnigent-host-id/v1\\0"``.
    """
    normalized_url = config.normalize_omnigent_app_url(
        server_url, allow_loopback_http=True
    )
    normalized_email = user.email.strip().lower()
    digest = hashlib.sha256(
        _HOST_ID_DOMAIN
        + normalized_url.encode("utf-8")
        + b"\0"
        + normalized_email.encode("utf-8")
    ).hexdigest()
    slug = email_slug(normalized_email)
    prefix = "workshop-"
    if len(prefix) + len(slug) <= _HOST_NAME_MAX_LENGTH:
        name = f"{prefix}{slug}"
    else:
        local, _, email_digest = slug.rpartition("-")
        available = _HOST_NAME_MAX_LENGTH - len(prefix) - 1 - len(email_digest)
        name = f"{prefix}{local[:available]}-{email_digest}"
    return StableHostIdentity(host_id=digest[:32], name=name)


def build_host_launch(
    user: User, binary: str, server_url: str
) -> tuple[list[str], dict[str, str], str]:
    """Build a token-free, per-user host launch specification."""
    env = user.shell_env()
    for key in (
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_HOST",
        "OMNIGENT_HOST_TOKEN",
    ):
        env.pop(key, None)
    # Native terminals inherit this environment (their OSEnvSpec is
    # "caller_process"), so it is the agent's Databricks environment as much as
    # the host's. It points at the attendee-only config rather than
    # ~/.databrickscfg: the app service principal stays unreachable from the
    # Omnigent plane, while the agent's CLI still resolves an identity.
    env["DATABRICKS_CONFIG_FILE"] = cli_config.omnigent_databrickscfg_path(user)
    env["DATABRICKS_CONFIG_PROFILE"] = config.obo_profile_name()
    env["OMNIGENT_APP_URL"] = server_url
    # The host's own file logs are the only place a start failure keeps its
    # traceback; at the default level the interesting records are filtered out
    # before they are written, which is how an incident became archaeology.
    env.setdefault("OMNIGENT_LOG_LEVEL", config.omnigent_host_log_level())
    # Omnigent's runner reads this from its own process env (not Claude's
    # settings.json) and re-adds CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 when
    # it is unset, which turns off MCP tool search and loads every tool schema
    # eagerly. Gated on a resolved gateway for the same reason as the harness
    # settings: the serving-endpoints fallback is not the negotiating surface.
    env.update(cli_config.beta_negotiation_env(bool(cli_config.gateway_host())))
    identity = stable_host_identity(user, server_url)
    env["OMNIGENT_HOST_ID"] = identity.host_id
    env["OMNIGENT_HOST_NAME"] = identity.name
    argv = [
        os.path.realpath(binary),
        "host",
        "--server",
        server_url,
        "--non-interactive",
    ]
    cwd = os.path.join(user.home, "projects")
    return argv, env, cwd


def host_log_path(user: User) -> Path:
    """Where an attendee's captured host stdout/stderr lands.

    Under ``~/.omnigent/logs/host/`` so a single collector sweep finds it
    alongside the logs Omnigent writes itself, with a distinct name so the two
    never contend for the same file.
    """
    return Path(user.home) / ".omnigent" / "logs" / "host" / "host-stdio.log"


class _StdioCapture:
    """A pipe whose read end is drained into a bounded, redacted log.

    The host used to be spawned onto ``/dev/null``. Anything it printed outside
    the logging framework — an import error, an unhandled exception on the way
    up, a subprocess complaining — was gone before anyone could ask what
    happened, which is precisely the material a failed start consists of.
    """

    def __init__(self, log, write_fd: int, thread: threading.Thread) -> None:
        self.log = log
        self._write_fd: int | None = write_fd
        self._thread = thread

    @property
    def write_fd(self) -> int:
        assert self._write_fd is not None
        return self._write_fd

    def release(self) -> None:
        """Drop the parent's handle so the drain thread sees EOF at child exit."""
        fd, self._write_fd = self._write_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _start_stdio_capture(user: User) -> _StdioCapture | None:
    from . import diagnostics

    log = diagnostics.BoundedLog(
        host_log_path(user), max_bytes=config.omnigent_host_log_max_bytes()
    )
    try:
        read_fd, write_fd = os.pipe()
    except OSError:
        logger.warning("could not open a host stdio pipe for %s", user.email)
        return None

    def drain() -> None:
        try:
            with os.fdopen(read_fd, "rb") as stream:
                while True:
                    raw = stream.readline(16384)
                    if not raw:
                        break
                    log.write(
                        diagnostics.clip(
                            raw.decode("utf-8", "replace").rstrip("\r\n")
                        )
                    )
        except OSError:
            pass

    thread = threading.Thread(target=drain, daemon=True, name="omnigent-host-stdio")
    thread.start()
    return _StdioCapture(log, write_fd, thread)


def _token_path(user: User) -> Path:
    return Path(user.home) / ".omnigent" / "auth_tokens.json"


def fresh_mirrored_token_exists(
    user: User, server_url: str, now: float | None = None
) -> bool:
    """Check mirror freshness without returning secret material."""
    return _fresh_mirrored_token(user, server_url, now=now) is not None


def _fresh_mirrored_token(
    user: User, server_url: str, now: float | None = None
) -> str | None:
    """Return a fresh attendee-owned token for an internal authenticated probe."""
    now = time.time() if now is None else now
    with user.lock:
        try:
            data = json.loads(_token_path(user).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        entry = data.get(server_url.rstrip("/"))
        if not isinstance(entry, dict):
            return None
        expires_at = entry.get("expires_at")
        valid = (
            isinstance(entry.get("token"), str)
            and bool(entry.get("token"))
            and entry.get("user_id") == user.email.lower()
            and isinstance(expires_at, (int, float))
            and expires_at > now
        )
        return entry["token"] if valid else None


@dataclass
class _Host:
    user: User
    status: str = "waiting_for_token"
    wake: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    process: subprocess.Popen | None = None
    attempts: int = 0
    last_exit_code: int | None = None
    # Last state reported to Control Tower, so health events describe movement
    # rather than the supervisor's polling rate.
    announced_status: str | None = None


class RemoteHostManager:
    """Own exactly one reconnecting Omnigent host process per attendee."""

    def __init__(
        self,
        *,
        user_manager=None,
        popen_factory: Callable = subprocess.Popen,
        binary_resolver: Callable[[], str | None] | None = None,
        random_fn: Callable[[], float] = random.random,
        poll_interval: float = 0.5,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        shutdown_timeout: float = 4.0,
        stale_grace: float | None = None,
        stable_runtime: float | None = None,
        now_fn: Callable[[], datetime] | None = None,
        stdio_capture: Callable[[User], "_StdioCapture | None"] | None = None,
    ) -> None:
        if user_manager is None:
            from .users import user_manager as default_user_manager

            user_manager = default_user_manager
        self._users = user_manager
        self._popen = popen_factory
        self._binary_resolver = binary_resolver or self._resolve_binary
        self._stdio_capture = stdio_capture or _start_stdio_capture
        self._random = random_fn
        self._poll_interval = poll_interval
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._shutdown_timeout = shutdown_timeout
        # Grace before a stale mirror stands the host down. Long enough that a
        # write in flight or a tab mid-poll is not mistaken for a dead
        # credential, short enough that an attendee is gated rather than
        # collecting auth errors.
        self._stale_grace = (
            float(config.omnigent_stale_grace()) if stale_grace is None else stale_grace
        )
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._stable_runtime = (
            float(config.omnigent_host_stable_runtime())
            if stable_runtime is None
            else stable_runtime
        )
        self._hosts: dict[str, _Host] = {}
        self._lock = threading.Lock()
        self._spawn_condition = threading.Condition(self._lock)
        self._spawns_in_progress = 0
        self._started = False
        self._stopping = False
        self._stop_finished = False
        self._shutdown_deadline: float | None = None

    def start(self) -> None:
        # Validate once at startup so a production cleartext/malformed URL
        # fails deployment instead of silently degrading attendee requests.
        config.omnigent_app_url()
        from . import topology
        from .bootstrap import install

        topology.validate_remote_omnigent()
        if config.omnigent_remote_enabled():
            # Reject a malformed hint, but tolerate an absent one: the instance
            # binds its attendee on the first request (see server/attendee.py),
            # so failing startup here would prevent that from ever happening.
            config.workshop_attendee_email()
        install.validate_remote_compatibility()
        with self._lock:
            self._started = True
            self._stopping = False
            self._stop_finished = False
            self._shutdown_deadline = None

    def notify(self, email: str) -> None:
        """Wake or create the attendee worker after a fresh mirror update."""
        email = (email or "").strip().lower()
        if not email:
            return
        try:
            enabled = config.omnigent_remote_enabled()
        except ValueError:
            return
        if not enabled:
            return
        with self._lock:
            if not self._started or self._stopping:
                return
            host = self._hosts.get(email)
        if host is None:
            # Resolving the attendee can re-enter notify() through the deferred
            # OBO flush in UserManager.get(), so it must run with _lock released.
            # The nested call may register the host first; re-read under the
            # lock so both frames converge on one _Host and one worker thread.
            user = self._users.get(email)
            with self._lock:
                if not self._started or self._stopping:
                    return
                host = self._hosts.setdefault(email, _Host(user))
        with self._lock:
            if not self._started or self._stopping:
                return
            host.wake.set()
            if host.thread is None or not host.thread.is_alive():
                host.thread = threading.Thread(
                    target=self._run,
                    args=(email, host),
                    daemon=True,
                    name=f"omnigent-host-{host.user.slug}",
                )
                host.thread.start()

    def status(self, email: str) -> dict:
        try:
            url = config.omnigent_app_url()
        except ValueError:
            return {"enabled": True, "url": "", "status": "error"}
        if not url:
            return {"enabled": False, "url": "", "status": "disabled"}
        with self._lock:
            host = self._hosts.get((email or "").strip().lower())
            state = host.status if host else "waiting_for_token"
            last_exit = host.last_exit_code if host else None
        result = {"enabled": True, "url": url, "status": state}
        if last_exit is not None:
            result["last_exit_code"] = last_exit
        return result

    def readiness(self, email: str) -> dict:
        """Verify the expected host through Omnigent's authenticated host API."""
        normalized = (email or "").strip().lower()
        server_url = config.omnigent_app_url()
        if not server_url:
            return {
                "status": "disabled",
                "connected": False,
                "expected_host_id": "",
            }
        configured = attendee_binding.resolved_email()
        if normalized != configured:
            raise ValueError("readiness requested for the wrong attendee")
        user = self._users.get(configured)
        expected = stable_host_identity(user, server_url).host_id
        with self._lock:
            host = self._hosts.get(configured)
            local_status = host.status if host else "waiting_for_token"
            process_running = bool(
                host
                and host.status == "running"
                and host.process is not None
                and host.process.poll() is None
            )
        result = {
            "status": local_status,
            "connected": False,
            "expected_host_id": expected,
        }
        if not process_running:
            return result
        token = _fresh_mirrored_token(user, server_url)
        if token is None:
            return result
        try:
            response = requests.get(
                f"{server_url}/v1/hosts/{expected}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            if response.status_code != 200:
                return result
            body = response.json()
        except (requests.RequestException, ValueError, TypeError):
            return result
        if body.get("host_id") != expected or body.get("status") != "online":
            return result
        result.update(
            {
                "connected": True,
                "host_id": expected,
                "last_seen_at": (
                    self._now()
                    .astimezone(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
            }
        )
        return result

    def backoff_delay(self, attempt: int) -> float:
        ceiling = min(
            self._backoff_cap,
            self._backoff_base * (2 ** min(30, max(0, attempt))),
        )
        return self._random() * ceiling

    def attempt_after_exit(self, attempt: int, runtime: float) -> int:
        return 0 if runtime >= self._stable_runtime else attempt + 1

    def stop(self) -> None:
        deadline = time.monotonic() + min(14.0, max(0.0, self._shutdown_timeout))
        with self._spawn_condition:
            self._shutdown_deadline = deadline
            self._stopping = True
            self._started = False
            hosts = list(self._hosts.values())
            for host in hosts:
                host.wake.set()
            while self._spawns_in_progress and time.monotonic() < deadline:
                self._spawn_condition.wait(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            processes = [host.process for host in hosts if host.process is not None]
        # Signal every published process first, then use the one absolute
        # shutdown deadline for TERM, KILL, reap, and worker joins.
        live = [process for process in processes if process.poll() is None]
        for process in live:
            self._signal_group(process, signal.SIGTERM)
        remaining = max(0.0, deadline - time.monotonic())
        term_deadline = time.monotonic() + (remaining / 2)
        while live and time.monotonic() < term_deadline:
            live = [process for process in live if process.poll() is None]
            if live:
                time.sleep(min(0.02, max(0.0, term_deadline - time.monotonic())))
        for process in live:
            self._signal_group(process, signal.SIGKILL)
        for process in processes:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except (OSError, subprocess.SubprocessError):
                logger.warning("Omnigent host process could not be reaped")
        with self._lock:
            for host in hosts:
                if host.process in processes:
                    host.process = None
        for host in hosts:
            thread = host.thread
            if thread and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            host.status = "stopped"
        with self._lock:
            self._stop_finished = True

    def _resolve_binary(self) -> str | None:
        from .bootstrap import install

        if not install.ready().get("omnigent", False):
            return None
        candidate = os.path.join(config.shared_prefix(), "bin", "omnigent")
        found = candidate if os.path.isfile(candidate) else shutil.which("omnigent")
        if not found:
            return None
        resolved = os.path.realpath(found)
        return (
            resolved
            if os.path.isabs(resolved) and os.access(resolved, os.X_OK)
            else None
        )

    def _set_status(self, host: _Host, status: str) -> None:
        if status not in REMOTE_HOST_STATES:
            raise ValueError(f"invalid remote host state: {status}")
        with self._lock:
            host.status = status
        self._announce(host)

    def _announce(self, host: _Host) -> None:
        """Emit ``omnigent.host_health`` when this host's state has moved.

        Transitions only. A supervisor re-asserting ``backoff`` every few
        seconds would drown the event stream in exactly the situation an
        operator most needs to read it. Callers that set ``host.status``
        directly under the lock call this afterwards.
        """
        with self._lock:
            status = host.status
            if host.announced_status == status:
                return
            host.announced_status = status
            attempts, exit_code = host.attempts, host.last_exit_code
        from . import telemetry

        telemetry.omnigent_host_health(
            host.user.email,
            status,
            {"attempts": attempts, "last_exit_code": exit_code},
        )

    def _run(self, email: str, host: _Host) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    host.status = "stopped"
                    return
            try:
                server_url = config.omnigent_app_url()
            except ValueError:
                self._set_status(host, "error")
                return
            if not server_url:
                self._set_status(host, "disabled")
                return
            host.wake.clear()
            if not fresh_mirrored_token_exists(host.user, server_url):
                self._set_status(host, "waiting_for_token")
                # The supervisor is the only thing that notices staleness while
                # nobody is clicking, so it is where obo.health gets sampled.
                from .obo import obo_manager

                obo_manager.note_health(host.user.email)
                # Close the check/clear race: if a fresh capture landed between
                # the first check and state update, loop immediately.
                if fresh_mirrored_token_exists(host.user, server_url):
                    continue
                host.wake.wait()
                continue
            binary = self._binary_resolver()
            if not binary:
                self._set_status(host, "error")
                host.wake.clear()
                host.wake.wait(timeout=self._backoff_cap)
                continue

            argv, env, cwd = build_host_launch(host.user, binary, server_url)
            self._set_status(host, "starting")
            with self._spawn_condition:
                if self._stopping:
                    host.status = "stopped"
                    return
                self._spawns_in_progress += 1
            process = None
            capture = self._stdio_capture(host.user)
            stdio = capture.write_fd if capture is not None else subprocess.DEVNULL
            if capture is not None:
                capture.log.write(
                    f"=== host start {self._now().isoformat()} attempt={host.attempts}"
                )
            try:
                process = self._popen(
                    argv,
                    shell=False,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdio,
                    stderr=stdio,
                    start_new_session=True,
                    close_fds=True,
                )
            except (OSError, subprocess.SubprocessError) as error:
                logger.warning(
                    "Omnigent host failed to start for %s: %s",
                    email,
                    error,
                    exc_info=True,
                )
                if capture is not None:
                    capture.log.write(f"=== host spawn failed: {error!r}")
            finally:
                # The child holds its own duplicate; keeping ours open would
                # leave the drain thread waiting for an EOF that never comes.
                if capture is not None:
                    capture.release()
            with self._spawn_condition:
                self._spawns_in_progress -= 1
                self._spawn_condition.notify_all()
                if self._stopping:
                    if process is not None:
                        host.process = process
                    host.status = "stopped"
                    cleanup_after_stop = self._stop_finished
                else:
                    cleanup_after_stop = False
                    host.process = process
                    if process is not None:
                        host.status = "running"
            self._announce(host)
            if cleanup_after_stop and process is not None:
                self._terminate_group(
                    process,
                    deadline=self._shutdown_deadline or time.monotonic(),
                )
                with self._lock:
                    host.process = None
                return

            stood_down = False
            if process is not None:
                started_at = time.monotonic()
                stale_since: float | None = None
                while process.poll() is None:
                    with self._lock:
                        if self._stopping:
                            return
                    # A host running behind an expired mirror is the worst of
                    # the available states: it advertises itself as ready and
                    # fails every session with an auth error the attendee
                    # cannot act on. Better to stand it down and say so.
                    if fresh_mirrored_token_exists(host.user, server_url):
                        stale_since = None
                    else:
                        now = time.monotonic()
                        stale_since = stale_since or now
                        if now - stale_since >= self._stale_grace:
                            logger.warning(
                                "Omnigent host for %s stood down: OBO mirror stale for %.0fs",
                                email,
                                now - stale_since,
                            )
                            self._terminate_group(
                                process, deadline=now + self._shutdown_timeout
                            )
                            stood_down = True
                            break
                    host.wake.clear()
                    host.wake.wait(timeout=self._poll_interval)
                with self._lock:
                    host.last_exit_code = process.poll()
                    host.process = None
                    exit_code = host.last_exit_code
                runtime = time.monotonic() - started_at
                if exit_code:
                    tail = capture.log.tail(2048) if capture is not None else ""
                    logger.warning(
                        "Omnigent host for %s exited %s after %.1fs; last output:\n%s",
                        email,
                        exit_code,
                        runtime,
                        tail.strip() or "<nothing captured>",
                    )
            else:
                runtime = 0.0

            if stood_down:
                # Not a crash: nothing to back off from, and the attempt counter
                # must not be spent on a credential problem. Wait for the next
                # capture to wake us.
                self._set_status(host, "waiting_for_token")
                from .obo import obo_manager

                obo_manager.note_health(email)
                host.wake.clear()
                if fresh_mirrored_token_exists(host.user, server_url):
                    continue
                host.wake.wait()
                continue

            with self._lock:
                if self._stopping:
                    host.status = "stopped"
                    return
                host.status = "backoff"
                attempt = host.attempts
                host.attempts = self.attempt_after_exit(host.attempts, runtime)
                if host.attempts == 0:
                    attempt = 0
            self._announce(host)
            delay = self.backoff_delay(attempt)
            host.wake.clear()
            host.wake.wait(timeout=delay)

    @staticmethod
    def _signal_group(process: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def _terminate_group(self, process: subprocess.Popen, *, deadline: float) -> None:
        if process.poll() is not None:
            try:
                process.wait(timeout=0)
            except (OSError, subprocess.SubprocessError):
                pass
            return
        self._signal_group(process, signal.SIGTERM)
        term_deadline = time.monotonic() + max(0.0, (deadline - time.monotonic()) / 2)
        try:
            process.wait(timeout=max(0.0, term_deadline - time.monotonic()))
            return
        except subprocess.TimeoutExpired:
            pass
        self._signal_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except (OSError, subprocess.SubprocessError):
            logger.warning("Omnigent host process could not be reaped")


remote_host_manager = RemoteHostManager()
