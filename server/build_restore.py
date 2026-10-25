"""Bring an attendee's builds back after their container restarts.

A Databricks Apps restart (Control Tower's automatic restart of a crashed
terminal, or an operator's Restart terminal) starts the container with an empty
disk. The attendee's local builds, and the record of which one was current, are
gone; their source is not, because every commit syncs it to
``/Workspace/Users/<email>/projects``. Before this, the first thing they saw was
"No builds yet" until they found Check saved builds, and the natural reading of
that is "my work is lost".

On the attendee's first visit to a fresh container this restores each saved
build missing locally, newest first, through the same non-destructive recovery
as Recover source, and makes the newest one current. It runs once per attendee
per process, in the background: the page load, an agent start and the request
that triggered it never wait for it, and nothing it does raises into them. A
server restart inside a live container (the supervisor) finds the builds still
on disk and does nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
import time
from typing import Callable

from . import projects

logger = logging.getLogger(__name__)

#: How long to wait for the attendee's credentials. A restarted container gets
#: them when the reopened tab mirrors its sign-in, a few seconds after load.
CREDENTIAL_WAIT_SECONDS = 120.0
CREDENTIAL_POLL_SECONDS = 5.0

IN_PROGRESS = frozenset({"checking", "restoring"})


@dataclass
class Restore:
    started_at: float
    state: str = "checking"  # checking | restoring | restored | partial | failed | none
    restored: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    active: str = ""
    saved_at: float | None = None
    finished_at: float | None = None

    def public(self) -> dict | None:
        """What the attendee is told; ``None`` when there is nothing to say.

        "none" covers both a first visit with nothing saved and a check that
        could not run: neither is evidence of a restart, so the attendee hears
        nothing and My builds keeps its manual Check saved builds.
        """
        if self.state == "none":
            return None
        return {"id": f"{self.started_at:.3f}", "state": self.state, "restored": list(self.restored),
                "failed": list(self.failed), "active": self.active, "saved_at": self.saved_at}


class BuildRestorer:
    def __init__(self, *, wait: float = CREDENTIAL_WAIT_SECONDS, poll: float = CREDENTIAL_POLL_SECONDS,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 spawn: Callable[..., None] | None = None) -> None:
        self._wait, self._poll, self._clock, self._sleep = wait, poll, clock, sleep
        self._spawn = spawn or (lambda target, *args: threading.Thread(
            target=target, args=args, name="build-restore", daemon=True).start())
        self._records: dict[str, Restore | None] = {}
        self._lock = threading.Lock()

    def observe(self, user, inventory: dict, *, prepare: Callable[[], None],
                busy: Callable[[], bool], accepting: Callable[[], bool] = lambda: True) -> dict | None:
        """Start a restore the first time this attendee is seen with no builds.

        ``inventory`` is the local one just computed for the request. Seen with
        builds, the attendee is remembered as not needing one, so a later empty
        list (they deleted everything) never triggers a restore. Nothing starts
        once the workshop stops accepting input (closeout), and a running
        restore stops at the next build. Never raises.
        """
        try:
            with self._lock:
                if user.email not in self._records:
                    if inventory.get("projects") or inventory.get("active"):
                        self._records[user.email] = None
                    elif accepting():
                        record = Restore(started_at=self._clock())
                        self._records[user.email] = record
                        self._spawn(self._run, user, record, prepare, busy, accepting)
                    else:
                        return None
                record = self._records[user.email]
                return record.public() if record is not None else None
        except Exception:  # noqa: BLE001 — a status line never fails the build list
            logger.warning("build restore could not start for %s", user.email, exc_info=True)
            return None

    def _ready(self, prepare: Callable[[], None]) -> bool:
        deadline = self._clock() + self._wait
        while True:
            try:
                prepare()
                return True
            except Exception as error:  # noqa: BLE001 — credentials not mirrored yet
                if self._clock() >= deadline:
                    logger.info("build restore gave up waiting for credentials: %s", error)
                    return False
                self._sleep(self._poll)

    def _run(self, user, record: Restore, prepare: Callable[[], None], busy: Callable[[], bool],
             accepting: Callable[[], bool]) -> None:
        try:
            self._restore(user, record, prepare, busy, accepting)
        except Exception:  # noqa: BLE001 — fall back to the manual flow
            logger.warning("build restore failed for %s", user.email, exc_info=True)
            record.state = ("partial" if record.restored else "failed") if record.state == "restoring" else "none"
        finally:
            record.finished_at = self._clock()
            if record.state != "none":
                _emit(user.email, record)

    def _restore(self, user, record: Restore, prepare: Callable[[], None], busy: Callable[[], bool],
                 accepting: Callable[[], bool]) -> None:
        if not self._ready(prepare) or not accepting():
            record.state = "none"
            return
        saved = projects.saved_inventory(user)
        missing = [entry["name"] for entry in saved["projects"] if entry.get("saved") and not entry.get("available")]
        if not missing:
            record.state = "none"
            return
        times: dict[str, float | None] = {}
        for name in missing:
            try:
                times[name] = projects.saved_at(user, name)
            except projects.ProjectError:
                times[name] = None
        missing.sort(key=lambda name: (times[name] or 0.0, name), reverse=True)
        record.state = "restoring"
        for name in missing:
            if not accepting():
                record.failed.append(name)
                continue
            try:
                projects.recover(user, name, activate=False)
                record.restored.append(name)
            except projects.ProjectError as error:
                logger.info("build restore skipped %s for %s: %s", name, user.email, error)
                record.failed.append(name)
        if record.restored:
            newest = record.restored[0]
            record.saved_at = times.get(newest)
            with user.content_lock:
                # The attendee may have chosen or started a build meanwhile; never
                # switch them away from it, or from under a running agent.
                if not projects.read(user)["active"] and not busy():
                    projects.select(user, newest)
                    record.active = newest
            try:
                prepare()
            except Exception:  # noqa: BLE001 — next launch provisions again
                logger.info("build restore could not refresh instructions for %s", user.email, exc_info=True)
        record.state = "restored" if not record.failed else "partial" if record.restored else "failed"


def _emit(email: str, record: Restore) -> None:
    from . import telemetry

    telemetry.emit("selfheal.attempted", email, {
        "reason": "container restart: saved builds",
        "actions": ["restored builds"] if record.restored else [],
        "credential_fresh": True,
        "outcome": record.state,
        "restored": len(record.restored),
        "failed": len(record.failed),
    })


restorer = BuildRestorer()

__all__ = ["BuildRestorer", "IN_PROGRESS", "Restore", "restorer"]
