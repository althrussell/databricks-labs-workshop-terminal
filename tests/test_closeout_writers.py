"""Closeout verifies accepted writers rather than just empty registries."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.host_process


def _closing(journal):
    from server.closeout_guard import control_transaction, initial_state

    def apply(state):
        value = state.setdefault("closeout", initial_state())
        value.update(phase="CLOSING", capture_id="writer-test")

    control_transaction(journal, apply)


def _wait(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_existing_terminal_cannot_accept_input_after_close(monkeypatch):
    from server.closeout_guard import CaptureClosedError
    from server.event_emitter import event_emitter
    from server.sessions import Session

    writes = []
    monkeypatch.setattr("server.sessions.os.write", lambda *args: writes.append(args))
    session = Session("alice@example.com", "bash", "Shell", -1, 999999)
    _closing(event_emitter.journal)

    with pytest.raises(CaptureClosedError):
        session.write_input("late input")
    assert writes == []


def test_session_drain_kills_group_and_joins_reader_after_registry_removal(monkeypatch, tmp_path):
    from server import projects
    from server.closeout_writers import process_group_alive
    from server.sessions import SessionManager

    ready = tmp_path / "child-ready"
    child = ("import signal,time,pathlib; "
             "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)")
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    user = SimpleNamespace(email="alice@example.com", home=str(tmp_path), shell_env=lambda: os.environ.copy())
    monkeypatch.setattr(projects, "launch_directory", lambda _user: str(tmp_path))
    monkeypatch.setattr(projects, "read", lambda _user: {"active": ""})
    manager = SessionManager()
    session = manager.create(user, "bash", [sys.executable, "-c", parent], "Shell")
    try:
        _wait(ready.exists)
        # Simulate another termination caller having claimed the public record.
        with manager._lock:
            manager._sessions.pop(session.id)
        result = manager.drain_for_closeout(deadline=time.monotonic() + 3)
        assert result["complete"], result
        assert session._process.poll() is not None
        assert not session._reader_thread.is_alive()
        assert not process_group_alive(session.pid)
    finally:
        try:
            os.killpg(session.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        session._process.wait(timeout=3)
        try:
            os.close(session.master_fd)
        except OSError:
            pass


def test_session_drain_reports_a_reader_still_running(monkeypatch):
    from server.sessions import Session, SessionManager

    release = threading.Event()
    reader = threading.Thread(target=release.wait, daemon=True)
    reader.start()
    session = Session("alice@example.com", "bash", "Shell", -1, 999999)
    session._reader_thread = reader
    manager = SessionManager()
    manager._writers[session.id] = session
    monkeypatch.setattr(manager, "_group_alive", lambda _session: False)
    try:
        result = manager.drain_for_closeout(deadline=time.monotonic() + 0.01)
        assert not result["complete"]
        assert "terminal_reader_pending" in result["issues"]
        assert session.id in manager._writers
    finally:
        release.set()
        reader.join(1)


def test_reader_keeps_descendant_output_after_the_leader_has_been_reaped(monkeypatch, tmp_path):
    from server import projects
    from server.sessions import SessionManager

    ready = tmp_path / "descendant-ready"
    child = ("import signal,time,pathlib,sys\n"
             "def finish(*args):\n print('DESCENDANT_CLOSEOUT_TAIL', flush=True)\n sys.exit(0)\n"
             "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
             "signal.signal(signal.SIGTERM, finish)\n"
             f"pathlib.Path({str(ready)!r}).touch()\n"
             "while True: time.sleep(0.1)\n")
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    user = SimpleNamespace(email="alice@example.com", home=str(tmp_path), shell_env=lambda: os.environ.copy())
    monkeypatch.setattr(projects, "launch_directory", lambda _user: str(tmp_path))
    monkeypatch.setattr(projects, "read", lambda _user: {"active": ""})
    sessions = SessionManager()
    observed = []
    sessions.output_observer = lambda _session, chunk: observed.append(chunk)
    session = sessions.create(user, "claude", [sys.executable, "-c", parent], "Claude")
    try:
        _wait(ready.exists)
        _wait(lambda: session.exit_code == 0)
        assert session._reader_thread.is_alive()
        result = sessions.drain_for_closeout(deadline=time.monotonic() + 3)
        assert result["complete"], result
        assert "DESCENDANT_CLOSEOUT_TAIL" in "".join(observed)
        assert session._reader_done.is_set()
    finally:
        try:
            os.killpg(session.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        session._process.wait(timeout=3)


@pytest.mark.parametrize("fail_exit", [False, True])
def test_live_session_lifecycle_and_shutdown_output_are_in_the_sealed_capture(monkeypatch, tmp_path, fail_exit):
    from server import event_emitter as emitter_module, projects, telemetry
    from server.capture_journal import CaptureJournal, MemoryCaptureStorage
    from server.closeout import CloseoutManager
    from server.event_emitter import EventEmitter
    from server.sessions import SessionManager

    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    binding = {"run_id": "live-run", "unit_id": "live-unit", "workspace_id": "123",
               "owner": "alice@example.com"}
    journal = CaptureJournal(MemoryCaptureStorage(), binding, {})
    source = EventEmitter(run_id=binding["run_id"], workspace_id=binding["workspace_id"],
                          ingest_url="", ingest_token="", journal=journal)
    monkeypatch.setattr(emitter_module, "event_emitter", source)
    user = SimpleNamespace(email=binding["owner"], home=str(tmp_path), shell_env=lambda: os.environ.copy())
    monkeypatch.setattr(projects, "launch_directory", lambda _user: str(tmp_path))
    monkeypatch.setattr(projects, "read", lambda _user: {"active": ""})
    ready = tmp_path / "reader-ready"
    child = ("import signal,time,pathlib,sys\n"
             "def finish(*args):\n print('CLOSEOUT_TAIL', flush=True)\n sys.exit(0)\n"
             "signal.signal(signal.SIGHUP, finish)\n"
             f"pathlib.Path({str(ready)!r}).touch()\n"
             "while True: time.sleep(0.1)\n")
    sessions = SessionManager()

    def observe(session, chunk):
        if "CLOSEOUT_TAIL" in chunk:
            source.emit("milestone.completed", session.owner_email, {"shutdown_tail": True})

    sessions.output_observer = observe
    session = sessions.create(user, "claude", [sys.executable, "-c", child], "Claude")
    try:
        _wait(ready.exists)
        telemetry.session_started(user.email, "claude", session.id)
        assert len(journal.read()["closeout"]["writers"]) == 1
        if fail_exit:
            original_emit = source.emit

            def emit(event_type, *args, **kwargs):
                if event_type == "session.exited":
                    raise OSError("retention failure")
                return original_emit(event_type, *args, **kwargs)

            monkeypatch.setattr(source, "emit", emit)

        def finalize(emitter, capture_id):
            seq = emitter.emit("workshop.signal", user.email, {"final_capture_id": capture_id})
            return {"complete": True, "issues": [], "outputs": {"final_signal": seq}}

        now = [100.0]
        manager = CloseoutManager(source, drainer=lambda timeout: sessions.drain_for_closeout(
            deadline=time.monotonic() + timeout), finalizer=finalize, clock=lambda: now[0])
        manager.begin(capture_id="live-close", stream_id=source.stream_id, source=binding,
                      drain_timeout_seconds=5)
        now[0] += 6
        closed = manager.advance(wait_seconds=3)
        entries = source.collect()["events"]
        types = [item["event"]["type"] for item in entries]
        assert types[0] == "session.started"
        assert "milestone.completed" in types  # Final SIGHUP output crossed the barrier.
        assert journal.read()["closeout"]["writers"] == {}
        if fail_exit:
            assert closed["phase"] == "INCOMPLETE"
            assert "accepted_writer_failed" in closed["issues"]
            assert "session.exited" not in types
        else:
            assert closed["phase"] == "SEALED", closed
            assert types == ["session.started", "milestone.completed", "session.exited", "workshop.signal"]
            exit_payload = entries[2]["event"]["payload"]
            assert exit_payload["code"] == "workshop_closing"
            assert closed["manifest"]["streams"]["insight"]["count"] == 4
    finally:
        sessions.drain_for_closeout(deadline=time.monotonic() + 2)


@pytest.mark.skipif(not sys.platform.startswith("linux") or not os.path.isdir("/proc"),
                    reason="default writer proof requires Linux process census")
def test_default_closeout_drains_real_terminal_and_requires_complete_host_census(monkeypatch, tmp_path):
    from server import discovery, event_emitter as emitter_module, insight_summary, projects, sessions, stats, telemetry
    from server import omnigent_remote
    from server.capture_journal import CaptureJournal, MemoryCaptureStorage
    from server.closeout import CloseoutManager
    from server.event_emitter import EventEmitter
    from server.users import user_manager

    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    binding = {"run_id": "integrated-run", "unit_id": "integrated-unit", "workspace_id": "123",
               "owner": "alice@example.com"}
    journal = CaptureJournal(MemoryCaptureStorage(), binding, {})
    source = EventEmitter(run_id=binding["run_id"], workspace_id=binding["workspace_id"],
                          ingest_url="", ingest_token="", journal=journal)
    monkeypatch.setattr(emitter_module, "event_emitter", source)
    user_manager.configure_capture(journal, binding["owner"])
    discovery.discovery_store.configure_capture(source)
    stats.configure_capture(journal)
    insight_summary.stamps.configure_capture(journal)
    monkeypatch.setattr(insight_summary, "_background_workers", set())
    monkeypatch.setattr(insight_summary, "_background_outcomes", [])
    owner = user_manager.peek(binding["owner"])
    home = tmp_path / "attendee"
    owner.home = str(home)
    owner.first_seen = owner.last_seen = time.time()
    owner.sessions_launched = {"claude": 1}
    (home / "projects").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".claude" / "session.jsonl").write_text(json.dumps({
        "role": "user", "content": "Build our streaming orders pipeline"}) + "\n")
    monkeypatch.setattr(owner, "shell_env", lambda: {**os.environ, "HOME": owner.home})
    monkeypatch.setattr(projects, "launch_directory", lambda _user: str(home / "projects"))
    monkeypatch.setattr(projects, "read", lambda _user: {"active": ""})
    # Only the external workspace API is substituted. Drain stages, census,
    # artifact harvest, final outputs, journal transactions and seal are real.
    counts = {"jobs": 0, "pipelines": 0, "apps": 0, "dashboards": 0}
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census(counts, confirmed=counts))
    local_sessions = sessions.SessionManager()
    monkeypatch.setattr(sessions, "session_manager", local_sessions)
    monkeypatch.setattr(omnigent_remote, "remote_host_manager", omnigent_remote.RemoteHostManager())
    ready = tmp_path / "integrated-ready"
    child = ("import pathlib,time; "
             f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)")
    session = local_sessions.create(owner, "claude", [sys.executable, "-c", child], "Claude")
    try:
        _wait(ready.exists)
        telemetry.session_started(owner.email, "claude", session.id)
        manager = CloseoutManager(source)
        manager.begin(capture_id="integrated-close", stream_id=source.stream_id, source=binding,
                      drain_timeout_seconds=1)
        closed = manager.advance(wait_seconds=0.2)
        # Each call makes bounded progress; the Linux process census can still
        # see a finishing writer on a busy runner. Exercise the caller's normal
        # retry protocol rather than requiring the first request to seal.
        deadline = time.monotonic() + 2
        while closed["phase"] == "CLOSING" and time.monotonic() < deadline:
            time.sleep(0.05)
            closed = manager.advance(wait_seconds=0.1)
        assert session._process.poll() is not None
        assert session._reader_done.is_set()
        assert not session._reader_thread.is_alive()
        assert journal.read()["closeout"]["writers"] == {}
        if closed["issues"] == ["attendee_process_census_unreadable"]:
            # Shared Linux runners can contain same-UID processes whose HOME
            # and cwd cannot be inspected. Verify the required hold explicitly;
            # the isolated packaged HTTP smoke requires an unconditional seal
            # through this same default drain/finalization pipeline.
            assert closed["phase"] == "INCOMPLETE", json.dumps(closed, sort_keys=True)
            assert closed["complete"] is False
            assert closed["active_writers"] == {}
            assert closed["writer_outcomes"] == []
            assert journal.read().get("producer_issues", []) == []
            assert closed["manifest"] is None
            assert closed["manifest_sha256"] is None
            assert closed["final_outputs"] == {}
            outcomes = {row["kind"]: row for row in journal.read()["closeout"]["drain"]["outcomes"]}
            assert set(outcomes) == {"workspace_sync_admission", "terminal_processes",
                                     "paired_host_processes", "identity_workers",
                                     "workspace_sync_workers", "attendee_process_stop",
                                     "attendee_process_census"}
            census = outcomes.pop("attendee_process_census")
            assert all(row["complete"] for row in outcomes.values()), outcomes
            assert census["complete"] is False
            assert census["unreadable"] > 0
            assert census["matched"] == 0
            types = [row["event"]["type"] for row in source.collect()["events"]]
            assert types.count("session.started") == types.count("session.exited") == 1
            assert "insight.summary" not in types
            assert "workshop.signal" not in types
            return
        assert closed["phase"] == "SEALED", json.dumps(closed, sort_keys=True)
        assert closed["complete"] is True
        assert len(closed["manifest"]["drain"]["outcomes"]) >= 7
        assert all(item["complete"] for item in closed["manifest"]["drain"]["outcomes"])
        categories = {row["type"]: row["count"] for row in closed["manifest"]["categories"]}
        assert categories["session.started"] == categories["session.exited"] == 1
        assert categories["insight.summary"] == categories["workshop.signal"] == 1
        assert closed["final_outputs"]["summary_facts"][owner.email]["status"] == "captured"
        assert journal.read()["closeout"]["writers"] == {}
    finally:
        local_sessions.drain_for_closeout(deadline=time.monotonic() + 2)


def test_identity_worker_has_independent_ticket_and_is_drained(monkeypatch):
    from server import identity
    from server.closeout_guard import admitted_writer
    from server.event_emitter import event_emitter

    entered, release = threading.Event(), threading.Event()

    def resolve(user):
        entered.set()
        assert release.wait(3)
        return {"attendee": user.email, "planes": {}, "resolved_at": time.time()}

    monkeypatch.setattr(identity, "resolve", resolve)
    with admitted_writer(event_emitter.journal, "http"):
        identity.observe(SimpleNamespace(email="alice@example.com"))
        assert entered.wait(1)
        assert len(event_emitter.journal.read()["closeout"]["writers"]) == 2
    assert len(event_emitter.journal.read()["closeout"]["writers"]) == 1
    _closing(event_emitter.journal)
    first = identity.drain_for_closeout(deadline=time.monotonic() + 0.01)
    assert first["issues"] == ["identity_workers_pending"]
    release.set()
    final = identity.drain_for_closeout(deadline=time.monotonic() + 2)
    assert final["complete"], final
    assert event_emitter.journal.read()["closeout"]["writers"] == {}
    assert event_emitter.collect()["events"][-1]["event"]["type"] == "identity.resolved"


def test_identity_worker_failure_is_not_reported_drained(monkeypatch):
    from server import identity

    def fail(_user):
        raise RuntimeError("private failure text")

    monkeypatch.setattr(identity, "resolve", fail)
    identity.observe(SimpleNamespace(email="alice@example.com"))
    result = identity.drain_for_closeout(deadline=time.monotonic() + 1)
    assert not result["complete"]
    assert result["issues"] == ["identity_worker_failed"]
    assert "private" not in json.dumps(result)


def test_host_drain_reports_process_and_stdio_uncertainty():
    from server.omnigent_remote import RemoteHostManager

    class Process:
        def poll(self):
            return None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("private-command", timeout)

    hosts = RemoteHostManager()
    process = Process()
    hosts._observed_processes[id(process)] = process
    hosts._captures = [SimpleNamespace(drain=lambda **_kwargs: False)]
    hosts._signal_group = lambda *_args: None
    result = hosts.drain_for_closeout(deadline=time.monotonic() + 0.01)
    assert not result["complete"]
    assert set(result["issues"]) == {"paired_host_process_not_reaped", "paired_host_process_pending", "paired_host_stdio_pending"}


def test_host_drain_stops_descendants_before_waiting_for_stdio_eof(tmp_path):
    from server.omnigent_remote import RemoteHostManager, _StdioCapture

    ready = tmp_path / "host-child-ready"
    child = ("import signal,time,pathlib; "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)")
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    read_fd, write_fd = os.pipe()

    def read_to_eof():
        with os.fdopen(read_fd, "rb") as handle:
            handle.read()

    reader = threading.Thread(target=read_to_eof, daemon=True)
    reader.start()
    capture = _StdioCapture(None, write_fd, reader)
    process = subprocess.Popen([sys.executable, "-c", parent], stdout=write_fd,
                               stderr=write_fd, start_new_session=True)
    capture.release()
    hosts = RemoteHostManager()
    hosts._observed_processes[id(process)] = process
    hosts._captures = [capture]
    try:
        _wait(ready.exists)
        process.wait(timeout=2)
        assert reader.is_alive()
        result = hosts.drain_for_closeout(deadline=time.monotonic() + 2)
        assert result["complete"], result
        assert not reader.is_alive()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=2)


def test_paired_host_shutdown_health_and_stdio_finish_under_accepted_lifetime(monkeypatch, tmp_path):
    from server import omnigent_remote
    from server.event_emitter import event_emitter
    from server.omnigent_remote import RemoteHostManager, host_log_path

    monkeypatch.setenv("OMNIGENT_APP_URL", "https://paired.example.databricksapps.com")
    user = SimpleNamespace(email="alice@example.com", home=str(tmp_path), slug="alice")
    ready = tmp_path / "host-ready"
    child = ("import signal,time,pathlib,sys\n"
             "def finish(*args):\n print('HOST_CLOSEOUT_TAIL', flush=True)\n sys.exit(0)\n"
             "signal.signal(signal.SIGTERM, finish)\n"
             f"pathlib.Path({str(ready)!r}).touch()\n"
             "while True: time.sleep(0.1)\n")
    monkeypatch.setattr(omnigent_remote, "build_host_launch", lambda *_args: (
        [sys.executable, "-c", child], os.environ.copy(), str(tmp_path)))
    monkeypatch.setattr(omnigent_remote, "fresh_mirrored_token_exists", lambda *_args: True)
    monkeypatch.setattr("server.user_content.provision", lambda _user: None)
    hosts = RemoteHostManager(user_manager=SimpleNamespace(get=lambda _email: user),
                              binary_resolver=lambda: sys.executable, poll_interval=0.01)
    hosts._started = True
    hosts.notify(user.email)
    try:
        _wait(ready.exists)
        assert any(item["kind"] == "paired_host_lifetime" for item in
                   event_emitter.journal.read()["closeout"]["writers"].values())
        _closing(event_emitter.journal)
        result = hosts.stop(deadline=time.monotonic() + 3)
        assert result["complete"], result
        assert event_emitter.journal.read()["closeout"]["writers"] == {}
        assert "HOST_CLOSEOUT_TAIL" in host_log_path(user).read_text()
        health = [item["event"]["payload"]["status"] for item in event_emitter.collect()["events"]
                  if item["event"]["type"] == "omnigent.host_health"]
        assert health[-1] == "stopped"
    finally:
        hosts.stop(deadline=time.monotonic() + 2)


def test_sync_barrier_and_receipts_are_bounded_and_legacy_is_visible(tmp_path):
    from server import user_content

    user = SimpleNamespace(home=str(tmp_path))
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    hook = hooks / "post-commit"
    hook.write_text("legacy detached hook")
    result = user_content.closeout_sync_barrier([user], deadline=time.monotonic() + 1)
    assert result["issues"] == ["legacy_workspace_sync_untracked"]
    assert (tmp_path / ".workshop" / "workspace-sync.closed").exists()
    hook.write_text("WORKSHOP_SYNC_WRITER_PROTOCOL=1")
    assert user_content.closeout_sync_barrier([user], deadline=time.monotonic() + 1)["complete"]
    receipts = tmp_path / ".workshop" / "workspace-sync-writers"
    receipts.mkdir()
    receipt = receipts / "pending.json"
    receipt.write_text("{}")
    assert user_content.drain_sync_for_closeout([user], deadline=time.monotonic())["issues"] == ["workspace_sync_workers_pending"]
    receipt.unlink()
    (tmp_path / ".workshop" / "workspace-sync").write_text("at=1760000000\nexit=0\ndetail=\n")
    assert user_content.drain_sync_for_closeout([user], deadline=time.monotonic())["complete"]


def test_real_sync_hook_registers_before_detach_and_respects_close_barrier(tmp_path):
    from server import user_content

    home = tmp_path / "attendee"
    repo = home / "projects" / "demo"
    repo.mkdir(parents=True)
    hooks = home / ".githooks"
    hooks.mkdir()
    state = home / ".workshop"
    state.mkdir()
    hook = hooks / "post-commit"
    hook.write_text(user_content._POST_COMMIT.replace("{email}", "alice@example.com").replace("{status_path}", str(state / "workspace-sync")))
    hook.chmod(0o755)
    shim = tmp_path / "shim"
    shim.mkdir()
    started, release = tmp_path / "started", tmp_path / "release"
    cli = shim / "databricks"
    cli.write_text(f"#!/bin/sh\ntouch '{started}'\nwhile [ ! -f '{release}' ]; do sleep 0.01; done\n")
    cli.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{shim}:{os.environ['PATH']}"}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    subprocess.run([str(hook)], cwd=repo, env=env, check=True)
    user = SimpleNamespace(home=str(home))
    try:
        _wait(started.exists)
        assert user_content.closeout_sync_barrier([user], deadline=time.monotonic() + 1)["complete"]
        pending = user_content.drain_sync_for_closeout([user], deadline=time.monotonic())
        assert pending["pending"] == 1
        assert not pending["complete"]
        release.touch()
        assert user_content.drain_sync_for_closeout([user], deadline=time.monotonic() + 3)["complete"]
        started.unlink()
        subprocess.run([str(hook)], cwd=repo, env=env, check=True)
        assert not started.exists()
        assert not list((state / "workspace-sync-writers").iterdir())
    finally:
        release.touch()


def test_coordinator_keeps_failed_stage_and_continues_with_one_deadline(monkeypatch):
    from server import closeout_writers, identity, user_content
    from server.omnigent_remote import remote_host_manager
    from server.sessions import session_manager

    deadlines = []

    def good(*_args, deadline):
        deadlines.append(deadline)
        return {"complete": True, "issues": []}

    def bad(*_args, deadline):
        deadlines.append(deadline)
        raise OSError("private-path")

    monkeypatch.setattr(user_content, "closeout_sync_barrier", bad)
    monkeypatch.setattr(session_manager, "drain_for_closeout", good)
    monkeypatch.setattr(remote_host_manager, "drain_for_closeout", good)
    monkeypatch.setattr(identity, "drain_for_closeout", good)
    monkeypatch.setattr(user_content, "drain_sync_for_closeout", good)
    monkeypatch.setattr(closeout_writers, "stop_attendee_writers", good)
    monkeypatch.setattr(closeout_writers, "_settled_attendee_census", good)
    result = closeout_writers.drain(1)
    assert not result["complete"]
    assert len(result["outcomes"]) == 7
    assert len(set(deadlines)) == 1
    assert result["issues"] == ["workspace_sync_admission_drain_failed:OSError"]
    assert "private" not in json.dumps(result)


def _busy_seat(monkeypatch, *, unreadable_first=False, clock=None):
    """A closing seat whose writers have stopped and whose /proc census takes 0.4 s."""
    from server import closeout_writers, identity, insight_summary, user_content
    from server.capture_journal import CaptureJournal, MemoryCaptureStorage
    from server.closeout import CloseoutManager
    from server.event_emitter import EventEmitter
    from server.omnigent_remote import remote_host_manager
    from server.sessions import session_manager
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    binding = {"run_id": "busy-run", "unit_id": "busy-unit", "workspace_id": "123",
               "owner": "alice@example.com"}
    journal = CaptureJournal(MemoryCaptureStorage(), binding, {})
    source = EventEmitter(run_id=binding["run_id"], workspace_id=binding["workspace_id"],
                          ingest_url="", ingest_token="", journal=journal)

    def idle(*_args, deadline):
        return {"complete": True, "issues": []}

    # Every writer has already stopped; only the census is left to do real work.
    monkeypatch.setattr(user_content, "closeout_sync_barrier", idle)
    monkeypatch.setattr(session_manager, "drain_for_closeout", idle)
    monkeypatch.setattr(remote_host_manager, "drain_for_closeout", idle)
    monkeypatch.setattr(identity, "drain_for_closeout", idle)
    monkeypatch.setattr(user_content, "drain_sync_for_closeout", idle)
    monkeypatch.setattr(user_manager, "all", lambda: [])
    monkeypatch.setattr(insight_summary, "_background_workers", set())
    monkeypatch.setattr(insight_summary, "_background_outcomes", [])
    censuses = []

    def busy_census(_users, *, deadline, proc_root=None):
        if unreadable_first and not censuses:
            censuses.append("unreadable")
            return {"complete": False, "issues": ["attendee_process_census_unreadable"],
                    "matched": 0, "unreadable": 1}
        # Checks the shared deadline as it goes, like the real /proc scan, but
        # keeps no verdicts: every read pays the whole census again (worst case).
        finish = time.monotonic() + 0.4
        while time.monotonic() < finish:
            if time.monotonic() >= deadline:
                censuses.append("timeout")
                return {"complete": False, "issues": ["attendee_process_census_timeout"],
                        "matched": 0, "unreadable": 0}
            time.sleep(0.01)
        censuses.append("complete")
        return {"complete": True, "issues": [], "matched": 0, "unreadable": 0, "inspected": 400}

    monkeypatch.setattr(closeout_writers, "census_attendee_writers", busy_census)

    def finalize(emitter, capture_id):
        seq = emitter.emit("workshop.signal", binding["owner"], {"final_capture_id": capture_id})
        return {"complete": True, "issues": [], "outputs": {"final_signal": seq}}

    options = {} if clock is None else {"clock": clock}
    manager = CloseoutManager(source, finalizer=finalize, **options)
    return manager, source, binding, censuses


def test_default_status_read_lets_a_busy_seat_finish_its_process_census(monkeypatch):
    """A busy seat's census (this fake keeps no progress) must fit one status read's budget.

    Both admin endpoints call ``advance()`` with its defaults. With a 0.25 s drain
    per read, a seat whose census took longer stayed CLOSING on every read and no
    event seat sealed (``attendee_process_census_timeout``, AKL to ADL, Nov 2026).
    """
    manager, source, binding, censuses = _busy_seat(monkeypatch)
    manager.begin(capture_id="busy-close", stream_id=source.stream_id, source=binding,
                  drain_timeout_seconds=120)
    closed = manager.advance(capture_id="busy-close")
    assert closed["phase"] == "SEALED", json.dumps(closed, sort_keys=True)
    # The first drain and the racing-writer recheck both finish the census.
    assert censuses == ["complete", "complete"]


def test_read_after_the_drain_deadline_still_gets_a_whole_census(monkeypatch):
    """INCOMPLETE resumes on the next read, which needs the same budget to seal."""
    now = [100.0]
    manager, source, binding, censuses = _busy_seat(
        monkeypatch, unreadable_first=True, clock=lambda: now[0])
    manager.begin(capture_id="late-close", stream_id=source.stream_id, source=binding,
                  drain_timeout_seconds=5)
    now[0] += 6
    held = manager.advance(capture_id="late-close")
    assert held["phase"] == "INCOMPLETE"
    assert held["issues"] == ["attendee_process_census_unreadable"]
    closed = manager.advance(capture_id="late-close")
    assert closed["phase"] == "SEALED", json.dumps(closed, sort_keys=True)
    assert censuses == ["unreadable", "complete", "complete"]


def _proc_entry(root, pid, *, home, cwd, state="S"):
    entry = root / str(pid)
    entry.mkdir()
    (entry / "stat").write_text(f"{pid} (private process name) {state} 1 {pid} {pid} 0")
    (entry / "environ").write_bytes(b"TOKEN=private-secret\0HOME=" + os.fsencode(home) + b"\0")
    (entry / "cwd").symlink_to(cwd)
    return entry


def test_process_census_detects_home_and_cwd_detachments_without_leaking_contents(tmp_path):
    from server.closeout_writers import census_attendee_writers

    root = tmp_path / "proc"
    root.mkdir()
    home = tmp_path / "attendee"
    home.mkdir()
    user = SimpleNamespace(home=str(home))
    _proc_entry(root, 60001, home=home, cwd="/tmp")
    _proc_entry(root, 60002, home="/platform", cwd=home / "projects")
    _proc_entry(root, 60003, home=home, cwd=home, state="Z")
    _proc_entry(root, 60004, home="/platform", cwd="/tmp")
    result = census_attendee_writers([user], proc_root=root)
    assert result["matched"] == 2
    assert result["issues"] == ["unmanaged_writer_unverified"]
    assert "private" not in json.dumps(result)
    assert str(home) not in json.dumps(result)


def test_process_census_reports_unreadable_potentially_owned_process(tmp_path):
    from server.closeout_writers import census_attendee_writers

    root = tmp_path / "proc"
    root.mkdir()
    entry = _proc_entry(root, 60001, home="/platform", cwd="/tmp")
    (entry / "environ").unlink()
    result = census_attendee_writers([SimpleNamespace(home="/attendee")], proc_root=root)
    assert result["issues"] == ["attendee_process_census_unreadable"]
    assert result["unreadable"] == 1


def test_process_census_skips_the_supervisor_it_cannot_read(tmp_path, monkeypatch):
    from server import supervisor
    from server.closeout_writers import census_attendee_writers

    root = tmp_path / "proc"
    root.mkdir()
    entry = _proc_entry(root, os.getppid(), home="/platform", cwd="/tmp")
    (entry / "environ").unlink()
    monkeypatch.setenv(supervisor.SUPERVISOR_PID_ENV, str(os.getppid()))
    result = census_attendee_writers([SimpleNamespace(home="/attendee")], proc_root=root)
    assert result["complete"], result
    assert result["unreadable"] == 0

    monkeypatch.delenv(supervisor.SUPERVISOR_PID_ENV)
    assert census_attendee_writers(
        [SimpleNamespace(home="/attendee")], proc_root=root,
    )["issues"] == ["attendee_process_census_unreadable"]


def _proc_entry_started(root, pid, *, home, cwd, start, stack=0x7FFD0000):
    """A Linux process: stat with a start time (field 22) and argument/environment
    addresses (fields 49-52), plus exe and cmdline."""
    entry = root / str(pid)
    entry.mkdir()
    addresses = [str(stack), str(stack + 24), str(stack + 24), str(stack + 96)]
    after_comm = (["S", "1", str(pid), str(pid), "0", "-1", "4194560"] + ["0"] * 12
                  + [str(start)] + ["0"] * 26 + addresses + ["0"])
    (entry / "stat").write_text(f"{pid} (private process name) " + " ".join(after_comm))
    (entry / "environ").write_bytes(b"TOKEN=private-secret\0HOME=" + os.fsencode(home) + b"\0")
    (entry / "cwd").symlink_to(cwd)
    (entry / "exe").symlink_to("/usr/bin/private-binary")
    (entry / "cmdline").write_bytes(b"private-binary\0--flag\0")
    return entry


def test_process_census_keeps_environ_verdicts_by_pid_and_start_time(tmp_path, monkeypatch):
    """A census that outlasts one status read must not start from nothing on the next.

    18 Nov rehearsal: every busy seat's census timed out on every 5 s read and the
    seat never sealed. A process's environment changes only through execve, so its
    HOME verdict is kept for its program image; its cwd can, so cwd is read on every pass.
    """
    from server import closeout_writers
    from server.closeout_writers import census_attendee_writers

    monkeypatch.setattr(closeout_writers, "_ENVIRON_VERDICTS", {})
    monkeypatch.setattr(closeout_writers, "_ENVIRON_HOMES", ())
    root = tmp_path / "proc"
    root.mkdir()
    home = tmp_path / "attendee"
    home.mkdir()
    user = SimpleNamespace(home=str(home))
    attendee = _proc_entry_started(root, 61001, home=home, cwd="/tmp", start=111)
    platform = _proc_entry_started(root, 61002, home="/platform", cwd="/tmp", start=222)

    first = census_attendee_writers([user], proc_root=root)
    assert first["matched"] == 1 and first["environ_reads"] == 2 and first["environ_cached"] == 0
    assert first["processes"] == first["scanned"] == 2

    # Verdicts outlive the files: a later pass reads no environ for the same processes.
    (attendee / "environ").unlink()
    (platform / "environ").unlink()
    second = census_attendee_writers([user], proc_root=root)
    assert second["matched"] == 1 and second["unreadable"] == 0
    assert second["environ_reads"] == 0 and second["environ_cached"] == 2

    # cwd is still read every pass: a cleared process that moved into the home matches.
    (platform / "cwd").unlink()
    (platform / "cwd").symlink_to(home)
    assert census_attendee_writers([user], proc_root=root)["matched"] == 2
    (platform / "cwd").unlink()
    (platform / "cwd").symlink_to("/tmp")

    # A reused pid has a new start time, so its environ is read again (and is missing now).
    (platform / "stat").write_text((platform / "stat").read_text().replace(" 222 ", " 333 "))
    third = census_attendee_writers([user], proc_root=root)
    assert third["environ_reads"] == 0 and third["environ_cached"] == 1
    assert third["unreadable"] == 1 and "attendee_process_census_unreadable" in third["issues"]

    # Another set of attendee homes starts with no verdicts.
    other = tmp_path / "other"
    other.mkdir()
    assert census_attendee_writers([SimpleNamespace(home=str(other))], proc_root=root)["environ_cached"] == 0
    assert "private" not in json.dumps(first) and str(home) not in json.dumps(first)


def test_process_census_rereads_environ_after_exec_and_forgets_exited_processes(tmp_path, monkeypatch):
    """execve replaces the environment but keeps the pid and start time (code review on #166)."""
    from server import closeout_writers
    from server.closeout_writers import census_attendee_writers

    monkeypatch.setattr(closeout_writers, "_ENVIRON_VERDICTS", {})
    monkeypatch.setattr(closeout_writers, "_ENVIRON_HOMES", ())
    root = tmp_path / "proc"
    root.mkdir()
    home = tmp_path / "attendee"
    home.mkdir()
    user = SimpleNamespace(home=str(home))
    launcher = _proc_entry_started(root, 62001, home="/platform", cwd="/tmp", start=500)
    gone = _proc_entry_started(root, 62002, home="/platform", cwd="/tmp", start=600)
    assert census_attendee_writers([user], proc_root=root)["matched"] == 0
    assert len(closeout_writers._ENVIRON_VERDICTS) == 2

    # The launcher execs a new image whose HOME is the attendee's: same pid and start time.
    (launcher / "environ").write_bytes(b"HOME=" + os.fsencode(home) + b"\0")
    (launcher / "exe").unlink()
    (launcher / "exe").symlink_to("/usr/bin/private-tool")
    (launcher / "cmdline").write_bytes(b"private-tool\0")
    after_exec = census_attendee_writers([user], proc_root=root)
    assert after_exec["matched"] == 1 and after_exec["environ_reads"] == 1
    assert "unmanaged_writer_unverified" in after_exec["issues"]

    # Re-exec of the same binary with the same arguments: only the stack moves.
    again = _proc_entry_started(root, 62003, home="/platform", cwd="/tmp", start=700)
    assert census_attendee_writers([user], proc_root=root)["matched"] == 1
    (again / "environ").write_bytes(b"HOME=" + os.fsencode(home) + b"\0")
    stat = (again / "stat").read_text().replace(str(0x7FFD0000), str(0x7FFE0000))
    (again / "stat").write_text(stat)
    after_reexec = census_attendee_writers([user], proc_root=root)
    assert after_reexec["matched"] == 2 and after_reexec["environ_reads"] == 1
    for name in ("stat", "environ", "exe", "cmdline", "cwd"):
        (again / name).unlink()
    again.rmdir()

    # A complete pass drops verdicts for processes that have exited.
    for name in ("stat", "environ", "exe", "cmdline", "cwd"):
        (gone / name).unlink()
    gone.rmdir()
    census_attendee_writers([user], proc_root=root)
    assert {key[1] for key in closeout_writers._ENVIRON_VERDICTS} == {62001}


def test_drain_outcomes_report_where_the_budget_went(monkeypatch):
    from server import closeout_writers, identity, user_content
    from server.omnigent_remote import remote_host_manager
    from server.sessions import session_manager

    def good(*_args, deadline):
        return {"complete": True, "issues": []}

    for target, name in ((user_content, "closeout_sync_barrier"), (session_manager, "drain_for_closeout"),
                         (remote_host_manager, "drain_for_closeout"), (identity, "drain_for_closeout"),
                         (user_content, "drain_sync_for_closeout"), (closeout_writers, "_settled_attendee_census")):
        monkeypatch.setattr(target, name, good)
    result = closeout_writers.drain(1)
    assert result["complete"] and set(result) == {"complete", "issues", "outcomes"}
    assert all(type(row["stage_elapsed_ms"]) is int and row["stage_elapsed_ms"] >= 0
               for row in result["outcomes"])


def test_process_census_never_infers_success_when_unavailable(tmp_path):
    from server.closeout_writers import census_attendee_writers

    result = census_attendee_writers([SimpleNamespace(home="/attendee")], proc_root=tmp_path / "missing")
    assert not result["complete"]
    assert result["issues"] == ["attendee_process_census_unavailable"]


@pytest.mark.parametrize("timeout", [-1, True, float("inf"), float("nan")])
def test_invalid_drain_deadline_is_rejected(timeout):
    from server.closeout_writers import drain

    with pytest.raises(ValueError):
        drain(timeout)


def _stat_line(pid, *, start, state="S", ppid=1):
    # Fields after the comm: state is field 3, so stat field N is fields[N - 3];
    # the parent is field 4 (fields[1]) and the start time field 22 (fields[19]).
    fields = [state, str(ppid), str(pid), str(pid)] + ["0"] * 46
    fields[19] = str(start)
    return f"{pid} (private process name) " + " ".join(fields)


def _running(root, pid, *, home, cwd, start, ppid=1):
    entry = _proc_entry(root, pid, home=home, cwd=cwd)
    (entry / "stat").write_text(_stat_line(pid, start=start, ppid=ppid))
    return entry


def _exit(root, pid):
    (root / str(pid) / "stat").write_text(_stat_line(pid, start=0, state="Z"))


def _stop(users, root, **kwargs):
    from server.closeout_writers import stop_attendee_writers

    kwargs.setdefault("deadline", time.monotonic() + 5)
    return stop_attendee_writers(users, proc_root=root, **kwargs)


def test_closeout_stops_what_the_attendee_left_running_then_reports_counts_only(tmp_path):
    """RAC, 20 Nov 2026: every seat held CLOSING because something the attendee
    left running under their HOME never exited. Closeout now ends it: SIGTERM on
    one read, SIGKILL on a later read once the grace has passed, counts only."""
    root = tmp_path / "proc"
    root.mkdir()
    home = tmp_path / "attendee"
    home.mkdir()
    _running(root, 61001, home=home, cwd="/tmp", start=100)            # exits on SIGTERM
    _running(root, 61002, home="/platform", cwd=home / "app", start=200)  # ignores SIGTERM
    _running(root, 61003, home="/platform", cwd="/tmp", start=300)      # not an attendee's
    sent = []

    def send(pid, sig):
        sent.append((pid, sig))
        if pid == 61001 or sig == signal.SIGKILL:
            _exit(root, pid)

    users = [SimpleNamespace(home=str(home))]
    first = _stop(users, root, send=send, grace_seconds=60)
    assert first["complete"] is False
    assert first["issues"] == ["attendee_process_stop_pending"]
    assert (first["matched"], first["terminated"], first["killed"], first["remaining"]) == (2, 2, 0, 1)
    # Inside the grace a later read sends nothing new.
    assert _stop(users, root, send=send, grace_seconds=60)["issues"] == ["attendee_process_stop_pending"]
    assert sorted(sent) == [(61001, signal.SIGTERM), (61002, signal.SIGTERM)]

    after_grace = _stop(users, root, send=send, grace_seconds=0)
    assert after_grace["complete"] is True, after_grace
    assert (after_grace["killed"], after_grace["remaining"]) == (1, 0)
    assert sent[-1] == (61002, signal.SIGKILL)
    assert "private" not in json.dumps([first, after_grace]) and str(home) not in json.dumps(first)


def test_closeout_never_signals_a_reused_pid_or_one_it_cannot_identify(tmp_path, monkeypatch):
    from server import closeout_writers

    root = tmp_path / "proc"
    root.mkdir()
    home = tmp_path / "attendee"
    home.mkdir()
    _running(root, 61011, home=home, cwd=home, start=999)  # the pid now belongs to another program
    unreadable = _running(root, 61012, home=home, cwd=home, start=500)
    (unreadable / "stat").unlink()
    (unreadable / "stat").mkdir()  # reading it raises an OSError other than "not found"
    _running(root, 61013, home=home, cwd=home, start=700)  # matched with no start time

    def census(_users, *, proc_root, deadline, matches):
        matches.extend([(61011, "100"), (61012, "500"), (61013, "")])
        return {"complete": False, "issues": ["unmanaged_writer_unverified"]}

    monkeypatch.setattr(closeout_writers, "census_attendee_writers", census)
    result = _stop([SimpleNamespace(home=str(home))], root, deadline=time.monotonic() + 1,
                   send=lambda pid, sig: pytest.fail(f"signalled {pid}"), grace_seconds=0)
    # The reused pid is not ours and is not left over; the other two are not called stopped.
    assert result["issues"] == ["attendee_process_stop_incomplete"]
    assert (result["terminated"], result["killed"], result["remaining"]) == (0, 0, 2)


def test_closeout_refuses_to_signal_when_an_attendee_home_would_cover_the_server(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    _running(root, 61021, home="/", cwd="/", start=1)
    for home in ("/", os.getcwd(), os.path.dirname(os.getcwd())):
        result = _stop([SimpleNamespace(home=home)], root, deadline=time.monotonic() + 1,
                       send=lambda *_: pytest.fail("must not signal"))
        assert result["issues"] == ["attendee_process_stop_refused"], home
        assert result["complete"] is False


def test_closeout_never_signals_the_server_its_ancestors_or_its_own_children(tmp_path, monkeypatch):
    """The server, its supervisor and the launcher above it stay up, and an
    in-flight git or CLI call the server started is not an attendee program."""
    from server import closeout_writers

    root = tmp_path / "proc"
    root.mkdir()
    home = tmp_path / "attendee"
    home.mkdir()
    launcher = 61030
    _running(root, launcher, home=home, cwd=home, start=5)
    _running(root, os.getppid(), home=home, cwd=home, start=6, ppid=launcher)
    _running(root, os.getpid(), home=home, cwd=home, start=7, ppid=os.getppid())
    _running(root, 61031, home=home, cwd=home, start=8, ppid=os.getpid())  # the server's git call
    _running(root, 61032, home=home, cwd=home, start=9, ppid=61031)        # and its child

    def census(_users, *, proc_root, deadline, matches):
        matches.extend([(launcher, "5"), (os.getppid(), "6"), (os.getpid(), "7"), (61031, "8"),
                        (61032, "9"), (1, "1")])
        return {"complete": False, "issues": ["unmanaged_writer_unverified"]}

    monkeypatch.setattr(closeout_writers, "census_attendee_writers", census)
    result = _stop([SimpleNamespace(home=str(home))], root, deadline=time.monotonic() + 1,
                   send=lambda *_: pytest.fail("must not signal"), grace_seconds=0)
    assert result["complete"] is True, result
    assert (result["matched"], result["server_children"]) == (0, 2)


def test_closeout_stop_says_when_its_census_did_not_see_everything(tmp_path, monkeypatch):
    from server import closeout_writers

    root = tmp_path / "proc"
    root.mkdir()
    home = tmp_path / "attendee"
    home.mkdir()
    monkeypatch.setattr(closeout_writers, "census_attendee_writers",
                        lambda *_a, **_k: {"complete": False, "issues": ["attendee_process_census_timeout"]})
    result = _stop([SimpleNamespace(home=str(home))], root, send=lambda *_: None)
    # Recorded, not raised again: the census stage reports its own timeout.
    assert result["complete"] is True and result["issues"] == []
    assert result["census_complete"] is False


def test_a_census_cut_off_between_passes_reports_what_it_last_found(monkeypatch):
    """RAC, 20 Nov 2026: 22 of 32 seats reported only ``attendee_process_census_timeout``
    with nothing scanned. The pass before had found an attendee program; say so."""
    from server import closeout_writers

    passes = iter([
        {"complete": False, "issues": ["unmanaged_writer_unverified"], "matched": 2, "scanned": 24},
        {"complete": False, "issues": ["attendee_process_census_timeout"], "matched": 0, "scanned": 0},
    ])
    monkeypatch.setattr(closeout_writers, "census_attendee_writers", lambda *_a, **_k: next(passes))
    result = closeout_writers._settled_attendee_census([], deadline=time.monotonic() + 1)
    assert result["complete"] is False
    assert result["matched"] == 2
    assert result["issues"] == ["attendee_process_census_timeout", "unmanaged_writer_unverified"]


@pytest.mark.skipif(not sys.platform.startswith("linux") or not os.path.isdir("/proc"),
                    reason="stopping real processes needs the Linux process census")
def test_closeout_stops_real_orphaned_programs_including_one_that_ignores_sigterm(tmp_path):
    """Real orphans, as an attendee's detached dev server is: the launcher exits, so
    the programs reparent away from this process, and only HOME ties them to the seat."""
    from server.closeout_writers import census_attendee_writers, stop_attendee_writers

    home = tmp_path / "attendee"
    home.mkdir()
    pids = tmp_path / "pids"
    programs = ["import time; time.sleep(60)",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]
    launcher = ("import subprocess,sys\n"
                f"pids=[subprocess.Popen([sys.executable,'-c',p],start_new_session=True).pid for p in {programs!r}]\n"
                f"open({str(pids)!r},'w').write(' '.join(map(str,pids)))\n")
    subprocess.run([sys.executable, "-c", launcher], cwd=home, env={**os.environ, "HOME": str(home)},
                   check=True, timeout=30)
    orphans = [int(pid) for pid in pids.read_text().split()]

    def alive(pid):
        try:
            return (Path("/proc") / str(pid) / "stat").read_text().rpartition(") ")[2].split()[0] not in {"Z", "X"}
        except FileNotFoundError:
            return False

    try:
        user = SimpleNamespace(home=str(home))
        first = stop_attendee_writers([user], deadline=time.monotonic() + 5)
        assert first["matched"] >= 2 and first["terminated"] >= 2, first
        assert first["issues"] == ["attendee_process_stop_pending"], first  # one ignores SIGTERM
        second = stop_attendee_writers([user], deadline=time.monotonic() + 5, grace_seconds=0)
        assert second["complete"] is True and second["killed"] >= 1, second
        _wait(lambda: not any(alive(pid) for pid in orphans), timeout=5)
        assert census_attendee_writers([user], deadline=time.monotonic() + 5)["matched"] == 0
    finally:
        for pid in orphans:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
