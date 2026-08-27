#!/usr/bin/env python3
"""Signed, staged acceptance validation for exactly two workshop apps."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

try:
    from .ct_inventory import normalize_apps
    from .ct_verify import (
        _prewarm_valid,
        _setup_valid,
    )
except ImportError:  # pragma: no cover - direct script execution
    from ct_inventory import normalize_apps
    from ct_verify import _prewarm_valid, _setup_valid


SIGNING_KEY_ENV = "CT_VALIDATION_HMAC_KEY"
CT_ATTESTATION_PUBLIC_KEY_ENV = "CT_ATTESTATION_PUBLIC_KEY"
CT_ATTESTATION_PUBLIC_KEY_FILE_ENV = "CT_ATTESTATION_PUBLIC_KEY_FILE"
CT_ATTESTATION_KEY_ID_ENV = "CT_ATTESTATION_KEY_ID"
REQUIRED_READY_CHECKS = frozenset({
    "topology",
    "attendee_identity",
    "credentials",
    # Not the same question as ``credentials``: that one asks whether the app
    # credential is healthy now, this one asks whether both planes can be kept
    # alive until the event ends. An instance can pass the first and strand an
    # attendee at hour three.
    "credential_durability",
    "installers",
    "supply_chain",
    "session_state",
    "catalog",
    "entitlements",
    "obo",
    "release_pins",
})
REQUIRED_PHASES = (
    "late_grant",
    "baseline",
    "restart",
    "lunch_resume",
    "teardown",
)
_PHASE_PROVENANCE = {
    "late_grant": "ct_attestation",
    "baseline": "automated_collector",
    "restart": "automated_collector",
    "lunch_resume": "operator_attestation",
    "teardown": "ct_attestation",
}
_RETRYABLE_PHASES = {"baseline", "restart", "lunch_resume"}
_LUNCH_DURATION_TOLERANCE_SECONDS = 5
_CORE_BINARIES = frozenset({"node", "claude", "codex", "databricks"})
_OMNIGENT_BINARIES = frozenset({"omnigent", "tmux"})
_SECRET_KEY_PARTS = ("token", "secret", "authorization", "password", "bearer")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TOKEN_PREFIX = re.compile(
    r"(?i)^(?:bearer\s+|dapi|token[_-]|tok[_-]|gh[pousr]_|sk[-_])[A-Za-z0-9._~+/=-]{6,}$"
)
_JWT = re.compile(
    r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}$"
)
_RECEIPT_ACTIONS = {
    "deployment": "teardown_completed",
    "app": "deleted",
    "workspace": "deleted",
    "catalog": "deleted",
    "credential": "revoked",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _signature(signing_key: bytes, digest: str) -> str:
    if not signing_key:
        raise ValueError("signing key is required")
    return hmac.new(signing_key, digest.encode("ascii"), hashlib.sha256).hexdigest()


def _timestamp(now: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(now(), timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _parse_timestamp(value: object) -> datetime | None:
    if not _valid_timestamp(value):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else None


def _identifier(value: object) -> str:
    text = str(value or "")
    if not _SAFE_IDENTIFIER.fullmatch(text):
        raise ValueError("invalid identifier")
    return text


def _unsafe_scalar(value: str, known_secrets: set[str]) -> bool:
    stripped = value.strip()
    lowered = stripped.casefold()
    if _TOKEN_PREFIX.fullmatch(stripped) or _JWT.fullmatch(stripped):
        return True
    if lowered.startswith("bearer "):
        return True
    if stripped.startswith(("http://", "https://")):
        parsed = urlsplit(stripped)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return True
    return any(secret and secret in value for secret in known_secrets)


def _contains_secret(value: Any, *, known_secrets: set[str] | None = None) -> bool:
    secrets = {item for item in (known_secrets or set()) if len(item) >= 6}
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                return True
            if _contains_secret(item, known_secrets=secrets):
                return True
    elif isinstance(value, list):
        return any(_contains_secret(item, known_secrets=secrets) for item in value)
    elif isinstance(value, str):
        return _unsafe_scalar(value, secrets)
    return False


def _phase_apps(header: dict, evidence: dict) -> list[dict]:
    expected = [app["name"] for app in header["apps"]]
    apps = evidence.get("apps")
    if not isinstance(apps, list) or len(apps) != 2:
        return []
    by_name = {
        str(app.get("name")): app
        for app in apps
        if isinstance(app, dict) and app.get("name")
    }
    if sorted(by_name) != sorted(expected):
        return []
    return [by_name[name] for name in expected]


def _verify_ct_attestation(
    header: dict,
    phase: str,
    evidence: dict,
    ct_public_key: Ed25519PublicKey,
    ct_key_id: str,
) -> tuple[bool, str | None]:
    if phase not in {"late_grant", "teardown"}:
        return True, None
    attestation = evidence.get("ct_attestation")
    if not isinstance(attestation, dict):
        return False, None
    if (
        attestation.get("key_id") != ct_key_id
        or attestation.get("algorithm") != "Ed25519"
        or not _valid_timestamp(attestation.get("attested_at"))
    ):
        return False, None
    nonce = str(attestation.get("nonce") or "")
    if not _SAFE_IDENTIFIER.fullmatch(nonce):
        return False, None
    signature_text = str(attestation.get("signature") or "")
    payload = {
        key: value
        for key, value in evidence.items()
        if key != "ct_attestation"
    }
    message = {
        "schema_version": header.get("schema_version"),
        "report_id": header.get("report_id"),
        "run_id": header.get("run_id"),
        "inventory": header.get("apps"),
        "phase": phase,
        "attested_at": attestation.get("attested_at"),
        "nonce": nonce,
        "payload_hash": _digest(payload),
    }
    try:
        signature = base64.b64decode(signature_text, validate=True)
        ct_public_key.verify(signature, _canonical(message))
    except (ValueError, InvalidSignature):
        return False, None
    return True, nonce


def _completed_events(report: dict) -> list[dict]:
    return [
        event
        for event in report.get("checkpoints", [])
        if isinstance(event, dict) and event.get("event_type") == "phase_completion"
    ]


def _phase_completion_timestamp(report: dict, phase: str) -> str | None:
    return next(
        (
            str(event.get("recorded_at"))
            for event in _completed_events(report)
            if event.get("phase") == phase and _valid_timestamp(event.get("recorded_at"))
        ),
        None,
    )


def _next_phase(report: dict) -> str | None:
    completed = _completed_events(report)
    return REQUIRED_PHASES[len(completed)] if len(completed) < len(REQUIRED_PHASES) else None


def _workspace_hosts(apps: list[dict]) -> dict[str, str]:
    hosts: dict[str, str] = {}
    for app in apps:
        name = str(app.get("name") or "")
        raw_host = app.get("workspace_host")
        if not isinstance(raw_host, str) or not raw_host.strip():
            raise ValueError("workspace_host is required")
        normalized = normalize_apps(
            [{"name": f"{name}-workspace", "url": raw_host}],
            exact_count=1,
        )[0]["url"]
        if urlsplit(normalized).path:
            raise ValueError("workspace_host must be an origin without a path")
        hosts[name] = normalized
    if len(set(hosts.values())) != len(hosts):
        raise ValueError("workspace_host values must be distinct")
    return hosts


def _verify_chain(
    report: dict,
    signing_key: bytes,
    *,
    ct_public_key: Ed25519PublicKey | None = None,
    ct_key_id: str | None = None,
) -> tuple[bool, str]:
    header = report.get("header")
    checkpoints = report.get("checkpoints")
    if not isinstance(header, dict) or not isinstance(checkpoints, list):
        return False, "invalid_structure"
    header_body = {
        key: value
        for key, value in header.items()
        if key not in {"hash", "signature"}
    }
    header_hash = _digest(header_body)
    if (
        not hmac.compare_digest(str(header.get("hash") or ""), header_hash)
        or not hmac.compare_digest(
            str(header.get("signature") or ""),
            _signature(signing_key, header_hash),
        )
    ):
        return False, "header_signature"
    if header_body.get("schema_version") != 2:
        return False, "schema_version"
    if not isinstance(header_body.get("ct_attestation_key_id"), str):
        return False, "ct_key_id"
    if ct_key_id is not None and header_body.get("ct_attestation_key_id") != ct_key_id:
        return False, "ct_attestation"
    if not _valid_timestamp(header_body.get("created_at")):
        return False, "timestamp"
    try:
        _identifier(header_body.get("report_id"))
        _identifier(header_body.get("run_id"))
        names = [_identifier(app.get("name")) for app in header_body.get("apps", [])]
    except (AttributeError, ValueError):
        return False, "identifier"
    if len(names) != 2:
        return False, "app_count"

    previous = header_hash
    previous_time = datetime.fromisoformat(
        str(header_body["created_at"]).replace("Z", "+00:00")
    )
    completed_count = 0
    seen_ct_nonces: set[str] = set()
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, dict) or completed_count >= len(REQUIRED_PHASES):
            return False, "checkpoint_structure"
        phase = REQUIRED_PHASES[completed_count]
        if checkpoint.get("phase") != phase:
            return False, "phase_order"
        event_type = checkpoint.get("event_type")
        if event_type not in {"probe_attempt", "phase_completion"}:
            return False, "event_type"
        if checkpoint.get("provenance") != _PHASE_PROVENANCE[phase]:
            return False, "provenance"
        if event_type == "probe_attempt":
            if phase not in _RETRYABLE_PHASES:
                return False, "attempt_phase"
            if checkpoint.get("outcome") not in {"failed", "passed"}:
                return False, "attempt_outcome"
        elif checkpoint.get("outcome") != "passed":
            return False, "completion_outcome"
        if checkpoint.get("previous_hash") != previous:
            return False, "hash_chain"
        body = {
            key: value
            for key, value in checkpoint.items()
            if key not in {"hash", "signature"}
        }
        checkpoint_hash = _digest(body)
        if (
            not hmac.compare_digest(str(checkpoint.get("hash") or ""), checkpoint_hash)
            or not hmac.compare_digest(
                str(checkpoint.get("signature") or ""),
                _signature(signing_key, checkpoint_hash),
            )
        ):
            return False, "checkpoint_signature"
        recorded_at = checkpoint.get("recorded_at")
        if not _valid_timestamp(recorded_at):
            return False, "timestamp"
        current_time = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
        if previous_time is not None and current_time < previous_time:
            return False, "timestamp_order"
        previous_time = current_time
        previous = checkpoint_hash
        if event_type == "phase_completion":
            if phase in {"late_grant", "teardown"}:
                if ct_public_key is None or ct_key_id is None:
                    return False, "ct_attestation"
                verified, nonce = _verify_ct_attestation(
                    header_body,
                    phase,
                    checkpoint.get("evidence", {}),
                    ct_public_key,
                    ct_key_id,
                )
                if not verified:
                    return False, "ct_attestation"
                if nonce in seen_ct_nonces:
                    return False, "ct_nonce"
                seen_ct_nonces.add(str(nonce))
            completed_count += 1
    return True, "ok"


def new_report(
    apps: list[dict],
    *,
    report_id: str,
    run_id: str,
    signing_key: bytes,
    ct_key_id: str,
    now: Callable[[], float] = time.time,
) -> dict:
    if len(apps) != 2:
        raise ValueError("acceptance validation requires exactly two apps")
    safe_report_id = _identifier(report_id)
    safe_run_id = _identifier(run_id)
    safe_ct_key_id = _identifier(ct_key_id)
    normalized = normalize_apps(apps, exact_count=2)
    workspace_hosts = _workspace_hosts(apps)
    safe_apps = [
        {
            "name": _identifier(app["name"]),
            "url": app["url"],
            "workspace_host": workspace_hosts[app["name"]],
        }
        for app in normalized
    ]
    header_body = {
        "schema_version": 2,
        "operation": "two_instance_acceptance",
        "report_id": safe_report_id,
        "run_id": safe_run_id,
        "ct_attestation_key_id": safe_ct_key_id,
        "apps": safe_apps,
        "created_at": _timestamp(now),
    }
    header_hash = _digest(header_body)
    return {
        "header": {
            **header_body,
            "hash": header_hash,
            "signature": _signature(signing_key, header_hash),
        },
        "checkpoints": [],
    }


def apps_from_inventory(
    inventory: dict,
    *,
    environ: Mapping[str, str] = os.environ,
) -> list[dict]:
    raw_apps = inventory.get("apps")
    if not isinstance(raw_apps, list) or len(raw_apps) != 2:
        raise ValueError("acceptance validation requires exactly two apps")
    apps = []
    attendee_by_name: dict[str, str] = {}
    credential_env_names: set[str] = set()
    credential_values: set[str] = set()
    for raw in raw_apps:
        if not isinstance(raw, dict):
            raise ValueError("invalid inventory entry")
        admin_env = raw.get("token_env")
        attendee_env = raw.get("attendee_token_env")
        if not isinstance(admin_env, str) or not admin_env:
            raise ValueError("admin credential environment variable is required")
        if not isinstance(attendee_env, str) or not attendee_env:
            raise ValueError("attendee credential environment variable is required")
        if admin_env == attendee_env:
            raise ValueError("distinct environment variables are required")
        if (
            admin_env in credential_env_names
            or attendee_env in credential_env_names
        ):
            raise ValueError("credential environment variables must be globally distinct")
        admin_value = environ.get(admin_env, "")
        attendee_value = environ.get(attendee_env, "")
        if not admin_value or not attendee_value:
            raise ValueError("credential environment variable is unset")
        if hmac.compare_digest(admin_value, attendee_value):
            raise ValueError("distinct credential values are required")
        if admin_value in credential_values or attendee_value in credential_values:
            raise ValueError("credential values must be globally distinct")
        workspace_host = raw.get("workspace_host")
        if not isinstance(workspace_host, str) or not workspace_host.strip():
            raise ValueError("workspace_host is required")
        credential_env_names.update((admin_env, attendee_env))
        credential_values.update((admin_value, attendee_value))
        name = raw.get("name")
        apps.append({
            "name": name,
            "url": raw.get("url"),
            "workspace_host": workspace_host,
            "token": admin_value,
        })
        attendee_by_name[str(name)] = attendee_value
    normalized = normalize_apps(apps, exact_count=2)
    workspace_hosts = _workspace_hosts(apps)
    for app in normalized:
        app["attendee_token"] = attendee_by_name[app["name"]]
        app["workspace_host"] = workspace_hosts[app["name"]]
    return normalized


def _checkpoint_gates(
    header: dict,
    phase: str,
    evidence: dict,
    *,
    recorded_at: str | None = None,
    restart_completed_at: str | None = None,
) -> dict[str, bool]:
    apps = _phase_apps(header, evidence)
    if phase == "late_grant":
        return {
            "observed_recovery": bool(apps) and all(
                app.get("before_state") in {"unknown", "unhealthy", "degraded"}
                and app.get("after_state") == "rotating"
                and app.get("credential_source") == "app_identity_oauth"
                and app.get("workshop_pat_present") is False
                for app in apps
            )
        }
    if phase == "baseline":
        both_ready = bool(apps) and all(
            app.get("probe_status") in {None, "ok"}
            and app.get("ready") is True
            and app.get("setup_ready") is True
            and app.get("prewarm_reusable") is True
            and app.get("manifest_consistent", True) is True
            and not app.get("residual_session_ids")
            and isinstance(app.get("ready_checks"), dict)
            and all(
                app["ready_checks"].get(check) is True
                for check in REQUIRED_READY_CHECKS
            )
            for app in apps
        )
        manifests_equal = bool(apps) and all(
            apps[0].get(key)
            and apps[0].get(key) == apps[1].get(key)
            for key in ("readyz_manifest", "setup_manifest", "prewarm_manifest")
        ) and all(
            app.get("readyz_manifest") == app.get("setup_manifest")
            for app in apps
        )
        resources = bool(apps) and all(
            isinstance(app.get("resource_verification"), dict)
            and app["resource_verification"].get("catalog_owner") is True
            and app["resource_verification"].get("all_privileges") is True
            and app["resource_verification"].get("attendee_access") is True
            and app["resource_verification"].get("attendee_identity_bound") is True
            and isinstance(app["resource_verification"].get("resources"), list)
            and bool(app["resource_verification"]["resources"])
            and all(
                item.get("state") == "handed_off"
                and item.get("permission_level") in {"IS_OWNER", "CAN_MANAGE"}
                for item in app["resource_verification"]["resources"]
                if isinstance(item, dict)
            )
            for app in apps
        )
        return {
            "both_ready": both_ready,
            "manifests_equal": manifests_equal,
            "credentials_rotating_no_pat": bool(apps) and all(
                isinstance(app.get("credential"), dict)
                and app["credential"].get("state") == "rotating"
                and app["credential"].get("source") == "app_identity_oauth"
                and app["credential"].get("workshop_pat_present") is False
                for app in apps
            ),
            "agents_ready": bool(apps)
            and all(app.get("agents_ready") is True for app in apps),
            "session_lifecycle": bool(apps) and all(
                isinstance(app.get("session"), dict)
                and all(
                    app["session"].get(item) is True
                    for item in ("created", "listed", "closed")
                )
                for app in apps
            ),
            "restart_markers": bool(apps)
            and all(bool(app.get("restart_marker")) for app in apps),
            "resources_verified": resources,
        }
    if phase == "restart":
        return {
            "ready_after_restart": bool(apps)
            and all(app.get("ready") is True for app in apps),
            "restart_evidence": bool(apps)
            and all(app.get("restart_ghost") is True for app in apps),
        }
    if phase == "lunch_resume":
        attestation = evidence.get("attestation")
        attestation = attestation if isinstance(attestation, dict) else {}
        seconds = attestation.get("closed_laptop_pause_seconds")
        started = _parse_timestamp(attestation.get("pause_started_at"))
        completed = _parse_timestamp(attestation.get("pause_completed_at"))
        recorded = _parse_timestamp(recorded_at)
        restart_completed = _parse_timestamp(restart_completed_at)
        measured = (
            (completed - started).total_seconds()
            if started is not None and completed is not None
            else None
        )
        timing_ok = bool(
            isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
            and measured is not None
            and measured >= 5400
            and seconds >= 5400
            and abs(measured - float(seconds))
            <= _LUNCH_DURATION_TOLERANCE_SECONDS
            and recorded is not None
            and completed <= recorded
            and restart_completed is not None
            and started >= restart_completed
        )
        return {
            "external_pause_recorded": timing_ok,
            "wifi_reconnected": attestation.get("wifi_reconnected") is True,
            "both_recovered": bool(apps) and all(
                app.get("ready") is True and app.get("obo_fresh") is True
                for app in apps
            ),
        }
    if phase == "teardown":
        attestation = evidence.get("attestation")
        attestation_ok = (
            isinstance(attestation, dict)
            and bool(attestation.get("control_tower_run_id"))
            and _valid_timestamp(attestation.get("completed_at"))
        )
        receipts_ok = bool(apps) and all(_teardown_app_valid(app) for app in apps)
        return {
            "external_attestation": attestation_ok,
            "external_receipts_complete": receipts_ok,
            "cross_instance_unique": (
                receipts_ok and _teardown_cross_instance_unique(apps)
            ),
        }
    return {}


def _teardown_app_valid(app: dict) -> bool:
    identifiers = {
        "deployment": app.get("deployment_id"),
        "app": app.get("app_id"),
        "workspace": app.get("workspace_id"),
        "catalog": app.get("catalog"),
    }
    if not all(isinstance(value, str) and value for value in identifiers.values()):
        return False
    receipts = app.get("receipts")
    if not isinstance(receipts, list):
        return False
    for resource_type, action in _RECEIPT_ACTIONS.items():
        matches = [
            receipt
            for receipt in receipts
            if isinstance(receipt, dict)
            and isinstance(receipt.get("receipt_id"), str)
            and bool(receipt["receipt_id"])
            and receipt.get("resource_type") == resource_type
            and receipt.get("action") == action
            and receipt.get("status") == "succeeded"
            and receipt.get("provenance") == "control_tower"
            and _valid_timestamp(receipt.get("completed_at"))
        ]
        if not matches:
            return False
        if resource_type != "credential" and not any(
            receipt.get("resource_id") == identifiers[resource_type]
            for receipt in matches
        ):
            return False
        if resource_type == "credential" and not all(
            isinstance(receipt.get("resource_id"), str)
            and bool(receipt["resource_id"])
            for receipt in matches
        ):
            return False
    return True


def _teardown_cross_instance_unique(apps: list[dict]) -> bool:
    if len(apps) != 2:
        return False
    for field in ("workspace_id", "app_id", "catalog", "deployment_id"):
        values = [app.get(field) for app in apps]
        if (
            not all(isinstance(value, str) and value for value in values)
            or len(set(values)) != len(values)
        ):
            return False

    receipts = [
        receipt
        for app in apps
        for receipt in (
            app.get("receipts") if isinstance(app.get("receipts"), list) else []
        )
        if isinstance(receipt, dict)
    ]
    receipt_ids = [receipt.get("receipt_id") for receipt in receipts]
    if (
        not all(isinstance(value, str) and value for value in receipt_ids)
        or len(set(receipt_ids)) != len(receipt_ids)
    ):
        return False
    for resource_type in _RECEIPT_ACTIONS:
        resource_ids = [
            receipt.get("resource_id")
            for receipt in receipts
            if receipt.get("resource_type") == resource_type
        ]
        if (
            len(resource_ids) != 2
            or not all(isinstance(value, str) and value for value in resource_ids)
            or len(set(resource_ids)) != len(resource_ids)
        ):
            return False
    return True


def record_phase(
    report: dict,
    phase: str,
    evidence: dict,
    *,
    signing_key: bytes,
    provenance: str,
    ct_public_key: Ed25519PublicKey,
    ct_key_id: str,
    now: Callable[[], float] = time.time,
    known_secrets: set[str] | None = None,
) -> dict:
    _assert_phase_allowed(
        report,
        signing_key,
        phase,
        ct_public_key=ct_public_key,
        ct_key_id=ct_key_id,
    )
    checkpoints = report["checkpoints"]
    if provenance != _PHASE_PROVENANCE[phase]:
        raise ValueError("invalid phase provenance")
    if not isinstance(evidence, dict):
        raise ValueError("phase evidence must be a JSON object")
    secrets = set(known_secrets or set())
    try:
        secrets.add(signing_key.decode("utf-8"))
    except UnicodeDecodeError:
        pass
    if _contains_secret(evidence, known_secrets=secrets):
        raise ValueError("secret-bearing evidence is forbidden")
    if phase in {"late_grant", "teardown"}:
        verified, nonce = _verify_ct_attestation(
            report["header"],
            phase,
            evidence,
            ct_public_key,
            ct_key_id,
        )
        existing_nonces = {
            event.get("evidence", {}).get("ct_attestation", {}).get("nonce")
            for event in _completed_events(report)
        }
        if not verified:
            raise ValueError("CT attestation signature is invalid")
        if nonce in existing_nonces:
            raise ValueError("CT attestation nonce was already used")
    previous_hash = (
        checkpoints[-1]["hash"] if checkpoints else report["header"]["hash"]
    )
    recorded_at = _timestamp(now)
    previous_recorded_at = (
        checkpoints[-1]["recorded_at"]
        if checkpoints
        else report["header"]["created_at"]
    )
    if datetime.fromisoformat(recorded_at.replace("Z", "+00:00")) < datetime.fromisoformat(
        str(previous_recorded_at).replace("Z", "+00:00")
    ):
        raise ValueError("checkpoint timestamp order is invalid")
    body = {
        "phase": phase,
        "event_type": "phase_completion",
        "outcome": "passed",
        "recorded_at": recorded_at,
        "provenance": provenance,
        "previous_hash": previous_hash,
        "evidence": copy.deepcopy(evidence),
    }
    checkpoint_hash = _digest(body)
    updated = copy.deepcopy(report)
    updated["checkpoints"].append({
        **body,
        "hash": checkpoint_hash,
        "signature": _signature(signing_key, checkpoint_hash),
    })
    return updated


def record_probe_attempt(
    report: dict,
    phase: str,
    evidence: dict,
    *,
    signing_key: bytes,
    ct_public_key: Ed25519PublicKey,
    ct_key_id: str,
    now: Callable[[], float] = time.time,
    known_secrets: set[str] | None = None,
) -> dict:
    if phase not in _RETRYABLE_PHASES:
        raise ValueError("phase does not support probe attempts")
    _assert_phase_allowed(
        report,
        signing_key,
        phase,
        ct_public_key=ct_public_key,
        ct_key_id=ct_key_id,
    )
    if not isinstance(evidence, dict):
        raise ValueError("probe evidence must be a JSON object")
    secrets = set(known_secrets or set())
    for key in (signing_key,):
        try:
            secrets.add(key.decode("utf-8"))
        except UnicodeDecodeError:
            pass
    if _contains_secret(evidence, known_secrets=secrets):
        raise ValueError("secret-bearing evidence is forbidden")

    recorded_at = _timestamp(now)
    checkpoints = report["checkpoints"]
    previous_recorded_at = (
        checkpoints[-1]["recorded_at"]
        if checkpoints
        else report["header"]["created_at"]
    )
    if datetime.fromisoformat(recorded_at.replace("Z", "+00:00")) < datetime.fromisoformat(
        str(previous_recorded_at).replace("Z", "+00:00")
    ):
        raise ValueError("checkpoint timestamp order is invalid")
    gates = _checkpoint_gates(
        report["header"],
        phase,
        evidence,
        recorded_at=recorded_at,
        restart_completed_at=_phase_completion_timestamp(report, "restart"),
    )
    passed = bool(gates) and all(gates.values())
    updated = copy.deepcopy(report)
    attempt_body = {
        "phase": phase,
        "event_type": "probe_attempt",
        "outcome": "passed" if passed else "failed",
        "recorded_at": recorded_at,
        "provenance": _PHASE_PROVENANCE[phase],
        "previous_hash": (
            updated["checkpoints"][-1]["hash"]
            if updated["checkpoints"]
            else updated["header"]["hash"]
        ),
        "evidence": copy.deepcopy(evidence),
    }
    attempt_hash = _digest(attempt_body)
    updated["checkpoints"].append({
        **attempt_body,
        "hash": attempt_hash,
        "signature": _signature(signing_key, attempt_hash),
    })
    if passed:
        completion_body = {
            "phase": phase,
            "event_type": "phase_completion",
            "outcome": "passed",
            "recorded_at": recorded_at,
            "provenance": _PHASE_PROVENANCE[phase],
            "previous_hash": attempt_hash,
            "evidence": copy.deepcopy(evidence),
        }
        completion_hash = _digest(completion_body)
        updated["checkpoints"].append({
            **completion_body,
            "hash": completion_hash,
            "signature": _signature(signing_key, completion_hash),
        })
    return updated


def _assert_phase_allowed(
    report: dict,
    signing_key: bytes,
    phase: str,
    *,
    ct_public_key: Ed25519PublicKey,
    ct_key_id: str,
) -> None:
    valid, _ = _verify_chain(
        report,
        signing_key,
        ct_public_key=ct_public_key,
        ct_key_id=ct_key_id,
    )
    if not valid:
        raise ValueError("report signature chain is invalid")
    expected = _next_phase(report)
    if expected is None:
        raise ValueError("report is complete; start a new run")
    if phase != expected:
        raise ValueError(f"expected {expected} phase")
    for checkpoint in _completed_events(report):
        gates = _checkpoint_gates(
            report["header"],
            checkpoint["phase"],
            checkpoint["evidence"],
            recorded_at=checkpoint.get("recorded_at"),
            restart_completed_at=_phase_completion_timestamp(report, "restart"),
        )
        if not gates or not all(gates.values()):
            raise ValueError("failed report requires an explicit new run")


def evaluate_report(
    report: dict,
    *,
    signing_key: bytes,
    ct_public_key: Ed25519PublicKey,
    ct_key_id: str,
) -> dict:
    valid, reason = _verify_chain(
        report,
        signing_key,
        ct_public_key=ct_public_key,
        ct_key_id=ct_key_id,
    )
    if not valid:
        return {
            "schema_version": 2,
            "operation": "two_instance_acceptance",
            "status": "tampered",
            "exit_code": 1,
            "error": reason,
            "missing_phases": [],
            "gates": {},
        }
    header = report["header"]
    checkpoints = _completed_events(report)
    gates = {
        checkpoint["phase"]: _checkpoint_gates(
            header,
            checkpoint["phase"],
            checkpoint["evidence"],
            recorded_at=checkpoint.get("recorded_at"),
            restart_completed_at=_phase_completion_timestamp(report, "restart"),
        )
        for checkpoint in checkpoints
    }
    attempts: dict[str, list[str]] = {}
    for event in report["checkpoints"]:
        if event.get("event_type") == "probe_attempt":
            attempts.setdefault(str(event["phase"]), []).append(str(event["outcome"]))
    missing = list(REQUIRED_PHASES[len(checkpoints):])
    failed = any(not checks or not all(checks.values()) for checks in gates.values())
    passed = not missing and not failed
    return {
        "schema_version": 2,
        "operation": "two_instance_acceptance",
        "run_id": header["run_id"],
        "apps": copy.deepcopy(header["apps"]),
        "status": "passed" if passed else ("failed" if failed else "incomplete"),
        "exit_code": 0 if passed else 1,
        "missing_phases": missing,
        "gates": gates,
        "attempts": attempts,
    }


def _runtime_apps(apps: list[dict]) -> list[dict]:
    attendee = {
        str(app.get("name")): str(app.get("attendee_token") or "")
        for app in apps
        if isinstance(app, dict)
    }
    workspace_hosts = _workspace_hosts(apps)
    normalized = normalize_apps(apps, exact_count=2)
    for app in normalized:
        app["attendee_token"] = attendee.get(app["name"], "")
        app["workspace_host"] = workspace_hosts[app["name"]]
        if not app["token"] or not app["attendee_token"]:
            raise ValueError("both runtime credentials are required")
        if hmac.compare_digest(app["token"], app["attendee_token"]):
            raise ValueError("distinct credential values are required")
    return normalized


def _manifest_by_name(manifest: dict, apps: list[dict]) -> dict[str, dict]:
    raw = manifest.get("apps")
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("resource manifest requires exactly two apps")
    by_name = {
        str(item.get("name")): item
        for item in raw
        if isinstance(item, dict) and item.get("name")
    }
    if sorted(by_name) != sorted(app["name"] for app in apps):
        raise ValueError("resource manifest does not match inventory")
    for item in by_name.values():
        if not isinstance(item.get("catalog"), str) or not item["catalog"]:
            raise ValueError("resource manifest catalog is required")
        if not isinstance(item.get("catalog_owner"), str) or not item["catalog_owner"]:
            raise ValueError("resource manifest catalog_owner is required")
        if (
            not isinstance(item.get("attendee_principal"), str)
            or not item["attendee_principal"]
        ):
            raise ValueError("resource manifest attendee_principal is required")
        tables = item.get("tables")
        if (
            not isinstance(tables, list)
            or not tables
            or not all(isinstance(table, str) and table for table in tables)
        ):
            raise ValueError("resource manifest tables are required")
        if (
            not isinstance(item.get("sql_warehouse_id"), str)
            or not item["sql_warehouse_id"]
        ):
            raise ValueError("resource manifest sql_warehouse_id is required")
        resources = item.get("resources")
        if not isinstance(resources, list) or not resources:
            raise ValueError("resource manifest resources are required")
        for resource in resources:
            if (
                not isinstance(resource, dict)
                or not resource.get("type")
                or not resource.get("id")
                or resource.get("required_permission") not in {"IS_OWNER", "CAN_MANAGE"}
            ):
                raise ValueError("invalid expected resource")
    return by_name


def _request_json(
    request: Callable,
    method: str,
    url: str,
    *,
    headers: dict,
    timeout: float,
    body: dict | None = None,
) -> tuple[int | None, dict, str | None]:
    try:
        response = request(
            method,
            url,
            headers=headers,
            json=body,
            timeout=timeout,
        )
        try:
            payload = response.json()
        except (AttributeError, ValueError):
            payload = {}
        return (
            int(response.status_code),
            payload if isinstance(payload, dict) else {},
            None,
        )
    except requests.RequestException:
        return None, {}, "request_failed"
    except Exception:  # noqa: BLE001 - custom transports also fail closed
        return None, {}, "request_failed"


def _resource_verification(
    expected: dict,
    app: dict,
    *,
    request: Callable,
    timeout: float,
) -> dict:
    catalog = expected["catalog"]
    principal = expected["attendee_principal"]
    workspace = app["workspace_host"].rstrip("/")
    admin_headers = {"Authorization": f"Bearer {app['token']}"}
    attendee_headers = {"Authorization": f"Bearer {app['attendee_token']}"}

    def probe(
        method: str,
        path: str,
        *,
        attendee: bool = False,
        body: dict | None = None,
    ) -> tuple[bool, dict]:
        status, payload, error = _request_json(
            request,
            method,
            f"{workspace}{path}",
            headers=attendee_headers if attendee else admin_headers,
            timeout=timeout,
            body=body,
        )
        return error is None and status == 200, payload

    current_ok, current_user = probe(
        "GET", "/api/2.0/current-user/me", attendee=True
    )
    scim_ok, scim_user = probe(
        "GET", "/api/2.0/preview/scim/v2/Me", attendee=True
    )
    current_id = str(current_user.get("id") or "")
    scim_id = str(scim_user.get("id") or "")
    current_name = str(
        current_user.get("userName") or current_user.get("user_name") or ""
    ).strip().casefold()
    scim_name = str(
        scim_user.get("userName") or scim_user.get("user_name") or ""
    ).strip().casefold()
    application_ids = {
        str(payload.get(key) or "")
        for payload in (current_user, scim_user)
        for key in ("applicationId", "application_id")
        if payload.get(key)
    }
    attendee_identity_bound = bool(
        current_ok
        and scim_ok
        and current_id
        and current_id == scim_id
        and current_name
        and current_name == scim_name
        and current_name == principal.casefold()
        and principal.casefold() == expected["catalog_owner"].casefold()
        and not application_ids
    )
    if not attendee_identity_bound:
        return {
            "catalog": catalog,
            "catalog_owner": False,
            "all_privileges": False,
            "attendee_access": False,
            "attendee_identity_bound": False,
            "resources": [
                {
                    "type": str(resource["type"]),
                    "id": str(resource["id"]),
                    "state": "missing",
                    "permission_level": None,
                }
                for resource in expected["resources"]
            ],
        }

    metadata_ok, metadata = probe(
        "GET", f"/api/2.1/unity-catalog/catalogs/{quote(catalog, safe='')}"
    )
    grants_ok, grants_payload = probe(
        "GET",
        f"/api/2.1/unity-catalog/permissions/catalog/{quote(catalog, safe='')}",
    )
    catalog_owner = bool(
        metadata_ok and metadata.get("owner") == expected["catalog_owner"]
    )
    privileges = {
        str(privilege)
        for assignment in grants_payload.get("privilege_assignments", []) or []
        if isinstance(assignment, dict)
        and assignment.get("principal") == principal
        for privilege in assignment.get("privileges", []) or []
    }
    all_privileges = grants_ok and "ALL_PRIVILEGES" in privileges

    resources = []
    for resource in expected["resources"]:
        permission_ok, permission_payload = probe(
            "GET",
            "/api/2.0/permissions/"
            f"{quote(str(resource['type']), safe='')}/"
            f"{quote(str(resource['id']), safe='')}",
        )
        effective = {
            str(permission.get("permission_level") or "")
            for acl in permission_payload.get("access_control_list", []) or []
            if isinstance(acl, dict)
            and (
                acl.get("user_name")
                or acl.get("service_principal_name")
                or acl.get("group_name")
            ) == principal
            for permission in acl.get("all_permissions", []) or []
            if isinstance(permission, dict)
        }
        required = resource["required_permission"]
        matched = permission_ok and required in effective
        resources.append({
            "type": str(resource["type"]),
            "id": str(resource["id"]),
            "state": "handed_off" if matched else "missing",
            "permission_level": required if matched else None,
        })
    attendee_catalog_ok, attendee_catalog = probe(
        "GET",
        f"/api/2.1/unity-catalog/catalogs/{quote(catalog, safe='')}",
        attendee=True,
    )
    table_reads = []
    for full_name in expected["tables"]:
        table_ok, table_payload = probe(
            "GET",
            f"/api/2.1/unity-catalog/tables/{quote(full_name, safe='')}",
            attendee=True,
        )
        table_reads.append(table_ok and table_payload.get("full_name") == full_name)
    sql_reads = []
    for full_name in expected["tables"]:
        sql_ok, statement = probe(
            "POST",
            "/api/2.0/sql/statements",
            attendee=True,
            body={
                "warehouse_id": expected["sql_warehouse_id"],
                "statement": f"SELECT * FROM {full_name} LIMIT 1",
                "wait_timeout": "30s",
            },
        )
        status = statement.get("status")
        sql_reads.append(
            sql_ok
            and isinstance(status, dict)
            and status.get("state") == "SUCCEEDED"
        )
    attendee_access = bool(
        attendee_catalog_ok
        and attendee_catalog.get("name") == catalog
        and all(table_reads)
        and all(sql_reads)
    )
    return {
        "catalog": catalog,
        "catalog_owner": catalog_owner,
        "all_privileges": all_privileges,
        "attendee_access": attendee_access,
        "attendee_identity_bound": True,
        "resources": resources,
    }


def _comparable_release_manifest(payload: dict) -> dict:
    raw = payload.get("release_manifest")
    if not isinstance(raw, dict):
        return {}
    comparable = {}
    for name, raw_entry in sorted(raw.items()):
        if not isinstance(raw_entry, dict):
            continue
        entry = {
            key: raw_entry.get(key)
            for key in ("enabled", "expected", "actual", "match")
            if key in raw_entry
        }
        if name == "databricks_agent_skills":
            for key in ("source", "resolved_commit", "checksum"):
                if key in raw_entry:
                    entry[key] = raw_entry.get(key)
        comparable[str(name)] = entry
    return comparable


def _expected_binaries(setup_payload: dict) -> frozenset[str]:
    manifest = setup_payload.get("release_manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    omnigent = manifest.get("omnigent")
    enabled = isinstance(omnigent, dict) and omnigent.get("enabled") is True
    return _CORE_BINARIES | (_OMNIGENT_BINARIES if enabled else frozenset())


def _prewarm_release_consistent(setup_payload: dict, prewarm_payload: dict) -> bool:
    release = _comparable_release_manifest(setup_payload)
    manifest = prewarm_payload.get("manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    binaries = manifest.get("binaries")
    if not isinstance(binaries, dict):
        return False
    for name in ("claude", "codex", "databricks", "omnigent"):
        release_entry = release.get(name)
        if not isinstance(release_entry, dict) or release_entry.get("enabled") is False:
            continue
        expected = release_entry.get("expected")
        binary = binaries.get(name)
        if (
            expected
            and (
                not isinstance(binary, dict)
                or binary.get("expected") != expected
                or binary.get("actual") != expected
            )
        ):
            return False
    release_kit = release.get("databricks_agent_skills")
    prewarm_kit = manifest.get("databricks_agent_skills")
    if isinstance(release_kit, dict) and isinstance(prewarm_kit, dict):
        comparisons = (
            ("expected", "expected_ref"),
            ("resolved_commit", "resolved_commit"),
            ("checksum", "actual_checksum"),
        )
        for release_key, prewarm_key in comparisons:
            value = release_kit.get(release_key)
            if value and prewarm_kit.get(prewarm_key) != value:
                return False
    return True


def _session_present(payload: dict, session_id: str) -> bool:
    return any(
        isinstance(item, dict) and item.get("id") == session_id
        for key in ("sessions", "prior_sessions")
        for item in (
            payload.get(key) if isinstance(payload.get(key), list) else []
        )
    )


def _live_session_ids(payload: dict) -> set[str]:
    sessions = payload.get("sessions")
    return {
        str(item["id"])
        for item in (sessions if isinstance(sessions, list) else [])
        if isinstance(item, dict) and item.get("id")
    }


def _probe_agent_id(payload: dict) -> str | None:
    """Pick a ready agent the deployed app actually offers.

    Raw Bash sessions are intentionally rejected. Prefer the bare agents
    for acceptance work so an optional Omnigent credential plane is not used
    merely to prove the Workshop Terminal session lifecycle.
    """
    agents = payload.get("agents")
    agents = agents if isinstance(agents, list) else []
    ready = {
        str(agent.get("id")): agent
        for agent in agents
        if isinstance(agent, dict)
        and agent.get("ready") is True
        and not agent.get("blocked")
    }
    for agent_id in ("claude", "codex", "omnigent"):
        if agent_id in ready:
            return agent_id
    return None


def _cleanup_session(
    request: Callable,
    *,
    base: str,
    headers: dict,
    session_id: str,
    timeout: float,
    retries: int,
) -> bool:
    for _ in range(max(1, min(int(retries), 10))):
        delete_status, _, delete_error = _request_json(
            request,
            "DELETE",
            f"{base}/api/sessions/{session_id}",
            headers=headers,
            timeout=timeout,
        )
        status, payload, error = _request_json(
            request,
            "GET",
            f"{base}/api/sessions",
            headers=headers,
            timeout=timeout,
        )
        if (
            error is None
            and status == 200
            and not _session_present(payload, session_id)
        ):
            return True
        if (
            delete_error is None
            and delete_status is not None
            and not 200 <= delete_status < 300
        ):
            continue
    return False


def baseline_residual_sessions(report: dict) -> dict[str, list[str]]:
    header = report.get("header")
    header = header if isinstance(header, dict) else {}
    residuals = {
        str(app.get("name")): set()
        for app in header.get("apps", [])
        if isinstance(app, dict) and app.get("name")
    }
    for event in report.get("checkpoints", []):
        if (
            not isinstance(event, dict)
            or event.get("phase") != "baseline"
            or event.get("event_type") != "probe_attempt"
            or event.get("outcome") != "failed"
        ):
            continue
        evidence = event.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        for app in evidence.get("apps", []):
            if not isinstance(app, dict) or app.get("name") not in residuals:
                continue
            values = app.get("residual_session_ids")
            if isinstance(values, list):
                residuals[str(app["name"])].update(
                    str(value) for value in values if value
                )
    return {
        name: sorted(values)
        for name, values in sorted(residuals.items())
    }


def collect_baseline(
    apps: list[dict],
    *,
    resource_manifest: dict,
    request: Callable = requests.request,
    timeout: float = 30,
    residual_sessions: Mapping[str, list[str]] | None = None,
    cleanup_retries: int = 3,
) -> dict:
    normalized = _runtime_apps(apps)
    expected = _manifest_by_name(resource_manifest, normalized)
    results: list[dict] = []
    probe_agent_ids: dict[str, str] = {}
    unresolved: dict[str, list[str]] = {app["name"]: [] for app in normalized}
    for app in normalized:
        base = app["url"].rstrip("/")
        headers = {"Authorization": f"Bearer {app['attendee_token']}"}
        for session_id in (residual_sessions or {}).get(app["name"], []):
            if not _cleanup_session(
                request,
                base=base,
                headers=headers,
                session_id=str(session_id),
                timeout=timeout,
                retries=cleanup_retries,
            ):
                unresolved[app["name"]].append(str(session_id))
    if any(unresolved.values()):
        return {
            "apps": [
                {
                    "name": app["name"],
                    "probe_status": "cleanup_failed",
                    "ready": False,
                    "ready_checks": {},
                    "setup_ready": False,
                    "prewarm_reusable": False,
                    "manifest_consistent": False,
                    "readyz_manifest": {},
                    "setup_manifest": {},
                    "prewarm_manifest": {},
                    "credential": {},
                    "agents_ready": False,
                    "session": {
                        "created": False,
                        "listed": False,
                        "closed": False,
                    },
                    "restart_marker": None,
                    "resource_verification": {},
                    "residual_session_ids": sorted(unresolved[app["name"]]),
                }
                for app in normalized
            ]
        }

    for app in normalized:
        base = app["url"].rstrip("/")
        admin_headers = {"Authorization": f"Bearer {app['token']}"}
        attendee_headers = {"Authorization": f"Bearer {app['attendee_token']}"}
        failures: list[str] = []

        def call(
            method: str,
            path: str,
            *,
            admin: bool = False,
            body: dict | None = None,
        ) -> tuple[int | None, dict]:
            status, payload, error = _request_json(
                request,
                method,
                f"{base}{path}",
                headers=admin_headers if admin else attendee_headers,
                timeout=timeout,
                body=body,
            )
            if error:
                failures.append(error)
            elif status is None or not 200 <= status < 300:
                failures.append("http_error")
            return status, payload

        ready_status, ready = call("GET", "/readyz")
        setup_status, setup = call("GET", "/api/admin/setup-status", admin=True)
        prewarm_status, prewarm = call(
            "GET", "/api/admin/prewarm-status", admin=True
        )
        config_status, config = call("GET", "/api/config")
        agents_status, agents_payload = call("GET", "/api/agents")
        probe_agent_id = _probe_agent_id(agents_payload)
        if probe_agent_id:
            probe_agent_ids[app["name"]] = probe_agent_id
        reconcile_status, reconcile = call(
            "POST",
            "/api/entitlements/reconcile",
            body={},
        )

        lifecycle = {"created": False, "listed": False, "closed": False}
        session_id: str | None = None
        app_residuals: list[str] = []
        pre_list_status, pre_listed = call("GET", "/api/sessions")
        pre_session_ids = (
            _live_session_ids(pre_listed) if pre_list_status == 200 else set()
        )
        cleanup_candidates: set[str] = set()
        try:
            if pre_list_status != 200:
                raise RuntimeError("session snapshot unavailable")
            if not probe_agent_id:
                raise RuntimeError("no ready supported agent")
            create_status, created = call(
                "POST", "/api/sessions", body={"agent_id": probe_agent_id}
            )
            session = created.get("session")
            session_id = (
                str(session.get("id"))
                if isinstance(session, dict) and session.get("id")
                else None
            )
            lifecycle["created"] = bool(
                create_status is not None
                and 200 <= create_status < 300
                and session_id
            )
            list_status, listed = call("GET", "/api/sessions")
            live = listed.get("sessions")
            if list_status == 200:
                cleanup_candidates.update(
                    _live_session_ids(listed) - pre_session_ids
                )
            if session_id:
                cleanup_candidates.add(session_id)
            lifecycle["listed"] = bool(
                list_status is not None
                and 200 <= list_status < 300
                and session_id
                and isinstance(live, list)
                and any(
                    isinstance(item, dict) and item.get("id") == session_id
                    for item in live
                )
            )
        except RuntimeError:
            pass
        finally:
            cleanup_results = []
            for candidate_id in sorted(cleanup_candidates):
                cleaned = _cleanup_session(
                    request,
                    base=base,
                    headers=attendee_headers,
                    session_id=candidate_id,
                    timeout=timeout,
                    retries=cleanup_retries,
                )
                cleanup_results.append(cleaned)
                if not cleaned:
                    app_residuals.append(candidate_id)
            lifecycle["closed"] = bool(cleanup_candidates) and all(cleanup_results)

        checks = ready.get("checks")
        checks = checks if isinstance(checks, dict) else {}
        ready_checks = {
            name: (
                isinstance(checks.get(name), dict)
                and checks[name].get("ok") is True
            )
            for name in REQUIRED_READY_CHECKS
        }
        credential = config.get("credential")
        credential = credential if isinstance(credential, dict) else {}
        agents = agents_payload.get("agents")
        agents = agents if isinstance(agents, list) else []
        expected_binaries = _expected_binaries(setup)
        ready_manifest = _comparable_release_manifest(ready)
        setup_manifest = _comparable_release_manifest(setup)
        results.append({
            "name": app["name"],
            "probe_status": "request_failed" if "request_failed" in failures else (
                "http_error" if failures else "ok"
            ),
            "ready": ready_status == 200 and ready.get("ready") is True,
            "ready_checks": ready_checks,
            "setup_ready": setup_status == 200 and _setup_valid(setup),
            "prewarm_reusable": (
                prewarm_status == 200
                and _prewarm_valid(
                    prewarm,
                    expected_binaries=expected_binaries,
                )
            ),
            "expected_binaries": sorted(expected_binaries),
            "manifest_consistent": (
                ready_manifest == setup_manifest
                and _prewarm_release_consistent(setup, prewarm)
            ),
            "readyz_manifest": ready_manifest,
            "setup_manifest": setup_manifest,
            "prewarm_manifest": prewarm.get("manifest"),
            "credential": {
                "state": credential.get("state"),
                "source": credential.get("source"),
                "workshop_pat_present": not ready_checks["credentials"],
            },
            "agents_ready": (
                agents_status == 200
                and bool(agents)
                and all(
                    isinstance(agent, dict) and agent.get("ready") is True
                    for agent in agents
                )
            ),
            "session": lifecycle,
            "restart_marker": None,
            "residual_session_ids": app_residuals,
            "resource_verification": _resource_verification(
                expected[app["name"]],
                app,
                request=request,
                timeout=timeout,
            ),
        })

    provisional = {"apps": results}
    provisional_gates = _checkpoint_gates(
        {"apps": [{"name": app["name"]} for app in normalized]},
        "baseline",
        provisional,
    )
    marker_ids: dict[str, str] = {}
    if all(
        value
        for key, value in provisional_gates.items()
        if key != "restart_markers"
    ):
        marker_failed = False
        pre_marker_ids: dict[str, set[str]] = {}
        for app in normalized:
            status, payload, error = _request_json(
                request,
                "GET",
                f"{app['url'].rstrip('/')}/api/sessions",
                headers={"Authorization": f"Bearer {app['attendee_token']}"},
                timeout=timeout,
            )
            if error or status != 200:
                marker_failed = True
                result = next(
                    item for item in results if item["name"] == app["name"]
                )
                result["probe_status"] = error or "http_error"
                break
            pre_marker_ids[app["name"]] = _live_session_ids(payload)
        for app in (normalized if not marker_failed else []):
            status, payload, error = _request_json(
                request,
                "POST",
                f"{app['url'].rstrip('/')}/api/sessions",
                headers={"Authorization": f"Bearer {app['attendee_token']}"},
                timeout=timeout,
                body={"agent_id": probe_agent_ids.get(app["name"])},
            )
            session = payload.get("session")
            session_id = (
                str(session.get("id"))
                if isinstance(session, dict) and session.get("id")
                else None
            )
            if error or status is None or not 200 <= status < 300 or not session_id:
                marker_failed = True
                break
            marker_ids[app["name"]] = session_id
        if marker_failed:
            for app in normalized:
                status, payload, error = _request_json(
                    request,
                    "GET",
                    f"{app['url'].rstrip('/')}/api/sessions",
                    headers={"Authorization": f"Bearer {app['attendee_token']}"},
                    timeout=timeout,
                )
                candidates = set()
                if error is None and status == 200:
                    candidates.update(
                        _live_session_ids(payload)
                        - pre_marker_ids.get(app["name"], set())
                    )
                known_marker = marker_ids.get(app["name"])
                if known_marker:
                    candidates.add(known_marker)
                result = next(
                    item for item in results if item["name"] == app["name"]
                )
                if error or status != 200:
                    result["probe_status"] = "cleanup_failed"
                for marker_id in sorted(candidates):
                    cleaned = _cleanup_session(
                        request,
                        base=app["url"].rstrip("/"),
                        headers={"Authorization": f"Bearer {app['attendee_token']}"},
                        session_id=marker_id,
                        timeout=timeout,
                        retries=cleanup_retries,
                    )
                    if not cleaned:
                        result["residual_session_ids"].append(marker_id)
            marker_ids.clear()
    for result in results:
        result["restart_marker"] = marker_ids.get(result["name"])
    known = {
        value
        for app in normalized
        for value in (app["token"], app["attendee_token"])
    }
    if _contains_secret(provisional, known_secrets=known):
        raise ValueError("collector produced secret-bearing evidence")
    return provisional


def collect_recovery(
    report: dict,
    apps: list[dict],
    *,
    phase: str,
    attestation: dict | None = None,
    request: Callable = requests.request,
    timeout: float = 30,
) -> dict:
    if phase not in {"restart", "lunch_resume"}:
        raise ValueError("invalid recovery phase")
    normalized = _runtime_apps(apps)
    header = report.get("header", {})
    if [app["name"] for app in normalized] != [
        app.get("name") for app in header.get("apps", [])
    ]:
        raise ValueError("recovery inventory differs from report")
    baseline = next((
        checkpoint["evidence"]
        for checkpoint in report.get("checkpoints", [])
        if checkpoint.get("phase") == "baseline"
    ), {})
    markers = {
        app["name"]: app.get("restart_marker")
        for app in _phase_apps(header, baseline)
    }
    results = []
    for app in normalized:
        base = app["url"].rstrip("/")
        headers = {"Authorization": f"Bearer {app['attendee_token']}"}
        ready_status, ready, ready_error = _request_json(
            request, "GET", f"{base}/readyz", headers=headers, timeout=timeout
        )
        item = {
            "name": app["name"],
            "probe_status": ready_error or (
                "ok" if ready_status == 200 else "http_error"
            ),
            "ready": ready_status == 200 and ready.get("ready") is True,
        }
        if phase == "restart":
            status, sessions, error = _request_json(
                request,
                "GET",
                f"{base}/api/sessions",
                headers=headers,
                timeout=timeout,
            )
            prior = sessions.get("prior_sessions")
            prior = prior if isinstance(prior, list) else []
            marker = markers.get(app["name"])
            item["restart_ghost"] = bool(
                not error
                and status == 200
                and marker
                and any(
                    isinstance(entry, dict)
                    and entry.get("id") == marker
                    and entry.get("exit_reason") == "server_restarted"
                    for entry in prior
                )
            )
        else:
            status, config, error = _request_json(
                request,
                "GET",
                f"{base}/api/config",
                headers=headers,
                timeout=timeout,
            )
            obo = config.get("obo")
            item["obo_fresh"] = bool(
                not error
                and status == 200
                and isinstance(obo, dict)
                and obo.get("fresh") is True
            )
        results.append(item)
    evidence = {"apps": results}
    if phase == "lunch_resume":
        evidence["attestation"] = copy.deepcopy(attestation or {})
    known = {
        value
        for app in normalized
        for value in (app["token"], app["attendee_token"])
    }
    if _contains_secret(evidence, known_secrets=known):
        raise ValueError("collector produced secret-bearing evidence")
    return evidence


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _environment_secrets(environ: Mapping[str, str]) -> set[str]:
    return {
        value
        for name, value in environ.items()
        if value
        and len(value) >= 6
        and any(part in name.casefold() for part in _SECRET_KEY_PARTS + ("key",))
    }


def _signing_key(environ: Mapping[str, str]) -> bytes:
    value = environ.get(SIGNING_KEY_ENV, "")
    if not value:
        raise ValueError("signing key environment variable is unset")
    return value.encode("utf-8")


def load_ct_public_key(environ: Mapping[str, str]) -> Ed25519PublicKey:
    encoded = environ.get(CT_ATTESTATION_PUBLIC_KEY_ENV, "").strip()
    path = environ.get(CT_ATTESTATION_PUBLIC_KEY_FILE_ENV, "").strip()
    if bool(encoded) == bool(path):
        raise ValueError("provide exactly one CT attestation public key source")
    if path:
        encoded = Path(path).read_text(encoding="utf-8").strip()
    try:
        raw = base64.b64decode(encoded, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as error:
        raise ValueError("invalid CT attestation public key") from error


def _ct_attestation_key_id(environ: Mapping[str, str]) -> str:
    return _identifier(environ.get(CT_ATTESTATION_KEY_ID_ENV, ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--inventory", required=True, type=Path)
    init.add_argument("--run-id", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("phase", choices=("late_grant", "teardown"))
    record.add_argument("--evidence", required=True, type=Path)
    baseline = subparsers.add_parser("probe-baseline")
    baseline.add_argument("--inventory", required=True, type=Path)
    baseline.add_argument("--resource-manifest", required=True, type=Path)
    baseline.add_argument("--timeout", type=float, default=30)
    recovery = subparsers.add_parser("probe-recovery")
    recovery.add_argument("phase", choices=("restart", "lunch_resume"))
    recovery.add_argument("--inventory", required=True, type=Path)
    recovery.add_argument("--attestation", type=Path)
    recovery.add_argument("--timeout", type=float, default=30)
    subparsers.add_parser("evaluate")
    args = parser.parse_args()

    try:
        key = _signing_key(os.environ)
        ct_public_key = load_ct_public_key(os.environ)
        ct_key_id = _ct_attestation_key_id(os.environ)
        known = _environment_secrets(os.environ)
        if args.command == "init":
            inventory = _read_json(args.inventory)
            apps = apps_from_inventory(inventory)
            report = new_report(
                apps,
                report_id=f"report-{uuid.uuid4().hex}",
                run_id=args.run_id,
                signing_key=key,
                ct_key_id=ct_key_id,
            )
            _write_json(args.report, report)
        elif args.command == "record":
            report = record_phase(
                _read_json(args.report),
                args.phase,
                _read_json(args.evidence),
                signing_key=key,
                provenance=_PHASE_PROVENANCE[args.phase],
                ct_public_key=ct_public_key,
                ct_key_id=ct_key_id,
                known_secrets=known,
            )
            _write_json(args.report, report)
        elif args.command == "probe-baseline":
            report = _read_json(args.report)
            _assert_phase_allowed(
                report,
                key,
                "baseline",
                ct_public_key=ct_public_key,
                ct_key_id=ct_key_id,
            )
            apps = apps_from_inventory(_read_json(args.inventory))
            evidence = collect_baseline(
                apps,
                resource_manifest=_read_json(args.resource_manifest),
                timeout=args.timeout,
                residual_sessions=baseline_residual_sessions(report),
            )
            report = record_probe_attempt(
                report,
                "baseline",
                evidence,
                signing_key=key,
                ct_public_key=ct_public_key,
                ct_key_id=ct_key_id,
                known_secrets=known,
            )
            _write_json(args.report, report)
        elif args.command == "probe-recovery":
            report = _read_json(args.report)
            _assert_phase_allowed(
                report,
                key,
                args.phase,
                ct_public_key=ct_public_key,
                ct_key_id=ct_key_id,
            )
            apps = apps_from_inventory(_read_json(args.inventory))
            attestation = (
                _read_json(args.attestation) if args.attestation else None
            )
            evidence = collect_recovery(
                report,
                apps,
                phase=args.phase,
                attestation=attestation,
                timeout=args.timeout,
            )
            report = record_probe_attempt(
                report,
                args.phase,
                evidence,
                signing_key=key,
                ct_public_key=ct_public_key,
                ct_key_id=ct_key_id,
                known_secrets=known,
            )
            _write_json(args.report, report)
        else:
            report = _read_json(args.report)
        result = evaluate_report(
            report,
            signing_key=key,
            ct_public_key=ct_public_key,
            ct_key_id=ct_key_id,
        )
        exit_code = int(result["exit_code"])
        if args.command != "evaluate" and result["status"] == "incomplete":
            exit_code = 0
            if (
                report.get("checkpoints")
                and report["checkpoints"][-1].get("event_type") == "probe_attempt"
                and report["checkpoints"][-1].get("outcome") == "failed"
            ):
                exit_code = 1
    except Exception:  # noqa: BLE001 - CLI errors must be stable and secret-free
        result = {
            "schema_version": 2,
            "operation": "two_instance_acceptance",
            "status": "invalid_input",
            "exit_code": 2,
            "error": "operation_failed",
        }
        exit_code = 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
