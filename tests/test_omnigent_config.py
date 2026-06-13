"""Omnigent integration: provider config, token rotation, installer, catalog.

The provider config (~/.omnigent/config.yaml) is written once and must stay
untouched by rotation — only the gateway-token file rotates. The installer's
version stamp (not exists→done) is what makes the GA bump a pure env change.
"""

import os
import time

import pytest

from server import cli_config
from server.bootstrap import install
from server.users import User


@pytest.fixture()
def user(_test_env):
    u = User("omni-test@example.com")
    u.bootstrap_home()
    return u


@pytest.fixture(autouse=True)
def _no_endpoint_discovery(monkeypatch):
    monkeypatch.setattr(cli_config, "_discover_serving_endpoints", lambda token: set())


def _config_path(user):
    return os.path.join(user.home, ".omnigent", "config.yaml")


def _token_path(user):
    return os.path.join(user.home, ".config", "workshop", "gateway-token")


# -- configure_omnigent --

def test_gateway_urls_and_auth_command(user, monkeypatch):
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "https://123.ai-gateway.cloud.databricks.com")
    cli_config.configure_omnigent(user, "tok-1")
    with open(_config_path(user)) as f:
        text = f.read()
    assert "base_url: https://123.ai-gateway.cloud.databricks.com/anthropic" in text
    assert "base_url: https://123.ai-gateway.cloud.databricks.com/openai/v1" in text
    assert "wire_api: responses" in text
    assert "kind: gateway" in text
    assert "default: true" in text
    # auth_command must carry the absolute per-user token path.
    assert f"auth_command: cat {_token_path(user)}" in text
    assert os.path.isabs(_token_path(user))


def test_serving_endpoints_fallback_urls(user, monkeypatch):
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    cli_config.configure_omnigent(user, "tok-1")
    with open(_config_path(user)) as f:
        text = f.read()
    host = "https://test.cloud.databricks.com"
    assert f"base_url: {host}/serving-endpoints/anthropic" in text
    assert f"base_url: {host}/serving-endpoints" in text


def test_token_file_written_0600(user, monkeypatch):
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    cli_config.configure_omnigent(user, "tok-secret")
    path = _token_path(user)
    with open(path) as f:
        assert f.read().strip() == "tok-secret"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_model_defaults_pick_from_available(user, monkeypatch):
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    monkeypatch.setattr(
        cli_config, "_discover_serving_endpoints",
        lambda token: {"databricks-claude-opus-4-7"},
    )
    cli_config.configure_omnigent(user, "tok-1")
    with open(_config_path(user)) as f:
        text = f.read()
    # Opus 4.8 absent → chain degrades to 4.7; codex default unaffected.
    assert "default: databricks-claude-opus-4-7" in text
    assert "default: databricks-gpt-5-5" in text


# -- update_tokens --

def test_rotation_rewrites_token_not_config(user, monkeypatch):
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    cli_config.configure_all(user, "tok-1")
    config_mtime = os.stat(_config_path(user)).st_mtime_ns
    time.sleep(0.01)
    cli_config.update_tokens(user, "tok-2")
    with open(_token_path(user)) as f:
        assert f.read().strip() == "tok-2"
    assert os.stat(_config_path(user)).st_mtime_ns == config_mtime, (
        "rotation must not rewrite config.yaml"
    )


def test_rotation_creates_config_when_absent(user, monkeypatch):
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    cli_config.update_tokens(user, "tok-1")
    assert os.path.exists(_config_path(user))
    with open(_token_path(user)) as f:
        assert f.read().strip() == "tok-1"


# -- installer version stamp --

def test_version_stamp_match_short_circuits(monkeypatch, tmp_path):
    from server import config
    monkeypatch.setattr(config, "shared_prefix", lambda: str(tmp_path))
    os.makedirs(tmp_path / "bin")
    (tmp_path / "bin" / "omnigent").write_text("#!/bin/sh\n")
    (tmp_path / "omnigent.version").write_text(install.OMNIGENT_PIP_SPEC)

    def boom(*a, **kw):
        raise AssertionError("must not reinstall on stamp match")

    monkeypatch.setattr(install.subprocess, "run", boom)
    install._install_omnigent()
    assert install.status()["steps"]["omnigent"]["status"] == "complete"


def test_version_stamp_mismatch_reinstalls(monkeypatch, tmp_path):
    from server import config
    monkeypatch.setattr(config, "shared_prefix", lambda: str(tmp_path))
    os.makedirs(tmp_path / "bin")
    (tmp_path / "bin" / "omnigent").write_text("#!/bin/sh\n")
    (tmp_path / "omnigent.version").write_text("0.0.0-old")
    calls = []

    class FakeResult:
        returncode = 0
        stdout = stderr = ""

    def fake_run(argv, **kw):
        calls.append(argv)
        return FakeResult()

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    monkeypatch.setattr(install.shutil, "which", lambda *a, **kw: "/usr/bin/uv")
    install._install_omnigent()
    assert any("tool" in c and "install" in c for c in calls if isinstance(c, list))
    assert (tmp_path / "omnigent.version").read_text() == install.OMNIGENT_PIP_SPEC
    assert install.status()["steps"]["omnigent"]["status"] == "complete"


# -- readiness / catalog gating --

def test_omnigent_ready_needs_both_tmux_and_omnigent():
    install._set("omnigent", "complete")
    install._set("tmux", "running")
    assert install.status()["ready"]["omnigent"] is False
    install._set("tmux", "complete")
    assert install.status()["ready"]["omnigent"] is True


def test_catalog_gates_omnigent_entries(client, monkeypatch):
    from server.bootstrap import install as install_mod

    def fake_status():
        return {
            "steps": {},
            "ready": {"bash": True, "claude": True, "codex": True, "omnigent": False},
            "installing": True,
        }

    monkeypatch.setattr(install_mod, "status", fake_status)
    agents = {
        a["id"]: a for a in client.get("/api/agents", headers={"X-Forwarded-Email": "a@b.co"}).json()["agents"]
    }
    # Omnigent gates on its own readiness key; claude/codex are independent.
    assert agents["omnigent"]["ready"] is False
    assert agents["claude"]["ready"] is True
    assert agents["codex"]["ready"] is True
    # One button per agent: Omnigent leads, then the direct CLIs.
    ordered = [a["id"] for a in sorted(agents.values(), key=lambda a: a["order"])]
    assert ordered == ["omnigent", "claude", "codex", "bash"]
