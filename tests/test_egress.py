"""Egress / supply-chain controls (gap P1-21)."""

import json
import os

from server import config, user_content
from server.users import User


def _claude_json(user: User) -> dict:
    with open(os.path.join(user.home, ".claude.json")) as f:
        return json.load(f)


def test_public_mcp_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("ENABLE_PUBLIC_MCP", raising=False)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    assert config.enable_public_mcp() is False
    user = User("alice@example.com")
    user.bootstrap_home()
    user_content._write_claude_json(user)
    cfg = _claude_json(user)
    # No external MCP egress by default.
    assert set(cfg["mcpServers"]) == {"workshop"}
    assert cfg["mcpServers"]["workshop"]["type"] == "stdio"
    assert cfg["hasCompletedOnboarding"] is True


def test_public_mcp_opt_in_enables_servers(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_PUBLIC_MCP", "true")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    assert config.enable_public_mcp() is True
    user = User("bob@example.com")
    user.bootstrap_home()
    user_content._write_claude_json(user)
    cfg = _claude_json(user)
    assert set(cfg["mcpServers"]) == {"workshop", "deepwiki", "exa"}


def test_skills_ref_is_pinnable(monkeypatch):
    import importlib

    monkeypatch.setenv("SKILLS_REF", "v1.2.3")
    from server.bootstrap import install

    importlib.reload(install)
    try:
        assert install.SKILLS_REF == "v1.2.3"
    finally:
        monkeypatch.delenv("SKILLS_REF", raising=False)
        importlib.reload(install)


def test_skills_default_is_a_reviewed_tag_not_a_branch_tip(monkeypatch):
    """An unpinned default would install whatever landed upstream that morning."""
    import importlib

    monkeypatch.delenv("SKILLS_REF", raising=False)
    monkeypatch.delenv("SKILLS_REPO", raising=False)
    from server.bootstrap import install

    importlib.reload(install)

    assert install.SKILLS_REF.startswith("v")
    assert install.SKILLS_REF != "main"
    assert install.SKILLS_REPO.endswith("databricks-agent-skills.git")


def test_skills_default_matches_the_reviewed_manifest():
    from server.bootstrap import install
    from server.bootstrap.artifacts import load_manifest

    kit = load_manifest("")["artifacts"]["databricks_agent_skills"]

    assert kit["version"] == install.SKILLS_REF
    assert kit["source"] == install.SKILLS_REPO
