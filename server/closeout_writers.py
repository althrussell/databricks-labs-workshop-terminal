"""Bounded verification of the existing local workshop writers.

No background runner is created here. The close request calls this after closing
durable admission; all stages share one deadline and return explicit uncertainty.
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from pathlib import Path

from . import supervisor


def process_group_alive(pgid: int) -> bool:
    """A zombie cannot write; Linux containers can retain adopted zombies."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS can report EPERM for an exited group while adopted children are
        # being reaped. A process census distinguishes zombies from writers;
        # inability to inspect still means uncertainty, never success.
        pass
    proc = Path("/proc")
    if not proc.is_dir():
        try:
            result = subprocess.run(["ps", "-axo", "pgid=,stat="],
                                    capture_output=True, text=True, timeout=0.25)
            if result.returncode != 0:
                return True
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) != 2:
                    return True
                if int(fields[0]) == pgid and fields[1][0] not in {"Z", "X"}:
                    return True
            return False
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return True
    uncertain = False
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        try:
            # comm can contain spaces and parentheses; fields after its final
            # closing parenthesis begin with state, ppid, then process group.
            fields = (item / "stat").read_text().rpartition(") ")[2].split()
            if len(fields) < 3:
                uncertain = True
            elif int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                return True
        except FileNotFoundError:
            continue  # Process exited during the census.
        except (OSError, ValueError):
            uncertain = True
    return uncertain


def census_attendee_writers(users, *, proc_root: Path | None = None, deadline: float | None = None) -> dict:
    """Detect detached attendee programs without signalling unowned processes.

    Only HOME and cwd are compared. No command, environment value, native output,
    attendee identity, or filesystem path is copied into the receipt.
    """
    homes = [os.path.realpath(user.home) for user in users]
    if not homes:
        return {"complete": True, "issues": [], "matched": 0, "unreadable": 0}
    root = Path("/proc") if proc_root is None else proc_root
    if not root.is_dir():
        return {"complete": False, "issues": ["attendee_process_census_unavailable"],
                "matched": 0, "unreadable": 0}
    matched = unreadable = inspected = 0
    timed_out = False
    # This server and its supervisor are non-dumpable by design, so unreadable,
    # and neither is an attendee program.
    own = {os.getpid(), supervisor.supervisor_pid()}

    def owned(path: str) -> bool:
        # Linux appends this suffix when a live process retains a deleted cwd.
        canonical = os.path.realpath(path.removesuffix(" (deleted)"))
        return any(canonical == home or canonical.startswith(home + os.sep) for home in homes)

    try:
        entries = list(root.iterdir())
    except OSError:
        return {"complete": False, "issues": ["attendee_process_census_unreadable"],
                "matched": 0, "unreadable": 1}
    for item in entries:
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        if not item.name.isdigit() or int(item.name) in own:
            continue
        try:
            if item.stat().st_uid != os.getuid():
                continue
            fields = (item / "stat").read_text().rpartition(") ")[2].split()
            if not fields:
                unreadable += 1
                continue
            if fields[0] in {"Z", "X"}:
                continue
            inspected += 1
            uncertain = False
            belongs = False
            try:
                belongs = owned(os.readlink(item / "cwd"))
            except OSError:
                uncertain = True
            try:
                with (item / "environ").open("rb") as handle:
                    raw = handle.read(262145)
                if len(raw) > 262144:
                    uncertain = True
                else:
                    home_values = [entry[5:] for entry in raw.split(b"\0") if entry.startswith(b"HOME=")]
                    belongs = belongs or any(owned(os.fsdecode(value)) for value in home_values)
            except OSError:
                uncertain = True
            if belongs:
                matched += 1
            elif uncertain:
                # A process disappearing during both probes cannot write again.
                if item.exists():
                    unreadable += 1
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            unreadable += 1
    issues = []
    if matched:
        issues.append("unmanaged_writer_unverified")
    if unreadable:
        issues.append("attendee_process_census_unreadable")
    if timed_out:
        issues.append("attendee_process_census_timeout")
    return {"complete": not issues, "issues": issues, "matched": matched,
            "unreadable": unreadable, "inspected": inspected}


def _settled_attendee_census(users, *, deadline: float) -> dict:
    while True:
        result = census_attendee_writers(users, deadline=deadline)
        if result["complete"] or "unmanaged_writer_unverified" not in result["issues"] or time.monotonic() >= deadline:
            return result
        # A detached sync receipt clears just before its shell exits. Give such
        # finishing processes the remaining bounded drain window to disappear.
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def drain(timeout: float) -> dict:
    from . import identity, user_content
    from .omnigent_remote import remote_host_manager
    from .sessions import session_manager
    from .users import user_manager

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("writer drain timeout must be a finite nonnegative number")
    deadline = time.monotonic() + timeout
    outcomes: list[dict] = []
    issues: list[str] = []
    users = user_manager.all()

    # Close the hook's local registration lock before stopping its parent. A
    # detached worker already registered remains visible until its command exits.
    stages = (
        ("workspace_sync_admission", lambda: user_content.closeout_sync_barrier(users, deadline=deadline)),
        ("terminal_processes", lambda: session_manager.drain_for_closeout(deadline=deadline)),
        ("paired_host_processes", lambda: remote_host_manager.drain_for_closeout(deadline=deadline)),
        ("identity_workers", lambda: identity.drain_for_closeout(deadline=deadline)),
        ("workspace_sync_workers", lambda: user_content.drain_sync_for_closeout(users, deadline=deadline)),
        ("attendee_process_census", lambda: _settled_attendee_census(users, deadline=deadline)),
    )
    for name, operation in stages:
        try:
            result = operation()
            if not isinstance(result, dict) or type(result.get("complete")) is not bool:
                raise ValueError("writer returned an invalid drain receipt")
            outcome = {"kind": name, **result}
            for issue in result.get("issues", []):
                if issue not in issues:
                    issues.append(issue)
            if not result["complete"] and not result.get("issues"):
                issues.append(f"{name}_unverified")
        except Exception as error:
            # Do not expose attendee paths, native output, or exception strings.
            issue = f"{name}_drain_failed:{type(error).__name__}"
            issues.append(issue)
            outcome = {"kind": name, "complete": False, "issues": [issue]}
        outcomes.append(outcome)
    return {"complete": not issues and all(row["complete"] for row in outcomes),
            "issues": issues, "outcomes": outcomes}
