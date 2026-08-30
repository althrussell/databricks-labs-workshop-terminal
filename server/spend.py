"""Emergency coding-agent control and launch activity.

Launch count is an activity metric, not a proxy for tokens, dollars, or budget.
Actual consumption limits belong to the event-scoped Unity AI Gateway service.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from . import config

_lock = threading.Lock()
# None → follow the deploy-time config default; True/False → operator override.
_kill_override: bool | None = None
_reason = ""


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


def set_kill_switch(killed: bool, reason: str = "operator control") -> bool:
    """Halt or resume launches; return whether effective state changed."""
    global _kill_override, _reason
    with _lock:
        before = (
            not _kill_override
            if _kill_override is not None
            else config.agents_enabled_default()
        )
        _kill_override = killed
        _reason = reason.strip()[:300]
        return before != (not killed)


def reset() -> None:
    """Clear the operator override (revert to the config default). For tests."""
    global _kill_override, _reason
    with _lock:
        _kill_override = None
        _reason = ""


def control_state() -> dict:
    with _lock:
        override = _kill_override
        reason = _reason
    return {
        "agents_enabled": (
            (not override) if override is not None else config.agents_enabled_default()
        ),
        "override": override is not None,
        "reason": reason,
    }


def is_llm_agent(agent: dict) -> bool:
    """Every supported launch surface is a metered coding agent."""
    return bool(agent.get("requires"))


def agent_launches(user) -> int:
    """Lifetime count of this attendee's LLM-agent launches (bash excluded)."""
    return sum(n for aid, n in user.sessions_launched.items() if aid != "bash")


def check_can_launch(user, agent: dict) -> None:
    """Fail fast when the emergency switch has paused new agent launches."""
    if not is_llm_agent(agent):
        return
    if not agents_enabled():
        raise SpendBlocked(
            "Coding agents are paused by the workshop operator.",
            status=403,
        )


@contextmanager
def launch_guard(user, agent: dict):
    """Linearize final launch admission with the emergency switch.

    The admin path takes this same lock before terminating the active session.
    Therefore a concurrent create either sees the pause, or finishes spawning
    first and is then found and terminated by the admin operation.
    """
    if not is_llm_agent(agent):
        yield
        return
    with _lock:
        enabled = (
            not _kill_override
            if _kill_override is not None
            else config.agents_enabled_default()
        )
        if not enabled:
            raise SpendBlocked(
                "Coding agents are paused by the workshop operator.", status=403
            )
        yield


def metering(user) -> dict:
    """Per-attendee launch activity; deliberately carries no budget fields."""
    used = agent_launches(user)
    return {
        "email": user.email,
        "agent_launches": used,
        "by_agent": {
            aid: n for aid, n in user.sessions_launched.items() if aid != "bash"
        },
    }
