"""Identity and group-based authorization."""


def test_no_identity_headers_is_403(client, monkeypatch):
    monkeypatch.delenv("LOCAL_DEV")
    resp = client.get("/api/config")
    assert resp.status_code == 403


def test_forwarded_email_identifies_user(client):
    resp = client.get("/api/config", headers={"X-Forwarded-Email": "Alice@Example.com"})
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == "alice@example.com"  # lowercased


def test_access_group_blocks_non_members(client, monkeypatch, as_non_admin):
    monkeypatch.setenv("ACCESS_GROUP", "workshop_attendees")
    resp = client.get("/api/config", headers={"X-Forwarded-Email": "alice@example.com"})
    assert resp.status_code == 403
    assert "workshop_attendees" in resp.json()["detail"]


def test_access_group_admits_members(client, monkeypatch):
    from server import auth

    monkeypatch.setenv("ACCESS_GROUP", "workshop_attendees")
    monkeypatch.setattr(auth, "get_groups", lambda p: {"workshop_attendees"})
    resp = client.get("/api/config", headers={"X-Forwarded-Email": "alice@example.com"})
    assert resp.status_code == 200


def test_admin_routes_require_group(client, as_non_admin):
    resp = client.get("/api/admin/state", headers={"X-Forwarded-Email": "alice@example.com"})
    assert resp.status_code == 403
    assert "platform_admins" in resp.json()["detail"]


def test_admin_routes_admit_group_members(client, as_admin):
    resp = client.get("/api/admin/state", headers={"X-Forwarded-Email": "op@example.com"})
    assert resp.status_code == 200
    assert "phase" in resp.json()


def test_admin_accepts_sp_bearer_token(client, monkeypatch):
    from server import auth, config

    def fake_groups(principal):
        return {config.admin_group()} if principal.access_token == "sp-token" else set()

    monkeypatch.setattr(auth, "get_groups", fake_groups)
    resp = client.get("/api/admin/state", headers={"Authorization": "Bearer sp-token"})
    assert resp.status_code == 200


def test_websocket_rejected_without_identity(client, monkeypatch):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.delenv("LOCAL_DEV")
    # The socket is accepted first, then closed 4403, so the close code reaches
    # the client (a pre-accept close would surface to browsers as a generic
    # 1006). The disconnect is observed on the first receive, not at connect.
    with client.websocket_connect("/ws/events") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4403
