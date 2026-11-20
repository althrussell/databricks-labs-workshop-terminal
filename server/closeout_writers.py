"""Bounded verification of the existing local workshop writers.

No background runner is created here. The close request calls this after closing
durable admission; all stages share one deadline and return explicit uncertainty.
"""

from __future__ import annotations

import hashlib
import math
import os
import signal
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


def census_attendee_writers(users, *, proc_root: Path | None = None, deadline: float | None = None,
                            matches: list[tuple[int, str]] | None = None) -> dict:
    """Detect detached attendee programs. The census itself signals nothing.

    Only HOME and cwd are compared. No command, environment value, native output,
    attendee identity, or filesystem path is copied into the receipt. ``matches``,
    when given, receives each matched process's pid and start time (stat field 22,
    "" when the stat line has none) for ``stop_attendee_writers``; it never
    leaves the instance.
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
                if matches is not None:
                    matches.append((int(item.name), fields[19] if len(fields) > 19 else ""))
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
    started = time.monotonic()
    previous: dict | None = None
    while True:
        result = census_attendee_writers(users, deadline=deadline)
        if previous is not None and not result["complete"] and not result.get("scanned"):
            # The deadline fell between passes, so this pass looked at nothing.
            # Report what the last pass that looked found: that is why the seat
            # is still closing, not the empty pass's timeout alone (RAC, 20 Nov).
            return {**previous, "complete": False,
                    "issues": sorted(set(previous["issues"]) | set(result["issues"])),
                    "elapsed_ms": round((time.monotonic() - started) * 1000)}
        if result["complete"] or "unmanaged_writer_unverified" not in result["issues"] or time.monotonic() >= deadline:
            return result
        previous = result
        # A detached sync receipt clears just before its shell exits. Give such
        # finishing processes the remaining bounded drain window to disappear.
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


# Closeout forces the seat closed. A program the attendee left running gets
# SIGTERM on one status read, so a dev server can flush, and SIGKILL on a later
# read once the grace has passed. Waiting across reads keeps the grace out of the
# step budget the verification census needs.
_STOP_TERM_WAIT_SECONDS = 0.5
_STOP_GRACE_SECONDS = 2.0
# When each program was asked to stop, keyed by (proc root, pid, start time).
_STOP_REQUESTED: dict[tuple[str, int, str], float] = {}
_STOP_REQUESTED_MAX = 4096


def _stat_fields(root: Path, pid: int) -> list[str] | None:
    """Fields after the comm (stat field N is fields[N - 3]); None once the pid is gone.

    Raises OSError or ValueError when the line exists but cannot be read.
    """
    try:
        return (root / str(pid) / "stat").read_text().rpartition(") ")[2].split()
    except FileNotFoundError:
        return None


def _live_start(root: Path, pid: int) -> str | None:
    """A live process's start time ("" when unknown), or None once it has exited.

    A zombie cannot write, so it counts as exited. A stat line that cannot be
    read counts as live: stopping must never be reported on a guess.
    """
    try:
        fields = _stat_fields(root, pid)
    except (OSError, ValueError):
        return ""
    if fields is None or (fields and fields[0] in {"Z", "X"}):
        return None
    return fields[19] if len(fields) > 19 else ""


def _parent(root: Path, pid: int) -> int | None:
    try:
        fields = _stat_fields(root, pid)
        return int(fields[1]) if fields and len(fields) > 1 else None
    except (OSError, ValueError):
        return None


def _protected_pids(root: Path) -> set[int]:
    """This server, init, and every ancestor of the server (its supervisor included)."""
    protected = {1, os.getpid()}
    pid = os.getppid()
    for _ in range(64):
        if pid is None or pid <= 1 or pid in protected:
            break
        protected.add(pid)
        pid = _parent(root, pid)
    return protected


def _server_descendant(root: Path, pid: int) -> bool:
    """Whether the server started this process (an in-flight git or CLI call).

    Programs an attendee left behind are orphans: they reparent to the
    supervisor, a subreaper, and are no longer the server's descendants.
    """
    server = os.getpid()
    for _ in range(64):
        pid = _parent(root, pid)
        if pid is None or pid <= 1:
            return False
        if pid == server:
            return True
    return False


def _signal_verified(root: Path, pid: int, start: str, sig: int, send) -> bool:
    """Signal ``pid`` only while it is still the program the census matched.

    On Linux a pidfd names one process, so checking its start time and then
    signalling through it cannot hit a pid reused in between.
    """
    if send is None and root == Path("/proc") and hasattr(os, "pidfd_open"):
        try:
            handle = os.pidfd_open(pid)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        try:
            if _live_start(root, pid) != start:
                return False
            signal.pidfd_send_signal(handle, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False
        finally:
            os.close(handle)
    if _live_start(root, pid) != start:
        return False
    try:
        (send or os.kill)(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_attendee_writers(users, *, deadline: float, proc_root: Path | None = None,
                          send=None, grace_seconds: float = _STOP_GRACE_SECONDS,
                          clock=time.monotonic) -> dict:
    """Stop every program still running as an attendee, so closeout can seal.

    CT begins closeout only just before this app is stopped, redeployed or
    deleted, and closeout never reopens. The terminal stage has already ended the
    attendee's sessions; what is left under an attendee HOME or cwd (an orphaned
    ``apx dev`` or vite server, a detached script) would otherwise hold the seal
    for good: no event seat sealed through Nov 2026.

    Never signalled: the server, init, the server's ancestors, the server's own
    child processes, a process whose start time is unknown, and a pid now held
    by another program. The census after this stage still verifies that nothing
    is left. Counts only leave the instance.
    """
    started = clock()
    root = Path("/proc") if proc_root is None else proc_root
    empty = {"matched": 0, "terminated": 0, "killed": 0, "remaining": 0, "server_children": 0}
    homes = [os.path.realpath(user.home) for user in users]
    if not homes:
        return {"complete": True, "issues": [], **empty, "elapsed_ms": 0}
    # Attendee homes sit under DATA_ROOT/users/<slug>, never around this server.
    # A home that is "/" or contains the server's own cwd or HOME would match the
    # platform's processes too: refuse to signal anything, and closeout holds.
    server_places = {os.path.realpath(os.getcwd()), os.path.realpath(os.environ.get("HOME") or os.getcwd())}
    if any(home == os.sep or any(place == home or place.startswith(home + os.sep) for place in server_places)
           for home in homes):
        return {"complete": False, "issues": ["attendee_process_stop_refused"], **empty,
                "elapsed_ms": round((clock() - started) * 1000)}
    found: list[tuple[int, str]] = []
    census = census_attendee_writers(users, proc_root=proc_root, deadline=deadline, matches=found)
    protected = _protected_pids(root)
    candidates = [target for target in found if target[0] not in protected]
    server_children = [target for target in candidates if _server_descendant(root, target[0])]
    targets = [target for target in candidates if target not in server_children]

    def state(target: tuple[int, str]) -> str:
        live = _live_start(root, target[0])
        if live is None:
            return "exited"
        if not target[1] or not live:
            return "unknown"  # never signalled, never reported stopped
        return "running" if live == target[1] else "exited"  # its pid now belongs to another program

    terminated = killed = 0
    now = clock()
    for pid, start in targets:
        if state((pid, start)) != "running":
            continue
        key = (str(root), pid, start)
        asked = _STOP_REQUESTED.get(key)
        if asked is None:
            if _signal_verified(root, pid, start, signal.SIGTERM, send):
                if len(_STOP_REQUESTED) >= _STOP_REQUESTED_MAX:
                    _STOP_REQUESTED.clear()
                _STOP_REQUESTED[key] = now
                terminated += 1
        elif now - asked >= grace_seconds and _signal_verified(root, pid, start, signal.SIGKILL, send):
            killed += 1
    remaining = [target for target in targets if state(target) != "exited"]
    until = min(deadline, clock() + _STOP_TERM_WAIT_SECONDS)
    while remaining and clock() < until:
        time.sleep(0.02)
        remaining = [target for target in remaining if state(target) != "exited"]
    for key in [key for key in _STOP_REQUESTED if key[0] == str(root)
                and state((key[1], key[2])) == "exited"]:
        del _STOP_REQUESTED[key]
    in_grace = [target for target in remaining if (str(root), *target) in _STOP_REQUESTED]
    issues = []
    if in_grace:
        issues.append("attendee_process_stop_pending")  # SIGKILL follows on a later read
    if len(remaining) > len(in_grace):
        issues.append("attendee_process_stop_incomplete")
    if not census["complete"] and set(census["issues"]) - {"unmanaged_writer_unverified"}:
        issues.append("attendee_process_stop_partial")  # the census behind it did not see everything
    return {"complete": not issues, "issues": issues, "matched": len(targets),
            "terminated": terminated, "killed": killed, "remaining": len(remaining),
            "server_children": len(server_children),
            "elapsed_ms": round((clock() - started) * 1000)}


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
        # Closeout is forced: end whatever the attendee left running, then verify.
        ("attendee_process_stop", lambda: stop_attendee_writers(users, deadline=deadline)),
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
