"""Omnigent integration: provider config, token rotation, installer, catalog.

The provider config (~/.omnigent/config.yaml) is written once and must stay
untouched by rotation — only the gateway-token file rotates. The installer's
version stamp (not exists→done) is what makes the GA bump a pure env change.
"""

import json
import os
import shutil
import time

import pytest
import yaml

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


def test_host_identity_matches_the_supervised_host(user, monkeypatch):
    """Every omnigent command in this home must target the host we run for it.

    The CLI resolves the host to launch a runner on from this file, so without
    the section it minted a fresh uuid and waited 30s for a daemon nobody was
    running: "The connect daemon for host '...' did not come online within 30s."
    """
    from server import omnigent_remote

    monkeypatch.setenv("OMNIGENT_APP_URL", "https://omni.example.databricksapps.com")
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")

    cli_config.configure_omnigent(user, "tok-1")

    with open(_config_path(user)) as handle:
        document = yaml.safe_load(handle)
    expected = omnigent_remote.stable_host_identity(
        user, "https://omni.example.databricksapps.com"
    )
    assert document["host"] == {"host_id": expected.host_id, "name": expected.name}


def test_config_identity_equals_the_supervised_launch_environment(user, monkeypatch):
    """The two mechanisms must not drift: same attendee, same host.

    The CLI takes the host from the config file and the supervised host takes it
    from its launch env. If those ever disagree the terminal waits out its
    timeout for a daemon nobody runs, which is exactly what a missing section
    caused.
    """
    from server import omnigent_remote

    url = "https://omni.example.databricksapps.com"
    monkeypatch.setenv("OMNIGENT_APP_URL", url)
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")

    cli_config.configure_omnigent(user, "tok-1")
    _, launch_env, _ = omnigent_remote.build_host_launch(user, "/usr/bin/omnigent", url)

    with open(_config_path(user)) as handle:
        document = yaml.safe_load(handle)
    assert document["host"]["host_id"] == launch_env["OMNIGENT_HOST_ID"]
    assert document["host"]["name"] == launch_env["OMNIGENT_HOST_NAME"]


def test_a_host_identity_the_cli_invented_is_corrected(user, monkeypatch):
    """An attendee whose home already has a stray identity must be repaired.

    The CLI persists a uuid the first time it runs without this section, and it
    keeps that stale id forever after — pointing the terminal at a host that
    does not exist while the supervised one sits online beside it.
    """
    from server import omnigent_remote

    monkeypatch.setenv("OMNIGENT_APP_URL", "https://omni.example.databricksapps.com")
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    os.makedirs(os.path.dirname(_config_path(user)), exist_ok=True)
    with open(_config_path(user), "w") as handle:
        yaml.safe_dump(
            {"host": {"host_id": "42565f209abe45b897ac35606216b964", "name": "stray"}},
            handle,
        )

    cli_config.configure_omnigent(user, "tok-1")

    with open(_config_path(user)) as handle:
        document = yaml.safe_load(handle)
    expected = omnigent_remote.stable_host_identity(
        user, "https://omni.example.databricksapps.com"
    )
    assert document["host"]["host_id"] == expected.host_id


def test_local_omnigent_keeps_its_own_host_identity(monkeypatch):
    """With no App to host against, the CLI owns the identity as it always did."""
    monkeypatch.delenv("OMNIGENT_APP_URL", raising=False)
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    local_user = User("omni-local@example.com")
    local_user.bootstrap_home()

    cli_config.configure_omnigent(local_user, "tok-1")

    with open(_config_path(local_user)) as handle:
        assert "host" not in (yaml.safe_load(handle) or {})


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


def test_configure_restart_preserves_upstream_host_identity_and_unrelated_keys(
    user, monkeypatch
):
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    path = _config_path(user)
    with open(path, "w") as handle:
        yaml.safe_dump(
            {
                "host": {"id": "stable-host-id", "name": "workshop-host"},
                "unrelated": {"keep": True},
                "providers": {"upstream-custom": {"kind": "anthropic"}},
            },
            handle,
        )

    cli_config.configure_omnigent(user, "tok-1")
    first = yaml.safe_load(open(path))
    cli_config.configure_omnigent(user, "tok-2")
    restarted = yaml.safe_load(open(path))

    assert restarted["host"] == {
        "id": "stable-host-id",
        "name": "workshop-host",
    }
    assert restarted["unrelated"] == {"keep": True}
    assert restarted["providers"]["upstream-custom"] == {"kind": "anthropic"}
    assert restarted["providers"]["databricks-gateway"]["default"] is True
    assert restarted == first
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert not [name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")]


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


def _mock_omnigent_artifacts(monkeypatch, tmp_path):
    lock = tmp_path / "omnigent.lock"
    lock.write_text(
        f"omnigent=={install.OMNIGENT_VERSION} --hash=sha256:{'a' * 64}\n"
    )
    archives = {
        "uv_binary": "uv-linux/uv",
        "python_3_12_runtime": "python/bin/python3.12",
    }
    entries = {
        "omnigent_lock": {
            "source": str(lock),
            "sha256": install._file_checksum(lock),
        },
    }
    for name, relative in archives.items():
        staged = tmp_path / f"{name}-src"
        (staged / relative).parent.mkdir(parents=True, exist_ok=True)
        (staged / relative).write_bytes(name.encode())
        archive = shutil.make_archive(
            str(tmp_path / name), "gztar", root_dir=str(staged)
        )
        entries[name] = {
            "source": archive,
            "sha256": install._file_checksum(archive),
            "kind": "archive",
            "executable_relative_path": relative,
        }
    monkeypatch.setattr(
        install,
        "_verified_artifact",
        lambda name: (entries[name]["source"], entries[name]),
    )
    return entries


def _write_omnigent_stamp(tmp_path, entries):
    venv_python = tmp_path / "omnigent-venv/bin/python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_bytes(b"venv-python")
    site = tmp_path / "omnigent-venv/lib/python3.12/site-packages/omnigent.py"
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_bytes(b"omnigent")
    (tmp_path / "omnigent.install.json").write_text(json.dumps({
        "uv_sha256": entries["uv_binary"]["sha256"],
        "python_runtime_sha256": entries["python_3_12_runtime"]["sha256"],
        "lock_sha256": entries["omnigent_lock"]["sha256"],
        "binary_sha256": install._file_checksum(tmp_path / "bin" / "omnigent"),
        "venv_sha256": install._directory_checksum(tmp_path / "omnigent-venv"),
    }))


def test_version_stamp_match_short_circuits(monkeypatch, tmp_path):
    """A pinned spec whose stamp already matches must NOT reinstall."""
    from server import config
    monkeypatch.setattr(config, "shared_prefix", lambda: str(tmp_path))
    monkeypatch.setattr(install, "OMNIGENT_VERSION", "0.1.0")
    monkeypatch.setattr(install, "_read_cli_version", lambda _: "0.1.0")
    os.makedirs(tmp_path / "bin")
    (tmp_path / "bin" / "omnigent").write_text("#!/bin/sh\n")
    entries = _mock_omnigent_artifacts(monkeypatch, tmp_path)
    _write_omnigent_stamp(tmp_path, entries)

    def boom(*a, **kw):
        raise AssertionError("must not reinstall on stamp match")

    monkeypatch.setattr(install.subprocess, "run", boom)
    install._install_omnigent()
    assert install.status()["steps"]["omnigent"]["status"] == "complete"


def test_reviewed_stamp_cannot_trigger_network_resolution(monkeypatch, tmp_path):
    from server import config
    monkeypatch.setattr(config, "shared_prefix", lambda: str(tmp_path))
    os.makedirs(tmp_path / "bin")
    (tmp_path / "bin" / "omnigent").write_text("#!/bin/sh\n")
    entries = _mock_omnigent_artifacts(monkeypatch, tmp_path)
    _write_omnigent_stamp(tmp_path, entries)
    monkeypatch.setattr(install, "_read_cli_version", lambda _: install.OMNIGENT_VERSION)
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("reviewed reusable install must not resolve network")
        ),
    )
    install._install_omnigent()
    assert install.status()["steps"]["omnigent"]["status"] == "complete"
def test_version_stamp_mismatch_reinstalls(monkeypatch, tmp_path):
    from server import config
    monkeypatch.setattr(config, "shared_prefix", lambda: str(tmp_path))
    os.makedirs(tmp_path / "bin")
    (tmp_path / "bin" / "omnigent").write_text("#!/bin/sh\n")
    _mock_omnigent_artifacts(monkeypatch, tmp_path)
    (tmp_path / "omnigent.install.json").write_text("{}")
    calls = []

    class FakeResult:
        returncode = 0
        stdout = stderr = ""

    def fake_run(argv, **kw):
        calls.append(argv)
        venv_bin = tmp_path / "omnigent-venv/bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        (venv_bin / "python").write_bytes(b"python")
        if argv[1] == "pip":
            (venv_bin / "omnigent").write_bytes(b"omnigent")
        return FakeResult()

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    versions = ["0.4.0", install.OMNIGENT_VERSION]
    monkeypatch.setattr(
        install,
        "_read_cli_version",
        lambda _: versions.pop(0) if len(versions) > 1 else versions[0],
    )
    install._install_omnigent()
    assert any(c[1:3] == ["pip", "install"] for c in calls)
    assert json.loads((tmp_path / "omnigent.install.json").read_text())[
        "lock_sha256"
    ]
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
    # The model-set variants are the same CLI under a different agent name, so
    # they must gate on that same key rather than appearing launchable early.
    for variant in ("omnigent-economy", "omnigent-balanced", "omnigent-frontier"):
        assert agents[variant]["ready"] is False
    # One button per agent: the Omnigent family leads, then the direct CLIs.
    ordered = [a["id"] for a in sorted(agents.values(), key=lambda a: a["order"])]
    assert ordered == [
        "omnigent",
        "omnigent-economy",
        "omnigent-balanced",
        "omnigent-frontier",
        "claude",
        "codex",
        "bash",
    ]


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
