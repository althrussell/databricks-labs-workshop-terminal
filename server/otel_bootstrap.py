"""Prepare Databricks Apps OpenTelemetry and start Workshop Terminal.

Databricks owns the collector endpoint, protocol, and its resource attributes.
Control Tower owns the workshop identity fields.  The OpenTelemetry launcher
must see the merged environment *before* it imports ``server.main``; doing this
in FastAPI's lifespan is too late for complete request instrumentation.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from urllib.parse import quote, unquote


def parse_resource_attributes(raw: str) -> dict[str, str]:
    """Parse the OTel comma-separated, percent-encoded resource format."""
    attributes: dict[str, str] = {}
    for part in (raw or "").split(","):
        key, separator, value = part.partition("=")
        key = unquote(key.strip())
        if not separator or not key:
            continue
        attributes[key] = unquote(value.strip())
    return attributes


def _encode_resource_attributes(attributes: Mapping[str, str]) -> str:
    return ",".join(
        f"{quote(key, safe='._-')}={quote(value, safe='._-')}"
        for key, value in attributes.items()
        if key and value
    )


def merged_resource_attributes(env: Mapping[str, str]) -> str:
    """Merge WT identity with, rather than replace, platform attributes."""
    attributes = parse_resource_attributes(env.get("OTEL_RESOURCE_ATTRIBUTES", ""))
    workspace_id = (
        env.get("DATABRICKS_WORKSPACE_ID", "").strip()
        or attributes.get("workspace.id", "").strip()
    )
    service_name = (
        env.get("OTEL_SERVICE_NAME", "").strip()
        or env.get("DATABRICKS_APP_NAME", "").strip()
        or attributes.get("app.name", "").strip()
    )
    workshop_attributes = {
        "workshop.run_id": env.get("WORKSHOP_RUN_ID", "").strip(),
        "workshop.unit_id": env.get("WORKSHOP_UNIT_ID", "").strip(),
        "workshop.event_name": env.get("EVENT_NAME", "").strip(),
        "workshop.release_sha": env.get("WORKSHOP_RELEASE_SHA", "").strip(),
        "databricks.workspace.id": workspace_id,
        "service.name": service_name,
    }
    attributes.update(
        {key: value for key, value in workshop_attributes.items() if value}
    )
    return _encode_resource_attributes(attributes)


def prepare_environment(env: MutableMapping[str, str]) -> None:
    merged = merged_resource_attributes(env)
    if merged:
        env["OTEL_RESOURCE_ATTRIBUTES"] = merged


def instrumentation_enabled(env: Mapping[str, str]) -> bool:
    """Only enable export when Databricks supplied the complete destination."""
    protocol = env.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
    return bool(
        env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        and protocol == "grpc"
    )


def uvicorn_command(env: Mapping[str, str]) -> list[str]:
    """Resolve the runtime port before passing argv to uvicorn."""
    port = env.get("DATABRICKS_APP_PORT", "").strip() or "8000"
    command = [
        "uvicorn",
        "server.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        port,
        "--workers",
        "1",
    ]
    if instrumentation_enabled(env):
        return ["opentelemetry-instrument", *command]
    return command


def main() -> None:
    prepare_environment(os.environ)
    command = uvicorn_command(os.environ)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
