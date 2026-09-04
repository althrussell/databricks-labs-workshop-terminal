"""Secret-free OpenTelemetry bridge for Workshop Terminal operations."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from opentelemetry import _logs, metrics, trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.metrics import Observation

from .otel_bootstrap import parse_resource_attributes

SCHEMA_VERSION = 1
MAX_ATTRIBUTE_CHARS = 256
READINESS_REEMIT_SECONDS = 60.0

logger = logging.getLogger("workshop-terminal.telemetry")
logger.setLevel(logging.INFO)

_EVENT_NAME = re.compile(r"[^a-z0-9_.-]+")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_DATABRICKS_TOKEN = re.compile(r"\bdapi[a-zA-Z0-9._-]{8,}\b")
_JWT = re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(authorization|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|databricks[_-]?token|workshop[_-]?pat|password|token|secret)"
    r"(\s*[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED[^\]]*\])"
    r"([^\s,}\]]+|\"[^\"]*\"|'[^']*')"
)
_CONTENT_TAIL = re.compile(
    r"(?is)\b(prompt|terminal[_ -]?(?:input|output|io)|full[_ -]?config(?:uration)?|"
    r"config(?:uration)?[_ -]?file)[\"']?\s*[:=].*$"
)
_SENSITIVE_QUERY_PARAMETER = re.compile(r"(?i)([?&](?:name|q)=)[^&\s]*")

_SAFE_FIELD_MAP = {
    "agent": "agent.id",
    "agent_id": "agent.id",
    "previous_agent": "session.previous_agent_id",
    "next_agent": "session.next_agent_id",
    "session_id": "session.id",
    "code": "event.reason_code",
    "outcome": "event.outcome",
    "status": "event.outcome",
    "step": "bootstrap.phase",
    "source": "operation.source",
    "duration_ms": "operation.duration_ms",
    "duration_s": "operation.duration_s",
    "exit_code": "process.exit_code",
    "signal": "process.signal",
    "rate_limited": "entitlement.rate_limited",
    "backoff_seconds": "operation.backoff_seconds",
    "request_count": "operation.request_count",
    "rate_limit_count": "entitlement.rate_limit_count",
    "http_429_count": "entitlement.http_429_count",
    "cache_hits": "entitlement.cache_hits",
    "cache_misses": "entitlement.cache_misses",
    "convergence_ms": "operation.convergence_ms",
    "resumed": "operation.resumed",
    "coverage": "mirror.coverage",
    "failed_checks": "readiness.failed_checks",
    "exception_type": "exception.type",
    "credential_fresh": "credential.fresh",
    "attempts": "operation.attempts",
    "last_exit_code": "process.exit_code",
    "expires_in": "credential.expires_in_seconds",
    "validation_state": "credential.validation_state",
}


def redact_text(value: object) -> str:
    """Redact credentials, attendee PII, prompts, terminal I/O, and configs."""
    text = str(value)
    text = _CONTENT_TAIL.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    # Remove a complete bearer before assignment redaction can consume only its
    # first word and accidentally leave the credential behind.
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text
    )
    text = _SENSITIVE_QUERY_PARAMETER.sub(r"\1[REDACTED]", text)
    text = _DATABRICKS_TOKEN.sub("[REDACTED_TOKEN]", text)
    text = _JWT.sub("[REDACTED_TOKEN]", text)
    return _EMAIL.sub("[REDACTED_EMAIL]", text)


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Uvicorn's access formatter does not format ``msg`` in the usual
            # logging way.  It unpacks the five structured arguments to add
            # colour and status metadata.  Flattening those arguments into an
            # already-rendered message makes every access log raise
            # ``ValueError: not enough values to unpack``.  Preserve the
            # contract while still redacting the request path and other string
            # fields before any handler (including OTel) can see them.
            structured_access_log = (
                record.name == "uvicorn.access"
                and isinstance(record.args, tuple)
                and len(record.args) == 5
            )
            if structured_access_log:
                record.msg = redact_text(record.msg)
                record.args = tuple(
                    redact_text(value) if isinstance(value, str) else value
                    for value in record.args
                )
            else:
                record.msg = redact_text(record.getMessage())
                record.args = ()
            if record.exc_info:
                formatter = logging.Formatter()
                exception_text = redact_text(
                    formatter.formatException(record.exc_info)
                )
                if exception_text:
                    record.msg = f"{record.getMessage()}\n{exception_text}"
                    record.args = ()
                # OTel and custom handlers may format ``exc_info`` themselves,
                # bypassing ``exc_text``. Flatten only its redacted rendering
                # into the message and remove the raw exception tuple.
                record.exc_info = None
                record.exc_text = exception_text
            if record.stack_info:
                record.stack_info = redact_text(record.stack_info)
        except Exception:  # noqa: BLE001 - logging must never break the app
            record.msg = "[REDACTION_FAILED]"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


_redaction_filter = RedactionFilter()
_redaction_install_lock = threading.Lock()
_redaction_factory_installed = False


def install_logging_redaction() -> None:
    """Redact records before current or future handlers can export them."""
    global _redaction_factory_installed

    with _redaction_install_lock:
        if not _redaction_factory_installed:
            previous_factory = logging.getLogRecordFactory()

            def redacting_factory(*args, **kwargs) -> logging.LogRecord:
                record = previous_factory(*args, **kwargs)
                _redaction_filter.filter(record)
                return record

            logging.setLogRecordFactory(redacting_factory)
            _redaction_factory_installed = True

    # Existing handlers receive a second defensive filter. In an
    # auto-instrumented process this includes OTel's logging handler; the record
    # factory above protects handlers attached later in the FastAPI lifespan.
    root = logging.getLogger()
    if _redaction_filter not in root.filters:
        root.addFilter(_redaction_filter)
    for handler in root.handlers:
        if _redaction_filter not in handler.filters:
            handler.addFilter(_redaction_filter)


def _active_sessions() -> int:
    from .sessions import session_manager

    return session_manager.count_all()


def _event_name(value: str) -> str:
    normalized = _EVENT_NAME.sub("_", (value or "unknown").strip().lower())
    return (normalized or "unknown")[:120]


def _safe_value(value: object) -> str | int | float | bool | tuple[str, ...] | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)[:MAX_ATTRIBUTE_CHARS]
    if isinstance(value, (list, tuple)):
        safe = tuple(
            redact_text(item)[:MAX_ATTRIBUTE_CHARS]
            for item in value[:20]
            if isinstance(item, (str, int, float, bool))
        )
        return safe or None
    return None


def safe_event_attributes(payload: Mapping[str, object] | None) -> dict[str, Any]:
    """Select fixed, low-cardinality fields; arbitrary detail is never exported."""
    attributes: dict[str, Any] = {}
    for source_key, target_key in _SAFE_FIELD_MAP.items():
        if source_key not in (payload or {}):
            continue
        value = _safe_value((payload or {})[source_key])
        if value is not None:
            attributes[target_key] = value
    return attributes


class WorkshopTelemetry:
    """OTel instruments backed by the provider configured by Databricks."""

    def __init__(
        self,
        *,
        tracer_provider=None,
        meter_provider=None,
        logger_provider=None,
        active_sessions: Callable[[], int] = _active_sessions,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        tracer_provider = tracer_provider or trace.get_tracer_provider()
        meter_provider = meter_provider or metrics.get_meter_provider()
        self._tracer = tracer_provider.get_tracer("workshop-terminal", "1")
        self._meter = meter_provider.get_meter("workshop-terminal", "1")
        self._logger = (
            logger_provider.get_logger("workshop-terminal", "1")
            if logger_provider is not None
            else _logs.get_logger("workshop-terminal", "1")
        )
        self._clock = clock
        self._active_sessions = active_sessions
        self._readiness_lock = threading.Lock()
        self._last_readiness_signature: tuple[object, ...] | None = None
        self._last_readiness_event_at = 0.0

        self.session_launches = self._meter.create_counter(
            "workshop.session.launches", unit="{session}"
        )
        self.session_refusals = self._meter.create_counter(
            "workshop.session.refusals", unit="{request}"
        )
        self.agent_exits = self._meter.create_counter(
            "workshop.agent.exits", unit="{process}"
        )
        self.bootstrap_duration = self._meter.create_histogram(
            "workshop.bootstrap.duration", unit="ms"
        )
        self.entitlement_rate_limits = self._meter.create_counter(
            "workshop.entitlement.rate_limits", unit="{response}"
        )
        self.entitlement_http_429 = self._meter.create_counter(
            "workshop.entitlement.http_429", unit="{response}"
        )
        self.entitlement_requests = self._meter.create_counter(
            "workshop.entitlement.requests", unit="{request}"
        )
        self.entitlement_duration = self._meter.create_histogram(
            "workshop.entitlement.reconcile.duration", unit="ms"
        )
        self.entitlement_backoff_duration = self._meter.create_histogram(
            "workshop.entitlement.backoff.duration", unit="s"
        )
        self.entitlement_cache_accesses = self._meter.create_counter(
            "workshop.entitlement.cache.accesses", unit="{access}"
        )
        self.entitlement_convergence_latency = self._meter.create_histogram(
            "workshop.entitlement.convergence.latency", unit="ms"
        )
        self.app_deploy_duration = self._meter.create_histogram(
            "workshop.app.deploy.duration", unit="ms"
        )
        self.mirror_coverage = self._meter.create_histogram(
            "workshop.mirror.coverage", unit="1"
        )
        self.readiness_latency = self._meter.create_histogram(
            "workshop.readiness.latency", unit="ms"
        )
        self._meter.create_observable_gauge(
            "workshop.session.active",
            callbacks=[self._observe_active_sessions],
            unit="{session}",
        )

    def _observe_active_sessions(self, _options) -> list[Observation]:
        try:
            value = max(0, min(1, int(self._active_sessions())))
        except Exception:  # noqa: BLE001
            value = 0
        return [Observation(value)]

    def record(self, event_name: str, payload: Mapping[str, object] | None = None) -> None:
        """Emit one correlated span and structured log, never raising."""
        try:
            name = _event_name(event_name)
            attributes = {"event.name": name, **safe_event_attributes(payload)}
            with self._tracer.start_as_current_span(f"workshop.{name}") as span:
                span.set_attributes(attributes)
                self._logger.emit(
                    body=name,
                    severity_number=SeverityNumber.INFO,
                    severity_text="INFO",
                    event_name=name,
                    attributes={"schema_version": SCHEMA_VERSION, **attributes},
                )
            self._record_metric(name, attributes)
        except Exception:
            logger.debug("structured telemetry emit failed", exc_info=True)

    def _record_metric(self, name: str, attributes: Mapping[str, object]) -> None:
        metric_attributes = {
            key: value
            for key, value in attributes.items()
            if key
            in {
                "agent.id",
                "event.outcome",
                "event.reason_code",
                "bootstrap.phase",
                "operation.source",
                "operation.backoff_seconds",
                "operation.resumed",
            }
            and isinstance(value, (str, int, float, bool))
        }
        if name == "session.started":
            self.session_launches.add(1, metric_attributes)
        elif name == "session.create_failed":
            self.session_refusals.add(1, metric_attributes)
        elif name == "session.exited":
            self.agent_exits.add(1, metric_attributes)
        elif name == "bootstrap.phase":
            self.bootstrap_duration.record(
                float(attributes.get("operation.duration_ms", 0)), metric_attributes
            )
        elif name == "entitlement.reconcile":
            self.entitlement_duration.record(
                float(attributes.get("operation.duration_ms", 0)), metric_attributes
            )
            request_count = int(attributes.get("operation.request_count", 0))
            rate_limit_count = int(
                attributes.get("entitlement.rate_limit_count", 0)
            )
            if not rate_limit_count and attributes.get("entitlement.rate_limited") is True:
                rate_limit_count = 1
            cache_hits = int(attributes.get("entitlement.cache_hits", 0))
            cache_misses = int(attributes.get("entitlement.cache_misses", 0))
            http_429_count = int(attributes.get("entitlement.http_429_count", 0))
            if request_count:
                self.entitlement_requests.add(request_count, metric_attributes)
            if rate_limit_count:
                self.entitlement_rate_limits.add(rate_limit_count, metric_attributes)
            if http_429_count:
                self.entitlement_http_429.add(http_429_count, metric_attributes)
            if cache_hits:
                self.entitlement_cache_accesses.add(
                    cache_hits, {**metric_attributes, "cache.result": "hit"}
                )
            if cache_misses:
                self.entitlement_cache_accesses.add(
                    cache_misses, {**metric_attributes, "cache.result": "miss"}
                )
            backoff = float(attributes.get("operation.backoff_seconds", 0))
            if backoff:
                self.entitlement_backoff_duration.record(backoff, metric_attributes)
            convergence = float(attributes.get("operation.convergence_ms", 0))
            if convergence:
                self.entitlement_convergence_latency.record(
                    convergence, metric_attributes
                )
        elif name == "mirror.fetch":
            self.mirror_coverage.record(
                float(attributes.get("mirror.coverage", 0)), metric_attributes
            )
        elif name == "app.deploy":
            self.app_deploy_duration.record(
                float(attributes.get("operation.duration_ms", 0)), metric_attributes
            )

    def record_readiness(
        self, ready: bool, failed_checks: list[str], duration_ms: float
    ) -> None:
        """Measure every probe, but bound repetitive structured events."""
        attributes = {"event.outcome": "ready" if ready else "not_ready"}
        self.readiness_latency.record(float(duration_ms), attributes)
        signature = (ready, *failed_checks)
        now = self._clock()
        with self._readiness_lock:
            emit = (
                signature != self._last_readiness_signature
                or now - self._last_readiness_event_at >= READINESS_REEMIT_SECONDS
            )
            if emit:
                self._last_readiness_signature = signature
                self._last_readiness_event_at = now
        if emit:
            self.record(
                "readiness.result",
                {
                    "outcome": attributes["event.outcome"],
                    "failed_checks": failed_checks,
                    "duration_ms": duration_ms,
                },
            )


runtime = WorkshopTelemetry()


def record(event_name: str, payload: Mapping[str, object] | None = None) -> None:
    runtime.record(event_name, payload)


def record_readiness(ready: bool, failed_checks: list[str], duration_ms: float) -> None:
    try:
        runtime.record_readiness(ready, failed_checks, duration_ms)
    except Exception:
        logger.debug("readiness telemetry failed", exc_info=True)


_REQUIRED_RESOURCE_ATTRIBUTES = (
    "workshop.run_id",
    "workshop.unit_id",
    "workshop.event_name",
    "workshop.release_sha",
    "databricks.workspace.id",
    "service.name",
)


def health(env: Mapping[str, str]) -> dict[str, object]:
    """Secret-free OTel configuration summary for ``/readyz``."""
    endpoint_present = bool(env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())
    raw_protocol = env.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
    protocol = raw_protocol if raw_protocol == "grpc" else None
    attributes = parse_resource_attributes(env.get("OTEL_RESOURCE_ATTRIBUTES", ""))
    missing = [key for key in _REQUIRED_RESOURCE_ATTRIBUTES if not attributes.get(key)]
    configured = endpoint_present and protocol is not None
    complete = configured and not missing
    return {
        "enabled": configured,
        "configured": complete,
        "state": "green" if complete else "amber",
        "protocol": protocol,
        "collector_endpoint_present": endpoint_present,
        "service_name_present": bool(env.get("OTEL_SERVICE_NAME", "").strip()),
        "required_resource_attributes": list(_REQUIRED_RESOURCE_ATTRIBUTES),
        "missing_resource_attributes": missing,
    }


def install_asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop,
) -> Callable | None:
    """Report uncaught task failures, then preserve asyncio's normal handling."""
    previous = loop.get_exception_handler()

    def handle(active_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        exception = context.get("exception")
        record(
            "asyncio.task_failed",
            {
                "code": "unhandled_exception",
                "exception_type": type(exception).__name__ if exception else "unknown",
            },
        )
        if previous is not None:
            previous(active_loop, context)
        else:
            active_loop.default_exception_handler(context)

    loop.set_exception_handler(handle)
    return previous


__all__ = [
    "RedactionFilter",
    "WorkshopTelemetry",
    "health",
    "install_asyncio_exception_handler",
    "install_logging_redaction",
    "record",
    "record_readiness",
    "redact_text",
    "runtime",
    "safe_event_attributes",
]
