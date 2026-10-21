"""Ordinary restart drains accepted capture without closing the workshop."""

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.capture_journal import (
    CaptureJournal,
    LocalCaptureStorage,
    MemoryCaptureStorage,
)
from server.event_emitter import EventEmitter

BINDING = {"run_id": "restart-run", "unit_id": "restart-unit",
           "workspace_id": "123", "owner": "alice@example.com"}


def _emitter(storage=None):
    return EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                        ingest_url="", ingest_token="", journal=CaptureJournal(
                            storage or MemoryCaptureStorage(), BINDING, {}))


def _wait(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def _idle_services(monkeypatch):
    from server import main

    for service in (main.credential_manager, main.entitlement_manager,
                    main.log_collector, main.obo.obo_watcher):
        monkeypatch.setattr(service, "stop", lambda: None)
    monkeypatch.setattr(main.remote_host_manager, "stop",
                        lambda **_kwargs: {"complete": True, "issues": []})


@pytest.mark.parametrize("fail_checkpoint", [False, True])
def test_lifespan_drains_live_pty_tail_and_retains_checkpoint_failure(monkeypatch, tmp_path, fail_checkpoint):
    from server import event_emitter as emitter_module
    from server import main, projects, telemetry
    from server.closeout import CloseoutManager
    from server.closeout_guard import admitted_writer
    from server.sessions import SessionManager

    source = _emitter()
    monkeypatch.setattr(main, "event_emitter", source)
    monkeypatch.setattr(emitter_module, "event_emitter", source)
    monkeypatch.setattr(main.config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(main.config, "session_state_path", lambda: "")
    monkeypatch.setattr(main.remote_host_manager, "start", lambda: None)
    monkeypatch.setattr(main.obo.obo_watcher, "start", lambda: None)
    monkeypatch.setattr(main, "install_app_error_journal", lambda: None)
    _idle_services(monkeypatch)
    sessions = SessionManager()
    monkeypatch.setattr(main, "session_manager", sessions)
    main.user_manager.configure_capture(source.journal, BINDING["owner"])
    user = main.user_manager.peek(BINDING["owner"])
    Path(user.home).mkdir(parents=True)
    monkeypatch.setattr(projects, "launch_directory", lambda _user: user.home)
    monkeypatch.setattr(projects, "read", lambda _user: {"active": ""})
    ready = tmp_path / "pty-ready"
    child = ("import signal,time,pathlib,sys\n"
             "def finish(*args):\n print('RESTART_TAIL', flush=True)\n sys.exit(0)\n"
             "signal.signal(signal.SIGHUP, finish)\n"
             f"pathlib.Path({str(ready)!r}).touch()\n"
             "while True: time.sleep(.05)\n")

    def observe(session, chunk):
        if "RESTART_TAIL" in chunk:
            user.topic_hits["shutdown_tail"] = 1
            source.emit("milestone.completed", session.owner_email, {"shutdown_tail": True})

    # lifespan installs the production observer, so replace that callable too.
    monkeypatch.setattr(main, "_observe_output", observe)
    if fail_checkpoint:
        monkeypatch.setattr(main.user_manager, "checkpoint",
                            lambda _user: (_ for _ in ()).throw(OSError("offline")))

    async def exercise():
        async with main.lifespan(main.app):
            session = sessions.create(user, "claude", [sys.executable, "-c", child], "Claude")
            telemetry.session_started(user.email, "claude", session.id)
            _wait(ready.exists)
            return session

    session = asyncio.run(exercise())
    assert session._process.poll() is not None
    assert not session._reader_thread.is_alive()
    state = source.journal.read()
    assert state["closeout"]["phase"] == "OPEN"
    assert state["closeout"]["writers"] == {}
    assert not (Path(user.home) / ".workshop" / "workspace-sync.closed").exists()
    events = list(state["insight"]["events"].values())
    assert [event["type"] for event in events] == [
        "session.started", "milestone.completed", "session.exited"]
    assert events[-1]["payload"]["code"] == "shutdown"

    replacement = _emitter(source.journal.storage)
    if fail_checkpoint:
        assert any(item["kind"] == "terminal_lifetime" for item in state["closeout"]["writer_outcomes"])
        now = [100.0]
        manager = CloseoutManager(replacement, drainer=lambda _timeout: {"complete": True, "issues": []},
                                  finalizer=lambda *_args: {"complete": True, "issues": [], "outputs": {}},
                                  clock=lambda: now[0])
        manager.begin(capture_id="after-restart", stream_id=source.stream_id, source=BINDING,
                      drain_timeout_seconds=1)
        now[0] += 2
        held = manager.advance()
        assert held["phase"] == "INCOMPLETE"
        assert "accepted_writer_failed" in held["issues"]
    else:
        assert state["users"][user.email]["topic_hits"] == {"shutdown_tail": 1}
        assert state["closeout"]["writer_outcomes"] == []
        # Replacement admission remains open; restart never seals this source.
        with admitted_writer(replacement.journal, "next-session"):
            replacement.emit("milestone.completed", user.email, {"after_restart": True})


def test_shutdown_deadline_retains_an_unresolved_accepted_summary(monkeypatch):
    from server import event_emitter as emitter_module
    from server import insight_summary, main
    from server.closeout import CloseoutManager
    from server.sessions import SessionManager

    _idle_services(monkeypatch)
    source = _emitter()
    monkeypatch.setattr(main, "event_emitter", source)
    monkeypatch.setattr(emitter_module, "event_emitter", source)
    monkeypatch.setattr(main, "session_manager", SessionManager())
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    entered, release = threading.Event(), threading.Event()

    def slow(*_args, **_kwargs):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(insight_summary, "summarise_all", slow)
    thread = insight_summary.summarise_in_background(
        [SimpleNamespace(email=BINDING["owner"])], phase="building", force=True)
    assert entered.wait(1)
    try:
        started = time.monotonic()
        result = main._shutdown_runtime(None, [], timeout=0.05)
        assert time.monotonic() - started < 0.5
        assert not result["complete"]
        state = source.journal.read()
        assert state["closeout"]["phase"] == "OPEN"
        assert [row["kind"] for row in state["closeout"]["writers"].values()] == ["insight_summary"]
        replacement = _emitter(source.journal.storage)
        now = [100.0]
        manager = CloseoutManager(replacement, drainer=lambda _timeout: {"complete": True, "issues": []},
                                  finalizer=lambda *_args: pytest.fail("interrupted summary was forgotten"),
                                  clock=lambda: now[0])
        manager.begin(capture_id="after-timeout", stream_id=source.stream_id, source=BINDING,
                      drain_timeout_seconds=1)
        now[0] += 2
        held = manager.advance()
        assert held["phase"] == "INCOMPLETE"
        assert "writers_not_drained" in held["issues"]
    finally:
        release.set()
        thread.join(2)


def test_blocked_persistence_cannot_overrun_shutdown_or_erase_its_reservation(monkeypatch):
    from server import main
    from server.sessions import SessionManager

    _idle_services(monkeypatch)
    source = _emitter()
    monkeypatch.setattr(main, "event_emitter", source)
    monkeypatch.setattr(main, "session_manager", SessionManager())
    main.user_manager.configure_capture(source.journal, BINDING["owner"])
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    checkpoint = main.user_manager.checkpoint

    def blocked(user):
        entered.set()
        release.wait(5)
        try:
            checkpoint(user)
        finally:
            completed.set()

    monkeypatch.setattr(main.user_manager, "checkpoint", blocked)
    try:
        started = time.monotonic()
        result = main._shutdown_runtime(None, [], timeout=0.1)
        assert time.monotonic() - started < 0.5
        assert entered.is_set()
        assert result["complete"] is False
        assert result["issues"] == ["shutdown_deadline_exceeded"]
        state = _emitter(source.journal.storage).journal.read()["closeout"]
        assert state["phase"] == "OPEN"
        assert [writer["kind"] for writer in state["writers"].values()] == ["shutdown_checkpoint"]
        assert state["writer_outcomes"] == []
    finally:
        release.set()
        assert completed.wait(2)
        _wait(lambda: not source.journal.read()["closeout"]["writers"])


def test_real_sigterm_drains_active_pty_before_process_exit(tmp_path):
    """Exercise Uvicorn's actual signal/lifespan path with a real child PTY."""
    journal_path = tmp_path / "journal"
    ready = tmp_path / "ready"
    script = tmp_path / "serve.py"
    script.write_text('''
import asyncio, os, pathlib, sys, time
from contextlib import asynccontextmanager
import uvicorn
from server import event_emitter as emitter_module, main, projects, telemetry
from server.capture_journal import CaptureJournal, LocalCaptureStorage
from server.event_emitter import EventEmitter
from server.sessions import SessionManager
binding = ''' + repr(BINDING) + '''
source = EventEmitter(run_id=binding['run_id'], workspace_id=binding['workspace_id'],
    ingest_url='', ingest_token='', journal=CaptureJournal(LocalCaptureStorage(sys.argv[1]), binding, {}))
main.event_emitter = emitter_module.event_emitter = source
main.session_manager = sessions = SessionManager()
main.user_manager.configure_capture(source.journal, binding['owner'])
user = main.user_manager.peek(binding['owner'])
pathlib.Path(user.home).mkdir(parents=True, exist_ok=True)
projects.launch_directory = lambda _user: user.home
projects.read = lambda _user: {'active': ''}
child_ready = pathlib.Path(sys.argv[2] + '-child')
child = "import signal,time,pathlib,sys\\ndef finish(*args):\\n print('SIGTERM_TAIL',flush=True)\\n sys.exit(0)\\nsignal.signal(signal.SIGHUP,finish)\\npathlib.Path(%r).touch()\\nwhile True: time.sleep(.05)\\n" % str(child_ready)
def observe(session, chunk):
    if 'SIGTERM_TAIL' in chunk:
        user.topic_hits['sigterm_tail'] = 1
        source.emit('milestone.completed', user.email, {'shutdown_tail': True})
main._observe_output = observe
original = main.lifespan
@asynccontextmanager
async def active(app):
    async with original(app):
        session = sessions.create(user, 'claude', [sys.executable, '-c', child], 'Claude')
        telemetry.session_started(user.email, 'claude', session.id)
        while not child_ready.exists():
            await asyncio.sleep(.01)
        pathlib.Path(sys.argv[2]).write_text(str(session.pid))
        yield
main.app.router.lifespan_context = active
uvicorn.run(main.app, host='127.0.0.1', port=0, timeout_graceful_shutdown=2, log_level='warning')
''')
    env = {**os.environ, "LOCAL_DEV": "1", "DATA_ROOT": str(tmp_path / "data"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process = subprocess.Popen([sys.executable, str(script), str(journal_path), str(ready)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    child_pid = None
    try:
        _wait(lambda: ready.exists() or process.poll() is not None)
        assert process.poll() is None, process.communicate()
        child_pid = int(ready.read_text())
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=14)
        assert time.monotonic() - started < 14, (stdout, stderr)
        assert process.returncode in {0, -signal.SIGTERM}, (stdout, stderr)
        state = _emitter(LocalCaptureStorage(journal_path)).journal.read()
        assert state["closeout"] == {"schema_version": 1, "phase": "OPEN", "writers": {}, "writer_outcomes": []}
        events = list(state["insight"]["events"].values())
        assert [event["type"] for event in events] == [
            "session.started", "milestone.completed", "session.exited"]
        assert events[-1]["payload"]["code"] == "shutdown"
        assert state["users"][BINDING["owner"]]["topic_hits"] == {"sigterm_tail": 1}
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
