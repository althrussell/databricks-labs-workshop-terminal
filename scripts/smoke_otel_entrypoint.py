#!/usr/bin/env python3
"""Prove the packaged entry point exports all OTel signals over OTLP/gRPC.

The normal release smoke imports WT through ``PEX_INTERPRETER`` so it can test
agent lifecycles without starting a second process. That deliberately bypasses
``server.otel_bootstrap``. This smoke launches the PEX exactly as Databricks
Apps does and sends its telemetry to a local collector in an offline container.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_PORT = 8767
COLLECTOR_PORT = 4314


def _request(route: str) -> int:
    request = Request(
        f"http://127.0.0.1:{APP_PORT}{route}",
        headers={"X-Forwarded-Email": "packaged-smoke@example.com"},
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status
    except HTTPError as error:
        return error.code


def _wait_until_running() -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if _request("/healthz") == 200:
                return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise AssertionError("packaged OTel smoke never became healthy")


def _wait_for_collector() -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", COLLECTOR_PORT), timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("local OTLP collector never started")


def _read_result(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _wait_for_all_signals(path: Path) -> dict:
    deadline = time.monotonic() + 15
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = _read_result(path)
        if all(
            int(latest.get(signal, {}).get("records", 0)) > 0
            for signal in ("logs", "metrics", "traces")
        ):
            return latest
        time.sleep(0.2)
    return latest


def _stop(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        process.terminate()
    try:
        output, _ = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate(timeout=5)
        raise AssertionError("OTel smoke process did not stop after SIGTERM")
    return output


def _assert_telemetry(result: dict, output: str, collector_output: str) -> None:
    missing: list[str] = []
    required_attributes = {
        "workshop.run_id": "packaged-smoke",
        "workshop.unit_id": "unit-smoke",
        "databricks.workspace.id": "123456789",
        "service.name": "wt-packaged-smoke",
    }
    for signal in ("logs", "metrics", "traces"):
        value = result.get(signal, {})
        if int(value.get("records", 0)) <= 0:
            missing.append(f"{signal} records")
        attributes = value.get("resource_attributes", {})
        for name, expected in required_attributes.items():
            if attributes.get(name) != expected:
                missing.append(f"{signal} {name}")
    metric_names = set(result.get("metrics", {}).get("names", []))
    if "workshop.readiness.latency" not in metric_names:
        missing.append("custom readiness metric")
    if missing:
        raise AssertionError(
            "packaged entry point did not export "
            + ", ".join(missing)
            + "\n--- collector result ---\n"
            + json.dumps(result, indent=2, sort_keys=True)
            + "\n--- runtime output ---\n"
            + output[-8000:]
            + "\n--- collector output ---\n"
            + collector_output[-4000:]
        )


def main() -> int:
    artifact = Path(os.environ["WT_RELEASE_ARTIFACT"]).resolve()
    if not artifact.is_file():
        raise AssertionError(f"release artifact is missing: {artifact}")
    collector_script = Path(__file__).with_name("fake_otlp_collector.py").resolve()

    with tempfile.TemporaryDirectory(prefix="wt-otel-entrypoint-") as temporary:
        root = Path(temporary)
        result_path = root / "collector-result.json"
        pex_root = root / "pex"
        collector_environment = {
            **os.environ,
            "PEX_INTERPRETER": "1",
            "PEX_ROOT": str(pex_root),
            "WT_OTLP_PORT": str(COLLECTOR_PORT),
            "WT_OTLP_RESULT": str(result_path),
        }
        collector = subprocess.Popen(
            [sys.executable, str(artifact), str(collector_script)],
            cwd=root,
            env=collector_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        app: subprocess.Popen[str] | None = None
        app_output = ""
        result: dict = {}
        try:
            _wait_for_collector()
            environment = {
                **os.environ,
                "LOCAL_DEV": "1",
                "DATA_ROOT": str(root / "data"),
                "DATABRICKS_APP_NAME": "wt-packaged-smoke",
                "DATABRICKS_APP_PORT": str(APP_PORT),
                "DATABRICKS_HOST": "https://smoke.invalid",
                "DATABRICKS_WORKSPACE_ID": "123456789",
                "EVENT_NAME": "Packaged OTel smoke",
                "OMNIGENT_ENABLED": "true",
                "OTEL_BLRP_SCHEDULE_DELAY": "100",
                "OTEL_BSP_SCHEDULE_DELAY": "100",
                "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{COLLECTOR_PORT}",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
                "OTEL_METRIC_EXPORT_INTERVAL": "250",
                "OTEL_SERVICE_NAME": "wt-packaged-smoke",
                "OTEL_TRACES_SAMPLER": "always_on",
                "PEX_ROOT": str(pex_root),
                "WORKSHOP_AGENTS": "claude,codex,omnigent",
                "WORKSHOP_RELEASE_SHA": "a" * 40,
                "WORKSHOP_RUN_ID": "packaged-smoke",
                "WORKSHOP_UNIT_ID": "unit-smoke",
            }
            app = subprocess.Popen(
                [sys.executable, str(artifact)],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            _wait_until_running()
            if _request("/readyz") not in {200, 503}:
                raise AssertionError("packaged readiness returned an invalid status")
            result = _wait_for_all_signals(result_path)
        finally:
            if app is not None:
                app_output = _stop(app)
            collector_output = _stop(collector)

    _assert_telemetry(result, app_output, collector_output)
    print("packaged entry point exported OTel logs, metrics, spans, and identity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
