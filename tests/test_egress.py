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
    assert cfg["mcpServers"] == {}
    assert cfg["hasCompletedOnboarding"] is True


def test_public_mcp_opt_in_enables_servers(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_PUBLIC_MCP", "true")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    assert config.enable_public_mcp() is True
    user = User("bob@example.com")
    user.bootstrap_home()
    user_content._write_claude_json(user)
    cfg = _claude_json(user)
    assert set(cfg["mcpServers"]) == {"deepwiki", "exa"}


def test_ai_dev_kit_ref_is_pinnable(monkeypatch):
    import importlib

    monkeypatch.setenv("AI_DEV_KIT_REF", "v1.2.3")
    from server.bootstrap import install

    importlib.reload(install)
    try:
        assert install.AI_DEV_KIT_REF == "v1.2.3"
    finally:
        monkeypatch.delenv("AI_DEV_KIT_REF", raising=False)
        importlib.reload(install)


def test_ai_dev_kit_ref_defaults_to_main(monkeypatch):
    import importlib

    monkeypatch.delenv("AI_DEV_KIT_REF", raising=False)
    from server.bootstrap import install

    importlib.reload(install)
    assert install.AI_DEV_KIT_REF == "main"
