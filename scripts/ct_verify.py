#!/usr/bin/env python3
"""Read-only two-instance prewarm gate for an external Control Tower."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable

import requests

try:
    from .ct_inventory import normalize_apps
except ImportError:  # pragma: no cover - direct script execution
    from ct_inventory import normalize_apps


def _safe_json(response) -> dict:
    try:
        payload = response.json()
    except (ValueError, AttributeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _setup_valid(payload: dict) -> bool:
    steps = payload.get("steps")
    manifest = payload.get("release_manifest")
    if not isinstance(steps, dict) or not steps:
        return False
    if any(
        not isinstance(step, dict) or step.get("status") != "complete"
        for step in steps.values()
    ):
        return False
    if not isinstance(manifest, dict) or not manifest:
        return False
    for name, entry in manifest.items():
        if not isinstance(entry, dict):
            return False
        if entry.get("enabled", True) and entry.get("match") is not True:
            return False
        if name == "ai_dev_kit" and entry.get("source") not in {
            "network",
            "prewarmed",
        }:
            return False
    return True


_VOLATILE_RELEASE_FIELDS = {
    "source",
    "started_at",
    "completed_at",
    "duration_ms",
}
_CORE_BINARIES = frozenset({"node", "claude", "codex", "databricks"})
_OMNIGENT_BINARIES = frozenset({"omnigent", "tmux"})


def _canonical_release_manifest(payload: dict) -> dict:
    manifest = payload.get("release_manifest")
    if not isinstance(manifest, dict):
        return {}
    return {
        str(name): {
            str(key): value
            for key, value in entry.items()
            if key not in _VOLATILE_RELEASE_FIELDS
        }
        for name, entry in sorted(manifest.items())
        if isinstance(entry, dict)
    }


def _prewarm_valid(
    payload: dict,
    *,
    expected_binaries: frozenset[str] | None = None,
) -> bool:
    manifest = payload.get("manifest")
    if payload.get("reusable") is not True or not isinstance(manifest, dict):
        return False
    raw_expected = manifest.get("expected_binaries")
    binaries = manifest.get("binaries")
    ai_dev_kit = manifest.get("ai_dev_kit")
    if (
        not isinstance(raw_expected, list)
        or not all(isinstance(name, str) and name for name in raw_expected)
        or len(raw_expected) != len(set(raw_expected))
        or not isinstance(binaries, dict)
    ):
        return False
    expected = frozenset(raw_expected)
    allowed = {
        _CORE_BINARIES,
        _CORE_BINARIES | _OMNIGENT_BINARIES,
    }
    if expected not in allowed:
        return False
    if expected_binaries is not None and expected != expected_binaries:
        return False
    if set(binaries) != expected:
        return False
    if not all(
        isinstance(entry, dict)
        and entry.get("reusable") is True
        and bool(entry.get("expected"))
        and bool(entry.get("actual"))
        and entry.get("actual") == entry.get("expected")
        and re.fullmatch(r"[0-9a-f]{64}", str(entry.get("actual_checksum") or ""))
        and entry.get("source") == "persistent"
        for entry in binaries.values()
    ):
        return False
    if not isinstance(ai_dev_kit, dict):
        return False
    expected_checksum = str(ai_dev_kit.get("expected_checksum") or "")
    actual_checksum = str(ai_dev_kit.get("actual_checksum") or "")
    return bool(
        ai_dev_kit.get("expected_ref")
        and ai_dev_kit.get("expected_ref") == ai_dev_kit.get("actual_ref")
        and re.fullmatch(
            r"[0-9a-fA-F]{40}",
            str(ai_dev_kit.get("resolved_commit") or ""),
        )
        and expected_checksum == actual_checksum
        and re.fullmatch(r"[0-9a-f]{64}", actual_checksum)
        and ai_dev_kit.get("source") == "persistent"
    )


def verify_apps(
    apps: list[dict],
    *,
    get: Callable = requests.get,
    timeout: float = 300,
    poll_interval: float = 5,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    allow_local_http: bool = False,
) -> dict:
    """Wait until exactly two apps pass both readiness and setup verification."""
    if len(apps) != 2:
        raise ValueError("prewarm verification requires exactly two apps")
    normalized = normalize_apps(
        apps,
        exact_count=2,
        allow_local_http=allow_local_http,
    )
    results = {
        app["name"]: {
            "name": str(app["name"]),
            "url": str(app["url"]).rstrip("/"),
            "ready": False,
            "readyz_status": None,
            "setup_status": None,
            "prewarm_status": None,
            "_release_manifest": None,
            "_prewarm_manifest": None,
        }
        for app in normalized
    }
    deadline = monotonic() + max(0.1, float(timeout))
    expired = False

    def request_with_deadline(url: str, headers: dict):
        nonlocal expired
        remaining = deadline - monotonic()
        if remaining <= 0:
            expired = True
            return None
        return get(
            url,
            headers=headers,
            timeout=min(30.0, remaining),
        )

    while True:
        for app in normalized:
            result = results[app["name"]]
            headers = {"Authorization": f"Bearer {app.get('token', '')}"}
            base = str(app["url"]).rstrip("/")
            try:
                ready_response = request_with_deadline(
                    f"{base}/readyz",
                    headers,
                )
                if ready_response is None:
                    break
                ready_payload = _safe_json(ready_response)
                result["readyz_status"] = int(ready_response.status_code)
                ready_ok = (
                    ready_response.status_code == 200
                    and ready_payload.get("ready") is True
                )
                setup_response = request_with_deadline(
                    f"{base}/api/admin/setup-status",
                    headers,
                )
                if setup_response is None:
                    break
                setup_payload = _safe_json(setup_response)
                result["setup_status"] = int(setup_response.status_code)
                prewarm_response = request_with_deadline(
                    f"{base}/api/admin/prewarm-status",
                    headers,
                )
                if prewarm_response is None:
                    break
                prewarm_payload = _safe_json(prewarm_response)
                result["prewarm_status"] = int(prewarm_response.status_code)
                result["ready"] = (
                    ready_ok
                    and setup_response.status_code == 200
                    and _setup_valid(setup_payload)
                    and prewarm_response.status_code == 200
                    and _prewarm_valid(
                        prewarm_payload,
                        expected_binaries=(
                            _CORE_BINARIES | _OMNIGENT_BINARIES
                            if (
                                isinstance(
                                    setup_payload.get("release_manifest"),
                                    dict,
                                )
                                and isinstance(
                                    setup_payload["release_manifest"].get(
                                        "omnigent"
                                    ),
                                    dict,
                                )
                                and setup_payload["release_manifest"][
                                    "omnigent"
                                ].get("enabled")
                                is True
                            )
                            else _CORE_BINARIES
                        ),
                    )
                )
                result["_release_manifest"] = _canonical_release_manifest(
                    setup_payload
                )
                result["_prewarm_manifest"] = prewarm_payload.get("manifest")
            except requests.RequestException:
                result["ready"] = False
        if expired:
            break
        if all(result["ready"] for result in results.values()):
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(max(0, float(poll_interval)), remaining))
    individually_ready = all(result["ready"] for result in results.values())
    release_manifests = [result["_release_manifest"] for result in results.values()]
    prewarm_manifests = [result["_prewarm_manifest"] for result in results.values()]
    manifests_match = bool(
        individually_ready
        and release_manifests[0] == release_manifests[1]
        and prewarm_manifests[0] == prewarm_manifests[1]
    )
    ready = individually_ready and manifests_match
    for result in results.values():
        result.pop("_release_manifest", None)
        result.pop("_prewarm_manifest", None)
        if not manifests_match:
            result["ready"] = False
    status = (
        "ready"
        if ready
        else ("manifest_mismatch" if individually_ready else "not_ready")
    )
    return {
        "schema_version": 1,
        "operation": "prewarm_verify",
        "status": status,
        "exit_code": 0 if ready else 1,
        "apps": [results[app["name"]] for app in normalized],
    }


def _load_apps(args) -> list[dict]:
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as handle:
            payload = json.load(handle)
        raw_apps = payload.get("apps", []) if isinstance(payload, dict) else []
    else:
        raw_apps = [
            {"name": f"app-{index + 1}", "url": url}
            for index, url in enumerate(args.app_url)
        ]
    apps = []
    for raw in raw_apps:
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("url"):
            raise ValueError("each app requires name and url")
        token_env = str(raw.get("token_env") or args.token_env)
        token = os.environ.get(token_env, "")
        if not token:
            raise ValueError(f"credential environment variable is unset: {token_env}")
        apps.append({"name": str(raw["name"]), "url": str(raw["url"]), "token": token})
    return apps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--app-url", action="append", default=[])
    source.add_argument("--manifest")
    parser.add_argument("--token-env", default="DATABRICKS_TOKEN")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument("--allow-local-http", action="store_true")
    args = parser.parse_args()
    try:
        report = verify_apps(
            _load_apps(args),
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            allow_local_http=args.allow_local_http,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        report = {
            "schema_version": 1,
            "operation": "prewarm_verify",
            "status": "invalid_input",
            "exit_code": 2,
            "error": str(error),
            "apps": [],
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
