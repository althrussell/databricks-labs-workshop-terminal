#!/usr/bin/env python3
"""Cold-start the release entry point and record its offline startup time."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MAX_STARTUP_SECONDS = 30.0


def _request(url: str) -> tuple[int, dict]:
    request = Request(url, headers={"X-Forwarded-Email": "smoke@example.com"})
    try:
        response = urlopen(request, timeout=2)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def _wait_for_health(url: str, process: subprocess.Popen, timeout: float) -> float:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"release exited before health check ({process.returncode})")
        try:
            status, payload = _request(f"{url}/healthz")
            if status == 200 and payload == {"status": "ok"}:
                return time.monotonic() - started
        except (URLError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise RuntimeError(f"release did not become healthy within {timeout:.1f}s")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timeout", type=float, default=MAX_STARTUP_SECONDS)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    artifact = args.artifact.resolve()
    if not artifact.is_file():
        raise SystemExit(f"artifact does not exist: {artifact}")
    url = f"http://127.0.0.1:{args.port}"
    with tempfile.TemporaryDirectory(prefix="wt-benchmark-") as temporary:
        root = Path(temporary)
        env = {
            **os.environ,
            "LOCAL_DEV": "1",
            "DATA_ROOT": str(root / "data"),
            "PEX_ROOT": str(root / "pex"),
            "DATABRICKS_APP_PORT": str(args.port),
            "DATABRICKS_HOST": "https://smoke.invalid",
            "WORKSHOP_AGENTS": "claude,codex,omnigent",
            "OMNIGENT_ENABLED": "true",
            "WORKSHOP_RUN_ID": "release-smoke",
            "WORKSHOP_UNIT_ID": "release-smoke-1",
            "WORKSHOP_RELEASE_SHA": "0" * 40,
            # Exercise the packaged OTel console-script path without waiting on
            # a collector. The SDK-disable flag prevents any export attempt.
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            "OTEL_SDK_DISABLED": "true",
        }
        process = subprocess.Popen(
            [str(artifact)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        failure: Exception | None = None
        try:
            startup_seconds = _wait_for_health(url, process, args.timeout)
            agents_status, agents = _request(f"{url}/api/agents")
            ready_status, ready = _request(f"{url}/readyz")
            agent_ids = [item["id"] for item in agents.get("agents", [])]
            if agents_status != 200 or agent_ids != ["omnigent", "claude", "codex"]:
                raise RuntimeError(f"unexpected packaged agent catalog: {agents}")
            if ready_status not in {200, 503} or "ready" not in ready:
                raise RuntimeError(f"packaged readiness endpoint is malformed: {ready}")
        except Exception as error:  # report the packaged process log with the failure
            failure = error
        finally:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate(timeout=5)
        if failure is not None:
            raise RuntimeError(f"{failure}\npackaged process output:\n{output[-4000:]}")
        if startup_seconds > args.timeout:
            raise RuntimeError(
                f"cold startup took {startup_seconds:.3f}s; budget is {args.timeout:.3f}s"
            )
        result = {
            "artifact_name": artifact.name,
            "size_bytes": artifact.stat().st_size,
            "cold_startup_seconds": round(startup_seconds, 3),
            "startup_budget_seconds": args.timeout,
            "healthz_status": 200,
            "readyz_status": ready_status,
            "agent_ids": agent_ids,
            "network_dependency_install": False,
            "otel_bootstrap_exercised": True,
        }
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, sort_keys=True))
        if output:
            print(output[-4000:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
