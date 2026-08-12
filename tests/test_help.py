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


def test_resolving_from_control_tower_lowers_the_hand_in_the_ui(
    client, as_admin, monkeypatch
):
    """The clear endpoint must announce itself, not just mutate server state.

    Operator replies moved to toasts, which meant dropping the ``clear_help``
    broadcast the frontend used to listen for. Lowering the hand now rests
    entirely on this ``help_state`` event, so if it ever stops being published
    the attendee is left with a raised hand nobody is coming for.
    """
    from server.events import event_hub

    published: list[dict] = []
    monkeypatch.setattr(event_hub, "publish", published.append)

    assert client.post("/api/admin/help/clear").status_code == 200

    lowered = [
        e
        for e in published
        if e.get("t") == "help_state" and e.get("raised") is False
    ]
    assert lowered, f"no help_state(raised=False) published: {published}"


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


def test_a_refused_push_still_leaves_the_message_for_collection(
    client, as_admin, ct_env, monkeypatch
):
    """The 401 that started this: the push cannot authenticate, and must not
    be the only way an attendee's words reach the operator.

    Control Tower is behind the Apps proxy of a different workspace than this
    terminal, and the token this app can mint is refused there. Everything the
    attendee wrote therefore has to be waiting on the surface Control Tower
    *can* read, which is presence.
    """
    monkeypatch.setattr(
        help_module.requests,
        "post",
        MagicMock(return_value=MagicMock(status_code=401, text="{}")),
    )
    monkeypatch.setattr(help_module, "app_identity_bearer", lambda: "oauth-bearer")

    posted = client.post(
        "/api/help/messages",
        json={"body": "my warehouse will not start"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert posted.status_code == 200
    assert posted.json()["pushed"] is False
    # One refused attempt, not two: the raise fallback is the same caller at
    # the same door, and it was doubling the failure in the operator's log.
    assert help_module.requests.post.call_count == 1

    outbox = client.get("/api/admin/presence").json()["help_outbox"]
    messages = [e for e in outbox if e["kind"] == "message"]
    assert [m["body"] for m in messages] == ["my warehouse will not start"]
    assert messages[0]["message_id"] == posted.json()["message"]["message_id"]


def test_a_push_that_works_names_the_same_message_the_outbox_is_holding(
    client, as_admin, ct_env, monkeypatch
):
    """Both paths are live where the push authenticates, so both must agree.

    Same workspace, no proxy in between: Control Tower accepts the push *and*
    later collects the outbox entry. Without a shared id it would file the
    attendee's sentence twice, and only one of the two could be marked seen.
    """
    mock_resp = MagicMock(status_code=200, text="ok")
    mock_resp.json.return_value = {}
    monkeypatch.setattr(
        help_module.requests, "post", MagicMock(return_value=mock_resp)
    )
    monkeypatch.setattr(help_module, "app_identity_bearer", lambda: "oauth-bearer")
    hdrs = {"X-Forwarded-Email": "alice@example.com"}

    raised = client.post("/api/help/raise", json={"note": "stuck on UC"}, headers=hdrs)
    posted = client.post(
        "/api/help/messages", json={"body": "still stuck"}, headers=hdrs
    )
    assert raised.status_code == 200 and posted.status_code == 200

    pushed_ids = [
        call.kwargs["json"].get("message_id")
        for call in help_module.requests.post.call_args_list
    ]
    outbox = client.get("/api/admin/presence").json()["help_outbox"]
    assert pushed_ids == [e["message_id"] for e in outbox if e["kind"] == "message"]
    assert posted.json()["message"]["message_id"] in pushed_ids


def test_the_operator_sees_the_opening_note_and_the_follow_up(client, as_admin):
    """Raising with a note is the first message of the conversation.

    Control Tower can derive that one from ``help_note``, but only that one and
    only its first version — so it travels the same way as every other message,
    with the id it was given here, and Control Tower stops guessing.
    """
    hdrs = {"X-Forwarded-Email": "alice@example.com"}
    client.post("/api/help/raise", json={"note": "stuck on UC"}, headers=hdrs)
    client.post("/api/help/messages", json={"body": "still stuck"}, headers=hdrs)

    outbox = client.get("/api/admin/presence").json()["help_outbox"]
    assert [e["body"] for e in outbox if e["kind"] == "message"] == [
        "stuck on UC",
        "still stuck",
    ]
    assert [e["seq"] for e in outbox] == sorted(e["seq"] for e in outbox)


def test_a_read_receipt_waits_for_collection_too(client, as_admin):
    client.post(
        "/api/help/ack",
        json={"message_id": "op-1"},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    outbox = client.get("/api/admin/presence").json()["help_outbox"]
    assert [(e["kind"], e["message_id"]) for e in outbox] == [("seen", "op-1")]


def test_nothing_is_dropped_until_control_tower_says_it_has_it(client, as_admin):
    """An unacknowledged message is indistinguishable from an unread one."""
    hdrs = {"X-Forwarded-Email": "alice@example.com"}
    client.post("/api/help/messages", json={"body": "first"}, headers=hdrs)
    client.post("/api/help/messages", json={"body": "second"}, headers=hdrs)

    outbox = client.get("/api/admin/presence").json()["help_outbox"]
    assert len(outbox) == 2

    # Collected the first only — the second is still owed to the operator.
    acked = client.post("/api/admin/help/ack", json={"through_seq": outbox[0]["seq"]})
    assert acked.status_code == 200
    assert acked.json()["pending"] == 1

    remaining = client.get("/api/admin/presence").json()["help_outbox"]
    assert [e["body"] for e in remaining] == ["second"]

    client.post("/api/admin/help/ack", json={"through_seq": remaining[0]["seq"]})
    assert client.get("/api/admin/presence").json()["help_outbox"] == []


def test_a_backlog_cannot_grow_without_bound(client, as_admin):
    """A terminal nobody is collecting from must not answer presence with a novel.

    Presence is polled every few seconds for every seat in the room, so an
    outbox that grew forever would turn one unreachable Control Tower into a
    fleet-wide payload problem.
    """
    hdrs = {"X-Forwarded-Email": "alice@example.com"}
    for i in range(help_module._OUTBOX_MAX + 15):
        client.post("/api/help/messages", json={"body": f"m{i}"}, headers=hdrs)

    assert len(help_module.outbox_snapshot()) == help_module._OUTBOX_MAX
    page = client.get("/api/admin/presence").json()["help_outbox"]
    assert len(page) == help_module._OUTBOX_PAGE
    # The oldest survivors go first, so collection drains in order.
    assert page[0]["body"] == "m15"


def test_the_refusal_is_explained_once_not_once_per_message(
    client, ct_env, monkeypatch, caplog
):
    monkeypatch.setattr(
        help_module.requests,
        "post",
        MagicMock(return_value=MagicMock(status_code=401, text="{}")),
    )
    monkeypatch.setattr(help_module, "app_identity_bearer", lambda: "oauth-bearer")
    hdrs = {"X-Forwarded-Email": "alice@example.com"}

    with caplog.at_level("WARNING", logger="server.help"):
        for i in range(5):
            client.post("/api/help/messages", json={"body": f"m{i}"}, headers=hdrs)

    rejected = [r for r in caplog.records if "ct_push_rejected" in r.getMessage()]
    assert len(rejected) == 1
    assert "collection" in rejected[0].getMessage()


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
