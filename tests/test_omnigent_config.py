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


@pytest.fixture(autouse=True)
def _omnigent_on(monkeypatch):
    """Most tests here exercise the omnigent path, which is feature-flagged off
    by default. Turn it on; the dedicated gating test overrides this."""
    monkeypatch.setenv("OMNIGENT_ENABLED", "true")


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
    # Only opus-4-7 available → the sonnet-first chain degrades all the way
    # through to it; codex default unaffected.
    assert "default: databricks-claude-opus-4-7" in text
    assert "default: databricks-gpt-5-5" in text


def test_default_claude_model_is_sonnet_5(user, monkeypatch):
    """With models available and no ANTHROPIC_MODEL pin, the default Claude
    model is Sonnet 5 (not Opus)."""
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    monkeypatch.setattr(
        cli_config, "_discover_serving_endpoints",
        lambda token: {"databricks-claude-sonnet-5", "databricks-claude-opus-4-8"},
    )
    cli_config.configure_omnigent(user, "tok-1")
    with open(_config_path(user)) as f:
        text = f.read()
    assert "default: databricks-claude-sonnet-5" in text
    assert "default: databricks-claude-opus-4-8" not in text


def test_runner_idle_timeout_written(user, monkeypatch):
    """The runner idle-timeout knob must land in config.yaml so Omnigent's
    background runner doesn't self-terminate after 1h ("Runner disconnected")."""
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    monkeypatch.setenv("OMNIGENT_RUNNER_IDLE_TIMEOUT_S", "0")
    cli_config.configure_omnigent(user, "tok-1")
    with open(_config_path(user)) as f:
        text = f.read()
    assert "runner:" in text
    assert "idle_timeout_s: 0" in text


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


# -- claude / codex dynamic auth (survive rotation without process restart) --

def test_claude_uses_apikeyhelper_not_static_token(user, monkeypatch):
    """settings.json must carry an apiKeyHelper (reading the rotating token
    file) and NO static ANTHROPIC_AUTH_TOKEN — else the helper is dormant and a
    live process keeps 401ing on the revoked startup token."""
    import json
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    cli_config.configure_claude(user, "tok-1")
    settings = json.load(open(os.path.join(user.home, ".claude", "settings.json")))
    assert "ANTHROPIC_AUTH_TOKEN" not in settings["env"]
    assert settings["apiKeyHelper"] == f"cat {_token_path(user)}"
    assert settings["env"]["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] == "240000"
    with open(_token_path(user)) as f:
        assert f.read().strip() == "tok-1"


def test_codex_uses_auth_command_not_env_key(user, monkeypatch):
    """config.toml must route auth through a provider auth command reading the
    rotating token file, not a static env_key/.env captured at startup."""
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    cli_config.configure_codex(user, "tok-1")
    toml = open(os.path.join(user.home, ".codex", "config.toml")).read()
    assert "[model_providers.databricks.auth]" in toml
    assert 'command = "cat"' in toml
    assert _token_path(user) in toml
    assert "refresh_interval_ms" in toml
    assert "env_key" not in toml
    assert not os.path.exists(os.path.join(user.home, ".codex", ".env"))


def test_rotation_updates_token_file_for_all_agents(user, monkeypatch):
    """A single token-file rewrite is the whole rotation for claude+codex+
    omnigent; their generated configs are not touched (only the file rotates)."""
    import json
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    cli_config.configure_all(user, "tok-1")
    settings_path = os.path.join(user.home, ".claude", "settings.json")
    codex_toml_path = os.path.join(user.home, ".codex", "config.toml")
    s_mtime = os.stat(settings_path).st_mtime_ns
    c_mtime = os.stat(codex_toml_path).st_mtime_ns
    time.sleep(0.01)

    cli_config.update_tokens(user, "tok-2")

    with open(_token_path(user)) as f:
        assert f.read().strip() == "tok-2"
    # Configs untouched — the dynamic auth commands re-read the rotated file.
    assert os.stat(settings_path).st_mtime_ns == s_mtime
    assert os.stat(codex_toml_path).st_mtime_ns == c_mtime
    # And the static-token trap is still absent after rotation.
    settings = json.load(open(settings_path))
    assert "ANTHROPIC_AUTH_TOKEN" not in settings["env"]


def test_rotation_creates_config_when_absent(user, monkeypatch):
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    cli_config.update_tokens(user, "tok-1")
    assert os.path.exists(_config_path(user))
    with open(_token_path(user)) as f:
        assert f.read().strip() == "tok-1"


# -- installer version stamp --

def test_version_stamp_match_short_circuits(monkeypatch, tmp_path):
    """A pinned spec whose stamp already matches must NOT reinstall."""
    from server import config
    monkeypatch.setattr(config, "shared_prefix", lambda: str(tmp_path))
    monkeypatch.setattr(install, "OMNIGENT_PIP_SPEC", "omnigent==0.1.0")
    os.makedirs(tmp_path / "bin")
    (tmp_path / "bin" / "omnigent").write_text("#!/bin/sh\n")
    (tmp_path / "omnigent.version").write_text(install.OMNIGENT_PIP_SPEC)

    def boom(*a, **kw):
        raise AssertionError("must not reinstall on stamp match")

    monkeypatch.setattr(install.subprocess, "run", boom)
    install._install_omnigent()
    assert install.status()["steps"]["omnigent"]["status"] == "complete"


def test_unpinned_spec_always_reinstalls(monkeypatch, tmp_path):
    """A bare 'omnigent' (unpinned, tracking latest) must reinstall even when
    the binary + a matching stamp already exist — otherwise the first-installed
    version freezes forever on the persistent volume."""
    from server import config
    monkeypatch.setattr(config, "shared_prefix", lambda: str(tmp_path))
    monkeypatch.setattr(install, "OMNIGENT_PIP_SPEC", "omnigent")
    os.makedirs(tmp_path / "bin")
    (tmp_path / "bin" / "omnigent").write_text("#!/bin/sh\n")
    (tmp_path / "omnigent.version").write_text("omnigent")  # stamp matches spec
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
    assert any("tool" in c and "install" in c for c in calls if isinstance(c, list)), (
        "unpinned spec must not short-circuit on stamp match"
    )
    assert install.status()["steps"]["omnigent"]["status"] == "complete"


def test_is_pinned_classification():
    assert install._is_pinned("omnigent==0.1.0")
    assert install._is_pinned("git+https://github.com/omnigent-ai/omnigent")
    assert install._is_pinned("/app/python/source_code/wheels/omnigent-0.1.0-py3-none-any.whl")
    assert not install._is_pinned("omnigent")


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


def test_feature_flag_off_hides_omnigent_everywhere(client, monkeypatch):
    """Default (OFF): no catalog entry, launch rejected, configs/installers skipped."""
    monkeypatch.setenv("OMNIGENT_ENABLED", "false")

    # Catalog: omnigent absent; the other agents are unaffected.
    from server import agents as agents_mod
    ids = [a["id"] for a in agents_mod.load_catalog()]
    assert "omnigent" not in ids
    assert ids == ["claude", "codex", "bash"]
    assert agents_mod.get_agent("omnigent") is None

    # API surface: omnigent not offered.
    body = client.get("/api/agents", headers={"X-Forwarded-Email": "a@b.co"}).json()
    assert "omnigent" not in {a["id"] for a in body["agents"]}

    # Credential config: configure_all writes no omnigent config and does not
    # mark omnigent ready.
    u = User("flagoff@example.com")
    u.bootstrap_home()
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    cli_config.configure_all(u, "tok-1")
    assert not os.path.exists(_config_path(u))
    assert "omnigent" not in u.cli_ready

    # Installer: omnigent/tmux steps are never registered.
    install._state.clear()
    monkeypatch.setattr(install, "_install_node", lambda: install._set("node", "complete"))
    for fn in ("_install_claude", "_install_codex", "_install_databricks_cli",
               "_install_skills", "_install_tmux", "_install_omnigent"):
        monkeypatch.setattr(install, fn, lambda *a, **k: None)
    install.run_in_background()
    import time as _t
    _t.sleep(0.2)
    steps = install.status()["steps"]
    assert "omnigent" not in steps
    assert "tmux" not in steps
