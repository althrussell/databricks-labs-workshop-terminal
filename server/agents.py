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
import threading

from . import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# The operator's "demote Omnigent" lever. None means nobody has pulled it.
_demoted: bool = False

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
    #
    # Gate on the required binary rather than the literal id so any future
    # omnigent-backed catalog cards disappear together when the feature is off.
    if not config.omnigent_enabled():
        agents = [
            a
            for a in agents
            if a.get("id") != "omnigent" and "omnigent" not in (a.get("requires") or [])
        ]
    return sorted(agents, key=lambda a: a.get("order", 50))


def get_agent(agent_id: str) -> dict | None:
    return next((a for a in load_catalog() if a.get("id") == agent_id), None)


def is_omnigent_backed(agent: dict) -> bool:
    """Whether launching this card goes through the Omnigent host.

    Keyed on the required binary, not the literal id, so a future
    Omnigent-backed card is gated by the same rule without anyone remembering
    to add it here.
    """
    return agent.get("id") == "omnigent" or "omnigent" in (agent.get("requires") or [])


def omnigent_demoted() -> bool:
    """Whether an operator has withdrawn the Omnigent tier for this instance."""
    with _lock:
        return _demoted


def set_omnigent_demoted(demoted: bool) -> None:
    """Withdraw (or restore) every Omnigent-backed card.

    The lever an operator reaches for when the Omnigent plane is failing across
    a room and waiting is not working: one call moves everyone to bare Claude,
    Codex and bash, which run on the app credential and cannot be taken out by
    an attendee's expired sign-in. Deliberately separate from the spend
    kill-switch, which pauses *all* agents — demoting is the move that keeps a
    workshop running rather than stopping it.

    Runtime state, not config: an operator flips this mid-event and it must take
    effect on the next poll without a redeploy.
    """
    global _demoted
    with _lock:
        _demoted = demoted
    logger.warning("Omnigent tier %s by operator", "demoted" if demoted else "restored")


def reset_demotion() -> None:
    """Clear the operator override. For tests."""
    set_omnigent_demoted(False)


def launch_block(agent: dict, email: str) -> str:
    """Why this agent cannot be launched right now — ``""`` when it can.

    Everything that runs through the Omnigent host inherits the attendee's OBO
    mirror, and a stale mirror kills every session it starts with an auth error
    the attendee cannot act on. Refusing up front is the difference between an
    explained wait and a room full of identical unexplained failures.

    Bare Claude, Codex and bash are deliberately not consulted here: they are
    the fallback the runbook sends people to, so they must never be gated by
    credential state on a plane they do not use.
    """
    if not is_omnigent_backed(agent):
        return ""
    if omnigent_demoted():
        return "operator_demoted"
    if not config.omnigent_remote_enabled():
        return ""
    from . import obo

    status = obo.obo_manager.status(email)
    if not status["present"]:
        return "credential_absent"
    expires_in = status["expires_in"]
    # Read freshness off the expiry rather than status()["fresh"], which is
    # additionally gated on ENABLE_OBO. The remote host needs the mirror whether
    # or not the ``me`` CLI profile is switched on.
    if expires_in is not None and expires_in <= obo.FRESH_MARGIN:
        return "credential_stale"
    return ""


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
