"""B1 runtime hygiene: what test1025 (25 Oct 2026) showed the terminal getting wrong.

- Codex never learned the event's reasoning effort.
- Omnigent's embedded-browser tools answer "no browser renderer is connected"
  in the terminal, and the agent loops on them.
- Omnigent's runner drops every attendee variable outside its own allowlist, so
  ``workshop-grant-me`` sent an empty email and got HTTP 422.
- Omnigent's Claude default must be Claude Code's driver.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tomllib

import pytest

from server import cli_config, config, models


@pytest.fixture
def user(monkeypatch, tmp_path):
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    monkeypatch.setattr(cli_config, "discover_model_services", lambda token: {})
    attendee = User("alice@example.com")
    attendee.bootstrap_home()
    return attendee


def _codex(user) -> dict:
    with open(os.path.join(user.home, ".codex", "config.toml"), "rb") as handle:
        return tomllib.load(handle)


def _claude(user) -> dict:
    with open(os.path.join(user.home, ".claude", "settings.json")) as handle:
        return json.load(handle)


@pytest.mark.parametrize("value, written", [("", "high"), ("xhigh", "xhigh"), ("Medium", "medium"),
                                            ("maximum", "high")])
def test_codex_gets_the_events_reasoning_effort(user, monkeypatch, value, written):
    monkeypatch.setenv("CODEX_REASONING_EFFORT", value)
    cli_config.configure_codex(user, "tok-1", available=set())
    assert _codex(user)["model_reasoning_effort"] == written


def test_codex_reasoning_defaults_to_high_when_unset(user, monkeypatch):
    monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
    cli_config.configure_codex(user, "tok-1", available=set())
    assert _codex(user)["model_reasoning_effort"] == "high"


def test_omnigents_browser_tools_are_denied_where_no_renderer_exists(user, monkeypatch):
    monkeypatch.setenv("OMNIGENT_ENABLED", "true")
    cli_config.configure_claude(user, "tok-1", available=set())
    deny = _claude(user)["permissions"]["deny"]
    assert set(cli_config.OMNIGENT_RENDERER_TOOLS) <= set(deny)
    assert "mcp__omnigent__browser_navigate" in deny
    # The workshop deploy tool stays explicitly allowed.
    assert "mcp__workshop__deploy_databricks_app" in _claude(user)["permissions"]["allow"]


def test_the_deny_is_idempotent_keeps_an_attendees_rules_and_leaves_with_omnigent(user, monkeypatch):
    monkeypatch.setenv("OMNIGENT_ENABLED", "true")
    cli_config.configure_claude(user, "tok-1", available=set())
    settings_path = os.path.join(user.home, ".claude", "settings.json")
    settings = _claude(user)
    settings["permissions"]["deny"].append("Bash(rm -rf:*)")
    with open(settings_path, "w") as handle:
        json.dump(settings, handle)
    cli_config.configure_claude(user, "tok-1", available=set())
    deny = _claude(user)["permissions"]["deny"]
    assert deny.count("mcp__omnigent__browser_click") == 1 and "Bash(rm -rf:*)" in deny

    monkeypatch.setenv("OMNIGENT_ENABLED", "false")
    cli_config.configure_claude(user, "tok-1", available=set())
    # The apx logs denial is unconditional (F-012), so it stays with Omnigent off.
    assert _claude(user)["permissions"]["deny"] == [*cli_config.APX_UNUSABLE_TOOLS, "Bash(rm -rf:*)"]


def test_the_apx_logs_tool_is_denied_on_claude_and_left_off_codex(user, monkeypatch):
    """F-012 (31 Oct): apx's databricks_apps_logs fails on every seat (no OAuth cache)."""
    monkeypatch.setenv("OMNIGENT_ENABLED", "false")
    cli_config.configure_claude(user, "tok-1", available=set())
    cli_config.configure_codex(user, "tok-1", available=set())
    assert "mcp__apx__databricks_apps_logs" in _claude(user)["permissions"]["deny"]
    enabled = _codex(user)["mcp_servers"]["apx"]["enabled_tools"]
    assert "databricks_apps_logs" not in enabled
    assert "check" in enabled and "docs" in enabled


def test_agents_get_one_apx_preview_rule_and_the_logs_command():
    """F-010 and F-012 (31 Oct): the port an apx dev server uses, and where logs come from."""
    root = Path(__file__).resolve().parents[1] / "assets"
    claude_md = (root / "instructions" / "CLAUDE.md").read_text()
    workshop = (root / "skills" / "workshop" / "SKILL.md").read_text()
    assert "URL from `apx dev start`, not `$DATABRICKS_APP_PORT`" in claude_md
    assert "Dev server started at http://localhost:<port>" in workshop
    assert "APX_DEV_SERVER_PORT" in workshop and "registry.toml" in workshop
    assert "databricks apps logs <app>" in workshop


def test_omnigents_claude_default_is_claude_codes_driver(user, monkeypatch):
    """The owner's rule: Omnigent drives Claude Code, so it runs what Claude runs."""
    monkeypatch.setenv("OMNIGENT_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_MODEL", "system.ai.claude-opus-5-5")
    served = {name: frozenset({models.ANTHROPIC_MESSAGES})
              for name in ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5")}
    cli_config.configure_claude(user, "tok-1", available=served)
    cli_config.configure_omnigent(user, "tok-1", available=served)
    import yaml

    with open(os.path.join(user.home, ".omnigent", "config.yaml")) as handle:
        omnigent = yaml.safe_load(handle)
    claude_model = _claude(user)["env"]["ANTHROPIC_MODEL"]
    assert claude_model == "system.ai.claude-opus-5-5"
    assert omnigent["providers"]["databricks-gateway"]["anthropic"]["models"]["default"] == claude_model
    assert _claude(user)["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "system.ai.claude-haiku-5-5"


def test_omnigents_runner_keeps_the_attendees_environment(monkeypatch, tmp_path):
    from server.omnigent_remote import RUNNER_ENV_PASSTHROUGH, build_host_launch
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setenv("WORKSHOP_CATALOG", "test1025_01")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret-value")
    attendee = User("alice@example.com")
    attendee.bootstrap_home()
    _, env, _ = build_host_launch(attendee, "/opt/workshop/bin/omnigent", "https://omni.example.com")
    forwarded = set(env[RUNNER_ENV_PASSTHROUGH].split(","))
    assert {"WORKSHOP_USER_EMAIL", "WORKSHOP_APP_URL", "HOME", "PATH"} <= forwarded
    if "WORKSHOP_CATALOG" in env:
        assert "WORKSHOP_CATALOG" in forwarded
    # Host-only wiring and secrets never reach a runner through this list.
    assert not any(name.startswith("OMNIGENT_") for name in forwarded)
    assert "DATABRICKS_CLIENT_SECRET" not in forwarded and "DATABRICKS_CLIENT_SECRET" not in env
    assert forwarded <= set(env)
    # Omnigent's Claude harnesses must not send Claude Code's non-essential
    # startup probes, which the gateway answers with HTTP 400.
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in forwarded


def test_a_helper_that_lost_its_email_is_identified_by_its_capability(client):
    """test1025, seat 1: workshop-grant-me inside Omnigent sent {"email": ""}."""
    from server import user_content
    from server.users import user_manager

    attendee = user_manager.get("helper-b1@example.com")
    user_content._write_callback_capability(attendee)
    with open(user_content.callback_capability_path(attendee)) as handle:
        capability = handle.read().strip()

    assert user_content.callback_capability_owner(capability) == "helper-b1@example.com"
    assert user_content.callback_capability_owner("not-a-capability") is None
    assert user_content.callback_capability_owner("") is None

    ok = client.post("/api/entitlements/reconcile", json={"email": ""},
                     headers={"X-Workshop-Capability": capability})
    assert ok.status_code == 200, ok.text
    wrong = client.post("/api/entitlements/reconcile", json={"email": ""},
                        headers={"X-Workshop-Capability": "forged"})
    assert wrong.status_code == 422
