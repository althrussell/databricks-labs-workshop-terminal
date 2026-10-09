"""Pure, bounded deployment plans for WT without contacting Control Tower.

The standalone Labs runner consumes this plan and applies the WT contract to
fresh resources. Planning makes no network calls and proves no applied grants,
OAuth, consent, CT provisioning, event-attendee equivalence, or fleet readiness.
An operator can act out a novice persona; that does not change their identity.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from .control_tower import _omnigent, _release, _secret_fields, _url


OBO_SCOPES = (
    "catalog.catalogs:read", "catalog.schemas:read", "catalog.tables:read", "sql",
)
SOURCE_DIRECTORIES = ("server", "static", "assets", "content")
SOURCE_FILES = ("app.yaml", "requirements.txt", "requirements.in")
# Databricks Apps names are at most 26 characters; reserve three for "-wt".
_MARKER = re.compile(r"wt-eval-[a-z0-9](?:[a-z0-9-]{0,13}[a-z0-9])?")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_COMMIT = re.compile(r"[a-fA-F0-9]{40}")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?")
_SPEC_FIELDS = frozenset({
    "marker", "workspace_host", "workspace_id", "profile", "attendee_email",
    "attendee_mode", "disposable", "ttl_seconds", "cost_budget_usd", "release",
    "harnesses", "onboarding_wizard", "llm_wizard", "evaluation_observation",
    "omnigent", "omnigent_compatibility",
})
_ARTIFACTS = frozenset({
    "claude_binary", "claude_installer", "codex_native_package_linux_x64",
    "codex_npm_launcher_package", "databricks_agent_skills",
    "databricks_cli_archive_linux_x64", "databricks_cli_installer",
    "node_linux_arm64", "node_linux_x64", "omnigent_lock", "pi_npm_package",
    "python_3_12_runtime", "tmux_linux_x64", "uv_binary",
})
_PINS = {
    "CLAUDE_CODE_VERSION": "claude_binary",
    "CODEX_CLI_VERSION": "codex_npm_launcher_package",
    "DATABRICKS_CLI_VERSION": "databricks_cli_archive_linux_x64",
    "NODE_VERSION": "node_linux_x64",
    "OMNIGENT_VERSION": "omnigent_lock",
    "PI_CLI_VERSION": "pi_npm_package",
}


def deployment_path_allowed(relative_path: str) -> bool:
    """Limit uploads to runtime sources; the executor must also reject links.

    No filesystem reads occur here. Private evaluation fixtures, docs, scripts,
    tests, environment files, VCS files, and interpreter caches are excluded.
    """
    if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
        return False
    path = PurePosixPath(relative_path)
    if path.is_absolute() or str(path) != relative_path:
        return False
    if any(part in {"..", "__pycache__", "node_modules"} or part.startswith(".")
           for part in path.parts):
        return False
    if path.suffix in {".pyc", ".pyo"}:
        return False
    return relative_path in SOURCE_FILES or (
        len(path.parts) > 1 and path.parts[0] in SOURCE_DIRECTORIES
    )


def _text(value: object, pattern: re.Pattern, message: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(message)
    return value


def _bool(spec: Mapping, name: str, default: bool) -> bool:
    value = spec.get(name, default)
    if type(value) is not bool:
        raise ValueError("simulation flags must be booleans")
    return value


def _simulation_release(value: object) -> dict:
    """Distinguish uploaded runtime bytes from a Git ref or deployed package."""
    if isinstance(value, Mapping) and value.get("source_kind") == "runtime-snapshot":
        if set(value) != {
            "source_kind", "source_digest", "parent_git_sha", "instrumentation",
            "prompt_policy_unchanged_asserted",
        }:
            raise ValueError("runtime snapshot identity must be explicit")
        if (type(value["instrumentation"]) is not bool
                or value["prompt_policy_unchanged_asserted"] is not True):
            raise ValueError("runtime snapshot must assert unchanged prompt policy")
        return {
            "source_kind": "runtime-snapshot",
            "source_digest": _text(value["source_digest"], _DIGEST, "invalid runtime snapshot digest"),
            "parent_git_sha": _text(value["parent_git_sha"], _COMMIT, "invalid runtime snapshot parent"),
            "instrumentation": value["instrumentation"],
            "prompt_policy_unchanged_asserted": True,
        }
    release = _release(value)
    if not (release.get("source_ref_immutable") is True or (
        release.get("source_kind") == "deployed-package" and release.get("source_digest")
    )):
        raise ValueError("simulation requires an immutable source or snapshot identity")
    return release


def _reviewed_release(manifest: Mapping) -> tuple[dict, dict]:
    if (not isinstance(manifest, Mapping) or manifest.get("reviewed") is not True
            or type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != 1 or _secret_fields(manifest)):
        raise ValueError("simulation requires the reviewed artifact manifest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) not in {_ARTIFACTS, _ARTIFACTS - {"pi_npm_package"}}:
        raise ValueError("simulation requires the complete reviewed artifact manifest")
    for name, entry in artifacts.items():
        if (not isinstance(entry, Mapping) or not isinstance(entry.get("source"), str)
                or not entry["source"] or not isinstance(entry.get("version"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}", entry["version"])):
            raise ValueError("invalid reviewed artifact entry")
        if ":" in entry["source"]:
            _url(entry["source"])
        elif (PurePosixPath(entry["source"]).is_absolute()
              or ".." in PurePosixPath(entry["source"]).parts):
            raise ValueError("invalid reviewed artifact source")
        if name == "databricks_agent_skills":
            _text(entry.get("commit"), _COMMIT, "reviewed skills commit is missing")
            _text(entry.get("content_sha256"), _DIGEST, "reviewed skills digest is missing")
            if not isinstance(entry.get("version"), str) or not entry["version"]:
                raise ValueError("reviewed skills ref is missing")
        else:
            _text(entry.get("sha256"), _DIGEST, "reviewed artifact digest is missing")
        if name in {"uv_binary", "python_3_12_runtime"}:
            executable = entry.get("executable_relative_path")
            if (entry.get("kind") != "archive" or not isinstance(executable, str)
                    or not executable or PurePosixPath(executable).is_absolute()
                    or ".." in PurePosixPath(executable).parts or "\\" in executable):
                raise ValueError("invalid reviewed artifact executable path")
    _text(artifacts["codex_native_package_linux_x64"].get("executable_sha256"),
          _DIGEST, "reviewed Codex executable digest is missing")
    if artifacts["omnigent_lock"].get("lock_sha256") != artifacts["omnigent_lock"]["sha256"]:
        raise ValueError("reviewed Omnigent lock digests disagree")
    pins = {
        env: _text(artifacts[name].get("version"), _VERSION, "reviewed version pin is missing")
        for env, name in _PINS.items() if name in artifacts
    }
    pairs = (
        ("claude_installer", "claude_binary"),
        ("databricks_cli_installer", "databricks_cli_archive_linux_x64"),
        ("node_linux_arm64", "node_linux_x64"),
    )
    if any(artifacts[a].get("version") != artifacts[b].get("version") for a, b in pairs):
        raise ValueError("reviewed artifact versions disagree")
    if artifacts["codex_native_package_linux_x64"].get("version") != (
        pins["CODEX_CLI_VERSION"] + "-linux-x64"
    ):
        raise ValueError("reviewed Codex launcher and native package disagree")
    canonical = {"schema_version": 1, "reviewed": True, "artifacts": dict(artifacts)}
    try:
        digest = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid reviewed artifact manifest") from exc
    return pins, {
        "digest": digest, "digest_kind": "canonical-json-sha256",
        "skills_ref": artifacts["databricks_agent_skills"]["version"],
        "skills_commit": artifacts["databricks_agent_skills"]["commit"],
        "skills_content_sha256": artifacts["databricks_agent_skills"]["content_sha256"],
    }


def plan_simulation(spec: Mapping, artifact_manifest: Mapping, *, now: datetime | None = None) -> dict:
    """Create a reviewable single-attendee deployment plan; perform no actions.

    ``attendee_mode`` is explicitly ``operator_bound`` or ``synthetic_attendee``.
    The latter is the caller's assertion and still needs genuine browser/SCIM
    evidence. ``cost_budget_usd`` is a runner limit, not an applied CT/native
    gateway budget. The independent executor owns enforcement and teardown.
    """
    if not isinstance(spec, Mapping) or set(spec) - _SPEC_FIELDS or _secret_fields(spec):
        raise ValueError("simulation spec must be allowlisted and secret-free")
    marker = _text(spec.get("marker"), _MARKER, "simulation requires a bounded wt-eval marker")
    if spec.get("disposable") is not True:
        raise ValueError("simulation must be explicitly disposable")
    profile = _text(spec.get("profile"), _PROFILE, "simulation requires a profile reference")
    attendee = _text(spec.get("attendee_email"), _EMAIL, "simulation requires an assigned browser principal").lower()
    if len(attendee) > 254:
        raise ValueError("invalid assigned browser principal")
    mode = spec.get("attendee_mode")
    if mode not in {"operator_bound", "synthetic_attendee"}:
        raise ValueError("simulation requires an explicit attendee identity mode")
    workspace = _url(spec.get("workspace_host"), origin_only=True)
    ttl, cost = spec.get("ttl_seconds"), spec.get("cost_budget_usd")
    if type(ttl) is not int or not 60 <= ttl <= 4 * 3600:
        raise ValueError("simulation TTL must be 60..14400 seconds")
    if (isinstance(cost, bool) or not isinstance(cost, (int, float))
            or not math.isfinite(cost) or cost <= 0):
        raise ValueError("simulation requires a positive finite cost budget")
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("simulation requires an aware start time")
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    expiry = now + timedelta(seconds=ttl)
    release = _simulation_release(spec.get("release"))
    harnesses = spec.get("harnesses")
    if (not isinstance(harnesses, list) or not harnesses
            or any(not isinstance(agent, str) or agent not in {"claude", "codex", "omnigent"}
                   for agent in harnesses) or len(set(harnesses)) != len(harnesses)):
        raise ValueError("simulation requires an explicit supported harness set")
    harnesses = list(harnesses)
    pins, artifact_identity = _reviewed_release(artifact_manifest)
    run_id, unit_id = f"sim-{marker}", f"sim-{marker}-unit-1"
    names = {
        "app_name": marker + "-wt", "admin_group": marker + "-operators",
        "catalog": marker.replace("-", "_"), "schema": "generated_apps",
        "resource_prefix": marker + "-",
        "source_path": f"/Workspace/Shared/{marker}/workshop-terminal",
    }
    environment = {
        "DATABRICKS_HOST": workspace,
        "DATABRICKS_GATEWAY_HOST": workspace + "/ai-gateway",
        "WORKSHOP_ATTENDEE_EMAIL": attendee,
        "WORKSHOP_RUN_ID": run_id, "WORKSHOP_UNIT_ID": unit_id,
        "WORKSHOP_EVENT_ENDS_AT": str(int(expiry.timestamp())),
        "ALLOW_SHARED_TOPOLOGY": "false",
        "MAX_SESSIONS_PER_USER": "1", "MAX_SESSIONS_GLOBAL": "1",
        "ENABLE_OBO": "true", "OBO_SCOPES": ",".join(OBO_SCOPES),
        "ENABLE_ENTITLEMENTS": "true",
        "WORKSHOP_CATALOG": names["catalog"], "WORKSHOP_SCHEMA": names["schema"],
        "ADMIN_GROUP": names["admin_group"],
        "DATA_ROOT": f"/app/python/source_code/data/{marker}",
        "SESSION_STATE_PATH": f"/app/python/source_code/data/{marker}/sessions.json",
        "WORKSHOP_INSIGHT_CAPTURE": "false", "DISCOVERY_ENABLED": "false",
        "WORKSHOP_ONBOARDING_WIZARD": str(_bool(spec, "onboarding_wizard", True)).lower(),
        "WORKSHOP_LLM_WIZARD": str(_bool(spec, "llm_wizard", True)).lower(),
        "WORKSHOP_DEMO_CATALOG": "", "WORKSHOP_DEFAULT_INDUSTRY": "",
        "WORKSHOP_AGENTS": ",".join(harnesses),
        "OMNIGENT_ENABLED": str("omnigent" in harnesses).lower(),
        **pins,
    }
    if "workspace_id" in spec:
        environment["DATABRICKS_WORKSPACE_ID"] = _text(
            spec["workspace_id"], re.compile(r"[1-9][0-9]{0,31}"), "invalid workspace ID",
        )
    observation = _bool(spec, "evaluation_observation", False)
    if observation:
        environment.update({
            "WORKSHOP_EVALUATION_ENABLED": "true",
            "WORKSHOP_EVALUATION_MARKER": marker,
            "WORKSHOP_EVALUATION_ATTENDEE_EMAIL": attendee,
            "WORKSHOP_EVALUATION_RUN_ID": run_id,
            "WORKSHOP_EVALUATION_UNIT_ID": unit_id,
        })
    else:
        environment["WORKSHOP_EVALUATION_ENABLED"] = "false"
    paired = None
    compatibility = "not_applicable"
    if "omnigent" in spec:
        if "omnigent" not in harnesses:
            raise ValueError("paired Omnigent requires its enabled harness")
        paired = _omnigent(spec["omnigent"], attendee)
        environment["OMNIGENT_APP_URL"] = paired["deployment"]["app_url"]
        compatibility = "unverified"
        server_version = paired["deployment"]["server_version"]
        if server_version == pins["OMNIGENT_VERSION"]:
            compatibility = "reviewed_version_match"
        policy = spec.get("omnigent_compatibility")
        if policy is not None:
            if (not isinstance(policy, Mapping)
                    or set(policy) != {"policy_id", "contract_version", "server_version"}
                    or not isinstance(policy.get("policy_id"), str)
                    or not _PROFILE.fullmatch(policy["policy_id"])
                    or policy.get("contract_version") != paired["contract_version"]
                    or policy.get("server_version") != server_version):
                raise ValueError("invalid explicit paired compatibility policy")
            compatibility = "explicit_policy_asserted"
    elif "omnigent_compatibility" in spec:
        raise ValueError("paired compatibility policy requires a handoff")
    else:
        environment["OMNIGENT_APP_URL"] = ""
    return {
        "schema_version": 1, "scope": "simulated_control_tower", "mode": "plan_only",
        "control_tower_requests": 0, "marker": marker, "disposable": True,
        "workspace_host": workspace, "profile": profile, "names": names,
        "run_id": run_id, "unit_id": unit_id,
        "attendee": {"email": attendee, "mode": mode, "persona": "synthetic_nontechnical",
                     "identity_verified": False, "event_attendee_equivalence_verified": False},
        "bounds": {"attendee_count": 1, "created_at": now.isoformat().replace("+00:00", "Z"),
                   "expires_at": expiry.isoformat().replace("+00:00", "Z"), "ttl_seconds": ttl,
                   "cost_budget_usd": cost, "budget_enforcement": "external_runner_required",
                   "native_gateway_budget_applied": False, "automatic_expiry_applied": False},
        "release": release, "artifact_manifest_identity": artifact_identity,
        "environment": environment,
        "app_resource": {"name": names["app_name"], "must_be_absent": True,
                         "user_authorization_enabled": True, "user_api_scopes": list(OBO_SCOPES)},
        "pending_runtime_patches": [{"environment_key": "WORKSHOP_APP_SP_ID",
                                     "source": "created_app.service_principal_id",
                                     "validation": "positive_numeric_scim_id",
                                     "required_before_deploy": True}],
        "source_upload": {"directories": list(SOURCE_DIRECTORIES), "files": list(SOURCE_FILES),
                          "reject_symlinks": True, "reject_hardlinks": True,
                          "existing_destination_allowed": False},
        "grants": {"catalog": names["catalog"], "schema": names["schema"],
                   "must_be_absent": True,
                   "attendee": {"principal": attendee, "owner": True,
                                "privileges": ["ALL_PRIVILEGES", "MANAGE"]},
                   "app_service_principal": {"principal": None,
                       "privileges": ["MANAGE", "USE_CATALOG", "CREATE_SCHEMA"]},
                   "operator_group": {"name": names["admin_group"], "must_be_absent": True},
                   "applied": False},
        "omnigent": paired, "omnigent_compatibility": compatibility,
        "provenance": {"deployment_applied": False, "direct_oauth_verified": False,
                       "obo_consent_verified": False, "resource_grants_verified": False,
                       "prompt_policy_unchanged_verified": False,
                       "control_tower_provisioning_verified": False,
                       "control_tower_integration_qualified": False, "fleet_qualified": False},
    }


def bind_created_app(plan: Mapping, created_app: Mapping) -> dict:
    """Patch the exact fresh app's returned SP ID without claiming readiness.

    This supplied receipt is not independently authenticated here. The runner
    must create only an absent app and collect direct OAuth/SCIM evidence later.
    """
    if (not isinstance(plan, Mapping) or plan.get("scope") != "simulated_control_tower"
            or plan.get("mode") != "plan_only" or plan.get("control_tower_requests") != 0
            or not isinstance(created_app, Mapping) or _secret_fields(created_app)):
        raise ValueError("invalid simulated created-app receipt")
    names = plan.get("names", {})
    if created_app.get("name") != names.get("app_name"):
        raise ValueError("created app does not match the isolated simulation")
    raw_id = created_app.get("service_principal_id")
    if type(raw_id) is int:
        raw_id = str(raw_id)
    sp_id = _text(raw_id, re.compile(r"[1-9][0-9]{0,31}"), "created app requires a numeric SP ID")
    result = copy.deepcopy(dict(plan))
    result["environment"]["WORKSHOP_APP_SP_ID"] = sp_id
    result["grants"]["app_service_principal"]["principal"] = sp_id
    result["pending_runtime_patches"] = []
    result["created_app_receipt"] = {"name": names["app_name"], "service_principal_id": sp_id,
                                     "independently_verified": False}
    if created_app.get("url"):
        result["created_app_receipt"]["url"] = _url(created_app["url"])
    return result
