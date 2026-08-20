"""The generated Codex config has to be loadable by the Codex we ship.

An unknown ``wire_api`` is not a skipped provider — codex-cli rejects the whole
file, falls back to its own default config, and every session dies at startup
with "Model provider `databricks` not found". That took out bare Codex and the
Omnigent native terminal together, because Omnigent copies this same file into
its per-session ``CODEX_HOME``. These tests pin the config to what the pinned
CLI actually accepts.
"""

from __future__ import annotations

import os
import tomllib

import pytest

from server import cli_config, models

# codex-cli 0.144.6 (assets/artifacts/manifest.json) accepts exactly one value:
# `unknown variant "chat", expected "responses"`. Chat completions is gone.
CODEX_ACCEPTED_WIRE_APIS = {"responses"}


@pytest.fixture
def user(monkeypatch, tmp_path):
    from server import config
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    monkeypatch.setenv("DATABRICKS_HOST", "https://ws.cloud.databricks.com")
    attendee = User("alice@example.com")
    attendee.bootstrap_home()
    return attendee


def _toml(user) -> dict:
    with open(os.path.join(user.home, ".codex", "config.toml"), "rb") as handle:
        return tomllib.load(handle)


def test_every_provider_speaks_a_wire_this_codex_accepts(user):
    """The regression itself: one bad wire invalidates the entire config."""
    cli_config.configure_codex(user, "tok-1", available=set())

    for name, provider in _toml(user)["model_providers"].items():
        assert provider["wire_api"] in CODEX_ACCEPTED_WIRE_APIS, (
            f"provider {name!r} would make codex discard the whole config"
        )


def test_no_model_provider_reference_dangles(user):
    """A profile or default pointing at an undefined table is the same failure."""
    cli_config.configure_codex(user, "tok-1", available=set())
    config = _toml(user)
    defined = set(config["model_providers"])

    assert config["model_provider"] in defined
    for name, profile in config.get("profiles", {}).items():
        assert profile["model_provider"] in defined, f"profile {name!r} dangles"


def test_the_everyday_driver_is_on_the_responses_wire(user):
    cli_config.configure_codex(user, "tok-1", available=set())

    config = _toml(user)

    assert config["model_provider"] == "databricks"
    assert config["model_providers"]["databricks"]["wire_api"] == "responses"


def test_the_driver_survives_token_rotation(user):
    cli_config.configure_codex(user, "tok-1", available=set())

    auth = _toml(user)["model_providers"]["databricks"]["auth"]

    assert auth["command"] == "cat"
    assert auth["args"] == [
        os.path.join(user.home, ".config", "workshop", "gateway-token")
    ]
    assert auth["refresh_interval_ms"] == 240000


def test_the_comparison_models_are_not_offered_as_codex_profiles(user):
    """They answer only on chat-completions, which this Codex cannot speak.

    The gateway's Responses surface refuses them too, so there is no wire that
    would let them back in — offering a profile here is offering a broken one.
    """
    cli_config.configure_codex(user, "tok-1", available=set())

    config = _toml(user)

    assert "profiles" not in config
    assert set(config["model_providers"]) == {"databricks"}


def test_failed_discovery_keeps_every_comparison_model_rather_than_none():
    """Empty availability means we could not ask, not that nothing is served."""
    assert set(models.comparison_models(set())) == {"glm", "gemini", "qwen"}
    assert set(models.comparison_models(None)) == {"glm", "gemini", "qwen"}


def test_a_model_this_region_does_not_serve_is_not_published(user):
    resolved = models.comparison_models(
        {
            "glm-5-2": frozenset({models.CHAT_COMPLETIONS}),
            "gpt-5-6-terra": frozenset({models.OPENAI_RESPONSES}),
        }
    )

    assert set(resolved) == {"glm"}
    assert resolved["glm"] == "system.ai.glm-5-2"


def test_a_model_served_on_the_wrong_wire_is_not_published(user):
    """Served is not the same as usable. The comparison exercise sends chat
    completions, so a model the gateway answers for on some other surface is a
    404 waiting to happen and is left out."""
    resolved = models.comparison_models(
        {"glm-5-2": frozenset({models.OPENAI_RESPONSES})}
    )

    assert resolved == {}


def test_a_withdrawn_endpoint_is_a_values_change_not_a_release(monkeypatch):
    monkeypatch.setenv("CODEX_COMPARE_GLM", "glm-5-3")

    assert models.comparison_models(set())["glm"] == "system.ai.glm-5-3"


def test_an_override_written_fully_qualified_is_left_alone(monkeypatch):
    """An operator reading the model names off the workspace UI writes them the
    way the UI shows them, and qualifying twice would name nothing."""
    monkeypatch.setenv("CODEX_COMPARE_GLM", "system.ai.glm-5-3")

    assert models.comparison_models(set())["glm"] == "system.ai.glm-5-3"
