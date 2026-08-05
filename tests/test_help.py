"""Raise-hand help queue: attendee API, CT push, and presence fields."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from server import config, help as help_module


@pytest.fixture(autouse=True)
def _reset_help_state():
    help_module.reset_for_tests()
    yield
    help_module.reset_for_tests()


def test_clear_hand_keeps_message_history(client, as_admin):
    client.post(
        "/api/admin/help/message",
        json={
            "message_id": "keep-1",
            "sender_role": "operator",
            "body": "still visible after resolve",
        },
    )
    resolved = client.post("/api/admin/help/clear")
    assert resolved.status_code == 200
    thread = client.get(
        "/api/help/thread", headers={"X-Forwarded-Email": "alice@example.com"}
    ).json()
    assert thread["raised"] is False
    assert len(thread["messages"]) == 1
    assert thread["messages"][0]["body"] == "still visible after resolve"


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


def test_resolve_clears_hand_via_dedicated_endpoint(client, as_admin):
    """Resolving a request has its own endpoint, not an empty broadcast.

    Clearing the hand used to ride along on ``/api/admin/broadcast`` as an
    empty message with a flag, which meant every resolve pushed a no-op notice
    at every attendee's banner and the broadcast contract carried a field that
    had nothing to do with broadcasting.
    """
    client.post("/api/help/raise", json={"note": "x"}, headers={"X-Forwarded-Email": "alice@example.com"})
    resp = client.post("/api/admin/help/clear")
    assert resp.status_code == 200
    cfg = client.get("/api/config", headers={"X-Forwarded-Email": "alice@example.com"}).json()
    assert cfg["help"]["raised"] is False


def test_operator_reply_never_hijacks_the_banner(client, as_admin):
    """An operator reply is a toast, and leaves the pinned notice alone."""
    client.post(
        "/api/admin/broadcast",
        json={"message": "Lunch at 12:30", "level": "info", "ttl_s": 600, "surface": "banner"},
    )
    client.post(
        "/api/admin/help/message",
        json={"message_id": "op-9", "sender_role": "operator", "body": "restart the warehouse"},
    )
    cfg = client.get("/api/config", headers={"X-Forwarded-Email": "alice@example.com"}).json()
    assert cfg["broadcast"]["message"] == "Lunch at 12:30"


def test_toast_surface_broadcast_is_not_retained_as_a_banner(client, as_admin):
    """A toast-surface broadcast fires and is gone; only banners are replayed."""
    client.post("/api/admin/broadcast", json={"message": "", "clear": True})
    resp = client.post(
        "/api/admin/broadcast",
        json={"message": "5 minutes left", "level": "info", "ttl_s": 20, "surface": "toast"},
    )
    assert resp.status_code == 200
    cfg = client.get("/api/config", headers={"X-Forwarded-Email": "alice@example.com"}).json()
    assert cfg["broadcast"] is None


def test_ack_pushes_read_receipt_to_control_tower(client, ct_env, monkeypatch):
    mock_post = MagicMock(return_value=MagicMock(status_code=200, text="ok"))
    monkeypatch.setattr(help_module.requests, "post", mock_post)
    monkeypatch.setattr(help_module, "app_identity_bearer", lambda: "oauth-bearer")

    resp = client.post(
        "/api/help/ack",
        json={"message_id": "op-1"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert resp.status_code == 200
    assert resp.json()["acked"] is True
    assert mock_post.call_args[0][0] == (
        "https://ct.example/api/help/messages/op-1/seen"
    )


def test_attendee_follow_up_message(client, ct_env, monkeypatch):
    mock_resp = MagicMock(status_code=200, text="ok")
    mock_resp.json.return_value = {
        "message_id": "m1",
        "help_request_id": "h1",
        "body": "still stuck",
    }
    mock_post = MagicMock(return_value=mock_resp)
    monkeypatch.setattr(help_module.requests, "post", mock_post)
    monkeypatch.setattr(help_module, "app_identity_bearer", lambda: "oauth-bearer")

    client.post(
        "/api/help/raise",
        json={"note": "first"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    mock_post.reset_mock()
    follow = client.post(
        "/api/help/messages",
        json={"body": "still stuck"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert follow.status_code == 200, follow.text
    assert follow.json()["message"]["body"] == "still stuck"
    assert follow.json()["pushed"] is True
    assert mock_post.call_args[0][0] == "https://ct.example/api/help/messages"

    thread = client.get(
        "/api/help/thread", headers={"X-Forwarded-Email": "alice@example.com"}
    )
    assert thread.status_code == 200
    assert len(thread.json()["messages"]) >= 2


def test_admin_help_message_ingest(client, as_admin):
    resp = client.post(
        "/api/admin/help/message",
        json={
            "message_id": "op-1",
            "help_request_id": "hr-1",
            "sender_role": "operator",
            "sender": "op@x.com",
            "body": "try restarting the warehouse",
        },
    )
    assert resp.status_code == 200, resp.text
    thread = client.get(
        "/api/help/thread", headers={"X-Forwarded-Email": "alice@example.com"}
    )
    assert thread.status_code == 200
    msgs = thread.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["body"] == "try restarting the warehouse"
    assert msgs[0]["sender_role"] == "operator"
