"""Config-driven agent catalog.

The launchable agents are defined in content/agents.json (overridable via
AGENT_CATALOG_PATH) so events can add or remove CLIs without code changes.
Each entry maps a launch button to a PTY command plus the binary it needs
(readiness is reported per-binary by the bootstrap installers).
"""

from __future__ import annotations

import json
import logging
import os
import shlex

from . import config

logger = logging.getLogger(__name__)

_DEFAULT_CATALOG = os.path.join(os.path.dirname(__file__), "..", "content", "agents.json")

# bash needs no install and is always launchable.
_BASH = {
    "id": "bash",
    "label": "Terminal",
    "description": "Plain bash shell with the Databricks CLI pre-authenticated.",
    "icon": "terminal",
    "command": "bash",
    "requires": [],
    "order": 99,
}


def load_catalog() -> list[dict]:
    path = os.environ.get("AGENT_CATALOG_PATH", "").strip() or os.path.normpath(_DEFAULT_CATALOG)
    agents: list[dict] = []
    try:
        with open(path) as f:
            agents = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("agent catalog unreadable at %s: %s — bash only", path, e)
    if not any(a.get("id") == "bash" for a in agents):
        agents.append(_BASH)
    # Omnigent is feature-flagged off by default (not on public PyPI yet). When
    # disabled, drop it everywhere at once — no card, no launch — regardless of
    # which catalog source (default or AGENT_CATALOG_PATH) declared it.
    if not config.omnigent_enabled():
        agents = [a for a in agents if a.get("id") != "omnigent"]
    return sorted(agents, key=lambda a: a.get("order", 50))


def get_agent(agent_id: str) -> dict | None:
    return next((a for a in load_catalog() if a.get("id") == agent_id), None)


def launch_command(agent: dict) -> list[str]:
    """PTY command for an agent: run the CLI inside a login-ish bash so the
    attendee lands back in a shell when the agent exits.

    Note: no prompt injection here — coaching context belongs in the agent's
    memory files (~/.claude/CLAUDE.md, written by user_content), never in a
    fabricated user message."""
    cmd = agent.get("command", "bash")
    if cmd == "bash":
        return ["/bin/bash"]
    return ["/bin/bash", "-c", f"{cmd}; exec /bin/bash"]


def quoted(cmd: str) -> str:
    return " ".join(shlex.quote(part) for part in cmd.split())
