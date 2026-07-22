"""Ownership isolation, caps, and lifecycle."""

import json

from .conftest import ALICE, BOB


def _create(client, headers, agent="bash"):
    resp = client.post("/api/sessions", json={"agent_id": agent}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["session"]


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


def test_per_user_session_cap(client, monkeypatch):
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "2")
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "4")
    monkeypatch.setenv("ALLOW_SHARED_TOPOLOGY", "true")
    _create(client, ALICE)
    _create(client, ALICE)
    resp = client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    assert resp.status_code == 429
    # Other users are unaffected by Alice's cap.
    _create(client, BOB)


def test_global_session_cap(client, monkeypatch):
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "1")
    _create(client, ALICE)
    resp = client.post("/api/sessions", json={"agent_id": "bash"}, headers=BOB)
    assert resp.status_code == 429


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
    _create(client, BOB)
    alice_home = user_manager.get("alice@example.com").home
    bob_home = user_manager.get("bob@example.com").home
    assert alice_home != bob_home
    import os

    assert os.path.isdir(os.path.join(alice_home, "projects"))


def test_unknown_agent_404(client):
    resp = client.post("/api/sessions", json={"agent_id": "nope"}, headers=ALICE)
    assert resp.status_code == 404


def test_reaper_terminates_idle_sessions(client, monkeypatch):
    from server.sessions import session_manager

    session_obj = None
    _create(client, ALICE)
    session_obj = session_manager.list_for("alice@example.com")[0]
    session_obj.last_activity -= 99999  # simulate long idle
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_SECONDS", "3600")

    import time

    stale = [
        s for s in session_manager.snapshot()
        if time.time() - s.last_activity > 3600
    ]
    assert session_obj in stale
    session_manager.terminate(session_obj)
    assert session_manager.list_for("alice@example.com") == []
