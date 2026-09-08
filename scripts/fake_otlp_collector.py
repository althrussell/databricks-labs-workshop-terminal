#!/usr/bin/env python3
"""Small offline OTLP/gRPC collector used only by the release smoke test."""

from __future__ import annotations

from concurrent import futures
import json
import os
from pathlib import Path
import signal
import threading

import grpc
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.logs.v1.logs_service_pb2_grpc import (
    LogsServiceServicer,
    add_LogsServiceServicer_to_server,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2_grpc import (
    MetricsServiceServicer,
    add_MetricsServiceServicer_to_server,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import (
    TraceServiceServicer,
    add_TraceServiceServicer_to_server,
)


RESULT = Path(os.environ["WT_OTLP_RESULT"])
LOCK = threading.Lock()
STATE = {
    signal_name: {"records": 0, "names": [], "resource_attributes": {}}
    for signal_name in ("logs", "metrics", "traces")
}


def _value(value) -> object:
    selected = value.WhichOneof("value")
    return getattr(value, selected) if selected else None


def _attributes(resource) -> dict[str, object]:
    return {item.key: _value(item.value) for item in resource.attributes}


def _record(signal_name: str, records: int, names: list[str], resource) -> None:
    with LOCK:
        state = STATE[signal_name]
        state["records"] += records
        state["names"] = sorted(set(state["names"]) | set(names))
        state["resource_attributes"].update(_attributes(resource))
        temporary = RESULT.with_suffix(".tmp")
        temporary.write_text(json.dumps(STATE, sort_keys=True), encoding="utf-8")
        temporary.replace(RESULT)


class LogsService(LogsServiceServicer):
    def Export(self, request, context):  # noqa: N802
        for resource_logs in request.resource_logs:
            records = [
                record
                for scope in resource_logs.scope_logs
                for record in scope.log_records
            ]
            _record(
                "logs",
                len(records),
                [record.body.string_value for record in records if record.body.string_value],
                resource_logs.resource,
            )
        return ExportLogsServiceResponse()


class MetricsService(MetricsServiceServicer):
    def Export(self, request, context):  # noqa: N802
        for resource_metrics in request.resource_metrics:
            metrics = [
                metric
                for scope in resource_metrics.scope_metrics
                for metric in scope.metrics
            ]
            _record(
                "metrics",
                len(metrics),
                [metric.name for metric in metrics],
                resource_metrics.resource,
            )
        return ExportMetricsServiceResponse()


class TraceService(TraceServiceServicer):
    def Export(self, request, context):  # noqa: N802
        for resource_spans in request.resource_spans:
            spans = [
                span
                for scope in resource_spans.scope_spans
                for span in scope.spans
            ]
            _record(
                "traces",
                len(spans),
                [span.name for span in spans],
                resource_spans.resource,
            )
        return ExportTraceServiceResponse()


def main() -> int:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    add_LogsServiceServicer_to_server(LogsService(), server)
    add_MetricsServiceServicer_to_server(MetricsService(), server)
    add_TraceServiceServicer_to_server(TraceService(), server)
    port = int(os.environ["WT_OTLP_PORT"])
    if server.add_insecure_port(f"127.0.0.1:{port}") != port:
        raise RuntimeError(f"could not bind OTLP collector to port {port}")
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stopped.set())
    signal.signal(signal.SIGINT, lambda *_args: stopped.set())
    server.start()
    stopped.wait()
    server.stop(grace=0).wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
