"""The model-comparison exercise, reachable without Pi.

Comparing what different models cost for the same task is the workshop's
headline exercise, and it used to require Pi — the one harness that routes per
model across three wire protocols, and the most fragile thing in the room. These
profiles put the same models on bare Codex over plain chat-completions, which
touches no attendee credential and cannot be taken out by a stale OBO token.
"""

from __future__ import annotations

import os
import tomllib

import pytest

from server import cli_config, models


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


def test_every_comparison_model_gets_a_profile_an_attendee_can_type(user):
    cli_config.configure_codex(user, "tok-1", available=set())

    config = _toml(user)

    assert set(config["profiles"]) == {"glm", "kimi", "gemini"}
    assert config["profiles"]["glm"]["model"] == "databricks-glm-5-2"
    assert config["profiles"]["kimi"]["model"] == "databricks-kimi-k3"
    assert config["profiles"]["gemini"]["model"] == "databricks-gemini-3-6-flash"


def test_the_comparison_runs_on_chat_completions_at_the_workspace_host(user):
    """The only surface these models answer on, and one Codex can speak."""
    cli_config.configure_codex(user, "tok-1", available=set())

    provider = _toml(user)["model_providers"]["databricks-chat"]

    assert provider["wire_api"] == "chat"
    assert provider["base_url"] == "https://ws.cloud.databricks.com/serving-endpoints"
    for profile in _toml(user)["profiles"].values():
        assert profile["model_provider"] == "databricks-chat"


def test_the_everyday_driver_is_left_on_the_responses_wire(user):
    """Codex is tuned for the OpenAI family; the comparison is the exception."""
    cli_config.configure_codex(user, "tok-1", available=set())

    config = _toml(user)

    assert config["model_provider"] == "databricks"
    assert config["model_providers"]["databricks"]["wire_api"] == "responses"


def test_the_comparison_survives_token_rotation_like_everything_else(user):
    cli_config.configure_codex(user, "tok-1", available=set())

    auth = _toml(user)["model_providers"]["databricks-chat"]["auth"]

    assert auth["command"] == "cat"
    assert auth["args"] == [
        os.path.join(user.home, ".config", "workshop", "gateway-token")
    ]
    assert auth["refresh_interval_ms"] == 240000


def test_a_model_this_region_does_not_serve_is_not_advertised(user):
    """An attendee typing `codex --profile kimi` into a 404 learns nothing."""
    cli_config.configure_codex(
        user, "tok-1", available={"databricks-glm-5-2", "databricks-gpt-5-6-terra"}
    )

    assert set(_toml(user)["profiles"]) == {"glm"}


def test_a_workspace_serving_none_of_them_writes_no_comparison_block(user):
    cli_config.configure_codex(user, "tok-1", available={"databricks-gpt-5-6-terra"})

    config = _toml(user)

    assert "profiles" not in config
    assert "databricks-chat" not in config["model_providers"]


def test_a_withdrawn_endpoint_is_a_values_change_not_a_release(user, monkeypatch):
    monkeypatch.setenv("CODEX_COMPARE_KIMI", "databricks-kimi-k4")

    cli_config.configure_codex(user, "tok-1", available=set())

    assert _toml(user)["profiles"]["kimi"]["model"] == "databricks-kimi-k4"


def test_failed_discovery_keeps_every_profile_rather_than_none():
    """Empty availability means we could not ask, not that nothing is served."""
    assert set(models.comparison_models(set())) == {"glm", "kimi", "gemini"}
    assert set(models.comparison_models(None)) == {"glm", "kimi", "gemini"}
