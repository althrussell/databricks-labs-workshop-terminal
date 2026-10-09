"""Pure, secret-free Control Tower gates for generated-app evaluations.

This module performs no discovery, provisioning, authentication, or teardown.
The external runner supplies authenticated observations. An inventory's explicit
``disposable`` designation is an operator assertion, not a signed CT receipt.
Existing ``scripts/ct_two_instance.py`` remains the integration-release gate;
qualifying one generated app does not qualify Control Tower or a whole fleet.

The evaluation inventory extends the existing CT ``apps`` inventory with
``ct_run_id``, ``ct_unit_id``, ``attendee_email``, and ``disposable: true``.
Optional ``release`` and ``resources`` describe expected, not observed, state.
An optional ``omnigent`` is the normative versioned Control Tower handoff.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


REQUIRED_HARD_CHECKS = frozenset({
    "topology", "attendee_identity", "credentials", "credential_durability",
    "app_sp_binding", "secret_protection", "installers", "supply_chain",
    "session_state", "catalog", "entitlements", "obo", "release_pins",
})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+ -]{0,255}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,255}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_COMMIT = re.compile(r"^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$")
_CHECKSUM = re.compile(r"^[a-fA-F0-9]{64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_SECRET_KEYS = frozenset({
    "token", "attendee_token", "access_token", "refresh_token", "bearer",
    "authorization", "password", "client_secret", "databricks_client_secret",
    "databricks_token", "workshop_pat", "omnigent_host_token",
})
_HOST_DOMAIN = b"databricks-workshop-terminal/omnigent-host-id/v1\0"


def _object(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _text(value: object, *, pattern: re.Pattern = _IDENTIFIER) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value.strip()):
        raise ValueError("invalid evaluation inventory field")
    return value.strip()


def _secret_fields(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in _SECRET_KEYS or _secret_fields(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_secret_fields(item) for item in value)
    return False


def _url(value: object, *, origin_only: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("evaluation inventory requires HTTPS URLs")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.casefold() != "https" or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment
        or (origin_only and parsed.path not in {"", "/"})
    ):
        raise ValueError("evaluation inventory requires clean HTTPS URLs")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid evaluation inventory URL") from error
    host = parsed.hostname.casefold()
    if any(character.isspace() for character in host):
        raise ValueError("invalid evaluation inventory URL")
    host = f"[{host}]" if ":" in host else host
    netloc = host if port in {None, 443} else f"{host}:{port}"
    return urlunsplit(("https", netloc, parsed.path.rstrip("/"), "", ""))


def expected_omnigent_host_id(app_url: str, attendee_email: str) -> str:
    """Match the versioned attendee/URL host derivation without an auth token."""
    normalized_url = _url(app_url)
    normalized_email = _text(attendee_email, pattern=_EMAIL).casefold()
    return hashlib.sha256(
        _HOST_DOMAIN + normalized_url.encode() + b"\0" + normalized_email.encode()
    ).hexdigest()[:32]


def _release(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("invalid evaluation release declaration")
    raw = _object(value)
    result = {}
    if "source_ref" in raw:
        result["source_ref"] = _text(raw["source_ref"], pattern=_REF)
    if "source_ref_immutable" in raw:
        if not isinstance(raw["source_ref_immutable"], bool):
            raise ValueError("invalid evaluation release declaration")
        result["source_ref_immutable"] = raw["source_ref_immutable"]
    if result.get("source_ref_immutable") is True and not _COMMIT.fullmatch(
        result.get("source_ref", "")
    ):
        raise ValueError("immutable evaluation source must be a commit")
    if "source_repo" in raw:
        result["source_repo"] = _url(raw["source_repo"])
    if "source_subdir" in raw:
        result["source_subdir"] = _text(raw["source_subdir"], pattern=_REF)
    if "manifest_digest" in raw:
        result["manifest_digest"] = _text(raw["manifest_digest"], pattern=_CHECKSUM)
    if "source_digest" in raw:
        result["source_digest"] = _text(raw["source_digest"], pattern=_CHECKSUM)
    if "source_kind" in raw:
        if raw["source_kind"] not in {"git", "deployed-package"}:
            raise ValueError("invalid evaluation release source kind")
        result["source_kind"] = raw["source_kind"]
    return result


def _resources(value: object) -> dict:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError("invalid evaluation resource declaration")
    raw = _object(value)
    result = {}
    for key in ("catalog", "sql_warehouse_id", "allowed_resource_prefix"):
        if key in raw:
            result[key] = _text(raw[key])
    if "tables" in raw:
        if not isinstance(raw["tables"], list):
            raise ValueError("invalid evaluation resource declaration")
        result["tables"] = [_text(table) for table in raw["tables"]]
    if "resources" in raw:
        if not isinstance(raw["resources"], list):
            raise ValueError("invalid evaluation resource declaration")
        result["resources"] = []
        for item in raw["resources"]:
            resource = _object(item)
            permission = resource.get("required_permission")
            if permission not in {"IS_OWNER", "CAN_MANAGE"}:
                raise ValueError("invalid evaluation resource permission")
            result["resources"].append({
                "type": _text(resource.get("type")),
                "id": _text(resource.get("id")),
                "required_permission": permission,
            })
    return result


def _omnigent(value: object, attendee: str) -> dict:
    raw = _object(value)
    deployment = _object(raw.get("deployment"))
    resources = _object(raw.get("resources"))
    environment = _object(raw.get("environment"))
    host = _object(raw.get("remote_host"))
    authentication = _object(host.get("authentication"))
    commands = _object(host.get("verified_commands"))
    if (
        raw.get("contract_version") != "1.6"
        or not all(key in deployment for key in (
            "source_repo", "source_ref", "source_ref_immutable"
        ))
        or not isinstance(deployment.get("server_version"), str)
        or not _VERSION.fullmatch(deployment["server_version"])
        or deployment.get("source_subdir") != "deploy/omnigent-app"
        or resources.get("app_resource_keys") != {
            "lakebase": "postgres", "artifact_volume": "artifact_volume"
        }
        or host.get("enabled") is not True
        or authentication != {
            "kind": "obo_token_mirror", "owner_identity": "attendee",
            "refresh_trigger": "authenticated_browser_request",
            "static_secret_required": False,
        }
        or commands != {
            "host": "omnigent host --server <OMNIGENT_APP_URL> --non-interactive",
            "terminal": "omnigent polly --server <OMNIGENT_APP_URL>",
        }
        or environment.get("ALLOW_SHARED_TOPOLOGY") != "false"
    ):
        raise ValueError("invalid paired Omnigent Control Tower handoff")
    try:
        per_user = int(environment.get("MAX_SESSIONS_PER_USER", ""))
        global_cap = int(environment.get("MAX_SESSIONS_GLOBAL", ""))
    except (ValueError, TypeError) as error:
        raise ValueError("invalid paired Omnigent topology") from error
    paired_url = _url(deployment.get("app_url"))
    if (
        not 1 <= global_cap <= per_user
        or _url(environment.get("OMNIGENT_APP_URL")) != paired_url
        or _text(environment.get("WORKSHOP_ATTENDEE_EMAIL"), pattern=_EMAIL).casefold() != attendee
        or host.get("expected_host_id") != expected_omnigent_host_id(paired_url, attendee)
        or host.get("status") not in {
            "waiting_for_token", "starting", "running", "backoff", "error",
            "stopped", "connected",
        }
        or host.get("host_id_derivation") != (
            "sha256(databricks-workshop-terminal/omnigent-host-id/v1\\0"
            "<normalized-server-url>\\0<normalized-attendee-email>)[:32]"
        )
    ):
        raise ValueError("paired Omnigent attendee or URL does not match")
    return {
        "contract_version": "1.6",
        "deployment": {
            "app_name": _text(deployment.get("app_name")),
            "app_url": paired_url,
            "deployment_id": _text(deployment.get("deployment_id")),
            "server_version": deployment["server_version"], **_release(deployment),
        },
        "resources": {
            key: _text(resources.get(key))
            for key in ("lakebase_endpoint", "lakebase_database", "artifact_volume")
        } | {"app_resource_keys": dict(resources["app_resource_keys"])},
        "environment": {
            "OMNIGENT_APP_URL": paired_url, "WORKSHOP_ATTENDEE_EMAIL": attendee,
            "ALLOW_SHARED_TOPOLOGY": "false",
            "MAX_SESSIONS_PER_USER": str(per_user),
            "MAX_SESSIONS_GLOBAL": str(global_cap),
        },
        "remote_host": {
            "enabled": True, "status": host["status"],
            "expected_host_id": host["expected_host_id"],
            "host_id_derivation": host["host_id_derivation"],
            "authentication": dict(authentication), "verified_commands": dict(commands),
        },
    }


def select_target(manifest: Mapping, *, instance_name: str) -> dict:
    """Select an exact, explicitly disposable CT attendee unit; never guess."""
    if not isinstance(manifest, Mapping) or _secret_fields(manifest):
        raise ValueError("evaluation inventory must be secret-free")
    apps = manifest.get("apps")
    if not isinstance(apps, list) or not apps:
        raise ValueError("evaluation inventory requires apps")
    normalized = []
    seen_names, seen_urls = set(), set()
    for raw in apps:
        item = _object(raw)
        name = _text(item.get("name"))
        url = _url(item.get("url"))
        if name.casefold() in seen_names or url.casefold() in seen_urls:
            raise ValueError("evaluation inventory contains duplicate apps")
        seen_names.add(name.casefold())
        seen_urls.add(url.casefold())
        normalized.append((name, url, item))
    candidates = [entry for entry in normalized if entry[0] == instance_name]
    if len(candidates) != 1:
        raise ValueError("explicit evaluation instance was not found")
    name, url, raw = candidates[0]
    if raw.get("disposable") is not True:
        raise ValueError("evaluation instance must be explicitly disposable")
    attendee = _text(raw.get("attendee_email"), pattern=_EMAIL).casefold()
    target = {
        "name": name, "url": url,
        "workspace_host": _url(raw.get("workspace_host"), origin_only=True),
        "ct_run_id": _text(raw.get("ct_run_id")),
        "ct_unit_id": _text(raw.get("ct_unit_id")),
        "disposable": True, "attendee_email": attendee,
        "release": _release(raw.get("release")),
        "resources": _resources(raw.get("resources")),
    }
    for key in ("token_env", "attendee_token_env"):
        if key in raw:
            target[key] = _text(raw[key], pattern=_ENV_NAME)
    if target.get("token_env") and target.get("token_env") == target.get("attendee_token_env"):
        raise ValueError("operator and attendee credential references must differ")
    for key in ("workspace_id", "deployment_id", "service_principal_id"):
        if key in raw:
            target[key] = _text(raw[key])
    for key in ("expires_at", "schema"):
        if key in raw:
            target[key] = _text(raw[key])
    if "omnigent" in raw:
        target["omnigent"] = _omnigent(raw["omnigent"], attendee)
    policy = _object(raw.get("omnigent_compatibility"))
    if policy:
        target["omnigent_compatibility"] = {
            "policy_id": _text(policy.get("policy_id")),
            "contract_version": _text(policy.get("contract_version")),
            "server_version": _text(policy.get("server_version"), pattern=_VERSION),
        }
    return target


def _canonical_manifest(value: object) -> dict:
    """Allowlisted release identity; transport/setup timing is not drift."""
    result = {}
    for name, value in _object(value).items():
        if not isinstance(name, str) or not _REF.fullmatch(name):
            continue
        entry = _object(value)
        safe = {}
        for key in ("enabled", "match"):
            if isinstance(entry.get(key), bool) or entry.get(key) is None:
                safe[key] = entry.get(key)
        for key in ("expected", "actual", "resolved_commit", "checksum"):
            item = entry.get(key)
            if isinstance(item, str) and _IDENTIFIER.fullmatch(item):
                safe[key] = item
            elif item is None:
                safe[key] = None
        result[name] = safe
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def assess_readiness(payload: Mapping, *, attendee_present: bool = False) -> dict:
    """Honor dynamic CT admission flags; execution also requires fresh OBO."""
    checks = _object(_object(payload).get("checks"))
    admission_blockers, execution_blockers, warnings = [], [], []
    for name in sorted(REQUIRED_HARD_CHECKS | set(checks), key=str):
        if not isinstance(name, str) or not _REF.fullmatch(name):
            admission_blockers.append("readiness_check_invalid")
            execution_blockers.append("readiness_check_invalid")
            continue
        check = _object(checks.get(name))
        required = name in REQUIRED_HARD_CHECKS
        if not check or (required and check.get("soft") is True):
            code = f"{name}:unverified"
            admission_blockers.append(code)
            execution_blockers.append(code)
        elif check.get("ok") is not True:
            code = f"{name}:red"
            if check.get("soft") is True:
                warnings.append(code)
            else:
                execution_blockers.append(code)
                if check.get("attendee_dependent") is True and not attendee_present:
                    warnings.append(f"{name}:awaiting_attendee")
                else:
                    admission_blockers.append(code)

    binding = _object(checks.get("attendee_identity"))
    credentials = _object(checks.get("credentials"))
    special = []
    if binding.get("source") != "control-tower":
        special.append("attendee_identity:control_tower_binding_unverified")
    if credentials.get("credential_state") != "rotating" or credentials.get("source") != "app_identity_oauth":
        special.append("credentials:direct_oauth_unverified")
    app_sp = _object(checks.get("app_sp_binding"))
    sp_id = str(app_sp.get("expected_service_principal_id") or "")
    application_id = app_sp.get("expected_application_id")
    if (
        not sp_id.isdecimal() or sp_id != str(app_sp.get("observed_service_principal_id") or "")
        or not application_id or application_id != app_sp.get("observed_application_id")
        or app_sp.get("validation_result") != "matched"
    ):
        special.append("app_sp_binding:identity_unverified")
    admission_blockers.extend(special)
    execution_blockers.extend(special)
    if not execution_blockers and _object(payload).get("ready") is not True:
        execution_blockers.append("readyz:verdict_inconsistent")
    manifest = _canonical_manifest(_object(payload).get("release_manifest"))
    return {
        "admitted": not admission_blockers,
        "execution_ready": not execution_blockers,
        "blockers": sorted(set(admission_blockers)),
        "execution_blockers": sorted(set(execution_blockers)),
        "warnings": sorted(set(warnings)),
        "release_manifest_digest": _digest(manifest) if manifest else None,
    }


def _identity_matches(target: Mapping, evidence: Mapping) -> bool:
    current = _object(evidence.get("attendee_current_user"))
    scim = _object(evidence.get("attendee_scim"))
    names = [str(item.get("userName") or item.get("user_name") or "").strip().casefold() for item in (current, scim)]
    ids = [str(item.get("id") or "") for item in (current, scim)]
    return bool(
        names[0] == names[1] == target["attendee_email"]
        and ids[0] and ids[0] == ids[1]
        and not any(item.get(key) for item in (current, scim) for key in ("applicationId", "application_id"))
    )


def _observed_pair(target: Mapping, evidence: Mapping, *, now: datetime) -> tuple[bool, list[str]]:
    pair = _object(target.get("omnigent"))
    config = _object(evidence.get("config"))
    remote = _object(config.get("omnigent_remote"))
    if not pair:
        return remote.get("enabled") is not True, ["omnigent:unrecorded_pair"] if remote.get("enabled") is True else []
    expected = pair["deployment"]
    observed = _object(evidence.get("omnigent"))
    health = _object(observed.get("health"))
    version = _object(observed.get("version"))
    resources = _object(observed.get("resources"))
    host = _object(observed.get("host_readiness"))
    problems = []
    if remote.get("enabled") is not True or remote.get("url") != expected["app_url"]:
        problems.append("omnigent:terminal_pair_mismatch")
    if health.get("status") != "ok" or version.get("version") != expected["server_version"]:
        problems.append("omnigent:app_health_unverified")
    if resources.get("lakebase_ready") is not True or resources.get("artifact_volume_ready") is not True:
        problems.append("omnigent:resources_unverified")
    host_id = pair["remote_host"]["expected_host_id"]
    try:
        seen_at = datetime.fromisoformat(str(host.get("last_seen_at") or "").replace("Z", "+00:00"))
        timestamp_valid = seen_at.tzinfo is not None and -30 <= (now - seen_at).total_seconds() <= 300
    except (ValueError, TypeError):
        timestamp_valid = False
    if (
        host.get("connected") is not True or host.get("status") not in {"running", "connected"}
        or host.get("host_id") != host_id or host.get("expected_host_id") != host_id
        or not timestamp_valid
    ):
        problems.append("omnigent:attendee_host_unverified")
    return not problems, problems


def assess_target(
    target: Mapping, evidence: Mapping, *, attendee_present: bool = False,
    now: datetime | None = None,
) -> dict:
    """Combine observations, without treating declarations as grant proof.

Evidence keys are ``readyz``, ``setup``, ``config``, ``attendee_current_user``,
``attendee_scim``, ``resources``, ``terminal_deployment``, and optional
``omnigent``. Resources use the existing CT independent resource-probe result:
catalog/owner/all_privileges/attendee_access/attendee_identity_bound plus
resource entries with type/id/state/permission_level. Credential values and
unrestricted endpoint details are never included in the result.
"""
    evidence = _object(evidence)
    ready = assess_readiness(_object(evidence.get("readyz")), attendee_present=attendee_present)
    blockers = list(ready["execution_blockers"])
    warnings = list(ready["warnings"])
    config = _object(evidence.get("config"))
    config_email = _object(config.get("user")).get("email")
    identity_verified = (
        _identity_matches(target, evidence) and isinstance(config_email, str)
        and config_email.strip().casefold() == target["attendee_email"]
    )
    if not identity_verified:
        blockers.append("attendee_identity:independent_proof_unverified")
    if config.get("workspace_url") != target["workspace_host"]:
        blockers.append("workspace:terminal_host_mismatch")
    setup = _object(evidence.get("setup"))
    steps = _object(setup.get("steps"))
    ready_manifest = _canonical_manifest(_object(evidence.get("readyz")).get("release_manifest"))
    setup_manifest = _canonical_manifest(setup.get("release_manifest"))
    setup_verified = bool(
        steps and all(_object(step).get("status") == "complete" for step in steps.values())
        and ready_manifest and ready_manifest == setup_manifest
        and all(entry.get("enabled") is False or entry.get("match") is True for entry in ready_manifest.values())
    )
    if not setup_verified:
        blockers.append("setup:release_proof_unverified")

    expected_resources = _object(target.get("resources"))
    resources = _object(evidence.get("resources"))
    resources_verified = bool(
        expected_resources.get("catalog")
        and resources.get("catalog") == expected_resources["catalog"]
        and all(resources.get(key) is True for key in (
            "catalog_owner", "all_privileges", "attendee_access", "attendee_identity_bound"
        ))
    )
    actual_resources = resources.get("resources")
    actual_resources = actual_resources if isinstance(actual_resources, list) else []
    for resource in expected_resources.get("resources", []):
        resources_verified = resources_verified and any(
            _object(actual).get("type") == resource["type"]
            and _object(actual).get("id") == resource["id"]
            and _object(actual).get("state") == "handed_off"
            and _object(actual).get("permission_level") == resource["required_permission"]
            for actual in actual_resources
        )
    if not resources_verified:
        blockers.append("resource_grants:unverified")

    release = _object(target.get("release"))
    deployed = _object(evidence.get("terminal_deployment"))
    git_source_verified = bool(
        release.get("source_ref_immutable") is True
        and deployed.get("source_ref_immutable") is True
        and release.get("source_ref")
        and release["source_ref"] == deployed.get("source_ref")
    )
    package_source_verified = bool(
        release.get("source_kind") == deployed.get("source_kind") == "deployed-package"
        and release.get("source_digest")
        and release["source_digest"] == deployed.get("source_digest")
    )
    source_verified = git_source_verified or package_source_verified
    if release.get("manifest_digest") and release["manifest_digest"] != ready["release_manifest_digest"]:
        source_verified = False
        warnings.append("release:expected_manifest_mismatch")
    if not source_verified:
        warnings.append("release:baseline_revision_unknown")
    pair_verified, pair_problems = _observed_pair(
        target, evidence, now=now or datetime.now(timezone.utc)
    )
    blockers.extend(pair_problems)
    qualification_blockers = list(blockers)
    if not source_verified:
        qualification_blockers.append("release:expected_run_release_unverified")
    paired_deployment = _object(_object(target.get("omnigent")).get("deployment"))
    observed_paired_deployment = _object(_object(evidence.get("omnigent")).get("deployment"))
    paired_source_verified = not paired_deployment or bool(
        paired_deployment.get("source_ref_immutable") is True
        and observed_paired_deployment.get("source_ref_immutable") is True
        and paired_deployment.get("source_ref")
        and paired_deployment["source_ref"] == observed_paired_deployment.get("source_ref")
    )
    if not paired_source_verified:
        qualification_blockers.append("omnigent:expected_run_release_unverified")
        warnings.append("omnigent:baseline_revision_unknown")
    paired_compatibility_verified = not paired_deployment
    if paired_deployment:
        policy = _object(target.get("omnigent_compatibility"))
        paired_compatibility_verified = bool(
            policy.get("policy_id")
            and policy.get("contract_version") == _object(target.get("omnigent")).get("contract_version")
            and policy.get("server_version") == paired_deployment.get("server_version")
        )
        if not policy and paired_deployment.get("server_version") == "0.10.0":
            # The repository's normative 1.6 contract records this reviewed pair.
            paired_compatibility_verified = True
        if not paired_compatibility_verified:
            qualification_blockers.append("omnigent:release_compatibility_unverified")
            warnings.append("omnigent:release_compatibility_unverified")
    return {
        "admitted": ready["admitted"], "execution_ready": not blockers,
        "qualification_ready": not qualification_blockers,
        "admission_blockers": ready["blockers"],
        "blockers": sorted(set(blockers)),
        "qualification_blockers": sorted(set(qualification_blockers)),
        "warnings": sorted(set(warnings)),
        "proofs": {
            "attendee_identity": "verified" if identity_verified else "unverified",
            "setup": "verified" if setup_verified else "unverified",
            "resource_grants": "verified" if resources_verified else "unverified",
            "source_release": "verified" if source_verified else "unverified",
            "paired_omnigent": "verified" if pair_verified else "unverified",
            "paired_source_release": "verified" if paired_source_verified else "unverified",
            "paired_compatibility": "verified" if paired_compatibility_verified else "unverified",
        },
        "provenance": {
            "instance": target["name"], "ct_run_id": target["ct_run_id"],
            "ct_unit_id": target["ct_unit_id"],
            "release_manifest_digest": ready["release_manifest_digest"],
            "scope": "single_generated_app_evaluation",
            "control_tower_integration_qualified": False,
            "fleet_qualified": False,
        },
    }
