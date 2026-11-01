"""Ownership isolation, caps, and lifecycle."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from .conftest import ALICE, BOB


@pytest.fixture(autouse=True)
def _test_agent_process(monkeypatch):
    """Use a controllable shell as the process behind a supported test agent."""
    import server.main as main

    original_launch_command = main.agents.launch_command
    monkeypatch.setattr(main, "ensure_user_credentials", lambda _user: None)
    monkeypatch.setattr(main.user_content, "provision", lambda _user: None)
    monkeypatch.setattr(main.identity, "observe", lambda _user: None)
    monkeypatch.setattr(
        main.install,
        "ready",
        lambda: {"claude": True, "codex": True, "omnigent": True},
    )
    monkeypatch.setattr(main.agents, "launch_command", lambda _agent: ["/bin/bash"])
    return original_launch_command


def _create(client, headers, agent="claude"):
    resp = client.post("/api/sessions", json={"agent_id": agent}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["session"]


@pytest.mark.parametrize("wizard_mode", ["skipped", "disabled"])
@pytest.mark.parametrize("agent_id", ["claude", "codex", "omnigent"])
def test_harness_session_does_not_require_onboarding(client, monkeypatch, tmp_path, wizard_mode, agent_id):
    from server import config, wizard
    from server.users import user_manager

    monkeypatch.setenv("OMNIGENT_ENABLED", "true")
    monkeypatch.setenv("WORKSHOP_AGENTS", "claude,codex,omnigent")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "onboarding_wizard_enabled", lambda: wizard_mode != "disabled")
    if wizard_mode == "skipped":
        response = client.post("/api/wizard", json={"operation": "skip", "expected_revision": 0}, headers=ALICE)
        assert response.status_code == 200, response.text
    user = user_manager.get("alice@example.com")
    before = wizard.read_brief(user).to_json()
    session = _create(client, ALICE, agent=agent_id)
    assert session["agent_id"] == agent_id
    with client.websocket_connect(f"/ws/sessions/{session['id']}", headers=ALICE) as ws:
        ws.send_json({"t": "input", "data": "printf 'onboarding-%s\\n' independent\r"})
        output = ""
        for _ in range(50):
            message = ws.receive_json()
            if message.get("t") in {"output", "replay"}:
                output += message.get("data", "")
            if "onboarding-independent" in output:
                break
        assert "onboarding-independent" in output
    assert wizard.read_brief(user).to_json() == before


def test_sessions_are_owner_scoped(client):
    session = _create(client, ALICE)

    mine = client.get("/api/sessions", headers=ALICE).json()["sessions"]
    assert [s["id"] for s in mine] == [session["id"]]

    # Bob can't see, close, or attach to Alice's session.
    assert client.get("/api/sessions", headers=BOB).json()["sessions"] == []
    assert client.delete(f"/api/sessions/{session['id']}", headers=BOB).status_code == 404

    import pytest
    from starlette.websockets import WebSocketDisconnect

    # The socket is accepted first, then closed 4404 (so the code reaches the
    # browser); the disconnect surfaces on the first receive. A wrong owner is
    # indistinguishable from a nonexistent session.
    with client.websocket_connect(f"/ws/sessions/{session['id']}", headers=BOB) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4404


def test_ws_nonexistent_session_closes_4404_after_accept(client):
    """A vanished/unknown session (e.g. after a restart wiped process-local
    PTYs) must close with 4404 AFTER accept, so the client receives the code
    and can stop instead of reconnecting forever."""
    import uuid

    import pytest
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect(f"/ws/sessions/{uuid.uuid4()}", headers=ALICE) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4404


def test_unsafe_session_cap_override_is_rejected(client, monkeypatch):
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "2")
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "4")
    resp = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)
    assert resp.status_code == 503
    assert "MAX_SESSIONS_PER_USER=1" in resp.json()["detail"]


def test_single_app_session_conflict_is_structured(client):
    active = _create(client, ALICE)
    resp = client.post("/api/sessions", json={"agent_id": "codex"}, headers=BOB)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail == {
        "code": "session_conflict",
        "message": "Claude Code is already open — close it before switching agents",
        "active_session": {
            "id": active["id"],
            "agent_id": "claude",
            "label": "Claude Code",
        },
    }
    assert "pid" not in resp.text
    assert "owner" not in resp.text


def test_conflict_precedes_target_readiness_and_credentials(client, monkeypatch):
    import server.main as main

    active = _create(client, ALICE)
    monkeypatch.setattr(main.install, "ready", lambda: {"codex": False})
    monkeypatch.setattr(
        main,
        "ensure_user_credentials",
        lambda _user: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    response = client.post(
        "/api/sessions", json={"agent_id": "codex"}, headers=ALICE
    )
    assert response.status_code == 409
    assert response.json()["detail"]["active_session"]["id"] == active["id"]


def test_terminal_io_and_replay(client):
    session = _create(client, ALICE)
    with client.websocket_connect(f"/ws/sessions/{session['id']}", headers=ALICE) as ws:
        ws.send_text(json.dumps({"t": "input", "data": "echo workshop-$((1+1))\n"}))
        output = ""
        for _ in range(50):
            msg = json.loads(ws.receive_text())
            if msg["t"] in ("output", "replay"):
                output += msg["data"]
            if "workshop-2" in output:
                break
        assert "workshop-2" in output

    # Reattach: scrollback replay includes earlier output.
    with client.websocket_connect(f"/ws/sessions/{session['id']}", headers=ALICE) as ws:
        msg = json.loads(ws.receive_text())
        assert msg["t"] == "replay"
        assert "workshop-2" in msg["data"]


def test_close_session(client):
    session = _create(client, ALICE)
    assert client.delete(f"/api/sessions/{session['id']}", headers=ALICE).status_code == 200
    assert client.get("/api/sessions", headers=ALICE).json()["sessions"] == []


def test_per_user_home_isolation(client):
    from server.users import user_manager

    _create(client, ALICE)
    active = client.get("/api/sessions", headers=ALICE).json()["sessions"][0]
    client.delete(f"/api/sessions/{active['id']}", headers=ALICE)
    _create(client, BOB)
    alice_home = user_manager.get("alice@example.com").home
    bob_home = user_manager.get("bob@example.com").home
    assert alice_home != bob_home
    import os

    assert os.path.isdir(os.path.join(alice_home, "projects"))


def test_unknown_agent_404(client):
    resp = client.post("/api/sessions", json={"agent_id": "nope"}, headers=ALICE)
    assert resp.status_code == 404


@pytest.mark.parametrize("agent_id", ["bash", "pi"])
def test_retired_and_raw_session_types_are_rejected(client, agent_id):
    resp = client.post("/api/sessions", json={"agent_id": agent_id}, headers=ALICE)
    assert resp.status_code == 404


def test_omnigent_obeys_the_same_single_session_contract(client):
    active = _create(client, ALICE, "omnigent")
    assert active["agent_id"] == "omnigent"
    conflict = client.post(
        "/api/sessions", json={"agent_id": "claude"}, headers=ALICE
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["active_session"]["agent_id"] == "omnigent"


def test_reaper_terminates_idle_sessions(client, monkeypatch):
    from server.sessions import session_manager

    session_obj = None
    _create(client, ALICE)
    session_obj = session_manager.list_for("alice@example.com")[0]
    session_obj.last_activity -= 99999  # simulate long idle
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_SECONDS", "3600")

    stale = [
        s for s in session_manager.snapshot()
        if time.time() - s.last_activity > 3600
    ]
    assert session_obj in stale
    session_manager.terminate(session_obj)
    assert session_manager.list_for("alice@example.com") == []


def test_concurrent_creates_spawn_exactly_one_child(monkeypatch, tmp_path):
    from server import sessions as sessions_module
    from server.sessions import SessionConflictError, SessionManager

    manager = SessionManager()
    user = SimpleNamespace(
        email="alice@example.com",
        home=str(tmp_path),
        shell_env=lambda: os.environ.copy(),
    )
    real_popen = sessions_module.subprocess.Popen
    popen_calls = 0
    calls_lock = threading.Lock()

    def counted_popen(*args, **kwargs):
        nonlocal popen_calls
        with calls_lock:
            popen_calls += 1
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(sessions_module.subprocess, "Popen", counted_popen)
    monkeypatch.setattr(sessions_module, "GRACEFUL_SHUTDOWN_WAIT", 0)
    gate = threading.Barrier(2)

    def create(agent_id):
        gate.wait()
        try:
            return manager.create(
                user,
                agent_id,
                ["/bin/sh", "-c", "sleep 30"],
                agent_id.title(),
            )
        except SessionConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["claude", "codex"]))

    assert popen_calls == 1
    assert len([result for result in results if hasattr(result, "pid")]) == 1
    assert len([result for result in results if isinstance(result, SessionConflictError)]) == 1
    manager.terminate(manager.snapshot()[0])


def test_agent_exit_ends_the_session_without_a_shell_fallback(
    monkeypatch, tmp_path, _test_agent_process
):
    from server.sessions import SessionManager

    manager = SessionManager()
    user = SimpleNamespace(
        email="alice@example.com",
        home=str(tmp_path),
        shell_env=lambda: os.environ.copy(),
    )
    command = _test_agent_process({"id": "claude", "command": "printf agent-done"})
    assert command[-1] == "exec printf agent-done"
    manager.create(user, "claude", command, "Claude Code")

    deadline = time.time() + 2
    while manager.count_all() and time.time() < deadline:
        time.sleep(0.01)
    assert manager.count_all() == 0


def test_agent_crash_reports_its_process_exit_code(monkeypatch, tmp_path):
    from server import telemetry
    from server.sessions import SessionManager

    observed = []
    delivered = []
    monkeypatch.setattr(
        telemetry,
        "session_exited",
        lambda *args, **kwargs: observed.append((args, kwargs)),
    )
    manager = SessionManager()
    monkeypatch.setattr(manager, "_fanout", lambda _session, message: delivered.append(message))
    user = SimpleNamespace(
        email="alice@example.com",
        home=str(tmp_path),
        shell_env=lambda: os.environ.copy(),
    )

    manager.create(user, "codex", ["/bin/sh", "-c", "exit 17"], "Codex")
    deadline = time.time() + 2
    while (manager.count_all() or not observed or not delivered) and time.time() < deadline:
        time.sleep(0.01)

    assert manager.count_all() == 0
    assert observed
    assert observed[0][0][2] == "process_error"
    assert observed[0][1]["exit_code"] == 17
    assert delivered == [{"t": "exit", "reason": "process_error", "exit_code": 17, "exit_signal": None, "agent": "codex"}]


def test_pty_eof_before_waitable_exit_retains_the_real_exit_status(monkeypatch):
    """Deterministically model EOF preceding waitpid(WNOHANG) readiness."""
    from server import sessions, telemetry

    observed, signals, waits = [], [], []
    manager = sessions.SessionManager()
    session = sessions.Session("alice@example.com", "codex", "Codex", -1, 999999)
    manager._sessions[session.id] = session
    manager._writers[session.id] = session

    def wait(*, timeout):
        waits.append(timeout)
        return 17

    session._process = SimpleNamespace(wait=wait)
    monkeypatch.setattr(sessions.select, "select", lambda *_args: ([-1], [], []))
    monkeypatch.setattr(sessions.os, "read", lambda *_args: b"")
    monkeypatch.setattr(sessions.os, "waitpid", lambda *_args: (0, 0))
    monkeypatch.setattr(sessions.os, "killpg", lambda *args: signals.append(args))
    monkeypatch.setattr(sessions.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(telemetry, "session_exited",
                        lambda *args, **kwargs: observed.append((args, kwargs)))

    manager._read_pty(session)
    assert observed[0][0][2] == "process_error"
    assert observed[0][1]["exit_code"] == 17
    # The leader is reaped, but anything it left running in its process group
    # (dev servers, watchers) is still swept — the pgid cannot have been reused
    # while those members live.
    assert signals[0] == (session.pid, sessions.signal.SIGHUP)
    assert signals[-1] == (session.pid, sessions.signal.SIGKILL)
    assert waits and 0 < waits[0] <= 0.25


def test_pty_eof_from_a_still_running_leader_keeps_a_bounded_reap(monkeypatch):
    from server import sessions, telemetry

    waits, signals = [], []
    manager = sessions.SessionManager()
    session = sessions.Session("alice@example.com", "codex", "Codex", -1, 999999)
    manager._sessions[session.id] = session
    manager._writers[session.id] = session

    def pending(*, timeout):
        waits.append(timeout)
        raise sessions.subprocess.TimeoutExpired("test-child", timeout)

    session._process = SimpleNamespace(wait=pending)
    monkeypatch.setattr(sessions.select, "select", lambda *_args: ([-1], [], []))
    monkeypatch.setattr(sessions.os, "read", lambda *_args: b"")
    monkeypatch.setattr(sessions.os, "waitpid", lambda *_args: (0, 0))
    monkeypatch.setattr(sessions.os, "killpg", lambda *args: signals.append(args))
    monkeypatch.setattr(sessions.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(telemetry, "session_exited", lambda *_args, **_kwargs: True)

    manager._read_pty(session)
    assert waits and 0 < waits[0] <= 0.25
    assert signals[0] == (session.pid, sessions.signal.SIGHUP)
    assert session.exited


def test_signalled_agent_reports_the_process_signal(monkeypatch, tmp_path):
    from server import telemetry
    from server.sessions import SessionManager

    observed = []
    monkeypatch.setattr(
        telemetry,
        "session_exited",
        lambda *args, **kwargs: observed.append((args, kwargs)),
    )
    manager = SessionManager()
    user = SimpleNamespace(
        email="alice@example.com",
        home=str(tmp_path),
        shell_env=lambda: os.environ.copy(),
    )

    manager.create(user, "codex", ["/bin/sh", "-c", "kill -TERM $$"], "Codex")
    deadline = time.time() + 2
    while (manager.count_all() or not observed) and time.time() < deadline:
        time.sleep(0.01)

    assert manager.count_all() == 0
    assert observed
    assert observed[0][0][2] == "process_signal"
    assert observed[0][1]["process_signal"] == 15


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_only_codex_gets_a_short_home_and_cwd_remains_the_attendee_projects(monkeypatch, tmp_path, agent):
    from server import sessions

    seen = []
    original_popen = sessions.subprocess.Popen

    def spawn(command, **kwargs):
        seen.append(kwargs)
        return original_popen(command, **kwargs)

    monkeypatch.setattr(sessions.subprocess, "Popen", spawn)
    monkeypatch.setattr(sessions, "codex_shell_home", lambda _home: "/tmp/owned-short-alias")
    user = SimpleNamespace(email="alice@example.com", home=str(tmp_path), shell_env=lambda: {**os.environ, "HOME": str(tmp_path)})
    manager = sessions.SessionManager()
    manager.create(user, agent, ["/bin/sh", "-c", "exit 0"], agent)
    deadline = time.time() + 2
    while manager.count_all() and time.time() < deadline:
        time.sleep(0.01)
    assert manager.count_all() == 0
    assert seen[0]["env"]["HOME"] == ("/tmp/owned-short-alias" if agent == "codex" else str(tmp_path))
    assert seen[0]["cwd"] == str(tmp_path / "projects")
    assert "CODEX_HOME" not in seen[0]["env"]


def _until(ws, predicate, limit=200):
    seen = ""
    for _ in range(limit):
        message = ws.receive_json()
        seen += message.get("data", "") if message.get("t") in {"output", "replay"} else ""
        if predicate(message, seen):
            return seen
    raise AssertionError("expected message never arrived")


def test_a_terminal_keepalive_is_presence_not_activity(client):
    """Pings keep the session from being reaped and the attendee online, but
    only typing or output counts as activity for idle cards."""
    from server.sessions import session_manager
    from server.users import user_manager

    session = _create(client, ALICE)
    live = session_manager.get(session["id"], "alice@example.com")
    user = user_manager.get("alice@example.com")
    with client.websocket_connect(f"/ws/sessions/{session['id']}", headers=ALICE) as ws:
        ws.send_json({"t": "input", "data": "printf 'quiet-%s\\n' now\r"})
        _until(ws, lambda _m, seen: "quiet-now" in seen)
        time.sleep(0.3)
        ws.send_json({"t": "ping"})
        _until(ws, lambda m, _s: m.get("t") == "pong")
        live.last_io = live.last_activity = user.last_seen = user.last_active = 1000.0

        ws.send_json({"t": "ping"})
        _until(ws, lambda m, _s: m.get("t") == "pong")

        assert live.last_activity > 1000.0 and user.last_seen > 1000.0
        assert live.last_io == 1000.0 and user.last_active == 1000.0

        ws.send_json({"t": "input", "data": "printf 'typed-%s\\n' now\r"})
        _until(ws, lambda _m, seen: "typed-now" in seen)
        assert live.last_io > 1000.0


def test_an_events_keepalive_is_presence_not_activity(client):
    from server.users import user_manager

    with client.websocket_connect("/ws/events", headers=ALICE) as ws:
        ws.send_text('{"t":"ping"}')
        _until(ws, lambda m, _s: m.get("t") == "pong")
        user = user_manager.get("alice@example.com")
        assert user.last_active > 0  # opening WT is the attendee doing something
        user.last_seen = user.last_active = 1000.0

        ws.send_text('{"t":"ping"}')
        _until(ws, lambda m, _s: m.get("t") == "pong")

        assert user.last_seen > 1000.0
        assert user.last_active == 1000.0


def test_natural_exit_leaves_no_background_servers_behind(tmp_path):
    """An agent that exits cleanly after starting a dev server used to leave it
    running for the rest of the day; the whole process group is now reaped."""
    import signal as _signal
    import subprocess

    from server.sessions import SessionManager

    marker = tmp_path / "orphan.pid"
    manager = SessionManager()
    user = SimpleNamespace(
        email="alice@example.com",
        home=str(tmp_path),
        shell_env=lambda: os.environ.copy(),
    )
    # The shell starts a long sleeper in the same process group, records its pid
    # and exits 0. The sleeper does not hold the PTY — like a dev server started
    # by an agent's tool, which gets pipes — so the session sees EOF and ends.
    # (A descendant that keeps the slave open deliberately keeps the session
    # alive instead; see _read_pty.)
    script = f"sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {marker}; exit 0"
    manager.create(user, "claude", ["/bin/sh", "-c", script], "Claude")

    deadline = time.time() + 5
    while (manager.count_all() or not marker.exists()) and time.time() < deadline:
        time.sleep(0.02)
    assert manager.count_all() == 0
    orphan = int(marker.read_text().strip())

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        # A zombie still answers kill(0); check its state via ps.
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")

    deadline = time.time() + 5
    while alive(orphan) and time.time() < deadline:
        time.sleep(0.05)
    try:
        assert not alive(orphan), "background sleeper survived the session"
    finally:
        try:
            os.kill(orphan, _signal.SIGKILL)
        except ProcessLookupError:
            pass
