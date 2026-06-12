"""P1-2: mint CLI tokens from the app's own OAuth identity (no attendee-readable PAT)."""

import pytest

from server import credentials as cred


class _Resp:
    status_code = 200

    @staticmethod
    def json():
        return {"token_value": "minted-short-lived",
                "token_info": {"token_id": "tok_1"}}


@pytest.fixture
def fresh_manager():
    # A clean manager per test (the module singleton carries state across tests).
    m = cred.CredentialManager(lambda: 1)
    return m


def test_app_identity_skipped_in_local_dev(monkeypatch):
    # conftest sets LOCAL_DEV=1 → the SDK auth probe is skipped (no hang, None).
    assert cred.app_identity_bearer() is None


def test_bootstrap_prefers_app_identity_over_pat(monkeypatch):
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "app-oauth-bearer")
    monkeypatch.setenv("WORKSHOP_PAT", "dapi-legacy")
    assert cred._bootstrap_auth() == "app-oauth-bearer"  # app identity wins


def test_token_minted_via_app_identity_without_pat(fresh_manager, monkeypatch):
    # No WORKSHOP_PAT, but the app identity can mint → token() returns the minted
    # short-lived token, never a static admin PAT.
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "app-oauth-bearer")
    monkeypatch.setattr(cred, "config", cred.config)
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(cred.requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(fresh_manager, "_fanout", lambda tok: None)

    assert fresh_manager.token() == "minted-short-lived"
    st = fresh_manager.status()
    assert st["configured"] is True and st["rotating"] is True


def test_token_raises_without_app_identity_or_pat(fresh_manager, monkeypatch):
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: None)
    with pytest.raises(cred.CredentialError):
        fresh_manager.token()


def test_vended_pat_still_works_as_fallback(fresh_manager, monkeypatch):
    # Legacy path: no app identity, token mint unavailable → serve the PAT.
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: None)
    monkeypatch.setenv("WORKSHOP_PAT", "dapi-legacy")
    monkeypatch.setattr(cred.config, "databricks_host", lambda: "https://x.test")

    class _Fail:
        status_code = 403
        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(cred.requests, "post", lambda *a, **k: _Fail())
    assert fresh_manager.token() == "dapi-legacy"  # mint failed → vended PAT served


def test_start_runs_in_app_identity_mode(fresh_manager, monkeypatch):
    monkeypatch.delenv("WORKSHOP_PAT", raising=False)
    monkeypatch.setattr(cred, "app_identity_bearer", lambda: "app-oauth-bearer")
    fresh_manager.start()
    assert fresh_manager._bootstrap_ok is True
    fresh_manager.stop()
