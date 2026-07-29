"""Deterministic deep-readiness checks for event admission.

The checks consume explicit status snapshots and environment mappings so unit
tests never need a live Databricks workspace. ``evaluate_runtime`` is the thin
adapter used by the HTTP endpoint.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
import re
import tempfile
import time


EXPECTED_OBO_SCOPES = frozenset(
    {
        "catalog.catalogs:read",
        "catalog.schemas:read",
        "catalog.tables:read",
        "sql",
    }
)
_TRUE = frozenset({"1", "true", "yes", "on", "enabled", "enable"})
_BRANCH_TIPS = frozenset({"", "main", "master", "head", "latest"})
CREDENTIAL_SUCCESS_MAX_AGE = 600
ENTITLEMENT_PROOF_MAX_AGE = 600
OBO_VALIDATION_MAX_AGE = 300


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, "") or default)
    except ValueError:
        return default


def _check(ok: bool, detail: str, **extra: object) -> dict:
    return {"ok": ok, "state": "green" if ok else "red", "detail": detail, **extra}


def _path_writable(path: str) -> bool:
    """Exercise journal-style atomic write/read on a disposable sibling."""
    if not path:
        return False
    if os.path.isdir(path):
        return False
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        return False
    probe_path = ""
    try:
        fd, probe_path = tempfile.mkstemp(dir=parent, prefix=".readyz-journal-")
        os.close(fd)
        os.unlink(probe_path)
        from .session_store import SessionMetadataStore

        store = SessionMetadataStore(probe_path)
        sentinel = {"id": "readyz-probe", "exited": True}
        store.upsert(sentinel)
        return store.load().get("readyz-probe") == sentinel
    except OSError:
        return False
    finally:
        if probe_path and os.path.exists(probe_path):
            os.unlink(probe_path)


def evaluate(
    *,
    env: Mapping[str, str],
    credential_status: Mapping[str, object],
    installer_status: Mapping[str, object],
    entitlement_status: Mapping[str, object],
    obo_status: Mapping[str, object],
    secret_protection_status: Mapping[str, object],
    attendee_binding: Mapping[str, str] | None = None,
    writable_probe: Callable[[str], bool] = _path_writable,
    now: float | None = None,
) -> dict:
    """Return the complete, secret-free readiness report."""
    current_time = time.time() if now is None else now
    per_user = _int(env, "MAX_SESSIONS_PER_USER", 3)
    global_cap = _int(env, "MAX_SESSIONS_GLOBAL", 30)
    topology_ok = (
        not _bool(env, "ALLOW_SHARED_TOPOLOGY")
        and per_user > 0
        and global_cap > 0
        and global_cap <= per_user
    )
    # The effective binding, which may be self-bound rather than injected by
    # Control Tower; the raw env var is only the hint.
    binding = attendee_binding or {}
    attendee_email = (
        str(binding.get("email") or env.get("WORKSHOP_ATTENDEE_EMAIL", ""))
        .strip()
        .lower()
    )
    attendee_identity_ok = (
        bool(attendee_email)
        and "@" in attendee_email
        and not any(char.isspace() for char in attendee_email)
    )
    attendee_binding_source = str(
        binding.get("source") or ("control-tower" if attendee_email else "unbound")
    )

    has_pat = bool(env.get("WORKSHOP_PAT", "").strip())
    last_credential_success = credential_status.get("last_successful_at")
    credential_recent = isinstance(last_credential_success, (int, float)) and (
        0 <= current_time - float(last_credential_success) <= CREDENTIAL_SUCCESS_MAX_AGE
    )
    credential_ok = (
        not has_pat
        and credential_status.get("state") == "rotating"
        and credential_status.get("rotating") is True
        and credential_status.get("healthy") is True
        and credential_status.get("source") == "app_identity_oauth"
        and credential_recent
    )
    expected_app_sp_id = env.get("WORKSHOP_APP_SP_ID", "").strip()
    expected_app_client_id = env.get("DATABRICKS_CLIENT_ID", "").strip()
    validation_diagnostic = credential_status.get("validation_diagnostic")
    validation_diagnostic = (
        validation_diagnostic if isinstance(validation_diagnostic, Mapping) else {}
    )
    observed_app_sp_id = str(
        validation_diagnostic.get("observed_service_principal_id") or ""
    )
    observed_app_client_id = str(
        validation_diagnostic.get("observed_application_id") or ""
    )
    app_sp_binding_ok = (
        bool(expected_app_client_id)
        and bool(re.fullmatch(r"[0-9]+", expected_app_sp_id))
        and validation_diagnostic.get("result") == "matched"
        and validation_diagnostic.get("expected_application_id")
        == expected_app_client_id
        and observed_app_client_id == expected_app_client_id
        and validation_diagnostic.get("expected_service_principal_id")
        == expected_app_sp_id
        and observed_app_sp_id == expected_app_sp_id
    )
    secret_protection_ok = (
        secret_protection_status.get("initialized") is True
        and secret_protection_status.get("env_scrubbed") is True
        and secret_protection_status.get("non_dumpable") is True
        and secret_protection_status.get("ok") is True
    )

    steps = installer_status.get("steps")
    steps = steps if isinstance(steps, Mapping) else {}
    required_steps = ["node", "claude", "codex", "databricks", "skills"]
    if _bool(env, "OMNIGENT_ENABLED", True):
        required_steps.extend(["tmux", "omnigent"])
    missing_installers = [
        name
        for name in required_steps
        if not isinstance(steps.get(name), Mapping)
        or steps[name].get("status") != "complete"
    ]
    # A degraded step produced something usable without meeting its reviewed
    # contract (skills served from the vendored fallback). It is not ready, and
    # naming it separately keeps an operator from reading it as "still going".
    degraded_installers = [
        name
        for name in required_steps
        if isinstance(steps.get(name), Mapping)
        and steps[name].get("status") == "degraded"
    ]
    artifact_manifest = installer_status.get("artifact_manifest")
    artifact_manifest = (
        artifact_manifest if isinstance(artifact_manifest, Mapping) else {}
    )
    artifact_proof = installer_status.get("artifact_proof")
    artifact_proof = artifact_proof if isinstance(artifact_proof, Mapping) else {}
    supply_chain_ok = (
        artifact_manifest.get("ok") is True
        and artifact_proof.get("reusable") is True
    )

    state_path = env.get("SESSION_STATE_PATH", "").strip()
    state_writable = bool(state_path and writable_probe(state_path))
    catalog = env.get("WORKSHOP_CATALOG", "").strip()

    entitlement_enabled = _bool(env, "ENABLE_ENTITLEMENTS")
    last_verified_at = entitlement_status.get("last_verified_at")
    entitlement_recent = isinstance(last_verified_at, (int, float)) and (
        0 <= current_time - float(last_verified_at) <= ENTITLEMENT_PROOF_MAX_AGE
    )
    attendee_verified = bool(entitlement_status.get("verified_email"))
    catalog_verified = entitlement_status.get("verified_catalog") == catalog
    reconciler_available = (
        entitlement_status.get("thread_alive") is True
        or entitlement_status.get("verification_source") == "on_demand"
    )
    entitlement_ok = (
        entitlement_enabled
        and entitlement_status.get("enabled") is True
        and entitlement_status.get("ok") is True
        and bool(catalog)
        and attendee_verified
        and catalog_verified
        and entitlement_recent
        and reconciler_available
    )
    catalog_ok = bool(catalog) and attendee_verified and catalog_verified and entitlement_recent

    configured_scopes = {
        scope.strip()
        for scope in env.get("OBO_SCOPES", "").split(",")
        if scope.strip()
    }
    observed_scopes = {
        str(scope)
        for scope in obo_status.get("verified_scopes", []) or []
        if str(scope)
    }
    missing_configured_scopes = EXPECTED_OBO_SCOPES - configured_scopes
    missing_observed_scopes = EXPECTED_OBO_SCOPES - observed_scopes
    missing_scopes = sorted(missing_configured_scopes | missing_observed_scopes)
    obo_validation_state = str(obo_status.get("validation_state") or "pending")
    obo_validated_at = obo_status.get("validated_at")
    obo_recent = isinstance(obo_validated_at, (int, float)) and (
        0 <= current_time - float(obo_validated_at) <= OBO_VALIDATION_MAX_AGE
    )
    obo_ok = (
        _bool(env, "ENABLE_OBO")
        and obo_status.get("enabled") is True
        and obo_status.get("present") is True
        and obo_status.get("fresh") is True
        and obo_validation_state == "verified"
        and not missing_scopes
        and obo_recent
    )

    pin_names = (
        "CLAUDE_CODE_VERSION",
        "CODEX_CLI_VERSION",
        "DATABRICKS_CLI_VERSION",
        "ANTHROPIC_MODEL",
        "CODEX_MODEL",
    )
    missing_pins = [name for name in pin_names if not env.get(name, "").strip()]
    omnigent_pinned = bool(env.get("OMNIGENT_VERSION", "").strip())
    if not omnigent_pinned:
        missing_pins.append("OMNIGENT_VERSION")
    # Unset is the expected state: the reviewed tag is repo-owned. Only an
    # explicitly configured branch tip is a missing pin.
    skills_ref = env.get("SKILLS_REF", "").strip()
    if skills_ref and (
        skills_ref.lower() in _BRANCH_TIPS or skills_ref.startswith("refs/heads/")
    ):
        missing_pins.append("SKILLS_REF")
    raw_manifest = installer_status.get("release_manifest")
    raw_manifest = raw_manifest if isinstance(raw_manifest, Mapping) else {}
    release_manifest: dict[str, dict[str, object]] = {}
    mismatched_tools: list[str] = []
    env_names = {
        "claude": "CLAUDE_CODE_VERSION",
        "codex": "CODEX_CLI_VERSION",
        "databricks": "DATABRICKS_CLI_VERSION",
        "omnigent": "OMNIGENT_VERSION",
        "databricks_agent_skills": "SKILLS_REF",
    }
    for tool, env_name in env_names.items():
        entry = raw_manifest.get(tool)
        entry = entry if isinstance(entry, Mapping) else {}
        enabled = bool(entry.get("enabled", tool != "omnigent" or _bool(
            env, "OMNIGENT_ENABLED", True
        )))
        expected = str(entry.get("expected") or "")
        actual = str(entry.get("actual") or "")
        configured_expected = env.get(env_name, "").strip()
        if tool == "databricks_agent_skills" and not configured_expected:
            # The skills ref is repo-owned in assets/artifacts/manifest.json, so
            # an unset SKILLS_REF is the normal case, not a missing pin. Setting
            # it still has to agree with what bootstrap installed.
            configured_expected = expected
        match = (
            enabled
            and bool(expected)
            and expected == configured_expected
            and actual == expected
            and entry.get("match") is True
        )
        if tool == "databricks_agent_skills":
            match = (
                match
                and entry.get("source") in {"network", "prewarmed"}
                and bool(entry.get("resolved_commit"))
                and bool(entry.get("checksum"))
            )
        release_manifest[tool] = {
            "enabled": enabled,
            "expected": expected or None,
            "actual": actual or None,
            "match": match if enabled else None,
        }
        if tool == "databricks_agent_skills":
            release_manifest[tool]["source"] = entry.get("source")
            release_manifest[tool]["resolved_commit"] = entry.get(
                "resolved_commit"
            )
            release_manifest[tool]["checksum"] = entry.get("checksum")
        if enabled and not match:
            mismatched_tools.append(tool)

    checks = {
        "topology": _check(
            topology_ok,
            "single-attendee limits enforced" if topology_ok else "configuration permits shared use",
            max_sessions_per_user=per_user,
            max_sessions_global=global_cap,
        ),
        "attendee_identity": _check(
            attendee_identity_ok,
            "instance is bound to one attendee identity"
            if attendee_identity_ok
            else "no valid attendee identity is bound to this instance",
            configured=bool(attendee_email),
            source=attendee_binding_source,
        ),
        "credentials": _check(
            credential_ok,
            "direct app-identity OAuth is recently validated and auto-refreshing"
            if credential_ok
            else "direct app-identity OAuth is stale/unhealthy or a static fallback is configured",
            credential_state=str(credential_status.get("state") or "unknown"),
            source=str(credential_status.get("source") or "unknown"),
            last_successful_at=last_credential_success,
            max_age_seconds=CREDENTIAL_SUCCESS_MAX_AGE,
        ),
        "app_sp_binding": _check(
            app_sp_binding_ok,
            "app client UUID and numeric service-principal identity are authoritatively verified"
            if app_sp_binding_ok
            else "app client UUID or WORKSHOP_APP_SP_ID is absent, invalid, or not authoritatively verified",
            expected_application_id=expected_app_client_id or None,
            observed_application_id=observed_app_client_id or None,
            expected_service_principal_id=expected_app_sp_id or None,
            observed_service_principal_id=observed_app_sp_id or None,
            validation_result=str(
                validation_diagnostic.get("result") or "unverified"
            ),
        ),
        "secret_protection": _check(
            secret_protection_ok,
            "OAuth client secret was captured before PTYs, scrubbed from env, and the server is non-dumpable"
            if secret_protection_ok
            else "OAuth client-secret process boundary is not hardened",
            initialized=secret_protection_status.get("initialized") is True,
            env_scrubbed=secret_protection_status.get("env_scrubbed") is True,
            non_dumpable=secret_protection_status.get("non_dumpable") is True,
        ),
        "installers": _check(
            not missing_installers,
            "all enabled agents and support tools are ready"
            if not missing_installers
            else (
                "serving a degraded fallback instead of the reviewed install"
                if degraded_installers
                else "one or more enabled installers are incomplete"
            ),
            missing=missing_installers,
            degraded=degraded_installers,
        ),
        "supply_chain": _check(
            supply_chain_ok,
            "reviewed bootstrap artifact manifest is complete"
            if supply_chain_ok
            else "reviewed bootstrap artifact manifest is missing or invalid",
            artifact_count=artifact_manifest.get("artifact_count"),
            error=artifact_manifest.get("error"),
            # "default" is the repo-owned contract; "override" means an operator
            # redirected sources at a mirror.
            source=artifact_manifest.get("source"),
            persistent_proof_reusable=artifact_proof.get("reusable") is True,
        ),
        "session_state": _check(
            state_writable,
            "session journal destination is writable"
            if state_writable
            else "SESSION_STATE_PATH is unset or not writable",
            configured=bool(state_path),
        ),
        "catalog": _check(
            catalog_ok,
            "attendee catalog access was recently verified"
            if catalog_ok
            else "catalog is unset or lacks recent attendee grant/owner proof",
            configured=bool(catalog),
            verified=catalog_verified and attendee_verified and entitlement_recent,
        ),
        "entitlements": _check(
            entitlement_ok,
            "entitlement reconciliation is enabled and healthy"
            if entitlement_ok
            else "entitlement reconciliation is disabled, unhealthy, or missing its catalog",
            enabled=entitlement_enabled,
            healthy=entitlement_status.get("ok") is True,
            thread_alive=entitlement_status.get("thread_alive") is True,
            last_verified_at=last_verified_at,
            verification_source=entitlement_status.get("verification_source"),
        ),
        "obo": _check(
            obo_ok,
            "OBO is enabled with required scopes"
            if obo_ok
            else "OBO is disabled or required scopes are missing",
            enabled=_bool(env, "ENABLE_OBO"),
            missing=missing_scopes,
            validation_state=obo_validation_state,
            external_validation_pending=obo_validation_state == "pending",
            present=obo_status.get("present") is True,
            fresh=obo_status.get("fresh") is True,
            validated_at=obo_validated_at,
            max_age_seconds=OBO_VALIDATION_MAX_AGE,
        ),
        "release_pins": _check(
            not missing_pins and not mismatched_tools,
            "all release inputs are pinned and installed versions match"
            if not missing_pins and not mismatched_tools
            else "release inputs are unpinned or installed versions differ",
            missing=sorted(missing_pins),
            mismatched=sorted(mismatched_tools),
        ),
    }
    ready = all(check["ok"] for check in checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "checks": checks,
        "release_manifest": release_manifest,
    }


def evaluate_runtime() -> dict:
    from . import attendee
    from .bootstrap import install
    from .credentials import credential_manager, secret_protection_status
    from .entitlements import entitlement_manager
    from .obo import obo_manager

    return evaluate(
        env=os.environ,
        credential_status=credential_manager.status(),
        installer_status=install.status(),
        entitlement_status=entitlement_manager.status(),
        obo_status=obo_manager.status(),
        secret_protection_status=secret_protection_status(),
        attendee_binding=attendee.binding(),
    )


__all__ = ["EXPECTED_OBO_SCOPES", "evaluate", "evaluate_runtime"]
