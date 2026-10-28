"""Prepare Databricks Apps OpenTelemetry and start the Omnigent control plane.

Merges Control Tower's workshop identity fields (``workshop.run_id``,
``workshop.unit_id``, ``workshop.event_name``, ``workshop.release_sha``) into
``OTEL_RESOURCE_ATTRIBUTES`` before ``opentelemetry-instrument`` or ``app.py``
imports the runtime, matching Workshop Terminal's ``server/otel_bootstrap.py``.
Omnigent's own internal telemetry remains unset.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, MutableMapping
from urllib.parse import quote, unquote


def parse_resource_attributes(raw: str) -> dict[str, str]:
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
    release_sha = (
        env.get("WORKSHOP_RELEASE_SHA", "").strip()
        or env.get("OMNIGENT_BUILD_SHA", "").strip()
    )
    workshop_attributes = {
        "workshop.run_id": env.get("WORKSHOP_RUN_ID", "").strip(),
        "workshop.unit_id": env.get("WORKSHOP_UNIT_ID", "").strip(),
        "workshop.event_name": env.get("EVENT_NAME", "").strip(),
        "workshop.release_sha": release_sha,
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
    env.setdefault("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    env.setdefault("OTEL_TRACES_SAMPLER_ARG", "0.1")
    env.setdefault(
        "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS",
        "health,/health,/healthz,/readyz",
    )


def instrumentation_enabled(env: Mapping[str, str]) -> bool:
    protocol = env.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
    return bool(
        env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        and protocol == "grpc"
    )


def launch_command(env: Mapping[str, str]) -> list[str]:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
    base = [sys.executable, script]
    if instrumentation_enabled(env):
        return ["opentelemetry-instrument", *base]
    return base


def main() -> None:
    prepare_environment(os.environ)
    cmd = launch_command(os.environ)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
