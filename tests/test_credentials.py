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
    from server import credentials

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    monkeypatch.setattr(
        credentials.requests,
        "get",
        lambda *args, **kwargs: _Resp(200),
    )
    assert credentials.credential_manager.token() == "dapi-test-token"
    assert credentials.credential_manager.status()["configured"] is True


def test_agent_session_requires_a_credential(client, monkeypatch):
    import server.main as main

    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(main.install, "ready", lambda: {"claude": True})
    resp = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)
    assert resp.status_code == 503
    assert "credential" in resp.json()["detail"].lower()


def test_session_create_writes_cli_configs(client, monkeypatch):
    from server import credentials
    import server.main as main
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    monkeypatch.setattr(
        credentials.requests,
        "get",
        lambda *args, **kwargs: _Resp(200),
    )
    monkeypatch.setattr(main.install, "ready", lambda: {"claude": True})
    monkeypatch.setattr(main.agents, "launch_command", lambda _agent: ["/bin/bash"])
    resp = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)
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


# --- app identity is also used by authorization lookups ---


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_new_manager_has_no_successful_probe_timestamp():
    from server import credentials as cred

    m = cred.CredentialManager(lambda: 0, now_fn=lambda: 100.0)

    assert m.status()["last_successful_at"] is None
    assert m.status()["source"] == "unknown"


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
