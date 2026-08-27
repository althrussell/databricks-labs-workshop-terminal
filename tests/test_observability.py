import json
import logging
import threading
import time

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    InMemoryLogRecordExporter,
    LogRecordExporter,
    LogRecordExportResult,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from server import observability


def _runtime(active_sessions=lambda: 0):
    resource = Resource.create(
        {
            "workshop.run_id": "run-1",
            "workshop.unit_id": "unit-2",
            "databricks.workspace.id": "123",
            "service.name": "wt",
        }
    )
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    log_exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    runtime = observability.WorkshopTelemetry(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        active_sessions=active_sessions,
    )
    return runtime, spans, metric_reader, log_exporter


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    return {
        metric.name
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def test_in_memory_exporters_receive_resource_spans_metrics_and_trace_ids():
    runtime, spans, metrics, logs = _runtime(active_sessions=lambda: 8)

    runtime.record(
        "session.exited",
        {
            "agent": "codex",
            "code": "process_error",
            "outcome": "ended",
            "exit_code": 17,
            "detail": "must never be exported",
        },
    )
    runtime.record(
        "entitlement.reconcile",
        {
            "source": "background",
            "outcome": "degraded",
            "code": "rate_limited",
            "rate_limited": True,
            "duration_ms": 42,
        },
    )

    exported = spans.get_finished_spans()
    assert [span.name for span in exported] == [
        "workshop.session.exited",
        "workshop.entitlement.reconcile",
    ]
    assert exported[0].resource.attributes["workshop.run_id"] == "run-1"
    first_log = logs.get_finished_logs()[0].log_record
    assert first_log.trace_id == exported[0].context.trace_id
    assert first_log.span_id == exported[0].context.span_id
    assert first_log.event_name == "session.exited"
    assert first_log.attributes["schema_version"] == 1
    assert first_log.attributes["event.reason_code"] == "process_error"
    assert first_log.attributes["process.exit_code"] == 17
    assert "detail" not in first_log.attributes
    rate_limit_log = logs.get_finished_logs()[1].log_record
    assert rate_limit_log.trace_id == exported[1].context.trace_id
    assert rate_limit_log.attributes["event.reason_code"] == "rate_limited"
    assert rate_limit_log.attributes["entitlement.rate_limited"] is True
    assert {
        "workshop.session.active",
        "workshop.agent.exits",
        "workshop.entitlement.rate_limits",
        "workshop.entitlement.reconcile.duration",
    } <= _metric_names(metrics)


def test_all_required_metric_families_are_registered_and_recorded():
    runtime, _spans, metrics, _logs = _runtime(active_sessions=lambda: 1)

    runtime.record("session.started", {"agent": "claude", "outcome": "started"})
    runtime.record(
        "session.create_failed",
        {"agent": "codex", "code": "session_conflict"},
    )
    runtime.record(
        "bootstrap.phase",
        {"step": "codex", "outcome": "complete", "duration_ms": 12},
    )
    runtime.record(
        "mirror.fetch", {"outcome": "served", "code": "hit", "coverage": 1.0}
    )
    runtime.record_readiness(True, [], 3.5)

    assert {
        "workshop.session.launches",
        "workshop.session.refusals",
        "workshop.session.active",
        "workshop.bootstrap.duration",
        "workshop.mirror.coverage",
        "workshop.readiness.latency",
    } <= _metric_names(metrics)


def test_redaction_filter_removes_secrets_prompts_terminal_io_configs_and_email():
    secret_values = [
        "Bearer top-secret-token",
        "Authorization: Bearer authorization-secret-token",
        '{"prompt": "private customer roadmap", "token": "also-secret"}',
        "DATABRICKS_TOKEN=dapi1234567890abcdef",
        "client_secret=hunter2",
        "alice@example.com",
        "prompt=show me unreleased customer data",
        "terminal_output=the private transcript",
        "config_file={everything: visible}",
        "GET /api/certificate?name=Ada%20Lovelace&q=private-plan HTTP/1.1",
    ]

    for value in secret_values:
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, value, (), None)
        assert observability.RedactionFilter().filter(record)
        rendered = record.getMessage()
        assert "top-secret-token" not in rendered
        assert "authorization-secret-token" not in rendered
        assert "private customer roadmap" not in rendered
        assert "also-secret" not in rendered
        assert "dapi1234567890abcdef" not in rendered
        assert "hunter2" not in rendered
        assert "alice@example.com" not in rendered
        assert "unreleased customer data" not in rendered
        assert "private transcript" not in rendered
        assert "everything: visible" not in rendered
        assert "Ada%20Lovelace" not in rendered
        assert "private-plan" not in rendered


def test_logging_handlers_installed_after_redaction_never_receive_raw_values():
    records: list[logging.LogRecord] = []

    class LateHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    observability.install_logging_redaction()
    handler = LateHandler()
    late_logger = logging.getLogger("test.late-redaction-handler")
    late_logger.propagate = False
    late_logger.addHandler(handler)
    try:
        late_logger.error("late alice@example.com Bearer late-secret")
        try:
            raise RuntimeError(
                "prompt=private-plan for bob@example.com Bearer exception-secret"
            )
        except RuntimeError:
            late_logger.exception("late handler exception")
    finally:
        late_logger.removeHandler(handler)

    rendered = [record.getMessage() for record in records]
    assert rendered[0] == "late [REDACTED_EMAIL] Bearer [REDACTED]"
    assert "private-plan" not in rendered[1]
    assert "bob@example.com" not in rendered[1]
    assert "exception-secret" not in rendered[1]
    assert records[1].exc_info is None


def test_redaction_is_idempotent_across_factory_and_handler_filters():
    once = observability.redact_text(
        "token=dapi1234567890abcdef&name=Ada%20Lovelace"
    )

    assert observability.redact_text(once) == once


def test_health_never_returns_the_collector_endpoint_or_credentials():
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.internal:4314/secret",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_SERVICE_NAME": "wt",
        "OTEL_RESOURCE_ATTRIBUTES": (
            "workshop.run_id=r,workshop.unit_id=u,workshop.event_name=e,"
            "workshop.release_sha=s,databricks.workspace.id=w,service.name=wt"
        ),
        "DATABRICKS_CLIENT_SECRET": "never-return-me",
    }

    status = observability.health(env)
    encoded = json.dumps(status)

    assert status["configured"] is True
    assert status["collector_endpoint_present"] is True
    assert "collector.internal" not in encoded
    assert "never-return-me" not in encoded


class _SlowExporter(SpanExporter):
    def __init__(self):
        self.called = threading.Event()
        self.release = threading.Event()

    def export(self, spans):
        self.called.set()
        self.release.wait(1)
        return SpanExportResult.SUCCESS


class _SlowLogExporter(LogRecordExporter):
    def __init__(self):
        self.called = threading.Event()
        self.release = threading.Event()

    def export(self, batch):
        self.called.set()
        self.release.wait(1)
        return LogRecordExportResult.SUCCESS

    def shutdown(self):
        self.release.set()

    def force_flush(self, timeout_millis=10_000):
        return True


def test_slow_exporter_does_not_block_a_session_event():
    exporter = _SlowExporter()
    log_exporter = _SlowLogExporter()
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=10))
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(log_exporter, schedule_delay_millis=10)
    )
    runtime = observability.WorkshopTelemetry(
        tracer_provider=provider,
        meter_provider=MeterProvider(),
        logger_provider=logger_provider,
    )

    started = time.monotonic()
    runtime.record("session.started", {"agent": "codex"})
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert exporter.called.wait(1)
    assert log_exporter.called.wait(1)
    exporter.release.set()
    log_exporter.release.set()
    provider.shutdown()
    logger_provider.shutdown()


def test_arbitrary_detail_and_attendee_content_never_enter_exported_records():
    payload = {
        "agent": "codex",
        "code": "turn_failed",
        "raw_code": "Bearer a-secret",
        "detail": "prompt=customer roadmap",
        "attendee": "alice@example.com",
    }
    attrs = observability.safe_event_attributes(payload)

    runtime, spans, _metrics, logs = _runtime()
    runtime.record("session.create_failed", payload)
    exported = repr(
        (
            spans.get_finished_spans()[0].attributes,
            logs.get_finished_logs()[0].log_record.body,
            logs.get_finished_logs()[0].log_record.attributes,
        )
    )

    assert attrs == {"agent.id": "codex", "event.reason_code": "turn_failed"}
    assert "a-secret" not in exported
    assert "customer roadmap" not in exported
    assert "alice@example.com" not in exported


def test_safe_event_attributes_are_allowlisted():
    attrs = observability.safe_event_attributes(
        {
            "agent": "codex",
            "code": "turn_failed",
            "raw_code": "Bearer a-secret",
            "detail": "prompt=customer roadmap",
            "attendee": "alice@example.com",
        }
    )

    assert attrs == {"agent.id": "codex", "event.reason_code": "turn_failed"}
