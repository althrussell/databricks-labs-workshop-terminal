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
import time

from . import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
SUPPORTED_AGENT_IDS = frozenset({"omnigent", "claude", "codex"})
# The operator's "demote Omnigent" lever. None means nobody has pulled it.
_demoted: bool = False

_DEFAULT_CATALOG = os.path.join(os.path.dirname(__file__), "..", "content", "agents.json")

def offered_agent_ids() -> list[str] | None:
    """The operator's agent selection, or ``None`` when they made none.

    Unsupported legacy IDs are discarded here as well as in ``load_catalog``.
    The server allowlist is the enforcement boundary; the catalogue is only
    presentation and may be overridden by an operator-controlled file.
    """
    selected = config.workshop_agents()
    if selected is None:
        return None
    return [agent_id for agent_id in selected if agent_id in SUPPORTED_AGENT_IDS]


def load_catalog() -> list[dict]:
    path = os.environ.get("AGENT_CATALOG_PATH", "").strip() or os.path.normpath(_DEFAULT_CATALOG)
    agents: list[dict] = []
    try:
        with open(path) as f:
            agents = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("agent catalog unreadable at %s: %s", path, e)
    agents = [a for a in agents if a.get("id") in SUPPORTED_AGENT_IDS]
    # The workshop's own selection, made when the run was created. Applied
    # before the Omnigent gate below so the two compose: an operator who picked
    # Omnigent still loses it on an instance where the feature is off.
    offered = offered_agent_ids()
    if offered is not None:
        agents = [a for a in agents if a.get("id") in offered]
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
    Claude and Codex, which run on the app credential and cannot be taken out by
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

    Bare Claude and Codex are deliberately not consulted here: they must never
    be gated by credential state on a plane they do not use.
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


#: The first message of an attendee's first agent session when nothing has told
#: the agent who they are yet (no wizard, no stated working approach, no build).
#: The CLI sends it once its UI is ready, so the agent greets them and gets to
#: know them instead of waiting at an empty prompt.
WORKSHOP_OPENER = "Hi! I've just sat down for the workshop. Let's get started."

#: The first message of any later fresh session (preferences saved, a build
#: exists, or they already had their first session).
RETURN_OPENER = "Hi! I'm back at the workshop. Let's pick up where we left off."

#: How each CLI takes a first message at launch: ``claude [prompt]`` and
#: ``codex [PROMPT]``. Omnigent's ``-p`` flag is intentionally omitted: passing
#: ``-p`` skips ``workshop-omnigent``'s resume/continue probe and causes the
#: 0.19.1 REPL to stop its session after the greeting turn (EQL, 27 Oct 2026).
_FIRST_MESSAGE_FLAGS = {"claude": (), "codex": ()}


def switch_opener(agent: dict | str) -> str:
    """The first message when switching to another agent without a starter prompt."""
    if isinstance(agent, dict):
        name = str(agent.get("label") or agent.get("id") or "this agent").strip()
    else:
        name = str(agent or "this agent").strip()
    return f"I've just switched to {name}. Let's pick up where we left off."


def claim_opener(
    user,
    agent: dict | str | None = None,
    *,
    has_starter_prompt: bool = False,
    replaces_agent_id: str | None = None,
) -> str | None:
    """Choose the session opener so every fresh or switched session speaks first.

    - Wizard or idea-chip starter prompt (``has_starter_prompt``): no opener.
    - Agent switch without a starter prompt (``replaces_agent_id``): switch opener.
    - First-ever session (no saved preferences, no build, no prior opener):
      ``WORKSHOP_OPENER``, claimed atomically under the attendee's state lock.
    - Any later fresh session: ``RETURN_OPENER``.
    """
    if has_starter_prompt:
        return None
    if replaces_agent_id:
        return switch_opener(agent or replaces_agent_id)

    from . import attendee_profile, attendee_state, projects

    try:
        effective = attendee_profile.read(user).effective()
        has_prefs = bool(
            effective.get("working_approach_stated")
            or effective.get("background_stated")
        )
        inv = projects.inventory(user)
        has_build = bool(inv.get("active") or inv.get("projects"))
        if has_prefs or has_build:
            return RETURN_OPENER
        marker = os.path.join(user.home, ".workshop", "opener.json")
        with attendee_state.locked(marker):
            if attendee_state.read_json(marker):
                return RETURN_OPENER
            attendee_state.write_json(marker, {"opener_sent_at": time.time()})
    except Exception as error:  # noqa: BLE001
        logger.warning("workshop opener unavailable (%s)", type(error).__name__)
        return None
    return WORKSHOP_OPENER


def release_opener(user) -> None:
    """Give the first-session opener back when the session it was claimed for never started."""
    try:
        os.remove(os.path.join(user.home, ".workshop", "opener.json"))
    except OSError:
        pass


def claude_plan_flags(auto_mode: bool) -> tuple[str, ...]:
    """Launch flags for a build whose plan the attendee has not yet approved.

    The session starts in Claude Code's plan mode. ``--allow-dangerously-skip-
    permissions`` adds bypass to the Shift+Tab cycle without starting in it, so
    the attendee's approval can switch the session to the full-permission mode
    (outside auto mode there is no such mode to offer).
    """
    flags = ("--permission-mode", "plan")
    return flags + ("--allow-dangerously-skip-permissions",) if auto_mode else flags


def claude_auto_flags(auto_mode: bool) -> tuple[str, ...]:
    """Launch flags for a Claude build whose plan is approved, or a resumed one.

    Full auto is a launch choice. The user settings no longer carry a base mode
    (F-003, 31 Oct): with bypass there, every session began in bypass and Claude
    Code announced it on the plan launch. Outside auto mode there are no flags.
    """
    return ("--dangerously-skip-permissions",) if auto_mode else ()


def with_launch_flags(agent: dict, flags: tuple[str, ...]) -> dict:
    """The catalogue agent with extra CLI flags after its command."""
    cmd = str(agent.get("command") or "").strip()
    if not flags or not cmd:
        return agent
    return {**agent, "command": " ".join((cmd, *(shlex.quote(flag) for flag in flags)))}


def with_first_message(agent: dict, first_message: str) -> dict:
    """The catalogue agent, launched so its CLI sends ``first_message`` first.

    Coaching context belongs in the agent's memory files (~/.claude/CLAUDE.md,
    written by user_content), never in a fabricated user message. The one
    exception is the visible ``WORKSHOP_OPENER`` that starts an attendee's
    first conversation.
    """
    cmd = str(agent.get("command") or "").strip()
    if not first_message or not cmd or agent.get("id") not in _FIRST_MESSAGE_FLAGS:
        return agent
    flags = _FIRST_MESSAGE_FLAGS[agent["id"]]
    return {**agent, "command": " ".join((cmd, *flags, shlex.quote(first_message)))}


def launch_command(agent: dict) -> list[str]:
    """PTY command for an agent. Exiting the CLI exits the PTY as well."""
    if agent.get("id") not in SUPPORTED_AGENT_IDS:
        raise ValueError(f"Unsupported agent '{agent.get('id', '')}'")
    cmd = str(agent.get("command") or "").strip()
    if not cmd:
        raise ValueError(f"Agent '{agent['id']}' has no launch command")
    # The catalogue command may contain reviewed arguments, so retain a shell
    # for parsing but replace it with the CLI. There is no shell to reveal when
    # the agent exits.
    return ["/bin/bash", "-c", f"exec {cmd}"]


def quoted(cmd: str) -> str:
    return " ".join(shlex.quote(part) for part in cmd.split())
