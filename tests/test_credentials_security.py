import threading
from types import SimpleNamespace

import pytest

from server import credentials as cred
from server.users import User


class _Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_initialize_uses_explicit_m2m_client_and_scrubs_secret_env(monkeypatch):
    created = []
    hardened = []

    class WorkspaceClient:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.config = SimpleNamespace(
                authenticate=lambda: {"Authorization": "Bearer app-oauth"}
            )

    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.test")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "app-client")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET_VALUE", "duplicate")
    monkeypatch.setenv("DATABRICKS_TOKEN", "must-not-be-selected")
    monkeypatch.setattr(cred.config, "local_dev", lambda: False)
    cred._reset_app_identity_for_tests()

    client = cred.initialize_app_identity(
        workspace_client_cls=WorkspaceClient,
        harden_fn=lambda: hardened.append(True) or True,
        system="Linux",
    )

    assert created == [{
        "host": "https://workspace.test",
        "client_id": "app-client",
        "client_secret": "secret",
        "auth_type": "oauth-m2m",
    }]
    assert client is cred.initialize_app_identity(
        workspace_client_cls=WorkspaceClient,
        harden_fn=lambda: False,
        system="Linux",
    )
    assert hardened == [True]
    assert "DATABRICKS_CLIENT_SECRET" not in cred.os.environ
    assert "DATABRICKS_CLIENT_SECRET_VALUE" not in cred.os.environ
    assert "DATABRICKS_CLIENT_SECRET" not in User("attendee@example.com").shell_env()
    assert cred.os.environ["DATABRICKS_TOKEN"] == "must-not-be-selected"


def test_production_linux_hardening_failure_fails_closed(monkeypatch):
    class WorkspaceClient:
        def __init__(self, **kwargs):
            self.config = SimpleNamespace(authenticate=lambda: {})

    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.test")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "app-client")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret")
    monkeypatch.setattr(cred.config, "local_dev", lambda: False)
    cred._reset_app_identity_for_tests()

    with pytest.raises(RuntimeError, match="non-dumpable"):
        cred.initialize_app_identity(
            workspace_client_cls=WorkspaceClient,
            harden_fn=lambda: False,
            system="Linux",
        )
    assert cred.secret_protection_status()["ok"] is False


def test_scim_identity_mismatch_clears_cached_bearer_atomically(monkeypatch):
    manager = cred.CredentialManager(lambda: 1, now_fn=lambda: 100.0)
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "expected-app")
    monkeypatch.setenv("WORKSHOP_APP_SP_ID", "12345")
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "bearer")
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://workspace.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _Response(
            200, {"applicationId": "different-app", "id": "12345"}
        ),
    )

    with pytest.raises(cred.CredentialError):
        manager.token()

    assert manager._token is None
    assert manager._source is None
    assert manager._last_successful_at == 0
    assert manager.status()["state"] == "unhealthy"
    assert manager.status()["rotating"] is False


def test_rejection_after_success_never_serves_rejected_cache(monkeypatch):
    manager = cred.CredentialManager(lambda: 1, now_fn=lambda: 100.0)
    accepted = [True]
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "app-client")
    monkeypatch.setenv("WORKSHOP_APP_SP_ID", "12345")
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "bearer")
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://workspace.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _Response(
            200 if accepted[0] else 401,
            {"applicationId": "app-client", "id": "12345"} if accepted[0] else {},
        ),
    )
    monkeypatch.setattr(manager, "_fanout", lambda token: None)

    assert manager.token() == "bearer"
    accepted[0] = False
    manager._self_probe(adopt=True)

    with pytest.raises(cred.CredentialError):
        manager.token()
    assert manager._token is None


def test_concurrent_refresh_is_single_flight(monkeypatch):
    manager = cred.CredentialManager(lambda: 1, now_fn=lambda: 100.0)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    results = []
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "app-client")
    monkeypatch.setenv("WORKSHOP_APP_SP_ID", "12345")

    def bearer():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return "bearer"

    monkeypatch.setattr(cred, "app_identity_bearer", bearer)
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://workspace.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _Response(
            200, {"applicationId": "app-client", "id": "12345"}
        ),
    )
    monkeypatch.setattr(manager, "_fanout", lambda token: None)

    threads = [
        threading.Thread(target=lambda: results.append(manager.token()))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert entered.wait(1)
    release.set()
    for thread in threads:
        thread.join(2)

    assert results == ["bearer", "bearer"]
    assert len(calls) == 1


def test_pat_fallback_atomically_clears_all_oauth_freshness(monkeypatch):
    manager = cred.CredentialManager(lambda: 1, now_fn=lambda: 1000.0)
    with manager._lock:
        manager._token = "stale-oauth"
        manager._expires_at = 5000.0
        manager._last_successful_at = 100.0
        manager._rotating = True
        manager._source = "app_identity_oauth"
        manager._health = cred.STATE_ROTATING
        manager._bootstrap_ok = True
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: None)
    monkeypatch.setenv("WORKSHOP_PAT", "emergency-pat")
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://workspace.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _Response(200),
    )

    assert manager.token() == "emergency-pat"
    status = manager.status()

    assert manager._token is None
    assert manager._expires_at is None
    assert manager._last_successful_at == 0
    assert status["source"] == "emergency_workshop_pat"
    assert status["state"] == "degraded"
    assert status["token_expires_in"] is None
    assert status["last_successful_at"] is None
