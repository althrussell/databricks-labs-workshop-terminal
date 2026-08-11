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

import json
import logging
import os
import subprocess
import threading
import time

from . import config

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_TTL = 900.0

_lock = threading.Lock()
_snapshots: dict[str, dict] = {}
_in_flight: set[str] = set()


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
    with _lock:
        _snapshots[user.email] = snapshot
    return snapshot


def snapshot(email: str) -> dict | None:
    with _lock:
        return dict(_snapshots.get(email) or {}) or None


def all_snapshots() -> list[dict]:
    with _lock:
        return [dict(entry) for entry in _snapshots.values()]


def observe(user) -> None:
    """Refresh ``user``'s identity snapshot in the background, at most per TTL.

    Never blocks a session launch: an attendee waiting on two CLI round trips to
    find out who they are is a worse outcome than a slightly stale answer.
    """
    email = user.email
    now = time.time()
    with _lock:
        existing = _snapshots.get(email)
        if email in _in_flight:
            return
        if existing and now - existing.get("resolved_at", 0.0) < _TTL:
            return
        _in_flight.add(email)

    def run() -> None:
        try:
            result = resolve(user)
        except Exception:  # noqa: BLE001 — attribution must never break a launch
            logger.warning("identity resolution failed for %s", email, exc_info=True)
            return
        finally:
            with _lock:
                _in_flight.discard(email)
        _emit(result)

    threading.Thread(
        target=run, daemon=True, name=f"identity-resolve-{email[:24]}"
    ).start()


def _emit(result: dict) -> None:
    from .event_emitter import event_emitter

    logger.info(
        "identity resolved for %s: %s",
        result.get("attendee"),
        json.dumps(result.get("planes", {}), sort_keys=True),
    )
    try:
        event_emitter.emit(
            "identity.resolved",
            str(result.get("attendee") or "system"),
            {"planes": result.get("planes", {})},
        )
    except Exception:  # noqa: BLE001 — telemetry is never load-bearing
        pass
