from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from server import event_deadline


@pytest.fixture(autouse=True)
def isolated_deadline_store(monkeypatch, tmp_path):
    monkeypatch.setattr("server.config.data_root", lambda: str(tmp_path / "data"))
    monkeypatch.setenv("WORKSHOP_EVENT_ENDS_AT", str(int(time.time()) + 3600))
    event_deadline.store.reset_for_tests()
    yield
    event_deadline.store.reset_for_tests()


def test_admin_extension_is_durable_exact_and_idempotent(client, as_admin):
    deployment_deadline = int(os.environ["WORKSHOP_EVENT_ENDS_AT"])
    wanted = int(time.time()) + 7200

    first = client.put("/api/admin/event-deadline", json={"event_ends_at": wanted})
    replay = client.put("/api/admin/event-deadline", json={"event_ends_at": wanted})

    assert first.status_code == 200
    assert first.json()["event_ends_at"] == wanted
    assert first.json()["changed"] is True
    assert first.json()["verified"] is True
    assert first.json()["durability"]["event_ends_in"] == pytest.approx(7200, abs=5)
    assert "app_plane_durable" in first.json()["credential_durability"]
    assert replay.status_code == 200
    assert replay.json()["changed"] is False

    event_deadline.store.reset_for_tests()
    assert event_deadline.store.snapshot() == wanted
    persisted = json.loads(Path(event_deadline.store._path()).read_text())
    assert persisted == {
        "deployment_event_ends_at": deployment_deadline,
        "event_ends_at": wanted,
        "schema_version": 2,
    }


def test_redeploy_with_later_deadline_discards_older_live_override(monkeypatch):
    deployment_deadline = int(time.time()) + 3600
    live_override = deployment_deadline + 1800
    redeployed_deadline = live_override + 1800
    monkeypatch.setenv("WORKSHOP_EVENT_ENDS_AT", str(deployment_deadline))
    event_deadline.store.apply(live_override)

    event_deadline.store.reset_for_tests()
    monkeypatch.setenv("WORKSHOP_EVENT_ENDS_AT", str(redeployed_deadline))

    assert event_deadline.store.snapshot() == redeployed_deadline
    assert not Path(event_deadline.store._path()).exists()


def test_redeploy_with_earlier_deadline_resets_monotonic_baseline(monkeypatch):
    deployment_deadline = int(time.time()) + 3600
    live_override = deployment_deadline + 3600
    redeployed_deadline = deployment_deadline - 1800
    next_extension = redeployed_deadline + 300
    monkeypatch.setenv("WORKSHOP_EVENT_ENDS_AT", str(deployment_deadline))
    event_deadline.store.apply(live_override)

    # The cache key includes the deployment baseline, so a changed environment
    # is observed even before a process-local cache reset.
    monkeypatch.setenv("WORKSHOP_EVENT_ENDS_AT", str(redeployed_deadline))

    assert event_deadline.store.snapshot() == redeployed_deadline
    assert not Path(event_deadline.store._path()).exists()
    assert event_deadline.store.apply(next_extension) == (next_extension, True)
    assert event_deadline.store.snapshot() == next_extension


def test_admin_extension_reports_failed_credential_durability(
    client, as_admin, monkeypatch
):
    wanted = int(time.time()) + 7200
    monkeypatch.setattr(
        "server.readiness.evaluate_runtime",
        lambda: {
            "durability": {"event_ends_in": 7200},
            "checks": {
                "credential_durability": {
                    "ok": False,
                    "state": "red",
                    "detail": "credential expires before the event",
                }
            },
        },
    )

    response = client.put("/api/admin/event-deadline", json={"event_ends_at": wanted})

    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert response.json()["verified"] is False
    assert response.json()["credential_durability"]["state"] == "red"


def test_stale_deadline_cannot_shorten_a_live_event(client, as_admin):
    current = int(time.time()) + 7200
    client.put("/api/admin/event-deadline", json={"event_ends_at": current})

    response = client.put(
        "/api/admin/event-deadline", json={"event_ends_at": current - 60}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "stale_event_deadline",
        "current_event_ends_at": current,
    }
    assert event_deadline.store.snapshot() == current


def test_non_admin_cannot_change_deadline(client, as_non_admin):
    response = client.put(
        "/api/admin/event-deadline",
        json={"event_ends_at": int(time.time()) + 7200},
    )

    assert response.status_code in {401, 403}


def test_runtime_readiness_uses_durable_override(monkeypatch):
    initial = int(time.time()) + 3600
    extended = initial + 3600
    monkeypatch.setenv("WORKSHOP_EVENT_ENDS_AT", str(initial))
    event_deadline.store.apply(extended)

    effective = event_deadline.effective_environment()

    assert effective["WORKSHOP_EVENT_ENDS_AT"] == str(extended)
