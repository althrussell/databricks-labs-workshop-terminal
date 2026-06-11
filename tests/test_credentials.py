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
    codex_env = open(os.path.join(home, ".codex", ".env")).read()
    assert "OPENAI_API_KEY=dapi-test-token" in codex_env


def test_config_exposes_credential_status(client, monkeypatch):
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    cfg = client.get("/api/config", headers=ALICE).json()
    assert cfg["credential"]["configured"] is False
