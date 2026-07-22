#!/usr/bin/env python3
"""Bounded fleet operations contract for an external Control Tower."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable

import requests

try:
    from .ct_inventory import normalize_apps
except ImportError:  # pragma: no cover - direct script execution
    from ct_inventory import normalize_apps

_ACTIONS = ("status", "pause", "resume", "repush", "teardown-report")
_MAX_SUMMARY_COUNT = 1_000_000
_CREDENTIAL_STATES = {"rotating", "degraded", "unhealthy", "unknown"}


def _bounded_count(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return min(_MAX_SUMMARY_COUNT, max(0, parsed))


def _bounded_label(value) -> str | None:
    if value is None:
        return None
    text = "".join(char for char in str(value) if char.isprintable()).strip()
    return text[:128] or None


def _payload(response) -> dict:
    try:
        value = response.json()
    except (ValueError, AttributeError):
        return {}
    return value if isinstance(value, dict) else {}


def _setup_verified(value: dict) -> bool:
    steps = value.get("steps")
    manifest = value.get("release_manifest")
    return (
        isinstance(steps, dict)
        and bool(steps)
        and all(
            isinstance(step, dict) and step.get("status") == "complete"
            for step in steps.values()
        )
        and isinstance(manifest, dict)
        and bool(manifest)
        and all(
            isinstance(entry, dict)
            and (
                not entry.get("enabled", True)
                or entry.get("match") is True
            )
            for entry in manifest.values()
        )
    )


def _calls_for(
    action: str,
    *,
    content_pack: dict | None,
    phase: str | None,
) -> list[tuple[str, str, dict | None]]:
    if action == "pause":
        return [("POST", "/api/admin/agent-controls", {"enabled": False})]
    if action == "resume":
        return [("POST", "/api/admin/agent-controls", {"enabled": True})]
    if action == "repush":
        calls = []
        if content_pack is not None:
            calls.append(("POST", "/api/admin/content-pack", content_pack))
        if phase:
            calls.append(("POST", "/api/admin/phase", {"phase": phase}))
        if not calls:
            raise ValueError("repush requires --content-pack and/or --phase")
        return calls
    if action == "status":
        return [
            ("GET", "/readyz", None),
            ("GET", "/api/admin/setup-status", None),
            ("GET", "/api/admin/prewarm-status", None),
            ("GET", "/api/admin/state", None),
            ("GET", "/api/admin/agent-controls", None),
        ]
    if action == "teardown-report":
        # Reporting only: deletion remains the external CT's responsibility.
        return [
            ("GET", "/api/admin/presence", None),
            ("GET", "/api/admin/state", None),
            ("GET", "/api/admin/agent-controls", None),
        ]
    raise ValueError(f"unsupported fleet action: {action}")


def execute(
    apps: list[dict],
    *,
    action: str,
    request: Callable = requests.request,
    dry_run: bool = False,
    timeout: float = 30,
    content_pack: dict | None = None,
    phase: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    allow_local_http: bool = False,
) -> dict:
    apps = normalize_apps(apps, allow_local_http=allow_local_http)
    calls = _calls_for(action, content_pack=content_pack, phase=phase)
    deadline = monotonic() + max(0.1, float(timeout))
    results = []
    for app in apps:
        base = str(app["url"]).rstrip("/")
        result = {
            "name": str(app["name"]),
            "url": base,
            "status": "dry_run" if dry_run else "ok",
            "operations": [],
        }
        if action == "status":
            result["rollup"] = {
                "ready": False,
                "setup_verified": False,
                "prewarm_reusable": False,
                "phase": None,
                "agents_enabled": None,
            }
        if action == "teardown-report":
            result["summary"] = {
                "presence_count": 0,
                "session_count": 0,
                "phase": None,
                "credential_state": "unknown",
                "entitlements_ok": None,
                "agents_enabled": None,
            }
        if dry_run:
            result["operations"] = [
                {"method": method, "path": path} for method, path, _ in calls
            ]
            results.append(result)
            continue
        headers = {"Authorization": f"Bearer {app.get('token', '')}"}
        for method, path, body in calls:
            operation = {"method": method, "path": path, "status_code": None}
            remaining = deadline - monotonic()
            if remaining <= 0:
                operation["error"] = "fleet_timeout"
                result["status"] = "error"
                result["operations"].append(operation)
                break
            try:
                response = request(
                    method,
                    f"{base}{path}",
                    headers=headers,
                    json=body,
                    timeout=min(30.0, remaining),
                )
                operation["status_code"] = int(response.status_code)
                payload = _payload(response)
                if action == "status" and response.status_code == 200:
                    if path == "/readyz":
                        result["rollup"]["ready"] = payload.get("ready") is True
                    elif path == "/api/admin/setup-status":
                        result["rollup"]["setup_verified"] = _setup_verified(payload)
                    elif path == "/api/admin/prewarm-status":
                        result["rollup"]["prewarm_reusable"] = (
                            payload.get("reusable") is True
                        )
                    elif path == "/api/admin/state":
                        result["rollup"]["phase"] = payload.get("phase")
                    elif path == "/api/admin/agent-controls":
                        result["rollup"]["agents_enabled"] = payload.get(
                            "agents_enabled"
                        )
                if action == "teardown-report" and response.status_code == 200:
                    summary = result["summary"]
                    if path == "/api/admin/presence":
                        users = payload.get("users")
                        summary["presence_count"] = (
                            _bounded_count(len(users))
                            if isinstance(users, list)
                            else 0
                        )
                        summary["session_count"] = _bounded_count(
                            payload.get("session_count")
                        )
                        credential = payload.get("credential")
                        entitlements = payload.get("entitlements")
                        if isinstance(credential, dict):
                            state = str(
                                credential.get("state") or "unknown"
                            ).lower()
                            summary["credential_state"] = (
                                state if state in _CREDENTIAL_STATES else "unknown"
                            )
                        if isinstance(entitlements, dict):
                            value = entitlements.get("ok")
                            summary["entitlements_ok"] = (
                                value if isinstance(value, bool) else None
                            )
                    elif path == "/api/admin/state":
                        summary["phase"] = _bounded_label(payload.get("phase"))
                    elif path == "/api/admin/agent-controls":
                        value = payload.get("agents_enabled")
                        summary["agents_enabled"] = (
                            value if isinstance(value, bool) else None
                        )
                if not 200 <= response.status_code < 300:
                    operation["error"] = "http_error"
                    result["status"] = "error"
            except requests.RequestException:
                operation["error"] = "request_error"
                result["status"] = "error"
            result["operations"].append(operation)
            if result["status"] == "error":
                break
        if action == "status" and (
            result["rollup"]["ready"] is not True
            or result["rollup"]["setup_verified"] is not True
            or result["rollup"]["prewarm_reusable"] is not True
        ):
            result["status"] = "error"
        results.append(result)
    ok = all(result["status"] in {"ok", "dry_run"} for result in results)
    return {
        "schema_version": 1,
        "operation": action,
        "dry_run": bool(dry_run),
        "status": "ok" if ok else "partial_failure",
        "exit_code": 0 if ok else 1,
        "apps": results,
    }


def _load_inventory(path: str, default_token_env: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_apps = payload.get("apps", []) if isinstance(payload, dict) else []
    apps = []
    for raw in raw_apps:
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("url"):
            raise ValueError("each inventory app requires name and url")
        token_env = str(raw.get("token_env") or default_token_env)
        token = os.environ.get(token_env, "")
        if not token:
            raise ValueError(f"credential environment variable is unset: {token_env}")
        apps.append({"name": str(raw["name"]), "url": str(raw["url"]), "token": token})
    return apps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--token-env", default="DATABRICKS_TOKEN")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-local-http", action="store_true")
    parser.add_argument("action", choices=_ACTIONS)
    parser.add_argument("--content-pack")
    parser.add_argument("--phase")
    args = parser.parse_args()
    try:
        pack = None
        if args.content_pack:
            with open(args.content_pack, encoding="utf-8") as handle:
                pack = json.load(handle)
            if not isinstance(pack, dict):
                raise ValueError("content pack must be a JSON object")
        report = execute(
            _load_inventory(args.inventory, args.token_env),
            action=args.action,
            dry_run=args.dry_run,
            timeout=args.timeout,
            content_pack=pack,
            phase=args.phase,
            allow_local_http=args.allow_local_http,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        report = {
            "schema_version": 1,
            "operation": args.action,
            "dry_run": bool(args.dry_run),
            "status": "invalid_input",
            "exit_code": 2,
            "error": str(error),
            "apps": [],
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
