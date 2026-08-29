"""Small, deterministic translation layer for Unity AI Gateway 429s."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GatewayLimit:
    reason: str
    message: str
    retry_after_seconds: int | None


def _response_text(response: Any) -> str:
    try:
        body = response.json()
    except (AttributeError, ValueError):
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error", body)
    if isinstance(error, dict):
        return " ".join(
            str(error.get(key) or "") for key in ("code", "type", "message")
        ).lower()
    return str(error).lower()


def _retry_after(response: Any) -> int | None:
    try:
        raw = response.headers.get("retry-after")
        value = int(raw) if raw else None
    except (AttributeError, TypeError, ValueError):
        return None
    return max(1, min(value, 300)) if value is not None else None


def classify(response: Any) -> GatewayLimit | None:
    if getattr(response, "status_code", None) != 429:
        return None
    detail = _response_text(response)
    allowance = any(
        marker in detail
        for marker in ("budget", "spend", "allowance", "quota exhausted", "cost limit")
    )
    retry_after = _retry_after(response)
    if allowance:
        return GatewayLimit(
            reason="gateway_allowance_exhausted",
            message=(
                "This event's model allowance is exhausted. Ask your workshop "
                "host before trying again."
            ),
            retry_after_seconds=None,
        )
    guidance = (
        f" Wait about {retry_after} seconds before trying again."
        if retry_after is not None
        else " Wait briefly before trying again."
    )
    return GatewayLimit(
        reason="gateway_rate_limited",
        message="The workshop model is temporarily rate limited." + guidance,
        retry_after_seconds=retry_after,
    )


def describe(response: Any) -> str:
    limited = classify(response)
    if limited is not None:
        try:
            from . import telemetry

            telemetry.emit(
                "gateway.request_limited",
                "system",
                {
                    "code": limited.reason,
                    "backoff_seconds": limited.retry_after_seconds or 0,
                    "outcome": "refused",
                },
            )
        except Exception:
            pass
        return limited.message
    return f"AI Gateway returned {getattr(response, 'status_code', 'an error')}"


__all__ = ["GatewayLimit", "classify", "describe"]
