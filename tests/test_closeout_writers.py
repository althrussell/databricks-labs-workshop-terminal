"""Closeout verifies accepted writers rather than just empty registries."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest


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
                      drain_timeout_seconds=10)
        closed = manager.advance(wait_seconds=3)
        # Each call makes bounded progress; the Linux process census can still
        # see a finishing writer on a busy runner. Exercise the caller's normal
        # retry protocol rather than requiring the first request to seal.
        deadline = time.monotonic() + 10
        while closed["phase"] == "CLOSING" and time.monotonic() < deadline:
            time.sleep(0.05)
            closed = manager.advance(wait_seconds=1)
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
                                     "workspace_sync_workers", "attendee_process_census"}
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
        assert len(closed["manifest"]["drain"]["outcomes"]) >= 6
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
    monkeypatch.setattr(closeout_writers, "_settled_attendee_census", good)
    result = closeout_writers.drain(1)
    assert not result["complete"]
    assert len(result["outcomes"]) == 6
    assert len(set(deadlines)) == 1
    assert result["issues"] == ["workspace_sync_admission_drain_failed:OSError"]
    assert "private" not in json.dumps(result)


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
