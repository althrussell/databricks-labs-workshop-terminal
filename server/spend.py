"""Per-attendee LLM agent spend controls (P1-16): kill-switch, budget, metering.

Workshop Terminal launches coding agents (Claude/Codex) as CLI subprocesses in
PTYs, so it can't observe model-serving token counts directly. The controllable
proxy for spend is **how many LLM-agent sessions an attendee starts** (and how
many run): we meter that, cap it per attendee (budget), and give operators a
fleet-wide **kill-switch** to halt all new agent launches mid-event if spend
runs hot. Bash sessions involve no model spend and are always free.

The kill-switch is in-memory runtime state (operators flip it live via the admin
API); it overrides the deploy-time ``AGENTS_ENABLED`` default. The per-attendee
launch count already lives on ``User.sessions_launched`` and is surfaced to
Control Tower via the stats harvest (``agent_sessions``), so spend is visible in
the operator's fleet view without new plumbing.
"""

from __future__ import annotations

import threading

from . import config

_lock = threading.Lock()
# None → follow the deploy-time config default; True/False → operator override.
_kill_override: bool | None = None


class SpendBlocked(Exception):
    """Raised when an agent launch is refused. ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def agents_enabled() -> bool:
    """Effective agent-launch state: operator override, else config default."""
    with _lock:
        if _kill_override is not None:
            return not _kill_override
    return config.agents_enabled_default()


def set_kill_switch(killed: bool) -> None:
    """Operator control: halt (or resume) all new LLM-agent launches fleet-wide."""
    global _kill_override
    with _lock:
        _kill_override = killed


def reset() -> None:
    """Clear the operator override (revert to the config default). For tests."""
    global _kill_override
    with _lock:
        _kill_override = None


def is_llm_agent(agent: dict) -> bool:
    """Every supported launch surface is a metered coding agent."""
    return bool(agent.get("requires"))


def agent_launches(user) -> int:
    """Lifetime count of this attendee's LLM-agent launches (bash excluded)."""
    return sum(n for aid, n in user.sessions_launched.items() if aid != "bash")


def check_can_launch(user, agent: dict) -> None:
    """Enforce the kill-switch + per-attendee budget for an LLM-agent launch.

    Raises :class:`SpendBlocked` (403 kill-switch, 429 budget).
    """
    if not is_llm_agent(agent):
        return
    if not agents_enabled():
        raise SpendBlocked(
            "Coding agents are paused by the workshop operator.",
            status=403,
        )
    cap = config.max_agent_launches_per_user()
    if cap > 0 and agent_launches(user) >= cap:
        raise SpendBlocked(
            f"You've used your {cap} coding-agent sessions for this workshop. "
            "Ask your host to extend it.",
            status=429,
        )


def metering(user) -> dict:
    """Per-attendee agent-usage snapshot for the operator/cost view."""
    cap = config.max_agent_launches_per_user()
    used = agent_launches(user)
    return {
        "email": user.email,
        "agent_launches": used,
        "budget": cap or None,
        "remaining": (max(cap - used, 0) if cap > 0 else None),
        "by_agent": {aid: n for aid, n in user.sessions_launched.items() if aid != "bash"},
    }
