"""Per-user content provisioning and terminal topic detection."""

import json
import os

from .conftest import ALICE


def _provisioned_home(client, monkeypatch):
    from server import user_content
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    user_content._provisioned.discard("alice@example.com")
    resp = client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    assert resp.status_code == 200
    return user_manager.get("alice@example.com").home


def test_instructions_written_with_coach(client, monkeypatch):
    home = _provisioned_home(client, monkeypatch)
    claude_md = open(os.path.join(home, ".claude", "CLAUDE.md")).read()
    assert "Workshop Edition" in claude_md
    assert "workshop-lab-coach" in claude_md  # coach appended by default

    agents_md = open(os.path.join(home, ".codex", "AGENTS.md")).read()
    assert agents_md.startswith("# Codex Agent Instructions")
    assert "workshop-lab-coach" in agents_md


def test_coach_disabled_by_env(client, monkeypatch):
    monkeypatch.setenv("LAB_COACH", "false")
    home = _provisioned_home(client, monkeypatch)
    claude_md = open(os.path.join(home, ".claude", "CLAUDE.md")).read()
    assert "workshop-lab-coach" not in claude_md


def test_subagents_and_skills_installed(client, monkeypatch):
    home = _provisioned_home(client, monkeypatch)
    agents = os.listdir(os.path.join(home, ".claude", "agents"))
    assert "prd-writer.md" in agents and "implementer.md" in agents

    skills_link = os.path.join(home, ".claude", "skills")
    assert os.path.islink(skills_link)
    assert os.path.isdir(os.path.join(skills_link, "databricks-docs"))
    assert os.path.islink(os.path.join(home, ".agents", "skills"))


def test_git_identity_and_sync_hook(client, monkeypatch):
    home = _provisioned_home(client, monkeypatch)
    gitconfig = open(os.path.join(home, ".gitconfig")).read()
    assert "email = alice@example.com" in gitconfig

    hook = open(os.path.join(home, ".githooks", "post-commit")).read()
    assert "/Workspace/Users/alice@example.com/projects" in hook
    assert "databricks sync" in hook


def test_claude_json_mcp(client, monkeypatch):
    home = _provisioned_home(client, monkeypatch)
    data = json.load(open(os.path.join(home, ".claude.json")))
    assert data["hasCompletedOnboarding"] is True
    assert "deepwiki" in data["mcpServers"] and "exa" in data["mcpServers"]


def test_launch_never_injects_prompts(client, monkeypatch):
    # Coaching context belongs in memory files, never in a fabricated user
    # message — the launch command must be the bare CLI.
    from server import agents

    agent = agents.get_agent("claude")
    assert "greeting" not in agent
    assert agents.launch_command(agent)[-1] == "claude; exec /bin/bash"


def test_auto_mode_defaults(client, monkeypatch):
    from server import cli_config
    from server.users import user_manager

    home = _provisioned_home(client, monkeypatch)
    settings = json.load(open(os.path.join(home, ".claude", "settings.json")))
    assert settings["permissions"]["defaultMode"] == "bypassPermissions"
    assert settings["skipDangerousModePermissionPrompt"] is True

    codex_toml = open(os.path.join(home, ".codex", "config.toml")).read()
    assert 'approval_policy = "never"' in codex_toml
    assert 'sandbox_mode = "danger-full-access"' in codex_toml

    # Opt out restores safe prompts.
    monkeypatch.setenv("WORKSHOP_AUTO_MODE", "false")
    user = user_manager.get("alice@example.com")
    cli_config.configure_claude(user, "tok")
    cli_config.configure_codex(user, "tok")
    settings = json.load(open(os.path.join(home, ".claude", "settings.json")))
    assert settings.get("permissions", {}).get("defaultMode") != "bypassPermissions"
    codex_toml = open(os.path.join(home, ".codex", "config.toml")).read()
    assert "approval_policy" not in codex_toml


def test_topic_detection_flags_user(client, monkeypatch):
    from server.content import content_service
    from server.main import _observe_output
    from server.sessions import session_manager
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    resp = client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    session = session_manager.list_for("alice@example.com")[0]

    assert "lakebase" in content_service.scan_topics("I'll provision Lakebase for the app")
    _observe_output(session, "Let me set up a Lakebase postgres instance...")
    user = user_manager.get("alice@example.com")
    assert "lakebase" in user.topics

    nuggets = client.get("/api/nuggets", headers=ALICE).json()["nuggets"]
    matched = [n for n in nuggets if n["matched_topic"] == "lakebase"]
    assert matched and matched[0]["id"] == "topic-lakebase"


def test_topic_detection_opt_out(client, monkeypatch):
    from server.main import _observe_output
    from server.sessions import session_manager
    from server.users import user_manager

    monkeypatch.setenv("TOPIC_DETECTION", "false")
    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    session = session_manager.list_for("alice@example.com")[0]
    user = user_manager.get("alice@example.com")
    user.topics.clear()
    _observe_output(session, "lakebase lakebase lakebase")
    assert user.topics == {}
