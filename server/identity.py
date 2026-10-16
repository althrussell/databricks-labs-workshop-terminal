"""Record which principal each CLI surface actually resolves to, per plane.

Two identities exist by design — the app service principal builds, the attendee
OBO reads — and they are selected by environment variables that differ between a
Workshop Terminal shell and an Omnigent harness. That is fine while it works and
unanswerable when it does not: an agent that deployed an app leaves no trace of
*which* identity deployed it. Error logs cannot help, because a successful
command logs nothing, and the privacy contract keeps PTY scrollback on the box.

So measure it once, at the moment an attendee starts working, by running the
commands the agent will run and recording what they say. The result is an
``identity.resolved`` event: cheap, boring while healthy, and the first thing
worth reading when someone asks who created a resource.

For attribution *after* the instance is gone, this pairs with the workspace
audit log, which records the acting principal for every API call and outlives
the container. ``docs/auth-identity-model.md`` carries the query.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import threading
import time

from . import config
from .capture_journal import CaptureHeldError

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_TTL = 900.0

_lock = threading.Lock()
_snapshots: dict[str, dict] = {}
_in_flight: set[str] = set()
_workers: set[threading.Thread] = set()
_worker_failures: set[str] = set()
_capture_emitter = None


def _identity_of(argv: list[str], env: dict[str, str], cwd: str) -> str:
    """Run one CLI surface and return the principal it authenticated as."""
    try:
        completed = subprocess.run(
            argv,
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"error: {type(error).__name__}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return f"error: {detail[-1][:160] if detail else completed.returncode}"
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return "error: unparseable response"
    # A service principal has no userName in some responses; applicationId is
    # what identifies it, and it is the value that appears in audit logs.
    for key in ("userName", "applicationId", "displayName", "id"):
        value = payload.get(key)
        if value:
            return str(value)
    return "error: response named no principal"


def _plane_environments(user) -> dict[str, dict[str, str]]:
    """The environments the two planes actually give a command.

    The Omnigent plane is read from ``build_host_launch`` rather than
    reconstructed, so this cannot quietly drift from what the host is launched
    with — the drift is exactly what it exists to catch.
    """
    from .omnigent_remote import build_host_launch

    planes = {"workshop_terminal": user.shell_env()}
    try:
        server_url = config.omnigent_app_url()
    except ValueError:
        server_url = ""
    if server_url:
        _, env, _ = build_host_launch(user, "omnigent", server_url)
        planes["omnigent"] = env
    return planes


def resolve(user) -> dict:
    """Measure both planes for ``user``. Blocking; callers use :func:`observe`."""
    cwd = os.path.join(user.home, "projects")
    if not os.path.isdir(cwd):
        cwd = user.home
    planes: dict[str, dict[str, str]] = {}
    for plane, env in _plane_environments(user).items():
        surfaces = {}
        for surface in ("databricks", "databricks-me"):
            binary = os.path.join(user.home, ".local", "bin", surface)
            if not os.path.exists(binary):
                surfaces[surface] = "error: not installed"
                continue
            surfaces[surface] = _identity_of(
                [binary, "current-user", "me", "-o", "json"], env, cwd
            )
        planes[plane] = surfaces
    snapshot = {
        "attendee": user.email,
        "planes": planes,
        "resolved_at": time.time(),
    }
    return snapshot


def _retained_snapshots(state: dict, journal) -> dict:
    try:
        entries = state.get("identity_snapshots", {})
        if not isinstance(entries, dict):
            raise CaptureHeldError("retained identity snapshots are invalid")
        for email, entry in entries.items():
            if (
                not isinstance(email, str)
                or not email
                or not isinstance(entry, dict)
                or set(entry) != {"revision", "snapshot"}
                or type(entry["revision"]) is not int
                or entry["revision"] < 1
            ):
                raise CaptureHeldError("retained identity revision is invalid")
            value = entry["snapshot"]
            if (
                not isinstance(value, dict)
                or set(value) != {"attendee", "planes", "resolved_at"}
                or value["attendee"] != email
                or not isinstance(value["planes"], dict)
                or type(value["resolved_at"]) not in (int, float)
            ):
                raise CaptureHeldError("retained identity snapshot is invalid")
            for surfaces in value["planes"].values():
                if not isinstance(surfaces, dict) or any(
                    not isinstance(name, str) or not isinstance(principal, str)
                    for name, principal in surfaces.items()
                ):
                    raise CaptureHeldError("retained identity planes are invalid")
        return entries
    except CaptureHeldError as error:
        journal.mark_held(error)
        raise


def configure_capture(emitter) -> None:
    """Recover current CLI attribution before serving headless diagnostics."""
    global _capture_emitter
    state = emitter.journal.read()
    entries = _retained_snapshots(state, emitter.journal)
    with _lock:
        _snapshots.clear()
        _snapshots.update(
            {
                email: copy.deepcopy(entry["snapshot"])
                for email, entry in entries.items()
            }
        )
        _capture_emitter = emitter


def reset_for_tests() -> None:
    global _capture_emitter
    with _lock:
        _capture_emitter = None
        _snapshots.clear()
        _in_flight.clear()
        _workers.clear()
        _worker_failures.clear()


def snapshot(email: str) -> dict | None:
    if _capture_emitter is not None:
        entries = _retained_snapshots(
            _capture_emitter.journal.read(), _capture_emitter.journal
        )
        entry = entries.get(email)
        return copy.deepcopy(entry["snapshot"]) if entry is not None else None
    with _lock:
        return copy.deepcopy(_snapshots.get(email))


def all_snapshots() -> list[dict]:
    if _capture_emitter is not None:
        entries = _retained_snapshots(
            _capture_emitter.journal.read(), _capture_emitter.journal
        )
        return [copy.deepcopy(entry["snapshot"]) for entry in entries.values()]
    with _lock:
        return copy.deepcopy(list(_snapshots.values()))


def observe(user) -> None:
    """Refresh ``user``'s identity snapshot in the background, at most per TTL.

    Never blocks a session launch: an attendee waiting on two CLI round trips to
    find out who they are is a worse outcome than a slightly stale answer.
    """
    from .closeout_guard import admitted_writer
    from .event_emitter import event_emitter

    emitter = _capture_emitter or event_emitter
    emitter._ensure_ready()
    email = user.email
    now = time.time()
    try:
        existing = snapshot(email)
    except Exception:
        logger.warning(
            "identity retention could not be read for %s; observation will retry",
            email,
            exc_info=True,
        )
        existing = None
    with _lock:
        if email in _in_flight:
            return
        if existing and now - existing.get("resolved_at", 0.0) < _TTL:
            return
        _in_flight.add(email)

    # Reserve before publishing the thread so a close racing with startup sees
    # this accepted observation, even before the new thread begins executing.
    try:
        ticket = admitted_writer(emitter.journal, "identity", detached=True)
    except Exception:
        with _lock:
            _in_flight.discard(email)
        raise

    def run() -> None:
        try:
            with ticket:
                result = resolve(user)
                _emit(result)
        except Exception:
            # Capture rejection leaves the previously accepted snapshot intact,
            # so the next observation retries rather than caching a false success.
            logger.warning(
                "identity capture was not accepted for %s; retry required",
                email,
                exc_info=True,
            )
            with _lock:
                _worker_failures.add("identity_worker_failed")
        finally:
            with _lock:
                _in_flight.discard(email)
                _workers.discard(threading.current_thread())

    thread = threading.Thread(
        target=run, daemon=True, name=f"identity-resolve-{email[:24]}"
    )
    try:
        with _lock:
            _workers.add(thread)
            thread.start()
    except Exception:
        with _lock:
            _workers.discard(thread)
            _in_flight.discard(email)
            _worker_failures.add("identity_worker_start_failed")
        # The reservation remains visible if its worker could not start.
        raise


def drain_for_closeout(*, deadline: float) -> dict:
    """Wait for accepted observations; do not pretend a daemon has completed."""
    with _lock:
        workers = list(_workers)
    for thread in workers:
        if thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
    with _lock:
        pending = len(_in_flight)
        issues = sorted(_worker_failures)
        if pending:
            issues.append("identity_workers_pending")
    return {"complete": not issues, "issues": issues,
            "workers_observed": len(workers), "pending": pending}


def _emit(result: dict) -> None:
    """Accept snapshot and exact event together; rejection is never cached."""
    from .event_emitter import event_emitter

    emitter = _capture_emitter or event_emitter
    email = str(result.get("attendee") or "system")
    value = copy.deepcopy(result)
    if _capture_emitter is None:
        # Explicit standalone/local mode has no configured recovery journal.
        emitter.emit("identity.resolved", email, {"planes": value.get("planes", {})})
    else:

        def prepare(state):
            entries = _retained_snapshots(state, emitter.journal)
            revision = (entries.get(email) or {}).get("revision", 0) + 1
            state.setdefault("identity_snapshots", {})[email] = {
                "revision": revision,
                "snapshot": value,
            }
            return {
                "planes": value.get("planes", {})
            }, f"resolved:{emitter.run_id}:{email}:{revision}"

        emitter.emit_transaction("identity.resolved", email, prepare)
    with _lock:
        _snapshots[email] = copy.deepcopy(value)
    logger.info(
        "identity resolved for %s: %s",
        email,
        json.dumps(value.get("planes", {}), sort_keys=True),
    )
