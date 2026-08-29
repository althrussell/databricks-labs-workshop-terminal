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
import time

logger = logging.getLogger(__name__)

# Why a session could not start. Every one of these is a branch an attendee can
# reach from the launch button.
SESSION_CREATE_CODES = frozenset(
    {
        "unknown_agent",
        "agents_paused",
        "agent_installing",
        "credential_unavailable",
        "obo_stale",
        "session_conflict",
        "session_configuration",
    }
)

# Why a session ended, separating clean process exit from crash or signal.
SESSION_EXIT_CODES = frozenset(
    {"exited", "process_error", "process_signal", "closed", "idle_reaped", "shutdown"}
)

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
        "gateway_rate_limited",
        "gateway_allowance_exhausted",
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
    """Buffer for CT and emit a non-PII structured OTel record. Never raises."""
    try:
        from .event_emitter import event_emitter

        event_emitter.emit(event_type, attendee or "system", payload or {})
    except Exception:
        logger.debug("telemetry emit failed for %s", event_type, exc_info=True)
    try:
        from . import observability

        # Attendee identity deliberately stays in the existing authenticated CT
        # event buffer. The shared UC telemetry tables receive only stable seat
        # resource attributes and the fixed operational payload below.
        observability.record(event_type, payload or {})
    except Exception:
        logger.debug("OTel emit failed for %s", event_type, exc_info=True)


def session_started(attendee: str, agent_id: str, session_id: str) -> None:
    emit(
        "session.started",
        attendee,
        {"agent": agent_id, "session_id": session_id, "outcome": "started"},
    )


def session_switched(
    attendee: str,
    previous_agent: str,
    next_agent: str,
    session_id: str,
) -> None:
    supported = {"omnigent", "claude", "codex"}
    emit(
        "session.switched",
        attendee,
        {
            "previous_agent": previous_agent if previous_agent in supported else UNKNOWN,
            "next_agent": next_agent if next_agent in supported else UNKNOWN,
            "session_id": session_id,
            "outcome": "switched",
        },
    )


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
            "outcome": "refused",
        },
    )


def session_exited(
    attendee: str,
    agent_id: str,
    code: str,
    *,
    session_id: str = "",
    duration_s: float = 0.0,
    exit_code: int | None = None,
    process_signal: int | None = None,
) -> None:
    bucketed, raw = normalize(code, SESSION_EXIT_CODES)
    payload = {
        "agent": agent_id,
        "code": bucketed,
        "raw_code": raw,
        "session_id": session_id,
        "duration_s": round(duration_s, 1),
        "outcome": "ended",
    }
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if process_signal is not None:
        payload["signal"] = process_signal
    emit(
        "session.exited",
        attendee,
        payload,
    )


def bootstrap_phase(
    step: str,
    outcome: str,
    duration_ms: float,
    source: str = "",
) -> None:
    emit(
        "bootstrap.phase",
        "system",
        {
            "step": step,
            "outcome": outcome,
            "code": outcome,
            "duration_ms": max(0, round(duration_ms)),
            "source": source or "unknown",
        },
    )


def mirror_fetch(outcome: str, reason: str, coverage: float) -> None:
    emit(
        "mirror.fetch",
        "system",
        {
            "outcome": outcome,
            "code": reason,
            "coverage": max(0.0, min(1.0, float(coverage))),
            "source": "volume" if outcome == "served" else "network",
        },
    )


def entitlement_reconcile(
    *, source: str, outcome: str, reason: str, duration_ms: float, rate_limited: bool
) -> None:
    from . import config

    emit(
        "entitlement.reconcile",
        "system",
        {
            "source": source,
            "outcome": outcome,
            "code": reason,
            "duration_ms": max(0, round(duration_ms)),
            "rate_limited": rate_limited,
            "backoff_seconds": config.entitlement_reconcile_interval()
            if rate_limited
            else 0,
        },
    )


def readiness_result(report: dict, started_at: float) -> None:
    try:
        from . import observability

        failed = sorted(
            name
            for name, check in report.get("checks", {}).items()
            if isinstance(check, dict) and check.get("ok") is not True
        )
        observability.record_readiness(
            bool(report.get("ready")),
            failed,
            max(0.0, (time.monotonic() - started_at) * 1000),
        )
    except Exception:
        logger.debug("readiness telemetry failed", exc_info=True)


def otel_health(env) -> dict:
    try:
        from .observability import health

        return health(env)
    except Exception:  # noqa: BLE001
        return {
            "enabled": False,
            "configured": False,
            "state": "amber",
            "protocol": None,
            "collector_endpoint_present": False,
            "service_name_present": False,
            "required_resource_attributes": [],
            "missing_resource_attributes": ["telemetry_runtime"],
        }


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
    "bootstrap_phase",
    "emit",
    "entitlement_reconcile",
    "install_step_failed",
    "mirror_fetch",
    "normalize",
    "obo_health",
    "omnigent_host_health",
    "otel_health",
    "readiness_result",
    "session_create_failed",
    "session_exited",
    "session_started",
    "session_switched",
]
