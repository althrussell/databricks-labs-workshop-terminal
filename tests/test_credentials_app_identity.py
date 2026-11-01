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


@pytest.mark.parametrize("expected_sp_id", ["sp-123", "12.3"])
def test_scim_pair_rejects_non_numeric_expected_sp_id(
    fresh_manager, monkeypatch, expected_sp_id
):
    monkeypatch.setenv("WORKSHOP_APP_SP_ID", expected_sp_id)
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


def test_scim_pair_binds_on_client_id_when_expected_sp_id_absent(
    fresh_manager, monkeypatch
):
    """An absent WORKSHOP_APP_SP_ID degrades the binding instead of breaking it.

    Control Tower can only resolve the numeric id after app create. When that
    injection is missed, refusing the app's own OAuth bearer took the AI gateway
    away from every coding agent, so the terminal could not run at all. The
    client-id binding still holds; the weaker check is reported, not hidden.
    """
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

    assert fresh_manager._validate("oauth-bearer") is True
    diagnostic = fresh_manager.status()["validation_diagnostic"]
    assert diagnostic["result"] == "matched_without_sp_id"
    assert diagnostic["service_principal_id_unverified"] is True


def test_user_token_falls_back_to_scim_when_current_user_me_unavailable(
    fresh_manager, monkeypatch
):
    """A user token must not be judged against app-SP preconditions.

    current-user/me answers 404 for an app service principal, and the app-SP
    precondition checks used to run before the SCIM fallback — so a perfectly
    valid vended PAT was reported as an app-identity failure and the emergency
    credential path could never engage.
    """
    monkeypatch.delenv("WORKSHOP_APP_SP_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.setattr(cred, "_app_client_id", None)
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda url, **kwargs: (
            _IdentityResponse(status_code=404, payload={})
            if url.endswith("/api/2.0/current-user/me")
            else _IdentityResponse(payload={"userName": "attendee@example.com"})
        ),
    )

    assert fresh_manager._validate("vended-pat", require_app_identity=False) is True
    assert fresh_manager.status()["validation_diagnostic"]["result"] == "authenticated"


def test_application_id_exact_match_binds_when_expected_sp_id_absent(
    fresh_manager, monkeypatch
):
    """Same degrade-don't-brick rule on the current-user/me path.

    See test_scim_pair_binds_on_client_id_when_expected_sp_id_absent — an absent
    numeric id weakens the binding but must not deny the app its own token.
    """
    monkeypatch.delenv("WORKSHOP_APP_SP_ID", raising=False)
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(
        cred.requests,
        "get",
        lambda *args, **kwargs: _IdentityResponse(
            payload={"applicationId": "app-client", "id": "anything"}
        ),
    )

    assert fresh_manager._validate("oauth-bearer") is True
    diagnostic = fresh_manager.status()["validation_diagnostic"]
    assert diagnostic["result"] == "matched_without_sp_id"
    assert diagnostic["service_principal_id_unverified"] is True


def test_application_id_exact_match_rejects_malformed_expected_sp_id(
    fresh_manager, monkeypatch
):
    """A malformed value is a broken deploy, not a missing one — still fatal."""
    monkeypatch.setenv("WORKSHOP_APP_SP_ID", "sp-123")
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
        "service_principal_id_unverified": False,
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


# --- event hardening: expiry-aware probing, forced mint, keep-last-good ---


def _ok_get(*args, **kwargs):
    return _IdentityResponse()


def _manager_with_token(monkeypatch, now, bearer, *, fanout=None):
    manager = cred.CredentialManager(
        lambda: 1, now_fn=lambda: now[0], sleep_fn=lambda s: None
    )
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(cred.requests, "get", _ok_get)
    monkeypatch.setattr(manager, "_fanout", fanout if fanout is not None else (lambda t: None))
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: bearer)
    manager._self_probe(adopt=True)
    assert manager.status()["state"] == "rotating"
    return manager


def test_transient_identity_failure_keeps_last_validated_bearer(monkeypatch):
    now = [100.0]
    bearer = _jwt(4000)
    manager = _manager_with_token(monkeypatch, now, bearer)
    alerts = []
    monkeypatch.setattr(manager, "_emit_health_alert", lambda s, e: alerts.append(s))

    def flaky(url, **kwargs):
        raise cred.requests.RequestException("timeout")

    monkeypatch.setattr(cred.requests, "get", flaky)
    now[0] = 400.0
    manager._tick()

    status = manager.status()
    assert manager._token == bearer, "a timeout must not wipe the held bearer"
    assert status["state"] == "rotating"
    assert "endpoints_unavailable" in (status["last_error"] or "")
    assert alerts == [], "no Control Tower alert for a single transient failure"


@pytest.mark.parametrize("code", [429, 503, 408])
def test_transient_status_codes_are_retried_then_kept(monkeypatch, code):
    now = [100.0]
    bearer = _jwt(4000)
    manager = _manager_with_token(monkeypatch, now, bearer)
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return _IdentityResponse(status_code=code, payload={})

    monkeypatch.setattr(cred.requests, "get", get)
    now[0] = 400.0
    manager._tick()

    # 1 + _BACKGROUND_VALIDATION_RETRIES attempts, each hitting both endpoints.
    assert len(calls) == 2 * (1 + cred._BACKGROUND_VALIDATION_RETRIES)
    assert manager._token == bearer
    assert manager.status()["state"] == "rotating"


def test_transient_then_recovered_validation_adopts_normally(monkeypatch):
    now = [100.0]
    bearer = _jwt(4000)
    manager = _manager_with_token(monkeypatch, now, bearer)
    responses = iter([503, 503, 200, 200])

    def get(url, **kwargs):
        code = next(responses)
        return _IdentityResponse(status_code=code, payload={} if code != 200 else None)

    monkeypatch.setattr(cred.requests, "get", get)
    now[0] = 400.0
    manager._tick()
    status = manager.status()
    assert status["state"] == "rotating"
    assert status["last_error"] is None
    assert status["last_successful_at"] == 400.0


def test_definitive_rejection_still_clears_the_bearer(monkeypatch):
    now = [100.0]
    bearer = _jwt(4000)
    manager = _manager_with_token(monkeypatch, now, bearer)
    monkeypatch.setattr(
        cred.requests, "get", lambda *a, **k: _IdentityResponse(status_code=401, payload={})
    )
    now[0] = 400.0
    manager._tick()
    assert manager._token is None
    assert manager.status()["state"] == "unhealthy"


def test_request_path_does_not_sleep_on_transient_failure(monkeypatch):
    now = [100.0]
    bearer = _jwt(4000)
    manager = _manager_with_token(monkeypatch, now, bearer)
    slept = []
    manager._sleep = slept.append
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        raise cred.requests.RequestException("timeout")

    monkeypatch.setattr(cred.requests, "get", get)
    now[0] += cred.OAUTH_VALIDATION_MAX_AGE + 1  # not fresh → token() probes inline
    assert manager.token() == bearer  # keep-last-good serves the held bearer
    assert slept == []
    assert len(calls) == 2  # one attempt, both endpoints


def test_tick_probes_every_heartbeat_once_expiry_is_near(monkeypatch):
    now = [100.0]
    bearer = _jwt(100 + cred.EXPIRY_PROBE_WINDOW + 30)
    manager = _manager_with_token(monkeypatch, now, bearer)
    probes = []
    monkeypatch.setattr(
        manager, "_self_probe", lambda **kw: probes.append(now[0])
    )

    now[0] = 130.0  # 30 s later, 600 s left: inside the window → probe
    manager._tick()
    now[0] = 160.0  # another heartbeat, still inside → probe again
    manager._tick()
    assert probes == [130.0, 160.0]


def test_tick_stays_on_slow_cadence_when_expiry_is_far(monkeypatch):
    now = [100.0]
    manager = _manager_with_token(monkeypatch, now, _jwt(100 + 3600))
    probes = []
    monkeypatch.setattr(manager, "_self_probe", lambda **kw: probes.append(now[0]))
    manager._last_probe = 100.0
    now[0] = 130.0
    manager._tick()
    now[0] = 100.0 + cred.PROBE_INTERVAL
    manager._tick()
    assert probes == [100.0 + cred.PROBE_INTERVAL]


def test_near_expiry_bearer_is_not_wiped(monkeypatch):
    now = [100.0]
    bearer = _jwt(4000)
    fanned = []
    manager = _manager_with_token(monkeypatch, now, bearer, fanout=fanned.append)
    monkeypatch.setattr(cred, "force_app_identity_refresh", lambda: None)

    now[0] = 4000 - 45  # 45 s left: SDK (blocking mode) has not refreshed yet
    manager._tick()

    assert manager._token == bearer, "attendee files must keep the still-valid bearer"
    assert "too close to expiry" in (manager.status()["last_error"] or "")
    assert manager.status()["state"] == "unhealthy"  # honest about freshness
    assert fanned == [bearer]  # nothing new fanned out, nothing wiped


def test_same_bearer_inside_force_window_triggers_a_forced_mint(monkeypatch):
    now = [100.0]
    old = _jwt(4000, "old")
    new = _jwt(4000 + 3600, "new")
    fanned = []
    manager = _manager_with_token(monkeypatch, now, old, fanout=fanned.append)
    forced = []

    def force():
        forced.append(now[0])
        monkeypatch.setattr(cred, "app_identity_bearer", lambda: new)
        return new

    monkeypatch.setattr(cred, "force_app_identity_refresh", force)

    now[0] = 4000 - 290  # inside EXPIRY_FORCE_WINDOW, SDK still returns `old`
    manager._tick()

    assert forced == [now[0]]
    assert manager._token == new
    assert fanned == [old, new]
    assert manager.status()["state"] == "rotating"


def test_forced_mint_respects_cooldown(monkeypatch):
    now = [100.0]
    old = _jwt(4000, "old")
    manager = _manager_with_token(monkeypatch, now, old)
    forced = []
    monkeypatch.setattr(
        cred, "force_app_identity_refresh", lambda: forced.append(now[0]) or None
    )
    now[0] = 4000 - 290
    manager._tick()
    now[0] += cred.HEARTBEAT
    manager._tick()
    now[0] += cred.FORCE_REFRESH_COOLDOWN
    manager._tick()
    assert forced == [4000 - 290, 4000 - 290 + cred.HEARTBEAT + cred.FORCE_REFRESH_COOLDOWN]


def test_forced_mint_is_skipped_when_sdk_already_rotated(monkeypatch):
    now = [100.0]
    old = _jwt(4000, "old")
    manager = _manager_with_token(monkeypatch, now, old)
    forced = []
    monkeypatch.setattr(cred, "force_app_identity_refresh", lambda: forced.append(1))
    # SDK hands back a *different* bearer that happens to be short-lived.
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: _jwt(4000 + 200, "rotated"))
    now[0] = 4000 - 290
    manager._tick()
    assert forced == []
    assert manager._token == _jwt(4000 + 200, "rotated")


def test_attendee_files_never_hold_an_expired_bearer_over_three_hours(monkeypatch):
    """Worst-case SDK: blocking refresh only when ≤40 s remain, 1 h bearers.

    Walk the maintenance loop heartbeat by heartbeat for three hours and assert
    the most recently fanned-out bearer is never past its ``exp``.
    """
    now = [0.0]
    minted = []

    class FakeSDK:
        def __init__(self):
            self.current = None
            self.mint()

        def mint(self):
            self.current = _jwt(int(now[0]) + 3600, f"t{len(minted)}")
            minted.append(self.current)

        def bearer(self):
            if cred._jwt_expiry(self.current) - now[0] <= 40:
                self.mint()
            return self.current

    sdk = FakeSDK()
    fanned = []
    manager = _manager_with_token(monkeypatch, now, sdk.bearer(), fanout=fanned.append)
    monkeypatch.setattr(cred, "app_identity_bearer", sdk.bearer)

    def force():
        sdk.mint()
        return sdk.current

    monkeypatch.setattr(cred, "force_app_identity_refresh", force)

    while now[0] < 3 * 3600:
        now[0] += cred.HEARTBEAT
        manager._tick()
        on_disk = fanned[-1]
        assert cred._jwt_expiry(on_disk) > now[0], f"expired bearer on disk at t={now[0]}"
        # A replacement must land at least EXPIRY_FORCE_WINDOW - HEARTBEAT early.
        assert cred._jwt_expiry(on_disk) - now[0] > cred.EXPIRY_FORCE_WINDOW - 2 * cred.HEARTBEAT

    assert len(minted) >= 3
    assert manager.status()["state"] == "rotating"
