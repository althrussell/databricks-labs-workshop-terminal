"""Attendees see only input availability; existing durable closeout stays authoritative."""

import copy
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from server.capture_journal import CaptureJournal, CaptureStorageError, MemoryCaptureStorage
from server.closeout_guard import control_transaction, initial_state
from server.event_emitter import EventEmitter

OWNER = {"X-Forwarded-Email": "alice@example.com"}
BINDING = {"run_id": "lifecycle-run", "unit_id": "lifecycle-unit", "workspace_id": "123", "owner": "alice@example.com"}


@pytest.fixture
def source(client, monkeypatch):
    from server import event_emitter, main

    emitter = EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                           ingest_url="", ingest_token="",
                           journal=CaptureJournal(MemoryCaptureStorage(), BINDING, {}))
    monkeypatch.setattr(event_emitter, "event_emitter", emitter)
    monkeypatch.setattr(main, "event_emitter", emitter)
    return emitter


@pytest.mark.parametrize("phase,state", [(None, "open"), ("OPEN", "open"),
    ("CLOSING", "closing"), ("INCOMPLETE", "closing"), ("SEALED", "closed")])
def test_attendee_status_is_small_read_only_and_never_advances_closeout(client, source, phase, state):
    if phase:
        control_transaction(source.journal, lambda data: data.update(closeout={
            **initial_state(), "phase": phase, "capture_id": "private-operation",
            "issues": ["private-operator-diagnostic"], "generation": "private-generation"}))
    before = source.journal.read()
    for _ in range(2):
        response = client.get("/api/workshop-status", headers=OWNER)
        assert response.status_code == 200
        assert response.json() == {"state": state, "accepting_input": state == "open"}
    assert source.journal.read() == before
    assert client.get("/api/config", headers=OWNER).json()["workshop"] == response.json()


def test_lifecycle_read_requires_normal_attendee_authentication(client, source, monkeypatch):
    monkeypatch.delenv("LOCAL_DEV")
    assert client.get("/api/workshop-status").status_code == 403


@pytest.mark.parametrize("invalid", [False, True])
def test_unavailable_or_invalid_lifecycle_never_reports_open(client, source, monkeypatch, invalid):
    state = copy.deepcopy(source.journal.read())
    state["closeout"] = {"schema_version": 2}

    def read():
        if invalid:
            return state
        raise CaptureStorageError("private storage identifier")

    monkeypatch.setattr(source.journal, "read", read)
    response = client.get("/api/workshop-status", headers=OWNER)
    assert response.status_code == 503
    assert response.json() == {"detail": "Workshop is temporarily unavailable. Please retry shortly."}


@pytest.mark.parametrize("path,method,body", [
    ("/api/sessions", "post", {"agent_id": "claude"}),
    ("/api/sessions/example/type", "post", {"text": "late input"}),
    ("/api/sessions/example", "delete", None),
    ("/api/profile", "post", {"expected_revision": 0, "help_preference": "guided"}),
    ("/api/wizard", "post", {"operation": "skip", "expected_revision": 0}),
    ("/api/help/messages", "post", {"body": "late help"}),
    ("/api/help/raise", "post", {"note": "late hand"}),
    ("/api/projects/select", "post", {"name": "late build", "new": True}),
    ("/api/certificate?name=Alice", "get", None),
])
def test_closed_actions_use_one_attendee_safe_refusal(client, source, path, method, body):
    control_transaction(source.journal, lambda data: data.update(closeout={
        **initial_state(), "phase": "CLOSING"}))
    before = source.journal.read()
    kwargs = {"headers": OWNER, **({"json": body} if body is not None else {})}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "workshop_closing",
        "message": "This workshop is closing. New input is disabled."}}
    assert source.journal.read() == before


@pytest.mark.parametrize("path", ["/ws/events", "/ws/sessions/existing-session"])
def test_new_sockets_get_the_terminal_closing_code(client, source, monkeypatch, path):
    from server import ws

    monkeypatch.setattr(ws.session_manager, "get", lambda *_args: object())
    control_transaction(source.journal, lambda data: data.update(closeout={
        **initial_state(), "phase": "CLOSING"}))
    before = source.journal.read()
    with client.websocket_connect(path, headers=OWNER) as socket:
        with pytest.raises(WebSocketDisconnect) as refused:
            socket.receive_text()
        assert refused.value.code == 4410
        assert refused.value.reason == "workshop is closing"
    assert source.journal.read() == before


def test_default_docs_and_configured_pack_links_remain_distinct(client, as_admin, monkeypatch, tmp_path):
    from server import content

    monkeypatch.delenv("CONTENT_PACK_PATH", raising=False)
    pack = content.ContentService().pack
    assert next(link.url for link in pack.shell.links if link.label == "AI Docs") == "https://naveenb83.github.io/ai-dev-day/"
    custom = pack.model_dump()
    custom["shell"]["links"] = [{"label": "AI Docs", "url": "https://docs.example.com/custom-workshop/"}]
    path = tmp_path / "custom-pack.json"
    path.write_text(json.dumps(custom))
    monkeypatch.setenv("CONTENT_PACK_PATH", str(path))
    assert content.ContentService().pack.shell.links[0].url == custom["shell"]["links"][0]["url"]
    assert client.post("/api/admin/content-pack", json=custom, headers=OWNER).status_code == 200
    assert client.get("/api/config", headers=OWNER).json()["shell"]["links"][0]["url"] == custom["shell"]["links"][0]["url"]
