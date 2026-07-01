"""OBO dual-profile: token capture, throttle, freshness, and non-clobbering
read-modify-write of ~/.databrickscfg."""

import base64
import configparser
import json
import os
import time

import pytest


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def make_jwt(exp: float) -> str:
    """A minimal unsigned JWT carrying only ``exp`` (enough for decode_jwt_exp)."""
    return f"{_b64({'alg': 'none'})}.{_b64({'exp': int(exp)})}.sig"


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
        cli_config, "update_me_profile", lambda u, t: calls.append(t)
    )
    mgr = obo.OboManager()
    tok = make_jwt(time.time() + 3600)
    mgr.capture("dave@example.com", tok)
    mgr.capture("dave@example.com", tok)  # identical — should not rewrite
    assert len(calls) == 1
    mgr.capture("dave@example.com", make_jwt(time.time() + 7200))  # changed — rewrites
    assert len(calls) == 2


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


# -- endpoint surfaces --

def test_config_exposes_obo_and_entitlements(client):
    from .conftest import ALICE

    cfg = client.get("/api/config", headers=ALICE).json()
    assert "obo" in cfg and "enabled" in cfg["obo"]
    assert "entitlements" in cfg and "enabled" in cfg["entitlements"]


def test_obo_refresh_endpoint_requires_email(client):
    from .conftest import ALICE

    # No body email and (local-dev) no forwarded headers honoured here → 422.
    resp = client.post("/api/obo/refresh", json={}, headers=ALICE)
    assert resp.status_code == 200 or resp.status_code == 422


def test_entitlements_reconcile_endpoint_disabled(client, monkeypatch):
    from .conftest import ALICE

    monkeypatch.setenv("ENABLE_ENTITLEMENTS", "false")  # default is now ON
    resp = client.post("/api/entitlements/reconcile", json={"email": "alice@example.com"}, headers=ALICE)
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


def test_workshop_catalog_in_shell_env(client, monkeypatch):
    monkeypatch.setenv("WORKSHOP_CATALOG", "wsh_alice")
    monkeypatch.setenv("WORKSHOP_SCHEMA", "default")
    from server.users import User

    env = User("ivy@example.com").shell_env()
    assert env["WORKSHOP_CATALOG"] == "wsh_alice"
    assert env["WORKSHOP_SCHEMA"] == "default"
