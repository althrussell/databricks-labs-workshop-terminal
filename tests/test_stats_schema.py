"""Versioned stats schema + error/abandonment telemetry (gap P1-14)."""

import time

from server import stats
from server.users import User


def test_gather_includes_schema_version():
    user = User("alice@example.com")
    out = stats.gather(user)
    assert out["schema_version"] == stats.STATS_SCHEMA_VERSION
    assert out["schema_version"] >= 1


def test_gather_all_includes_schema_version():
    out = stats.gather_all([User("a@x.com"), User("b@x.com")])
    assert out["schema_version"] == stats.STATS_SCHEMA_VERSION
    assert len(out["users"]) == 2
    # Every per-user row carries the new telemetry fields.
    for row in out["users"]:
        assert "errors" in row
        assert "idle_seconds" in row


def test_gather_all_exposes_websocket_queue_metrics(monkeypatch):
    from server.events import event_hub
    from server.sessions import session_manager

    monkeypatch.setattr(
        session_manager,
        "queue_metrics",
        lambda: {"terminal": {"current_depth": 2, "overflows": 1}},
    )
    monkeypatch.setattr(
        event_hub,
        "metrics",
        lambda: {"current_depth": 3, "overflows": 4, "policy": "drop_oldest"},
    )

    queues = stats.gather_all([])["websocket_queues"]
    assert queues["terminal"]["overflows"] == 1
    assert queues["events"]["overflows"] == 4


def test_error_counter_surfaces():
    user = User("alice@example.com")
    assert stats.gather_user(user)["errors"] == 0
    user.errors += 2
    assert stats.gather_user(user)["errors"] == 2


def test_idle_seconds_reflects_last_seen():
    user = User("alice@example.com")
    # Never seen → None (no abandonment signal yet).
    assert stats.gather_user(user)["idle_seconds"] is None
    user.last_seen = time.time() - 120
    idle = stats.gather_user(user)["idle_seconds"]
    assert idle is not None and idle >= 119
