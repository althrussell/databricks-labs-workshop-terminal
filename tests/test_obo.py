"""OBO dual-profile: token capture, throttle, freshness, and non-clobbering
read-modify-write of ~/.databrickscfg."""

import base64
import configparser
import json
import os
import threading
import time

import pytest


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def make_jwt(exp: float, *, scope=None, scp=None) -> str:
    claims = {"exp": int(exp)}
    if scope is not None:
        claims["scope"] = scope
    if scp is not None:
        claims["scp"] = scp
    return f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig"


@pytest.fixture()
def enable_obo(monkeypatch):
    monkeypatch.setenv("ENABLE_OBO", "true")


# -- JWT exp parsing --

def test_decode_jwt_exp_reads_expiry():
    from server import obo

    exp = int(time.time()) + 1800
    assert obo.decode_jwt_exp(make_jwt(exp)) == exp


def test_decode_jwt_exp_none_for_non_jwt():
    from server import obo

    assert obo.decode_jwt_exp("not-a-jwt") is None
    assert obo.decode_jwt_exp("dapi-opaque-token") is None


def test_decode_jwt_scopes_supports_scope_and_scp_claims():
    from server import obo

    scope_token = make_jwt(time.time() + 3600, scope="sql catalog.catalogs:read")
    scp_token = make_jwt(
        time.time() + 3600,
        scp=["catalog.schemas:read", "catalog.tables:read"],
    )

    assert obo.decode_jwt_scopes(scope_token) == {
        "sql",
        "catalog.catalogs:read",
    }
    assert obo.decode_jwt_scopes(scp_token) == {
        "catalog.schemas:read",
        "catalog.tables:read",
    }


def test_status_distinguishes_configured_hint_from_observed_scopes(
    client, monkeypatch, enable_obo
):
    from server import obo
    from server.users import user_manager

    email = "scopes@example.com"
    user_manager.get(email)
    monkeypatch.setenv(
        "OBO_SCOPES",
        "catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql",
    )
    mgr = obo.OboManager()
    mgr.capture(email, make_jwt(time.time() + 3600, scope="sql"))

    status = mgr.status(email)
    assert status["configured_scopes"] == [
        "catalog.catalogs:read",
        "catalog.schemas:read",
        "catalog.tables:read",
        "sql",
    ]
    assert status["observed_scopes"] == ["sql"]
    assert status["verified_scopes"] == ["sql"]
    assert status["validation_state"] == "insufficient"


def test_status_without_attendee_token_reports_validation_pending(
    monkeypatch, enable_obo
):
    from server import obo

    monkeypatch.setenv(
        "OBO_SCOPES",
        "catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql",
    )

    status = obo.OboManager().status()

    assert status["validation_state"] == "pending"
    assert status["observed_scopes"] == []
    assert status["validated_at"] is None


# -- capture / dual profile --

def _read_cfg(home: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(os.path.join(home, ".databrickscfg"))
    return parser


def test_capture_writes_me_profile_and_preserves_default(client, monkeypatch, enable_obo):
    from server import cli_config, obo
    from server.users import user_manager

    user = user_manager.get("alice@example.com")
    cli_config.configure_databricks_cli(user, "sp-default-token")

    mgr = obo.OboManager()
    mgr.capture("alice@example.com", make_jwt(time.time() + 3600))

    cfg = _read_cfg(user.home)
    # [DEFAULT] keeps the SP token; [me] holds the OBO token; both have a host.
    assert cfg["DEFAULT"]["token"] == "sp-default-token"
    assert cfg["me"]["token"].count(".") == 2  # the JWT we captured
    assert cfg["me"]["token"] != cfg["DEFAULT"]["token"]
    assert cfg["me"]["host"] == cfg["DEFAULT"]["host"]


def test_default_rotation_does_not_clobber_me(client, monkeypatch, enable_obo):
    from server import cli_config, obo
    from server.users import user_manager

    user = user_manager.get("bob@example.com")
    obo.OboManager().capture("bob@example.com", make_jwt(time.time() + 3600))
    # SP rotation writes DEFAULT afterwards — [me] must survive.
    cli_config.configure_databricks_cli(user, "rotated-sp-token")

    cfg = _read_cfg(user.home)
    assert cfg["DEFAULT"]["token"] == "rotated-sp-token"
    assert "me" in cfg
    assert cfg["me"]["token"].count(".") == 2


def test_databrickscfg_is_0600(client, monkeypatch, enable_obo):
    from server import obo
    from server.users import user_manager

    user = user_manager.get("carol@example.com")
    obo.OboManager().capture("carol@example.com", make_jwt(time.time() + 3600))
    mode = os.stat(os.path.join(user.home, ".databrickscfg")).st_mode & 0o777
    assert mode == 0o600


def test_capture_throttles_identical_token(client, monkeypatch, enable_obo):
    from server import cli_config, obo
    from server.users import user_manager

    user_manager.get("dave@example.com")
    calls = []
    monkeypatch.setattr(
        cli_config, "update_me_profile_locked", lambda u, t: calls.append(t)
    )
    mgr = obo.OboManager()
    tok = make_jwt(time.time() + 3600)
    mgr.capture("dave@example.com", tok)
    mgr.capture("dave@example.com", tok)  # identical — should not rewrite
    assert len(calls) == 1
    mgr.capture("dave@example.com", make_jwt(time.time() + 7200))  # changed — rewrites
    assert len(calls) == 2


def test_capture_before_user_creation_writes_when_home_is_ready(
    client, monkeypatch, enable_obo
):
    from server import obo
    from server.users import user_manager

    email = "deferred-after-restart@example.com"
    token = make_jwt(time.time() + 3600)
    assert user_manager.peek(email) is None

    obo.obo_manager.capture(email, token)
    assert obo.obo_manager.status(email)["present"] is False

    user = user_manager.get(email)

    cfg = _read_cfg(user.home)
    assert cfg["me"]["token"] == token
    assert obo.obo_manager.status(email)["present"] is True


def test_concurrent_older_write_cannot_overwrite_newer_token(
    client, monkeypatch, enable_obo
):
    from server import cli_config, obo
    from server.users import user_manager

    email = "ordered-writes@example.com"
    user = user_manager.get(email)
    old_token = make_jwt(time.time() + 3600)
    new_token = make_jwt(time.time() + 7200)
    old_write_entered = threading.Event()
    release_old_write = threading.Event()
    new_write_finished = threading.Event()
    original_update = cli_config.update_me_profile_locked

    def delayed_update(target_user, token):
        if token == old_token:
            old_write_entered.set()
            assert release_old_write.wait(2)
        original_update(target_user, token)
        if token == new_token:
            new_write_finished.set()

    monkeypatch.setattr(cli_config, "update_me_profile_locked", delayed_update)
    mgr = obo.OboManager()
    old_thread = threading.Thread(target=mgr.capture, args=(email, old_token))
    old_thread.start()
    assert old_write_entered.wait(1)

    new_thread = threading.Thread(target=mgr.capture, args=(email, new_token))
    new_thread.start()
    new_write_finished.wait(0.2)
    release_old_write.set()
    old_thread.join(2)
    new_thread.join(2)
    assert not old_thread.is_alive() and not new_thread.is_alive()

    cfg = _read_cfg(user.home)
    assert cfg["me"]["token"] == new_token
    assert mgr._by_email[email].written_token == new_token
    assert mgr.status(email)["fresh"] is True


def test_status_cannot_observe_disk_write_before_record_commit(
    client, monkeypatch, enable_obo
):
    from server import cli_config, obo
    from server.users import user_manager

    email = "atomic-status@example.com"
    user_manager.get(email)
    token = make_jwt(time.time() + 3600)
    disk_written = threading.Event()
    release_write = threading.Event()
    status_finished = threading.Event()
    status_result = {}
    original_update = cli_config.update_me_profile_locked

    def paused_after_disk_write(target_user, captured_token):
        original_update(target_user, captured_token)
        disk_written.set()
        assert release_write.wait(2)

    monkeypatch.setattr(
        cli_config, "update_me_profile_locked", paused_after_disk_write
    )
    mgr = obo.OboManager()
    writer = threading.Thread(target=mgr.capture, args=(email, token))
    writer.start()
    assert disk_written.wait(1)

    def read_status():
        status_result.update(mgr.status(email))
        status_finished.set()

    reader = threading.Thread(target=read_status)
    reader.start()
    status_was_blocked = not status_finished.wait(0.1)
    release_write.set()
    writer.join(2)
    reader.join(2)

    assert status_was_blocked
    assert status_result["present"] is True
    assert status_result["fresh"] is True


def test_capture_disabled_writes_nothing(client, monkeypatch):
    monkeypatch.delenv("ENABLE_OBO", raising=False)
    from server import cli_config, obo
    from server.users import user_manager

    user_manager.get("erin@example.com")
    calls = []
    monkeypatch.setattr(cli_config, "update_me_profile", lambda u, t: calls.append(t))
    obo.OboManager().capture("erin@example.com", make_jwt(time.time() + 3600))
    assert calls == []


# -- force refresh --

def test_force_refresh_writes_latest_and_false_when_empty(client, monkeypatch, enable_obo):
    from server import obo
    from server.users import user_manager

    user_manager.get("frank@example.com")
    mgr = obo.OboManager()
    assert mgr.force_refresh("frank@example.com") is False  # nothing captured yet
    mgr.capture("frank@example.com", make_jwt(time.time() + 3600))
    assert mgr.force_refresh("frank@example.com") is True


# -- status / freshness --

def test_status_fresh_and_stale(client, monkeypatch, enable_obo):
    from server import obo
    from server.users import user_manager

    user_manager.get("gita@example.com")
    mgr = obo.OboManager()
    mgr.capture("gita@example.com", make_jwt(time.time() + 3600))
    st = mgr.status("gita@example.com")
    assert st["enabled"] is True and st["present"] is True and st["fresh"] is True
    assert st["expires_in"] > 0

    mgr.capture("gita@example.com", make_jwt(time.time() - 10))  # already expired
    st = mgr.status("gita@example.com")
    assert st["fresh"] is False


def test_status_disabled():
    from server import obo

    st = obo.OboManager().status("nobody@example.com")
    assert st["enabled"] is False
    assert st["present"] is False


# -- shell env never carries the OBO token --

def test_obo_token_absent_from_shell_env(client, monkeypatch, enable_obo):
    from server import obo
    from server.users import user_manager

    user = user_manager.get("hugo@example.com")
    tok = make_jwt(time.time() + 3600)
    obo.OboManager().capture("hugo@example.com", tok)
    env = user.shell_env()
    assert all(tok not in v for v in env.values())
    # The profile name hint is exposed (non-secret) so databricks-me resolves it.
    assert env.get("OBO_PROFILE_NAME") == "me"


# -- authenticated helper endpoint surfaces --

def test_config_exposes_obo_and_entitlements(client):
    from .conftest import ALICE

    cfg = client.get("/api/config", headers=ALICE).json()
    assert "obo" in cfg and "enabled" in cfg["obo"]
    assert "entitlements" in cfg and "enabled" in cfg["entitlements"]


def _callback_capability(email: str) -> str:
    from server import user_content
    from server.users import user_manager

    user = user_manager.get(email)
    user_content._write_callback_capability(user)
    with open(user_content.callback_capability_path(user)) as f:
        return f.read().strip()


def test_callback_capability_is_0600_and_helpers_send_it(client):
    from server import user_content
    from server.users import user_manager

    user = user_manager.get("helper@example.com")
    user_content._write_callback_capability(user)
    path = user_content.callback_capability_path(user)
    assert os.stat(path).st_mode & 0o777 == 0o600

    assets = os.path.join(os.path.dirname(__file__), "..", "assets", "bin")
    for helper in ("databricks-me", "workshop-grant-me"):
        text = open(os.path.join(assets, helper)).read()
        assert "X-Workshop-Capability" in text
        assert "callback-capability" in text


def test_obo_refresh_rejects_unauthenticated_email(client):
    resp = client.post("/api/obo/refresh", json={"email": "alice@example.com"})
    assert resp.status_code == 403


def test_browser_callback_cannot_override_email(client):
    from .conftest import ALICE

    resp = client.post(
        "/api/obo/refresh",
        json={"email": "bob@example.com"},
        headers=ALICE,
    )
    assert resp.status_code == 403


def test_browser_callback_preserves_forwarded_access_token(client, monkeypatch):
    from server import obo
    from .conftest import ALICE

    captured = []
    monkeypatch.setattr(
        obo.obo_manager,
        "capture",
        lambda email, token: captured.append((email, token)),
    )
    monkeypatch.setattr(obo.obo_manager, "force_refresh", lambda email: False)
    headers = {**ALICE, "X-Forwarded-Access-Token": "forwarded-obo-token"}

    resp = client.post("/api/obo/refresh", json={}, headers=headers)

    assert resp.status_code == 200
    assert captured
    assert set(captured) == {("alice@example.com", "forwarded-obo-token")}


def test_obo_refresh_nudge_does_not_broadcast_attendee_identity(client, monkeypatch):
    from server import obo
    from server.main import event_hub
    from .conftest import ALICE

    published = []
    monkeypatch.setattr(obo.obo_manager, "capture", lambda email, token: None)
    monkeypatch.setattr(obo.obo_manager, "force_refresh", lambda email: False)
    monkeypatch.setattr(event_hub, "publish", published.append)

    resp = client.post(
        "/api/obo/refresh",
        json={},
        headers={**ALICE, "X-Forwarded-Access-Token": "forwarded-obo-token"},
    )

    assert resp.status_code == 200
    assert published == [{"t": "obo_refresh"}]


def test_capability_is_bound_to_attendee(client):
    alice_capability = _callback_capability("cap-alice@example.com")
    headers = {"X-Workshop-Capability": alice_capability}

    own = client.post(
        "/api/obo/refresh",
        json={"email": "cap-alice@example.com"},
        headers=headers,
    )
    spoof = client.post(
        "/api/obo/refresh",
        json={"email": "cap-bob@example.com"},
        headers=headers,
    )
    assert own.status_code == 200
    assert spoof.status_code == 403


def test_entitlements_reconcile_endpoint_disabled(client, monkeypatch):
    monkeypatch.setenv("ENABLE_ENTITLEMENTS", "false")  # default is now ON
    capability = _callback_capability("ent-alice@example.com")
    resp = client.post(
        "/api/entitlements/reconcile",
        json={"email": "ent-alice@example.com"},
        headers={"X-Workshop-Capability": capability},
    )
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


def test_workshop_catalog_in_shell_env(client, monkeypatch):
    monkeypatch.setenv("WORKSHOP_CATALOG", "wsh_alice")
    monkeypatch.setenv("WORKSHOP_SCHEMA", "default")
    from server.users import User

    env = User("ivy@example.com").shell_env()
    assert env["WORKSHOP_CATALOG"] == "wsh_alice"
    assert env["WORKSHOP_SCHEMA"] == "default"
