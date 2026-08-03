"""Raise-hand help queue: attendee API, CT push, and presence fields."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from server import config, help as help_module


@pytest.fixture(autouse=True)
def _reset_help_state():
    help_module.clear_hand()
    yield
    help_module.clear_hand()


@pytest.fixture
def ct_env(monkeypatch):
    monkeypatch.setenv("CONTROL_TOWER_URL", "https://ct.example")
    monkeypatch.setenv("WORKSHOP_RUN_ID", "run-11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv(
        "WORKSHOP_UNIT_ID", "unit-22222222-2222-2222-2222-222222222222"
    )


def test_raise_and_lower_update_local_state(client):
    up = client.post("/api/help/raise", json={"note": "stuck on UC"}, headers={"X-Forwarded-Email": "alice@example.com"})
    assert up.status_code == 200
    body = up.json()
    assert body["raised"] is True
    assert body["note"] == "stuck on UC"
    assert body["pushed"] is False

    cfg = client.get("/api/config", headers={"X-Forwarded-Email": "alice@example.com"}).json()
    assert cfg["help"]["raised"] is True
    assert cfg["help"]["note"] == "stuck on UC"

    down = client.post("/api/help/lower", headers={"X-Forwarded-Email": "alice@example.com"})
    assert down.status_code == 200
    assert down.json()["raised"] is False


def test_raise_without_url_is_fail_soft(client, monkeypatch):
    monkeypatch.delenv("CONTROL_TOWER_URL", raising=False)
    resp = client.post("/api/help/raise", json={}, headers={"X-Forwarded-Email": "alice@example.com"})
    assert resp.status_code == 200
    assert resp.json() == {"raised": True, "pushed": False, "note": None}


def test_note_truncated_to_280_chars(client):
    long_note = "x" * 300
    resp = client.post(
        "/api/help/raise",
        json={"note": long_note},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["note"]) == 280


def test_push_to_control_tower_uses_app_oauth(client, ct_env, monkeypatch):
    mock_post = MagicMock(return_value=MagicMock(status_code=200, text="ok"))
    monkeypatch.setattr(help_module.requests, "post", mock_post)
    monkeypatch.setattr(help_module, "app_identity_bearer", lambda: "oauth-bearer")

    resp = client.post(
        "/api/help/raise",
        json={"note": "need a hint"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 200
    assert resp.json()["pushed"] is True
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://ct.example/api/help/raise"
    assert kwargs["json"]["run_id"] == config.workshop_run_id()
    assert kwargs["json"]["unit_id"] == config.workshop_unit_id()
    assert kwargs["json"]["note"] == "need a hint"
    assert kwargs["headers"]["Authorization"] == "Bearer oauth-bearer"


def test_lower_pushes_to_control_tower(client, ct_env, monkeypatch):
    mock_post = MagicMock(return_value=MagicMock(status_code=200, text="ok"))
    monkeypatch.setattr(help_module.requests, "post", mock_post)
    monkeypatch.setattr(help_module, "app_identity_bearer", lambda: "oauth-bearer")

    client.post("/api/help/raise", json={}, headers={"X-Forwarded-Email": "alice@example.com"})
    mock_post.reset_mock()

    resp = client.post("/api/help/lower", headers={"X-Forwarded-Email": "alice@example.com"})
    assert resp.status_code == 200
    assert resp.json()["pushed"] is True
    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == "https://ct.example/api/help/lower"


def test_push_failure_is_fail_soft(client, ct_env, monkeypatch):
    mock_post = MagicMock(return_value=MagicMock(status_code=503, text="down"))
    monkeypatch.setattr(help_module.requests, "post", mock_post)
    monkeypatch.setattr(help_module, "app_identity_bearer", lambda: "oauth-bearer")

    resp = client.post("/api/help/raise", json={}, headers={"X-Forwarded-Email": "alice@example.com"})
    assert resp.status_code == 200
    assert resp.json()["pushed"] is False


def test_presence_includes_help_fields(client, as_admin):
    client.post("/api/help/raise", json={"note": "here"}, headers={"X-Forwarded-Email": "alice@example.com"})
    presence = client.get("/api/admin/presence").json()
    assert presence["help_raised"] is True
    assert presence["help_open"] is True
    assert presence["help_note"] == "here"
    assert presence["help_raised_at"]


def test_operator_broadcast_clear_help(client, as_admin):
    client.post("/api/help/raise", json={"note": "x"}, headers={"X-Forwarded-Email": "alice@example.com"})
    resp = client.post(
        "/api/admin/broadcast",
        json={"message": "", "level": "info", "ttl_s": 1, "clear_help": True},
    )
    assert resp.status_code == 200
    cfg = client.get("/api/config", headers={"X-Forwarded-Email": "alice@example.com"}).json()
    assert cfg["help"]["raised"] is False
