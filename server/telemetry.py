"""The event vocabulary an operator troubleshoots with.

Counters answer "how many"; these answer "what happened, to whom, and why". The
distinction mattered during the incident that motivated this module: the fleet
looked healthy on every metric we had while an attendee stared at
``native_terminal_start_failed``.

Two rules hold the vocabulary together.

**Codes are a fixed enum with an ``unknown`` bucket.** Aggregation across a
fleet only works if the same failure carries the same string, and a free-text
code drifts within a day. Unrecognised values are still emitted — bucketed as
``unknown`` with the original preserved in ``raw_code`` — because a code we have
not seen before is the most interesting kind.

**Emission never fails a caller.** Every helper here swallows its own errors.
Telemetry that can break a session launch is worse than no telemetry.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Why a session could not start. Every one of these is a branch an attendee can
# reach from the launch button.
SESSION_CREATE_CODES = frozenset(
    {
        "unknown_agent",
        "agents_paused",
        "agent_budget_exhausted",
        "agent_installing",
        "credential_unavailable",
        "obo_stale",
        "session_conflict",
        "session_configuration",
    }
)

# Why a session ended. "exited" is the agent's own choice; the rest are ours.
SESSION_EXIT_CODES = frozenset({"exited", "closed", "idle_reaped", "shutdown"})

# What an attendee can see on their screen. The Omnigent members of this set are
# the sanitized codes the harness surfaces; the rest are Workshop Terminal's.
ATTENDEE_ERROR_CODES = frozenset(
    {
        "native_terminal_start_failed",
        "spec_resolver_failed",
        "runner_disconnected",
        "turn_failed",
        "session_start_failed",
        "credential_unavailable",
        "obo_stale",
        "websocket_lost",
        "install_incomplete",
    }
)

UNKNOWN = "unknown"


def normalize(code: str | None, vocabulary: frozenset[str]) -> tuple[str, str]:
    """Bucket ``code`` into ``vocabulary``, keeping the original either way.

    Returns ``(code, raw_code)``. A value outside the vocabulary becomes
    ``unknown`` so fleet aggregation stays sound, while ``raw_code`` keeps the
    thing an operator actually needs to read.
    """
    raw = (code or "").strip()
    return (raw if raw in vocabulary else UNKNOWN), raw


def emit(event_type: str, attendee: str, payload: dict | None = None) -> None:
    """Buffer one event for Control Tower. Never raises."""
    try:
        from .event_emitter import event_emitter

        event_emitter.emit(event_type, attendee or "system", payload or {})
    except Exception:  # noqa: BLE001 — telemetry is never load-bearing
        logger.debug("telemetry emit failed for %s", event_type, exc_info=True)


def session_create_failed(attendee: str, agent_id: str, code: str, detail: str = "") -> None:
    bucketed, raw = normalize(code, SESSION_CREATE_CODES)
    emit(
        "session.create_failed",
        attendee,
        {
            "agent": agent_id,
            "code": bucketed,
            "raw_code": raw,
            "detail": detail[:300],
        },
    )


def session_exited(
    attendee: str, agent_id: str, code: str, *, session_id: str = "", duration_s: float = 0.0
) -> None:
    bucketed, raw = normalize(code, SESSION_EXIT_CODES)
    emit(
        "session.exited",
        attendee,
        {
            "agent": agent_id,
            "code": bucketed,
            "raw_code": raw,
            "session_id": session_id,
            "duration_s": round(duration_s, 1),
        },
    )


def install_step_failed(step: str, status: str, error: str = "") -> None:
    """A step that ends in error or degraded. An attendee sees this as an agent
    that never becomes launchable, so it must not be visible only in a spinner."""
    emit(
        "install.step_failed",
        "system",
        {"step": step, "status": status, "error": (error or "")[:300]},
    )


def omnigent_host_health(attendee: str, status: str, detail: dict | None = None) -> None:
    emit("omnigent.host_health", attendee, {"status": status, **(detail or {})})


def obo_health(attendee: str, state: str, detail: dict | None = None) -> None:
    """Mirror of ``credential.health`` for the plane that actually goes stale."""
    emit("obo.health", attendee, {"state": state, **(detail or {})})


def attendee_error_seen(attendee: str, code: str, detail: dict | None = None) -> None:
    """What the attendee's screen said, reported by the attendee's browser.

    The one event nothing else can produce: it is the proof that our sanitized
    code and the operator's diagnostics describe the same moment.
    """
    bucketed, raw = normalize(code, ATTENDEE_ERROR_CODES)
    emit(
        "attendee.error_seen",
        attendee,
        {"code": bucketed, "raw_code": raw, **(detail or {})},
    )


__all__ = [
    "ATTENDEE_ERROR_CODES",
    "SESSION_CREATE_CODES",
    "SESSION_EXIT_CODES",
    "UNKNOWN",
    "attendee_error_seen",
    "emit",
    "install_step_failed",
    "normalize",
    "obo_health",
    "omnigent_host_health",
    "session_create_failed",
    "session_exited",
]
