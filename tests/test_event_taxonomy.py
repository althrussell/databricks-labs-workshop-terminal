"""Every failure an attendee can hit produces an event an operator can read.

The taxonomy is deliberately a fixed enum with an ``unknown`` bucket: fleet
aggregation only works if the same failure carries the same string, and a
never-before-seen code must still arrive rather than being dropped.
"""

import pytest

from server import telemetry


@pytest.fixture
def emitted(monkeypatch):
    """Capture what would have been buffered for Control Tower."""
    from server import event_emitter as emitter_module

    records: list[tuple[str, str, dict]] = []

    def capture(event_type, attendee, payload=None, **_kwargs):
        records.append((event_type, attendee, payload or {}))

    monkeypatch.setattr(emitter_module.event_emitter, "emit", capture)
    return records


def _types(records):
    return [record[0] for record in records]


def _payload(records, event_type):
    return next(payload for kind, _who, payload in records if kind == event_type)


def test_an_unknown_agent_is_a_recorded_failure_not_just_a_404(client, emitted):
    resp = client.post(
        "/api/sessions",
        json={"agent_id": "nonexistent"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )

    assert resp.status_code == 404
    assert "session.create_failed" in _types(emitted)
    assert _payload(emitted, "session.create_failed")["code"] == "unknown_agent"


def test_a_paused_launch_says_why_it_was_refused(client, emitted, monkeypatch):
    from server import spend

    monkeypatch.setattr(spend, "agents_enabled", lambda: False)
    resp = client.post(
        "/api/sessions",
        json={"agent_id": "claude"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )

    assert resp.status_code == 403
    assert _payload(emitted, "session.create_failed")["code"] == "agents_paused"


def test_hitting_the_session_cap_is_visible_to_an_operator(
    client, emitted, monkeypatch, launchable_agents
):
    client.post(
        "/api/sessions",
        json={"agent_id": "claude"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    resp = client.post(
        "/api/sessions",
        json={"agent_id": "codex"},
        headers={"X-Forwarded-Email": "bob@example.com"},
    )

    assert resp.status_code == 409
    assert _payload(emitted, "session.create_failed")["code"] == "session_conflict"


def test_closing_an_agent_records_how_it_ended(client, emitted, launchable_agents):
    created = client.post(
        "/api/sessions",
        json={"agent_id": "claude"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    ).json()["session"]

    client.delete(
        f"/api/sessions/{created['id']}",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )

    payload = _payload(emitted, "session.exited")
    assert payload["code"] == "closed"
    assert payload["agent"] == "claude"
    assert payload["session_id"] == created["id"]


def test_a_session_is_only_reported_as_ended_once(client, emitted, launchable_agents):
    from server.sessions import session_manager

    created = client.post(
        "/api/sessions",
        json={"agent_id": "claude"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    ).json()["session"]
    session = session_manager.get(created["id"], "alice@example.com")

    session_manager.terminate(session, reason="closed")
    session_manager.terminate(session, reason="exited")

    assert _types(emitted).count("session.exited") == 1


def test_the_attendee_reports_what_their_screen_said(client, emitted):
    resp = client.post(
        "/api/telemetry/error",
        json={
            "code": "native_terminal_start_failed",
            "detail": "Codex exited",
            "agent_id": "codex",
        },
        headers={"X-Forwarded-Email": "alice@example.com"},
    )

    assert resp.status_code == 200
    payload = _payload(emitted, "attendee.error_seen")
    assert payload["code"] == "native_terminal_start_failed"
    assert payload["agent"] == "codex"


def test_a_code_nobody_has_seen_before_is_bucketed_but_never_dropped(client, emitted):
    client.post(
        "/api/telemetry/error",
        json={"code": "quantum_flux_failed"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )

    payload = _payload(emitted, "attendee.error_seen")
    assert payload["code"] == "unknown"
    assert payload["raw_code"] == "quantum_flux_failed"


def test_an_install_step_that_fails_is_announced_once_per_transition(emitted):
    from server.bootstrap import install

    install._set("claude", "error", "npm exploded")
    install._set("claude", "error", "npm exploded")

    failures = [record for record in emitted if record[0] == "install.step_failed"]
    assert len(failures) == 1
    assert failures[0][2]["step"] == "claude"
    assert "npm exploded" in failures[0][2]["error"]


def test_a_healthy_install_step_says_nothing(emitted):
    from server.bootstrap import install

    install._set("codex", "running")
    install._set("codex", "complete")

    assert "install.step_failed" not in _types(emitted)


def test_obo_health_is_emitted_on_change_and_stays_quiet_otherwise(emitted, monkeypatch):
    from server import config, obo

    monkeypatch.setattr(config, "obo_enabled", lambda: True)
    manager = obo.OboManager()

    assert manager.note_health("alice@example.com") == "absent"
    manager.note_health("alice@example.com")

    health = [record for record in emitted if record[0] == "obo.health"]
    assert len(health) == 1
    assert health[0][2]["state"] == "absent"


def test_telemetry_never_raises_into_a_caller(monkeypatch):
    from server import event_emitter as emitter_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("control tower is on fire")

    monkeypatch.setattr(emitter_module.event_emitter, "emit", explode)

    telemetry.session_create_failed("alice@example.com", "claude", "session_conflict")
    telemetry.attendee_error_seen("alice@example.com", "turn_failed")
