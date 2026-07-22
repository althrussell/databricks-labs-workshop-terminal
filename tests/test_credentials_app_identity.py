"""Direct app-identity OAuth credentials (no token API or attendee-readable secret)."""

import base64
import json
import pytest

from server import credentials as cred


class _IdentityResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "applicationId": "app-client",
            "id": "12345",
        }

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def app_client_id(monkeypatch):
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "app-client")
    monkeypatch.setenv("WORKSHOP_APP_SP_ID", "12345")


def _jwt(exp: int, marker: str = "value") -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "marker": marker}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


@pytest.fixture
def fresh_manager():
    m = cred.CredentialManager(lambda: 1)
    return m


def test_app_identity_skipped_in_local_dev(monkeypatch):
    # conftest sets LOCAL_DEV=1 → the SDK auth probe is skipped (no hang, None).
    assert cred.app_identity_bearer() is None


def test_app_identity_bearer_prefers_oauth_over_pat(monkeypatch):
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "app-oauth-bearer")
    monkeypatch.setenv("WORKSHOP_PAT", "dapi-legacy")
    assert cred._bootstrap_auth() == "app-oauth-bearer"  # app identity wins


def test_direct_oauth_never_calls_token_api(monkeypatch):
    calls = []
    now = [100.0]
    bearer = _jwt(1000)
    manager = cred.CredentialManager(lambda: 1, now_fn=lambda: now[0])
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: bearer)
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return _IdentityResponse()

    def post(*args, **kwargs):
        raise AssertionError("normal OAuth mode must never call token APIs")

    monkeypatch.setattr(cred.requests, "get", get)
    monkeypatch.setattr(cred.requests, "post", post)
    monkeypatch.setattr(manager, "_fanout", lambda token: None)

    assert manager.token() == bearer
    st = manager.status()
    assert st["configured"] is True and st["rotating"] is True
    assert st["source"] == "app_identity_oauth"
    assert st["token_expires_in"] == 900
    assert calls[0][0].endswith("/api/2.0/current-user/me")


def test_current_user_service_principal_match_is_authoritative(fresh_manager, monkeypatch):
    calls = []
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")

    def get(url, **kwargs):
        calls.append(url)
        return _IdentityResponse(payload={"application_id": "app-client", "id": "12345"})

    monkeypatch.setattr(cred.requests, "get", get)

    assert fresh_manager._validate("oauth-bearer") is True
    assert calls == ["https://x.test/api/2.0/current-user/me"]
    diagnostic = fresh_manager.status()["validation_diagnostic"]
    assert diagnostic["result"] == "matched"
    assert diagnostic["observed_application_id"] == "app-client"
    assert diagnostic["observed_service_principal_id"] == "12345"
    assert diagnostic["endpoints"][0] == {
        "endpoint": "current-user/me",
        "status": 200,
        "observed_identity": {
            "application_id": "app-client",
                "id": "12345",
        },
    }


def test_current_user_identity_mismatch_rejects_without_scim_override(
    fresh_manager, monkeypatch
):
    calls = []
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")

    def get(url, **kwargs):
        calls.append(url)
        return _IdentityResponse(payload={"applicationId": "different-client"})

    monkeypatch.setattr(cred.requests, "get", get)

    assert fresh_manager._validate("oauth-bearer") is False
    assert calls == ["https://x.test/api/2.0/current-user/me"]
    diagnostic = fresh_manager.status()["validation_diagnostic"]
    assert diagnostic["result"] == "identity_mismatch"
    assert diagnostic["expected_application_id"] == "app-client"


def test_current_user_does_not_let_username_override_application_id(
    fresh_manager, monkeypatch
):
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _IdentityResponse(
            payload={
                "applicationId": "different-client",
                "userName": "app-client",
            }
        ),
    )

    assert fresh_manager._validate("oauth-bearer") is False
    assert fresh_manager.status()["validation_diagnostic"]["result"] == (
        "identity_mismatch"
    )


def test_username_equal_to_client_id_does_not_prove_app_identity(
    fresh_manager, monkeypatch
):
    calls = []
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")

    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/api/2.0/current-user/me"):
            return _IdentityResponse(
                payload={"userName": "app-client", "id": "user-123"}
            )
        return _IdentityResponse(status_code=404, payload={})

    monkeypatch.setattr(cred.requests, "get", get)

    assert fresh_manager._validate("user-oauth-bearer") is False
    assert calls == [
        "https://x.test/api/2.0/current-user/me",
        "https://x.test/api/2.0/preview/scim/v2/Me",
    ]
    diagnostic = fresh_manager.status()["validation_diagnostic"]
    assert diagnostic["result"] == "endpoints_unavailable"
    assert diagnostic["endpoints"][0]["observed_identity"] == {
        "userName": "app-client",
        "id": "user-123",
    }


def test_username_only_current_user_continues_to_scim_fallback(
    fresh_manager, monkeypatch
):
    calls = []
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")

    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/api/2.0/current-user/me"):
            return _IdentityResponse(
                payload={"userName": "app-client", "id": "sp-123"}
            )
        return _IdentityResponse(payload={"applicationId": "app-client", "id": "12345"})

    monkeypatch.setattr(cred.requests, "get", get)

    assert fresh_manager._validate("oauth-bearer") is True
    assert calls == [
        "https://x.test/api/2.0/current-user/me",
        "https://x.test/api/2.0/preview/scim/v2/Me",
    ]
    diagnostic = fresh_manager.status()["validation_diagnostic"]
    assert diagnostic["result"] == "matched"
    assert diagnostic["endpoints"][0]["observed_identity"]["userName"] == (
        "app-client"
    )


def test_scim_me_is_fallback_with_exact_application_id_match(
    fresh_manager, monkeypatch
):
    calls = []
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")

    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/api/2.0/current-user/me"):
            return _IdentityResponse(status_code=403, payload={})
        return _IdentityResponse(payload={"applicationId": "app-client", "id": "12345"})

    monkeypatch.setattr(cred.requests, "get", get)

    assert fresh_manager._validate("oauth-bearer") is True
    assert calls == [
        "https://x.test/api/2.0/current-user/me",
        "https://x.test/api/2.0/preview/scim/v2/Me",
    ]
    diagnostic = fresh_manager.status()["validation_diagnostic"]
    assert diagnostic["result"] == "matched"
    assert [entry["status"] for entry in diagnostic["endpoints"]] == [403, 200]


def test_scim_me_exact_username_and_numeric_sp_id_pair_is_authoritative(
    fresh_manager, monkeypatch
):
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")

    def get(url, **kwargs):
        if url.endswith("/api/2.0/current-user/me"):
            return _IdentityResponse(status_code=404, payload={})
        return _IdentityResponse(payload={"userName": "app-client", "id": "12345"})

    monkeypatch.setattr(cred.requests, "get", get)

    assert fresh_manager._validate("oauth-bearer") is True
    diagnostic = fresh_manager.status()["validation_diagnostic"]
    assert diagnostic["result"] == "matched"
    assert diagnostic["expected_service_principal_id"] == "12345"
    assert diagnostic["endpoints"][1]["observed_identity"] == {
        "userName": "app-client",
        "id": "12345",
    }


def test_scim_me_username_without_numeric_id_is_rejected(fresh_manager, monkeypatch):
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda url, **kwargs: (
            _IdentityResponse(status_code=404, payload={})
            if url.endswith("/api/2.0/current-user/me")
            else _IdentityResponse(payload={"userName": "app-client"})
        ),
    )

    assert fresh_manager._validate("oauth-bearer") is False
    assert fresh_manager.status()["validation_diagnostic"]["result"] == (
        "identity_mismatch"
    )


def test_scim_me_numeric_sp_id_mismatch_is_rejected(fresh_manager, monkeypatch):
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda url, **kwargs: (
            _IdentityResponse(status_code=404, payload={})
            if url.endswith("/api/2.0/current-user/me")
            else _IdentityResponse(payload={"userName": "app-client", "id": "54321"})
        ),
    )

    assert fresh_manager._validate("oauth-bearer") is False
    diagnostic = fresh_manager.status()["validation_diagnostic"]
    assert diagnostic["result"] == "identity_mismatch"
    assert diagnostic["expected_service_principal_id"] == "12345"
    assert diagnostic["endpoints"][1]["observed_identity"]["id"] == "54321"


@pytest.mark.parametrize("expected_sp_id", ["", "sp-123", "12.3"])
def test_scim_pair_rejects_missing_or_non_numeric_expected_sp_id(
    fresh_manager, monkeypatch, expected_sp_id
):
    if expected_sp_id:
        monkeypatch.setenv("WORKSHOP_APP_SP_ID", expected_sp_id)
    else:
        monkeypatch.delenv("WORKSHOP_APP_SP_ID", raising=False)
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda url, **kwargs: (
            _IdentityResponse(status_code=404, payload={})
            if url.endswith("/api/2.0/current-user/me")
            else _IdentityResponse(payload={"userName": "app-client", "id": "12345"})
        ),
    )

    assert fresh_manager._validate("oauth-bearer") is False
    assert fresh_manager.status()["validation_diagnostic"]["result"] == (
        "expected_service_principal_id_invalid"
    )


def test_application_id_exact_match_rejects_missing_numeric_expected_sp_id(
    fresh_manager, monkeypatch
):
    monkeypatch.delenv("WORKSHOP_APP_SP_ID", raising=False)
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _IdentityResponse(
            payload={"applicationId": "app-client", "id": "anything"}
        ),
    )

    assert fresh_manager._validate("oauth-bearer") is False
    assert fresh_manager.status()["validation_diagnostic"]["result"] == (
        "expected_service_principal_id_invalid"
    )


@pytest.mark.parametrize("observed_sp_id", ["", "sp-123", "54321"])
def test_application_id_match_rejects_missing_non_numeric_or_mismatched_observed_id(
    fresh_manager, monkeypatch, observed_sp_id
):
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    payload = {"applicationId": "app-client"}
    if observed_sp_id:
        payload["id"] = observed_sp_id
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _IdentityResponse(payload=payload),
    )

    assert fresh_manager._validate("oauth-bearer") is False
    assert fresh_manager.status()["validation_diagnostic"]["result"] == (
        "identity_mismatch"
    )


def test_both_identity_endpoints_fail_with_safe_diagnostic(monkeypatch):
    bearer = _jwt(1000)
    manager = cred.CredentialManager(lambda: 1, now_fn=lambda: 100.0)
    manager._token = "previous-rejected-token"
    manager._rotating = True
    manager._source = "app_identity_oauth"
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: bearer)
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")

    def get(url, **kwargs):
        if url.endswith("/api/2.0/current-user/me"):
            return _IdentityResponse(status_code=403, payload={"secret": "do-not-log"})
        return _IdentityResponse(status_code=404, payload={"token": "do-not-log"})

    monkeypatch.setattr(cred.requests, "get", get)

    manager._self_probe(adopt=True)

    status = manager.status()
    assert status["state"] == "unhealthy"
    assert manager._token is None
    assert status["validation_diagnostic"] == {
        "result": "endpoints_unavailable",
        "expected_application_id": "app-client",
        "observed_application_id": None,
        "expected_service_principal_id": "12345",
        "observed_service_principal_id": None,
        "endpoints": [
            {"endpoint": "current-user/me", "status": 403, "observed_identity": {}},
            {"endpoint": "scim/v2/Me", "status": 404, "observed_identity": {}},
        ],
    }
    assert "403" in status["last_error"] and "404" in status["last_error"]
    assert "do-not-log" not in json.dumps(status)
    assert bearer not in json.dumps(status)


def test_user_token_is_not_mislabeled_as_app_identity(fresh_manager, monkeypatch):
    calls = []
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")

    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/api/2.0/current-user/me"):
            return _IdentityResponse(
                payload={"userName": "alice@example.com", "id": "user-123"}
            )
        return _IdentityResponse(status_code=404, payload={})

    monkeypatch.setattr(cred.requests, "get", get)

    assert fresh_manager._validate("user-oauth-bearer") is False
    diagnostic = fresh_manager.status()["validation_diagnostic"]
    assert diagnostic["result"] == "endpoints_unavailable"
    assert diagnostic["endpoints"][0]["observed_identity"] == {
        "userName": "alice@example.com",
        "id": "user-123",
    }
    assert len(calls) == 2


def test_token_raises_without_app_identity_or_pat(fresh_manager, monkeypatch):
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: None)
    with pytest.raises(cred.CredentialError):
        fresh_manager.token()


def test_vended_pat_is_explicit_degraded_fallback(fresh_manager, monkeypatch):
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: None)
    monkeypatch.setenv("WORKSHOP_PAT", "dapi-legacy")
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _IdentityResponse(),
    )
    monkeypatch.setattr(
        cred.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("PAT fallback must not mint another PAT")
        ),
    )

    assert fresh_manager.token() == "dapi-legacy"
    status = fresh_manager.status()
    assert status["state"] == "degraded"
    assert status["rotating"] is False
    assert status["healthy"] is False
    assert status["source"] == "emergency_workshop_pat"


def test_rejected_emergency_pat_is_not_served(fresh_manager, monkeypatch):
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: None)
    monkeypatch.setenv("WORKSHOP_PAT", "expired")
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: type("Response", (), {"status_code": 401})(),
    )

    with pytest.raises(cred.CredentialError):
        fresh_manager.token()
    assert fresh_manager.status()["state"] == "unhealthy"


def test_start_runs_in_app_identity_mode(fresh_manager, monkeypatch):
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "app-oauth-bearer")
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _IdentityResponse(),
    )
    monkeypatch.setattr(fresh_manager, "_fanout", lambda token: None)
    fresh_manager.start()
    assert fresh_manager._bootstrap_ok is True
    assert fresh_manager.status()["state"] == "rotating"
    fresh_manager.stop()


def test_changed_oauth_bearer_fans_out_once(monkeypatch):
    now = [100.0]
    bearers = iter([_jwt(1000, "first"), _jwt(1100, "second"), _jwt(1100, "second")])
    manager = cred.CredentialManager(lambda: 1, now_fn=lambda: now[0])
    fanout = []
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: next(bearers))
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _IdentityResponse(),
    )
    monkeypatch.setattr(manager, "_fanout", fanout.append)

    manager._self_probe(adopt=True)
    now[0] = 200.0
    manager._self_probe(adopt=True)
    now[0] = 300.0
    manager._self_probe(adopt=True)

    assert len(fanout) == 2
    assert fanout[0] != fanout[1]


def test_idle_request_reauthenticates_after_validation_goes_stale(monkeypatch):
    now = [100.0]
    bearers = iter([_jwt(1000, "first"), _jwt(2000, "second")])
    manager = cred.CredentialManager(lambda: 0, now_fn=lambda: now[0])
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: next(bearers))
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _IdentityResponse(),
    )
    monkeypatch.setattr(manager, "_fanout", lambda token: None)

    assert manager.token() == _jwt(1000, "first")
    now[0] += cred.OAUTH_VALIDATION_MAX_AGE + 1

    assert manager.token() == _jwt(2000, "second")


def test_rotating_state_turns_unhealthy_when_oauth_validation_is_stale(monkeypatch):
    now = [100.0]
    manager = cred.CredentialManager(lambda: 0, now_fn=lambda: now[0])
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: _jwt(1000))
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _IdentityResponse(),
    )
    monkeypatch.setattr(manager, "_fanout", lambda token: None)

    manager._self_probe(adopt=False)
    assert manager.status()["state"] == "rotating"

    now[0] += cred.OAUTH_VALIDATION_MAX_AGE + 1
    status = manager.status()
    assert status["state"] == "unhealthy"
    assert status["healthy"] is False
