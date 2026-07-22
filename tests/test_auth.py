"""Identity and group-based authorization."""

import hashlib


def test_no_identity_headers_is_403(client, monkeypatch):
    monkeypatch.delenv("LOCAL_DEV")
    resp = client.get("/api/config")
    assert resp.status_code == 403


def test_forwarded_email_identifies_user(client):
    resp = client.get("/api/config", headers={"X-Forwarded-Email": "Alice@Example.com"})
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == "alice@example.com"  # lowercased


def test_attendee_binding_rejects_a_second_attendee(client, monkeypatch):
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")

    assert client.get("/api/config", headers={"X-Forwarded-Email": "alice@example.com"}).status_code == 200
    response = client.get("/api/config", headers={"X-Forwarded-Email": "bob@example.com"})

    assert response.status_code == 403
    assert "assigned to alice@example.com" in response.json()["detail"]


def test_shared_topology_explicitly_allows_second_attendee(client, monkeypatch):
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")
    monkeypatch.setenv("ALLOW_SHARED_TOPOLOGY", "true")

    assert client.get("/api/config", headers={"X-Forwarded-Email": "bob@example.com"}).status_code == 200


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

    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")

    def fake_groups(principal):
        return {config.admin_group()} if principal.access_token == "sp-token" else set()

    monkeypatch.setattr(auth, "get_groups", fake_groups)
    resp = client.get("/api/admin/state", headers={"Authorization": "Bearer sp-token"})
    assert resp.status_code == 200


def test_admin_bearer_cache_keys_full_token_digest_not_shared_prefix(
    client, monkeypatch
):
    from server import auth

    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")
    monkeypatch.setenv("LOCAL_DEV", "false")
    monkeypatch.setattr(auth.config, "databricks_host", lambda: "https://example.test")
    auth._groups_cache.clear()
    tokens = ("same-prefix-admin-token", "same-prefix-denied-token")
    groups = {
        tokens[0]: [{"display": "platform_admins"}],
        tokens[1]: [],
    }

    class Response:
        status_code = 200

        def __init__(self, token):
            self.token = token

        def json(self):
            return {
                "id": f"principal-{hashlib.sha256(self.token.encode()).hexdigest()}",
                "groups": groups[self.token],
            }

    def fake_get(_url, *, headers, **_kwargs):
        return Response(headers["Authorization"].removeprefix("Bearer "))

    monkeypatch.setattr(auth.requests, "get", fake_get)

    assert client.get(
        "/api/admin/state", headers={"Authorization": f"Bearer {tokens[0]}"}
    ).status_code == 200
    assert client.get(
        "/api/admin/state", headers={"Authorization": f"Bearer {tokens[1]}"}
    ).status_code == 403
    cache_keys = set(auth._groups_cache)
    assert f"token:{hashlib.sha256(tokens[0].encode()).hexdigest()}" not in cache_keys
    assert all("same-prefix" not in key for key in cache_keys)


def test_rotating_bearer_mapping_is_ttl_lru_bounded(monkeypatch):
    from server import auth

    auth._token_principal_cache.clear()
    monkeypatch.setattr(auth.config, "local_dev", lambda: False)
    monkeypatch.setattr(auth, "_TOKEN_PRINCIPAL_CACHE_MAX", 2)
    monkeypatch.setattr(auth, "_CACHE_TTL", 10)
    now = [100.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    identities = iter(("principal-a", "principal-b", "principal-c"))
    monkeypatch.setattr(
        auth,
        "_scim_me_identity",
        lambda _token: ({"platform_admins"}, next(identities)),
    )

    for token in ("token-a", "token-b", "token-c"):
        auth.get_groups(auth.Principal("service-principal", token))

    assert len(auth._token_principal_cache) == 2
    assert hashlib.sha256(b"token-a").hexdigest() not in auth._token_principal_cache
    assert all(
        len(key) == 64 and set(key) <= set("0123456789abcdef")
        for key in auth._token_principal_cache
    )

    now[0] = 111.0
    monkeypatch.setattr(
        auth,
        "_scim_me_identity",
        lambda _token: ({"platform_admins"}, "principal-d"),
    )
    auth.get_groups(auth.Principal("service-principal", "token-d"))
    assert len(auth._token_principal_cache) == 1


def test_rotated_bearer_evicts_prior_mapping_for_same_principal(monkeypatch):
    from server import auth

    auth._token_principal_cache.clear()
    monkeypatch.setattr(auth.config, "local_dev", lambda: False)
    monkeypatch.setattr(
        auth,
        "_scim_me_identity",
        lambda _token: ({"platform_admins"}, "principal-1"),
    )

    auth.get_groups(auth.Principal("service-principal", "old-token"))
    auth.get_groups(auth.Principal("service-principal", "new-token"))

    assert list(auth._token_principal_cache) == [
        hashlib.sha256(b"new-token").hexdigest()
    ]


def test_groups_cache_is_ttl_lru_bounded_for_email_principals(monkeypatch):
    from server import auth

    auth._groups_cache.clear()
    monkeypatch.setattr(auth.config, "local_dev", lambda: False)
    monkeypatch.setattr(auth, "_GROUPS_CACHE_MAX", 2)
    monkeypatch.setattr(auth, "_CACHE_TTL", 10)
    now = [100.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    monkeypatch.setattr(auth, "_scim_me_identity", lambda _token: None)
    monkeypatch.setattr(
        auth,
        "_scim_lookup_groups_by_email",
        lambda email: {f"group-{email}"},
    )

    for email in ("a@example.com", "b@example.com", "c@example.com"):
        auth.get_groups(auth.Principal(email))

    assert list(auth._groups_cache) == ["b@example.com", "c@example.com"]
    auth.get_groups(auth.Principal("b@example.com"))
    assert list(auth._groups_cache) == ["c@example.com", "b@example.com"]

    now[0] = 111.0
    auth.get_groups(auth.Principal("d@example.com"))
    assert list(auth._groups_cache) == ["d@example.com"]


def test_groups_cache_bounds_token_digest_fallback_without_token_data(monkeypatch):
    from server import auth

    auth._groups_cache.clear()
    auth._token_principal_cache.clear()
    monkeypatch.setattr(auth.config, "local_dev", lambda: False)
    monkeypatch.setattr(auth, "_GROUPS_CACHE_MAX", 2)
    monkeypatch.setattr(
        auth,
        "_scim_me_identity",
        lambda _token: ({"platform_admins"}, None),
    )

    tokens = ("fallback-token-a", "fallback-token-b", "fallback-token-c")
    for token in tokens:
        auth.get_groups(auth.Principal("service-principal", token))

    assert len(auth._groups_cache) == 2
    assert all(
        key.startswith("token:") and len(key.removeprefix("token:")) == 64
        for key in auth._groups_cache
    )
    assert not any(token in repr(auth._groups_cache) for token in tokens)


def test_scim_me_explicitly_requests_and_consumes_authoritative_id(monkeypatch):
    from server import auth

    auth._groups_cache.clear()
    auth._token_principal_cache.clear()
    monkeypatch.setattr(auth.config, "local_dev", lambda: False)
    monkeypatch.setattr(auth.config, "databricks_host", lambda: "https://workspace")
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "id": "principal-123",
                "groups": [{"display": "platform_admins"}],
            }

    def fake_get(_url, **kwargs):
        seen.update(kwargs["params"])
        return Response()

    monkeypatch.setattr(auth.requests, "get", fake_get)
    groups = auth.get_groups(
        auth.Principal("service-principal", "rotating-bearer")
    )

    assert groups == {"platform_admins"}
    assert "id" in seen["attributes"].split(",")
    digest = hashlib.sha256(b"rotating-bearer").hexdigest()
    assert auth._token_principal_cache[digest][1] == "principal-123"
    assert "principal:principal-123" in auth._groups_cache


def test_admin_bearer_can_inspect_setup_and_prewarm_without_attendee_auth(
    client, monkeypatch
):
    from server import auth, config
    from server.bootstrap import install

    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")
    monkeypatch.setattr(
        auth,
        "get_groups",
        lambda principal: (
            {config.admin_group()} if principal.access_token == "sp-token" else set()
        ),
    )
    monkeypatch.setattr(install, "status", lambda: {"release_manifest": {"x": 1}})
    monkeypatch.setattr(
        install,
        "prewarm_status",
        lambda: {"reusable": True, "manifest": {"x": 1}},
    )
    headers = {"Authorization": "Bearer sp-token"}

    assert client.get("/api/admin/setup-status", headers=headers).json() == {
        "release_manifest": {"x": 1}
    }
    assert client.get("/api/admin/prewarm-status", headers=headers).json() == {
        "reusable": True,
        "manifest": {"x": 1},
    }


def test_attendee_setup_endpoint_remains_attendee_scoped(client, monkeypatch):
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")

    assert client.get(
        "/api/setup-status",
        headers={"X-Forwarded-Email": "alice@example.com"},
    ).status_code == 200
    assert client.get(
        "/api/setup-status",
        headers={"Authorization": "Bearer sp-token"},
    ).status_code == 403


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
