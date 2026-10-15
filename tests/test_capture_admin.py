"""Admin transport never releases capture through reads or invalid ACKs."""

from __future__ import annotations

import pytest

from server.capture_journal import CaptureStorageError
from server.event_emitter import EventEmitter


ADMIN = {"X-Forwarded-Email": "op@example.com"}


@pytest.fixture
def retained_emitter(client, monkeypatch):
    from server import event_emitter as emitter_module

    emitter = EventEmitter(run_id="", workspace_id="", ingest_url="", ingest_token="")
    for index in range(3):
        emitter.emit("heartbeat", "alice@example.com", {"index": index},
                     idempotency_key=f"admin-{index}")
    monkeypatch.setattr(emitter_module, "event_emitter", emitter)
    return emitter


def test_authenticated_v2_get_and_ack_are_separate(client, as_admin, retained_emitter):
    page = client.get("/api/admin/insight-events?limit=2", headers=ADMIN).json()
    epoch = page["stream_id"]
    assert page["schema_version"] == 2
    assert [entry["seq"] for entry in page["events"]] == [1, 2]
    assert retained_emitter.pending() == 3

    cursor_page = client.get(
        "/api/admin/insight-events", params={"stream": epoch, "after": 2}, headers=ADMIN,
    ).json()
    assert [entry["seq"] for entry in cursor_page["events"]] == [3]
    assert retained_emitter.pending() == 3

    ack = {"schema_version": 2, "stream_id": epoch, "through_seq": 2}
    response = client.post("/api/admin/insight-events/ack", json=ack, headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["acked_through"] == 2
    assert client.post("/api/admin/insight-events/ack", json=ack, headers=ADMIN).json() == response.json()
    assert retained_emitter.pending() == 1


@pytest.mark.parametrize("update", [
    {"schema_version": 1}, {"schema_version": 2.0}, {"schema_version": "2"},
    {"schema_version": True}, {"stream_id": ""}, {"stream_id": "   "},
    {"stream_id": "wrong-epoch"}, {"through_seq": -1}, {"through_seq": True},
    {"through_seq": "2"}, {"through_seq": 2.0}, {"through_seq": 4},
])
def test_invalid_v2_ack_is_422_without_mutation(client, as_admin, retained_emitter, update):
    ack = {"schema_version": 2, "stream_id": retained_emitter.stream_id, "through_seq": 2, **update}
    response = client.post("/api/admin/insight-events/ack", json=ack, headers=ADMIN)
    assert response.status_code == 422
    assert retained_emitter.pending() == 3


def test_capture_transport_requires_admin(client, as_non_admin, retained_emitter):
    assert client.get("/api/admin/insight-events", headers=ADMIN).status_code == 403
    response = client.post("/api/admin/insight-events/ack", headers=ADMIN, json={
        "schema_version": 2, "stream_id": retained_emitter.stream_id, "through_seq": 3,
    })
    assert response.status_code == 403
    assert retained_emitter.pending() == 3


def _held(*_args, **_kwargs):
    raise CaptureStorageError("sensitive storage URL and token must not leave this process")


def test_storage_failure_is_generic_503_for_get_and_ack(
    client, as_admin, retained_emitter, monkeypatch,
):
    epoch = retained_emitter.stream_id
    monkeypatch.setattr(retained_emitter, "collect", _held)
    monkeypatch.setattr(retained_emitter, "acknowledge", _held)
    responses = [
        client.get("/api/admin/insight-events", headers=ADMIN),
        client.post("/api/admin/insight-events/ack", headers=ADMIN, json={
            "schema_version": 2, "stream_id": epoch, "through_seq": 2,
        }),
    ]
    for response in responses:
        assert response.status_code == 503
        assert response.json() == {"detail": "Capture persistence unavailable"}
    assert retained_emitter.pending() == 3


@pytest.mark.parametrize("through_seq", [-1, True, "1", 1.0])
def test_help_ack_rejects_coerced_and_negative_sequences(client, as_admin, through_seq):
    response = client.post("/api/admin/help/ack", headers=ADMIN, json={"through_seq": through_seq})
    assert response.status_code == 422


def test_help_ack_invalid_tail_and_storage_failure_are_safe(client, as_admin, monkeypatch):
    from server import help as help_module

    def beyond_tail(_through_seq):
        raise ValueError("sensitive retained record")

    monkeypatch.setattr(help_module, "ack_outbox", beyond_tail)
    response = client.post("/api/admin/help/ack", headers=ADMIN, json={"through_seq": 100})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid help acknowledgement"}
    monkeypatch.setattr(help_module, "ack_outbox", _held)
    response = client.post("/api/admin/help/ack", headers=ADMIN, json={"through_seq": 0})
    assert response.status_code == 503
    assert response.json() == {"detail": "Capture persistence unavailable"}


@pytest.mark.parametrize("final", [False, True])
def test_held_capture_preserves_stats_and_reports_unknown_pending(
    client, as_admin, retained_emitter, monkeypatch, final,
):
    from server import insight_summary, stats

    monkeypatch.setattr(stats, "gather_all", lambda _users: {"users": [], "workspace": {"tables": 4}})
    monkeypatch.setattr(stats, "emit_signals", _held)
    monkeypatch.setattr(insight_summary, "summarise_all", _held)
    monkeypatch.setattr(insight_summary, "summarise_in_background", _held)
    monkeypatch.setattr(retained_emitter, "delivery_status", lambda: {
        "delivery": "pull", "schema_version": 2, "pending": None, "dropped": 0,
        "stream_id": "", "retention_ready": False, "retention_issues": ["capture_unavailable"],
        "persistence": {"ok": False, "held": True},
    })

    response = client.get("/api/admin/stats", params={"final": final}, headers=ADMIN)
    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace"] == {"tables": 4}
    assert payload["instance"]["events_pending"] is None
    assert payload["instance"]["capture_error"] == "capture_unavailable"
    assert payload["instance"]["summary_error"] == "capture_unavailable"
    assert payload["instance"]["capture_delivery"]["retention_ready"] is False
    assert "sensitive" not in response.text


def test_diagnostics_reports_capture_even_if_readiness_fails(
    client, as_admin, retained_emitter, monkeypatch,
):
    from server import readiness

    monkeypatch.setattr(readiness, "evaluate_runtime", _held)
    held = {"retention_ready": False, "pending": None, "retention_issues": ["capture_unavailable"]}
    monkeypatch.setattr(retained_emitter, "delivery_status", lambda: held)
    response = client.get("/api/admin/diagnostics", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["capture_delivery"] == held
    assert "error" in response.json()["readyz"]


@pytest.mark.parametrize("final", [False, True])
def test_real_harvest_keeps_live_counters_during_retained_storage_outage(
    client, as_admin, monkeypatch, final,
):
    from server import discovery, event_emitter as emitter_module, insight_summary, stats
    from server.capture_journal import CaptureJournal, MemoryCaptureStorage
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    binding = {"run_id": "admin-outage", "unit_id": "unit-outage",
               "workspace_id": "123", "owner": "alice@example.com"}
    storage = MemoryCaptureStorage()
    journal = CaptureJournal(storage, binding, {})
    retained = EventEmitter(run_id=binding["run_id"], workspace_id=binding["workspace_id"],
                            ingest_url="", ingest_token="", journal=journal)
    monkeypatch.setattr(emitter_module, "event_emitter", retained)
    user_manager.configure_capture(journal, binding["owner"])
    stats.configure_capture(journal)
    discovery.discovery_store.configure_capture(retained)
    discovery.record(binding["owner"], {"record_id": "pre-outage", "goal": "Retained goal"}, retained)
    owner = user_manager.peek(binding["owner"])
    owner.errors = 7
    owner.sessions_launched = {"claude": 2, "bash": 1}
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census())
    monkeypatch.setattr(insight_summary, "summarise_in_background", lambda *a, **k: None)
    monkeypatch.setattr(insight_summary, "summarise_all", lambda *a, **k: 0)
    saved_objects = dict(storage.objects)
    list_names = storage.list_names

    def offline():
        raise OSError("sensitive backend endpoint must stay private")

    monkeypatch.setattr(storage, "list_names", offline)
    response = client.get("/api/admin/stats", headers=ADMIN, params={"final": final})
    assert response.status_code == 200
    [row] = response.json()["users"]
    assert row["errors"] == 7
    assert row["agent_sessions"] == 2
    assert row["terminal_sessions"] == 3
    assert row["capture_observation"]["journal"] == "unavailable"
    assert row["discovery_records"] is None
    assert response.json()["instance"]["capture_error"] == "capture_unavailable"
    assert response.json()["instance"]["capture_delivery"]["retention_ready"] is False
    assert response.json()["instance"]["events_pending"] is None
    assert "sensitive" not in response.text
    assert storage.objects == saved_objects
    monkeypatch.setattr(storage, "list_names", list_names)
    with pytest.raises(CaptureStorageError, match="retry the harvest"):
        stats.emit_signals(response.json(), retained)
    fresh = stats.gather_all([owner])
    assert fresh["users"][0]["discovery_records"] == 1
    assert fresh["users"][0]["capture_observation"]["journal"] == "ready"
    assert stats.emit_signals(fresh, retained) == 1


def test_discovery_count_failure_marks_otherwise_readable_harvest_incomplete(
    client, as_admin, monkeypatch,
):
    from server import discovery, insight_summary, stats
    from server.users import user_manager

    user = user_manager.get("alice@example.com")
    user.errors = 4
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census())
    monkeypatch.setattr(discovery.discovery_store, "count_for", _held)
    monkeypatch.setattr(insight_summary, "summarise_in_background", lambda *a, **k: None)
    response = client.get("/api/admin/stats", headers=ADMIN)
    assert response.status_code == 200
    [row] = response.json()["users"]
    assert row["errors"] == 4
    assert row["discovery_records"] is None
    assert row["capture_observation"]["journal"] == "unavailable"
    assert response.json()["instance"]["capture_error"] == "capture_unavailable"
