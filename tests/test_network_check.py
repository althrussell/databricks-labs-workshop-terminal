"""Tests for the WT SSE + WebSocket network checker."""

from __future__ import annotations

import json

from server import network_check, observability
from .conftest import ALICE


def test_network_check_ping_and_fast_stream(client):
    ping = client.get("/api/network-check/ping", headers=ALICE)
    assert ping.status_code == 200
    assert ping.json()["ok"] is True

    stream = client.get("/api/network-check/stream?fast=true", headers=ALICE)
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["cache-control"] == "no-cache"
    assert stream.headers["x-accel-buffering"] == "no"
    events = [
        json.loads(line.removeprefix("data:").strip())
        for line in stream.text.splitlines()
        if line.startswith("data:")
    ]
    assert len(events) == 11
    assert [e["seq"] for e in events] == list(range(11))
    assert events[-1]["flush"] is True
    assert len(events[-1]["pad"]) >= 32 * 1024


def test_network_check_result_persists_emits_telemetry_and_shows_in_presence(
    client, monkeypatch, as_admin, tmp_path
):
    from server import config, telemetry
    from server.users import user_manager

    network_check.reset_for_tests()
    monkeypatch.setattr(config, "data_root", lambda: str(tmp_path))
    monkeypatch.setenv("WORKSHOP_RUN_ID", "379282d8-49ee-4d26-abee-3cb4aceae001")
    user_manager.get(ALICE["X-Forwarded-Email"])

    emitted: list[tuple[str, str, dict]] = []
    real_emit = telemetry.emit

    def spy_emit(event_type: str, attendee: str, payload: dict | None = None) -> bool:
        emitted.append((event_type, attendee, dict(payload or {})))
        return real_emit(event_type, attendee, payload)

    monkeypatch.setattr(telemetry, "emit", spy_emit)

    headers = {**ALICE, "X-Forwarded-For": "203.0.113.42"}
    resp = client.post(
        "/api/network-check/result",
        headers=headers,
        json={
            "verdict": "blocked",
            "first_event_ms": 30120,
            "max_lag_ms": 30120,
            "received_count": 11,
            "expected_count": 11,
            "clumped": True,
            "ws_rtt_ms": 80,
            "http_rtt_ms": 42,
            "browser_family": "Chrome",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "blocked"
    assert body["clumped"] is True
    assert body["outage"] is False
    assert len(body["network_label"]) == 12
    assert "203.0.113.42" not in json.dumps(body)

    net_events = [item for item in emitted if item[0] == "network.check"]
    assert len(net_events) == 1
    assert net_events[0][2]["verdict"] == "blocked"
    assert net_events[0][2]["network_label"] == body["network_label"]
    assert "max_lag_ms" in observability._SAFE_FIELD_MAP
    assert "verdict" in observability._SAFE_FIELD_MAP

    # Presence includes the latest network check for the user
    presence = client.get("/api/admin/presence", headers=ALICE)
    assert presence.status_code == 200
    users = {u["email"]: u for u in presence.json()["users"]}
    email = ALICE["X-Forwarded-Email"].lower()
    assert users[email]["network_check"]["verdict"] == "blocked"
    assert users[email]["network_check"]["network_label"] == body["network_label"]


def test_a_check_where_nothing_came_back_is_recorded_as_an_outage(client, monkeypatch, tmp_path):
    """E2E 28 Oct: a DNS outage on the attendee's laptop gave 0 of 11 events and
    no round trips. Still blocked, but flagged so nobody blames a proxy."""
    from server import config
    from server.users import user_manager

    network_check.reset_for_tests()
    monkeypatch.setattr(config, "data_root", lambda: str(tmp_path))
    user_manager.get(ALICE["X-Forwarded-Email"])
    resp = client.post(
        "/api/network-check/result",
        headers=ALICE,
        json={"verdict": "blocked", "received_count": 0, "expected_count": 11,
              "first_event_ms": None, "max_lag_ms": None, "ws_rtt_ms": None, "http_rtt_ms": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["verdict"] == "blocked"
    assert resp.json()["outage"] is True
    assert network_check.is_outage_shaped(received_count=0, http_rtt_ms=40, ws_rtt_ms=None) is False
    assert "outage" in observability._SAFE_FIELD_MAP
