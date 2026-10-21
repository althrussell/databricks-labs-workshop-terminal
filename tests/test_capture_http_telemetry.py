"""A swallowed operational emit cannot erase an accepted HTTP writer on restart."""

from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import event_emitter as emitter_module, telemetry
from server.auth import Principal
from server.capture_journal import CaptureJournal, LocalCaptureStorage
from server.closeout import CloseoutManager
from server.closeout_http import CloseoutAdmissionMiddleware, _request_outcome
from server.event_emitter import EventEmitter


BINDING = {"run_id": "http-capture-run", "unit_id": "http-capture-unit",
           "workspace_id": "123", "owner": "alice@example.com"}
OWNER = {"X-Forwarded-Email": BINDING["owner"]}


class FaultStore(LocalCaptureStorage):
    failures = 0
    failed_creates = 0

    def create(self, name, content):
        if self.failures:
            self.failures -= 1
            self.failed_creates += 1
            raise TimeoutError("temporary storage outage")
        return super().create(name, content)


def emitter(storage):
    return EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                        ingest_url="", ingest_token="",
                        journal=CaptureJournal(storage, BINDING, {}))


@pytest.fixture
def retained(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setattr("server.capture_journal.time.sleep", lambda _delay: None)
    store = FaultStore(tmp_path / "journal")
    source = emitter(store)
    monkeypatch.setattr(emitter_module, "event_emitter", source)
    return source, store


def assert_restart_cannot_seal(source, store):
    assert store.failed_creates == 6
    # Neither the failed event nor its rejection marker reached the journal.
    # Do not call the old emitter's read methods, which would flush its marker.
    assert len(source._pending_issues) == 1
    restarted = emitter(LocalCaptureStorage(store.path))
    state = restarted.journal.read()
    assert state["insight"]["next_seq"] == 0
    assert state.get("producer_issues", []) == []
    assert state["closeout"]["writers"] == {}
    outcomes = state["closeout"]["writer_outcomes"]
    assert len(outcomes) == 1 and outcomes[0]["status"] == "failed"
    now = [100.0]
    closeout = CloseoutManager(restarted, clock=lambda: now[0],
        drainer=lambda _timeout: {"complete": True, "issues": [], "outcomes": []},
        finalizer=lambda *_args: {"complete": True, "issues": [], "outputs": {}})
    closeout.begin(capture_id="http-capture", stream_id=restarted.stream_id,
                   source=BINDING, drain_timeout_seconds=1)
    now[0] += 2
    result = closeout.advance()
    assert result["complete"] is False
    assert "accepted_writer_failed" in result["issues"]


@pytest.mark.parametrize("kind", ["attendee_error", "app_deploy"])
def test_telemetry_acceptance_is_503_and_failed_writer_survives_restart(
    client, retained, monkeypatch, kind,
):
    from server import main

    source, store = retained
    monkeypatch.setattr(main, "event_emitter", source)
    helper_name = "attendee_error_seen" if kind == "attendee_error" else "app_deploy"
    original = getattr(telemetry, helper_name)

    def unavailable(*args, **kwargs):
        # Request admission already committed; exhaust event and marker writes,
        # then allow the existing writer completion transaction to succeed.
        store.failures = 6
        return original(*args, **kwargs)

    monkeypatch.setattr(telemetry, helper_name, unavailable)
    if kind == "attendee_error":
        path = "/api/telemetry/error"
        body = {"code": "gateway_rate_limited", "agent_id": "claude"}
    else:
        monkeypatch.setattr(main, "_callback_identity", lambda *_args: Principal(BINDING["owner"]))
        path = "/api/tools/app-deploy/event"
        body = {"email": BINDING["owner"], "outcome": "succeeded",
                "reason": "deployment_succeeded", "duration_ms": 10,
                "attempts": 1, "resumed": False, "source": "cli"}
    response = client.post(path, headers=OWNER, json=body)
    assert response.status_code == 503
    assert_restart_cannot_seal(source, store)


def test_optional_telemetry_keeps_successful_action_but_persists_failed_writer(retained):
    source, store = retained
    app = FastAPI()
    app.add_middleware(CloseoutAdmissionMiddleware)
    route_threads = []

    @app.post("/api/operator-action")
    def action():
        # A sync route runs on another thread, while the shared request outcome
        # must be visible to admission middleware after dispatch completes.
        route_threads.append(threading.get_ident())
        store.failures = 6
        assert telemetry.emit("agent_control.changed", "system", {"outcome": "enabled"}) is False
        return {"applied": True}

    with TestClient(app) as client:
        response = client.post("/api/operator-action")
    assert response.status_code == 200 and response.json() == {"applied": True}
    assert route_threads and route_threads[0] != threading.get_ident()
    assert _request_outcome.get() is None
    assert_restart_cannot_seal(source, store)


def test_failed_capture_context_does_not_leak_to_next_successful_request(retained):
    source, store = retained
    app = FastAPI()
    app.add_middleware(CloseoutAdmissionMiddleware)

    @app.post("/api/action")
    def action():
        return {"captured": telemetry.emit("agent_control.changed", "system", {})}

    with TestClient(app) as client:
        # Fail after admission, on the producer rather than the reservation.
        original = telemetry.emit

        def fail_once(*args, **kwargs):
            store.failures = 6
            return original(*args, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(telemetry, "emit", fail_once)
            assert client.post("/api/action").json() == {"captured": False}
        assert client.post("/api/action").json() == {"captured": True}
    state = source.journal.read()
    assert len(state["closeout"]["writer_outcomes"]) == 1
    assert state["closeout"]["writers"] == {}
    assert state["insight"]["next_seq"] == 1
