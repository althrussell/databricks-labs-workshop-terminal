"""Vended-credential behaviour."""

import os

from .conftest import ALICE


def test_token_requires_vended_pat(client, monkeypatch):
    from server.credentials import CredentialError, credential_manager

    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    import pytest

    with pytest.raises(CredentialError):
        credential_manager.token()


def test_token_serves_vended_pat(client, monkeypatch):
    from server.credentials import credential_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    assert credential_manager.token() == "dapi-test-token"
    assert credential_manager.status()["configured"] is True


def test_bash_session_works_without_credential(client, monkeypatch):
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    resp = client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    assert resp.status_code == 200


def test_session_create_writes_cli_configs(client, monkeypatch):
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    resp = client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    assert resp.status_code == 200

    home = user_manager.get("alice@example.com").home
    cfg = open(os.path.join(home, ".databrickscfg")).read()
    assert "dapi-test-token" in cfg
    # Codex/Claude read the rotating token from a file via a dynamic auth
    # command (apiKeyHelper / provider auth), never a static env baked in at
    # startup — so a live process survives rotation. The token lands in the
    # shared gateway-token file, and codex config points at it.
    token_file = open(os.path.join(home, ".config", "workshop", "gateway-token")).read()
    assert token_file.strip() == "dapi-test-token"
    codex_toml = open(os.path.join(home, ".codex", "config.toml")).read()
    assert "[model_providers.databricks.auth]" in codex_toml
    assert "gateway-token" in codex_toml
    assert not os.path.exists(os.path.join(home, ".codex", ".env"))


def test_config_exposes_credential_status(client, monkeypatch):
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    cfg = client.get("/api/config", headers=ALICE).json()
    assert cfg["credential"]["configured"] is False


# --- app-identity-first / idle-survival / health classification (no PAT clock) ---


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class _PostMock:
    """Fake requests.post: hand out scripted mint tokens, 200 on revoke."""

    def __init__(self, mint_values=None, mint_status=200):
        self._values = list(mint_values or [])
        self._mint_status = mint_status
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if url.endswith("/token/create"):
            if self._mint_status != 200:
                return _Resp(self._mint_status, {})
            value = self._values.pop(0) if self._values else "minted"
            return _Resp(200, {"token_value": value, "token_info": {"token_id": value + "_id"}})
        return _Resp(200, {})  # token/delete (revoke)


def _manager(monkeypatch, sessions=0):
    from server import credentials as cred

    m = cred.CredentialManager(lambda: sessions)
    monkeypatch.setattr(m, "_fanout", lambda tok: None)
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    return m


def test_probe_degraded_when_cannot_mint_but_credential_valid(monkeypatch):
    """403 on token/create but the credential still authenticates → degraded
    (a static credential is being served, no rotation), NOT healthy."""
    from server import auth, credentials as cred

    m = _manager(monkeypatch)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "app-oauth-bearer")
    monkeypatch.setattr(cred.requests, "post", _PostMock(mint_status=403))
    monkeypatch.setattr(auth, "scim_token_valid", lambda tok: True)

    m._self_probe(adopt=False)

    st = m.status()
    assert st["state"] == "degraded"
    assert st["degraded"] is True
    assert st["healthy"] is False
    assert st["rotating"] is False


def test_probe_unhealthy_when_credential_rejected(monkeypatch):
    """403 on mint AND the credential fails SCIM → unhealthy (likely expired)."""
    from server import auth, credentials as cred

    m = _manager(monkeypatch)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "stale-bearer")
    monkeypatch.setattr(cred.requests, "post", _PostMock(mint_status=403))
    monkeypatch.setattr(auth, "scim_token_valid", lambda tok: False)

    m._self_probe(adopt=False)

    st = m.status()
    assert st["state"] == "unhealthy"
    assert st["healthy"] is False


def test_probe_verify_only_when_idle_does_not_keep_token(monkeypatch):
    """Idle probe (no sessions) proves mint capability then revokes the probe
    token — healthy/rotating, but nothing churned for absent consumers."""
    from server import credentials as cred

    m = _manager(monkeypatch, sessions=0)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "app-oauth-bearer")
    post = _PostMock(mint_values=["probe-tok"])
    monkeypatch.setattr(cred.requests, "post", post)

    m._self_probe(adopt=False)

    st = m.status()
    assert st["state"] == "rotating"
    assert st["healthy"] is True
    # Verify-only must revoke what it minted (a create + a delete call).
    assert any(url.endswith("/token/delete") for url, _ in post.calls)


def test_idle_recovery_remints_after_token_goes_stale(monkeypatch):
    """The deploy→idle→event path: the minted token ages out during idle, and
    the first request on the day re-bootstraps a fresh token from the app
    identity (no static PAT involved)."""
    from server import credentials as cred

    m = _manager(monkeypatch, sessions=1)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "app-oauth-bearer")
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred.requests, "post", _PostMock(mint_values=["first", "second"]))

    assert m.token() == "first"

    # Simulate two idle days: the minted token is now well past its lifetime.
    m._minted_at -= cred.TOKEN_LIFETIME + 10

    assert m.token() == "second"  # re-minted on demand, not a stale/served PAT
    assert m.status()["state"] == "rotating"


def test_rotation_does_not_revoke_in_use_token(monkeypatch):
    """When rotating with live consumers, the previously-minted token must NOT
    be revoked: running agent processes re-read the token file on their own
    cadence and may still be holding it. Revoking eagerly 401s them mid-session
    (the Codex/Claude stale-token bug). The old token expires on its own clock."""
    from server import credentials as cred

    m = _manager(monkeypatch, sessions=2)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "app-oauth-bearer")
    post = _PostMock(mint_values=["first", "second"])
    monkeypatch.setattr(cred.requests, "post", post)

    # First mint (bootstrap), then an adopting probe rotates to a new token.
    assert m.token() == "first"
    m._self_probe(adopt=True)

    assert m.token() == "second"  # rotated
    # Rotation with live sessions issues creates only — never a delete.
    assert not any(url.endswith("/token/delete") for url, _ in post.calls), (
        "in-use minted token must expire naturally, not be revoked on rotation"
    )


def test_scim_lookup_prefers_app_identity_over_pat(monkeypatch):
    """auth's SCIM-by-email fallback must use the app identity, not the
    expiring vended PAT, so group resolution survives an idle window."""
    from server import auth, credentials as cred

    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "app-oauth-bearer")
    monkeypatch.setenv("WORKSHOP_PAT", "dapi-legacy")

    captured = {}

    def fake_get(url, headers=None, **kw):
        captured["auth"] = (headers or {}).get("Authorization", "")
        return _Resp(200, {"Resources": [{"groups": [{"display": "g1"}]}]})

    monkeypatch.setattr(auth.requests, "get", fake_get)
    monkeypatch.setattr(auth.config, "databricks_host", lambda: "https://x.test")

    groups = auth._scim_lookup_groups_by_email("someone@example.com")
    assert groups == {"g1"}
    assert captured["auth"] == "Bearer app-oauth-bearer"  # not the PAT
