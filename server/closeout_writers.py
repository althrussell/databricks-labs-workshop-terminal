"""Bounded verification of the existing local workshop writers.

No background runner is created here. The close request calls this after closing
durable admission; all stages share one deadline and return explicit uncertainty.
"""

from __future__ import annotations

import hashlib
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


# Whether a process's HOME is an attendee's, keyed by what identifies one program
# image: proc root, pid, start time, the argument and environment addresses, comm,
# exe target and a digest of cmdline. The environment can only change through
# execve, which keeps the pid and start time but lays out a new stack (moved by
# ASLR) and usually a new exe, comm or cmdline, so a verdict is reused only for
# the same image. cwd can change at any time, so it is read on every pass. Reading environ
# is the census's dearest step: on 18 Nov every busy seat's census outlasted a 5 s
# read, restarted from the first process each time, and the seat never sealed.
# With these verdicts a timed-out pass still leaves the next pass less to read.
_ENVIRON_VERDICTS: dict[tuple, bool] = {}
_ENVIRON_HOMES: tuple[str, ...] = ()
_ENVIRON_VERDICTS_MAX = 65536


def _image_key(root: Path, item: Path, stat_text: str, fields: list[str]) -> tuple | None:
    """The program image a HOME verdict belongs to, or None when it can't be told."""
    if len(fields) <= 49:
        return None  # Linux < 3.5 has no argument or environment addresses.
    try:
        exe = os.readlink(item / "exe")
        with (item / "cmdline").open("rb") as handle:
            cmdline = handle.read(4096)
    except OSError:
        return None
    comm = stat_text[stat_text.find("(") + 1:stat_text.rfind(")")]
    # stat field N is fields[N - 3]: start time is field 22; arg_start, arg_end,
    # env_start and env_end are fields 49-52.
    return (str(root), int(item.name), fields[19], tuple(fields[46:50]), comm, exe,
            hashlib.blake2s(cmdline, digest_size=16).hexdigest())


def census_attendee_writers(users, *, proc_root: Path | None = None, deadline: float | None = None) -> dict:
    """Detect detached attendee programs without signalling unowned processes.

    Only HOME and cwd are compared. No command, environment value, native output,
    attendee identity, or filesystem path is copied into the receipt.
    """
    global _ENVIRON_HOMES
    started = time.monotonic()
    homes = [os.path.realpath(user.home) for user in users]
    if not homes:
        return {"complete": True, "issues": [], "matched": 0, "unreadable": 0}
    root = Path("/proc") if proc_root is None else proc_root
    if not root.is_dir():
        return {"complete": False, "issues": ["attendee_process_census_unavailable"],
                "matched": 0, "unreadable": 0}
    if tuple(sorted(homes)) != _ENVIRON_HOMES:
        _ENVIRON_VERDICTS.clear()
        _ENVIRON_HOMES = tuple(sorted(homes))
    matched = unreadable = inspected = scanned = environ_reads = environ_cached = 0
    timed_out = False
    # This server and its supervisor are non-dumpable by design, so unreadable,
    # and neither is an attendee program.
    own = {os.getpid(), supervisor.supervisor_pid()}
    owned_paths: dict[str, bool] = {}
    seen: set[tuple] = set()

    def owned(path: str) -> bool:
        # realpath costs several syscalls, and many processes share a cwd.
        known = owned_paths.get(path)
        if known is None:
            # Linux appends this suffix when a live process retains a deleted cwd.
            canonical = os.path.realpath(path.removesuffix(" (deleted)"))
            known = any(canonical == home or canonical.startswith(home + os.sep) for home in homes)
            owned_paths[path] = known
        return known

    try:
        entries = [item for item in root.iterdir() if item.name.isdigit()]
    except OSError:
        return {"complete": False, "issues": ["attendee_process_census_unreadable"],
                "matched": 0, "unreadable": 1}
    for item in entries:
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        scanned += 1
        if int(item.name) in own:
            continue
        try:
            if item.stat().st_uid != os.getuid():
                continue
            stat_text = (item / "stat").read_text()
            fields = stat_text.rpartition(") ")[2].split()
            if not fields:
                unreadable += 1
                continue
            if fields[0] in {"Z", "X"}:
                continue
            inspected += 1
            uncertain = False
            belongs = False
            key = _image_key(root, item, stat_text, fields)
            if key is not None:
                seen.add(key)
            cached = _ENVIRON_VERDICTS.get(key) if key is not None else None
            if cached is True:
                # Already an attendee's program image: it holds the seat whatever its cwd.
                environ_cached += 1
                belongs = True
            else:
                try:
                    belongs = owned(os.readlink(item / "cwd"))
                except OSError:
                    uncertain = True
            if not belongs and cached is False:
                environ_cached += 1
            elif not belongs:
                try:
                    with (item / "environ").open("rb") as handle:
                        raw = handle.read(262145)
                    environ_reads += 1
                    if len(raw) > 262144:
                        uncertain = True
                    else:
                        home_values = [entry[5:] for entry in raw.split(b"\0") if entry.startswith(b"HOME=")]
                        belongs = any(owned(os.fsdecode(value)) for value in home_values)
                        if key is not None:
                            if len(_ENVIRON_VERDICTS) >= _ENVIRON_VERDICTS_MAX:
                                _ENVIRON_VERDICTS.clear()  # bounded: start over rather than stop caching
                            _ENVIRON_VERDICTS[key] = belongs
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
    if not timed_out:
        # A complete pass saw every live process under this root: forget exited ones.
        for stale in [key for key in _ENVIRON_VERDICTS if key[0] == str(root) and key not in seen]:
            del _ENVIRON_VERDICTS[stale]
    issues = []
    if matched:
        issues.append("unmanaged_writer_unverified")
    if unreadable:
        issues.append("attendee_process_census_unreadable")
    if timed_out:
        issues.append("attendee_process_census_timeout")
    return {"complete": not issues, "issues": issues, "matched": matched,
            "unreadable": unreadable, "inspected": inspected,
            # Counts only: how far this pass got, and how much the verdicts saved.
            "processes": len(entries), "scanned": scanned,
            "environ_reads": environ_reads, "environ_cached": environ_cached,
            "elapsed_ms": round((time.monotonic() - started) * 1000)}


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
        stage_started = time.monotonic()
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
        # Where one read's budget went: CT keeps this in the retained status.
        outcome["stage_elapsed_ms"] = round((time.monotonic() - stage_started) * 1000)
        outcomes.append(outcome)
    return {"complete": not issues and all(row["complete"] for row in outcomes),
            "issues": issues, "outcomes": outcomes}
