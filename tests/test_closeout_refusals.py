"""Only the retained, pre-input model-policy refusal is a normal completion."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server import event_emitter as emitter_module
from server.capture_journal import CaptureJournal, MemoryCaptureStorage
from server.closeout import CloseoutManager
from server.closeout_http import (
    CloseoutAdmissionMiddleware, _request_outcome,
    retained_model_policy_pending_refusal,
)
from server.event_emitter import EventEmitter


BINDING = {"run_id": "refusal-run", "unit_id": "refusal-unit", "workspace_id": "123",
           "owner": "alice@example.com"}
OWNER = {"X-Forwarded-Email": BINDING["owner"]}


@pytest.fixture
def retained(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    emitter = EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
        ingest_url="", ingest_token="",
        journal=CaptureJournal(MemoryCaptureStorage(), BINDING, {}))
    monkeypatch.setattr(emitter_module, "event_emitter", emitter)
    return emitter


def close(emitter):
    now = [100.0]
    manager = CloseoutManager(emitter, clock=lambda: now[0],
        drainer=lambda _timeout: {"complete": True, "issues": [], "outcomes": []},
        finalizer=lambda *_args: {"complete": True, "issues": [], "outputs": {}})
    manager.begin(capture_id="refusal-capture", stream_id=emitter.stream_id,
                  source=BINDING, drain_timeout_seconds=1)
    now[0] += 2
    return manager.advance()


def test_actual_pending_policy_refusal_retains_diagnostic_and_does_not_hold_closeout(
    client, retained, monkeypatch,
):
    from server import main

    monkeypatch.setenv("WORKSHOP_MODEL_POLICY_REQUIRED", "true")
    monkeypatch.setattr(main, "event_emitter", retained)
    monkeypatch.setattr(main.user_manager, "checkpoint",
        lambda _user: pytest.fail("policy refusal must precede the attendee checkpoint"))
    response = client.post("/api/sessions", headers=OWNER, json={"agent_id": "claude"})
    assert response.status_code == 503
    assert "still syncing" in response.json()["detail"]
    state = retained.journal.read()
    assert state["closeout"]["writer_outcomes"] == []
    assert state["closeout"]["writers"] == {}
    events = list(state["insight"]["events"].values())
    assert len(events) == 1 and events[0]["type"] == "session.create_failed"
    assert events[0]["payload"]["code"] == "model_policy_pending"
    assert close(retained)["complete"] is True


def test_unretained_policy_diagnostic_remains_an_unresolved_failed_writer(
    client, retained, monkeypatch,
):
    from server import main

    monkeypatch.setenv("WORKSHOP_MODEL_POLICY_REQUIRED", "true")
    monkeypatch.setattr(main, "event_emitter", retained)
    monkeypatch.setattr(main.telemetry, "session_create_failed", lambda *_args: False)
    response = client.post("/api/sessions", headers=OWNER, json={"agent_id": "claude"})
    assert response.status_code == 503
    result = close(retained)
    assert result["complete"] is False
    assert "accepted_writer_failed" in result["issues"]
    assert len(result["writer_outcomes"]) == 1


@pytest.mark.parametrize("path,status,partial", [
    ("/api/sessions", 503, False),  # unknown 503; marker absent
    ("/api/other", 503, True),    # marker cannot exempt another route
    ("/api/sessions", 500, True), # marker cannot exempt a server error
])
def test_unknown_503_other_route_and_500_remain_failed(retained, path, status, partial):
    app = FastAPI()
    app.add_middleware(CloseoutAdmissionMiddleware)

    @app.post("/{path:path}")
    def refuse(path: str):
        if partial:
            retained_model_policy_pending_refusal()
        raise HTTPException(status_code=status, detail="synthetic refusal")

    with TestClient(app) as client:
        assert client.post(path).status_code == status
    assert len(retained.journal.read()["closeout"]["writer_outcomes"]) == 1
    assert close(retained)["complete"] is False


def test_marker_does_not_hide_later_exception_or_leak_to_next_request(retained):
    app = FastAPI()
    app.add_middleware(CloseoutAdmissionMiddleware)
    mode = ["known"]

    @app.post("/api/sessions")
    def refuse():
        if mode[0] != "unknown":
            retained_model_policy_pending_refusal()
        if mode[0] == "partial":
            retained.emit("workshop.signal", BINDING["owner"], {"partial": True})
            raise RuntimeError("failed after accepted output")
        raise HTTPException(status_code=503, detail="synthetic refusal")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/api/sessions").status_code == 503
        assert retained.journal.read()["closeout"]["writer_outcomes"] == []
        mode[0] = "unknown"
        assert client.post("/api/sessions").status_code == 503
        mode[0] = "partial"
        assert client.post("/api/sessions").status_code == 500
    assert _request_outcome.get() is None
    result = close(retained)
    assert result["complete"] is False and len(result["writer_outcomes"]) == 2
    assert result["manifest"]["streams"]["insight"]["count"] == 1

