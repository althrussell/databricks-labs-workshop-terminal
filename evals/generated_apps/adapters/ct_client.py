"""Control Tower discovery and explicitly owned, bounded evaluation operations.

Discovery never marks an existing run disposable. Mutating methods return a
reviewable plan unless ``execute=True`` is explicitly supplied by the runner.
No method changes CT settings or the fleet's global workshop release.

An injected transport receives ``(method, url, json=body, timeout=seconds)`` and
returns ``(http_status, decoded_json)``. The default transport obtains request
headers from the named Databricks SDK profile without printing credentials.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .control_tower import _url, select_target


_TOKEN = re.compile(r"(?i)bearer\s+\S+|\bdapi[0-9a-f]{16,}\b|\beyJ[\w-]+\.[\w-]+\.[\w-]+\b")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_RUN_FIELDS = (
    "run_id", "lab_name", "workspace_prefix", "catalog_prefix", "requested_user_count",
    "target_region", "target_metastore_id", "termination_at", "status", "created_by",
    "created_at", "updated_at", "cost_budget_usd", "model_policy_revision", "unit_counts",
)
_UNIT_FIELDS = (
    "unit_id", "run_id", "sequence_number", "lab_user_email", "workspace_name",
    "workspace_id", "workspace_url", "catalog_name", "schema_name", "status",
    "cost_budget_usd", "suspended_at", "created_at", "updated_at",
)
_APP_FIELDS = (
    "app_id", "app_kind", "source_subdir", "app_name", "app_url", "status", "health",
    "version", "service_principal_id", "service_principal_client_id", "release_digest",
    "last_probe_at", "last_stats_synced_at", "last_insight_synced_at",
)
_CREATE_FIELDS = frozenset({
    "lab_name", "workspace_prefix", "catalog_prefix", "requested_user_count",
    "target_region", "target_metastore_id", "termination_at", "cost_budget_usd",
    "schema_name", "deployment_prefix", "pair_omnigent_app",
    "apps", "admin_principals", "ai_gateway_budget",
})
_WT_REPOS = frozenset({
    "https://github.com/althrussell/databricks-labs-workshop-terminal",
    "https://github.com/databricks-labs/workshop-terminal",
})
_EVAL_ENV = {
    "WORKSHOP_INSIGHT_CAPTURE": "false", "DISCOVERY_ENABLED": "false",
    "WORKSHOP_ONBOARDING_WIZARD": "true", "WORKSHOP_LLM_WIZARD": "true",
}


def _create_extras(payload: Mapping, *, cost: float, model_budget_policy: object = None) -> None:
    apps = payload.get("apps")
    if apps is not None:
        if not isinstance(apps, list) or len(apps) != 1:
            raise ValueError("evaluation app spec requires exactly one Workshop Terminal")
        app = _object(apps[0])
        allowed = {"git_url", "git_provider", "app_kind", "app_name_template", "required",
                   "enable_obo", "enable_entitlements", "sequence_number", "env_overrides", "source_subdir"}
        repo = _url(app.get("git_url")).removesuffix(".git")
        if (
            set(app) - allowed or repo not in _WT_REPOS
            or app.get("git_provider", "gitHub") != "gitHub"
            or app.get("app_kind") != "workshop_terminal"
            or app.get("required") is not True or app.get("enable_obo") is not True
            or app.get("enable_entitlements") is not True
            or app.get("sequence_number", 1) != 1 or app.get("source_subdir", "") != ""
        ):
            raise ValueError("evaluation app spec must retain the reviewed WT identity and grants")
        _identifier(app.get("app_name_template", "wt"))
        overrides = app.get("env_overrides", {})
        if not isinstance(overrides, Mapping) or any(_EVAL_ENV.get(key) != value for key, value in overrides.items()):
            raise ValueError("evaluation environment overrides are not allowlisted")
    principals = payload.get("admin_principals")
    if principals is not None:
        if not isinstance(principals, list) or not principals:
            raise ValueError("evaluation admin principals must be explicit")
        for entry in principals:
            principal = _object(entry)
            identifier = principal.get("principal_identifier")
            if (
                set(principal) != {"principal_type", "principal_identifier"}
                or principal.get("principal_type") not in {"user", "group", "service_principal"}
                or not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                or not re.fullmatch(r"[A-Za-z0-9_.@+ -]+", identifier) or _TOKEN.search(identifier)
            ):
                raise ValueError("invalid evaluation admin principal")
    budget = payload.get("ai_gateway_budget")
    if budget is not None:
        budget = _object(budget)
        if dict(budget) == {"mode": "disabled"}:
            if model_budget_policy != "event_default_unenforced":
                raise ValueError("disabled model budget requires explicit event-default policy acknowledgement")
            return
        if (
            set(budget) != {"mode", "per_user_monthly_usd", "block_usage"}
            or budget.get("mode") != "required" or budget.get("block_usage") is not True
            or isinstance(budget.get("per_user_monthly_usd"), bool)
            or budget.get("per_user_monthly_usd") != cost
        ):
            raise ValueError("evaluation model budget must retain its required blocking cap")
        if model_budget_policy == "event_default_unenforced":
            raise ValueError("required model budget conflicts with the event-default policy acknowledgement")


class ControlTowerError(RuntimeError):
    """Stable errors deliberately omit transport bodies and credential values."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _object(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _scalar(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return "[REDACTED]" if _TOKEN.search(value) else value[:1024]
    return None


def _pick(value: object, fields: tuple[str, ...]) -> dict:
    raw = _object(value)
    result = {key: _scalar(raw[key]) for key in fields if key in raw}
    for key in ("app_url", "workspace_url", "source_repo", "manifest_url", "OMNIGENT_APP_URL"):
        if result.get(key):
            try:
                result[key] = _url(result[key])
            except ValueError:
                result[key] = None
    return result


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("evaluation expiry must include a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("evaluation expiry must include a timezone") from error
    if parsed.tzinfo is None:
        raise ValueError("evaluation expiry must include a timezone")
    return parsed.astimezone(timezone.utc)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("exact Control Tower identifier required")
    return value


def _handoff(value: object) -> dict:
    raw = _object(value)
    if not raw:
        return {}
    host = _object(raw.get("remote_host"))
    resources = _object(raw.get("resources"))
    return {
        "contract_version": _scalar(raw.get("contract_version")),
        "deployment": _pick(raw.get("deployment"), (
            "app_name", "app_url", "deployment_id", "server_version", "source_repo",
            "source_ref", "source_ref_immutable", "source_subdir",
        )),
        "resources": _pick(resources, ("lakebase_endpoint", "lakebase_database", "artifact_volume")) | {
            "app_resource_keys": _pick(resources.get("app_resource_keys"), ("lakebase", "artifact_volume")),
        },
        "environment": _pick(raw.get("environment"), (
            "OMNIGENT_APP_URL", "WORKSHOP_ATTENDEE_EMAIL", "ALLOW_SHARED_TOPOLOGY",
            "MAX_SESSIONS_PER_USER", "MAX_SESSIONS_GLOBAL",
        )),
        "remote_host": _pick(host, ("enabled", "status", "expected_host_id", "host_id_derivation")) | {
            "authentication": _pick(host.get("authentication"), (
                "kind", "owner_identity", "refresh_trigger", "static_secret_required",
            )),
            "verified_commands": _pick(host.get("verified_commands"), ("host", "terminal")),
        },
    }


def _readiness(value: object) -> dict:
    raw = _object(value)
    if not raw:
        return {}
    app = _object(raw.get("app"))
    return _pick(raw, ("contract_version", "ready", "status")) | {
        "app": _pick(app, ("ready", "health_http_status", "server_version")) | {
            "health_body": _pick(app.get("health_body"), ("status",)),
        },
        "resources": _pick(raw.get("resources"), ("lakebase_ready", "artifact_volume_ready")),
        "remote_host": _pick(raw.get("remote_host"), (
            "required", "enabled", "connected", "status", "expected_host_id", "host_id", "last_seen_at",
        )),
        "failure": _pick(raw.get("failure"), ("code", "retryable")) or None,
    }


def _app(value: object) -> dict:
    raw = _object(value)
    state = _object(raw.get("resource_state"))
    result = _pick(raw, _APP_FIELDS)
    result["resource_state"] = {
        "deployment": _pick(state.get("deployment"), (
            "mode", "source_path", "deployment_id", "release_digest", "source_ref",
            "source_ref_immutable", "source_repo",
        )),
        "resource_ownership": _pick(state.get("resource_ownership"), ("app", "lakebase_autoscaling")),
        "readyz": _pick(state.get("readyz"), ("ready", "status", "admitted", "attempts")),
    }
    handoff = _handoff(raw.get("omnigent_handoff") or state.get("handoff"))
    readiness = _readiness(raw.get("omnigent_readiness") or state.get("readiness"))
    if handoff:
        result["omnigent_handoff"] = handoff
    if readiness:
        result["omnigent_readiness"] = readiness
    return result


def _unit(value: object) -> dict:
    raw = _object(value)
    apps = raw.get("apps")
    return _pick(raw, _UNIT_FIELDS) | {
        "apps": [_app(item) for item in apps if isinstance(item, Mapping)] if isinstance(apps, list) else [],
    }


def _run(value: object) -> dict:
    result = _pick(value, _RUN_FIELDS)
    counts = _object(_object(value).get("unit_counts"))
    if counts:
        result["unit_counts"] = {
            str(key): item for key, item in counts.items()
            if _ID.fullmatch(str(key)) and isinstance(item, int) and not isinstance(item, bool)
        }
    return result


def _release_status(value: object) -> dict:
    raw = _object(value)
    verify = _object(raw.get("verify"))
    manifest = _object(verify.get("manifest"))
    return {
        "config": _pick(raw.get("config"), (
            "configured", "deployment_mode", "manifest_url", "manifest_sha256", "volume_path", "contract_version",
        )),
        "verify": _pick(verify, (
            "schema_version", "contract_version", "status", "manifest_sha256", "artifact_sha256",
            "artifact_path", "manifest_path", "checked_at", "last_verified_at",
        )) | {"manifest": _pick(manifest, (
            "artifact_name", "artifact_type", "entry_point", "format_version", "logical_contents_file_count",
            "logical_contents_sha256", "minimum_control_tower_contract_version", "platform", "python_abi",
            "python_implementation", "sha256", "size_bytes", "wt_git_sha", "wt_release_tag",
        ))},
    }


def export_target(ct_run: Mapping, unit: Mapping, *, disposable: bool = False) -> dict:
    """Freeze exact assigned CT child state; caller explicitly asserts disposal."""
    if disposable is not True:
        raise ValueError("existing Control Tower units are not automatically disposable")
    run_id = _identifier(ct_run.get("run_id"))
    if unit.get("run_id") != run_id:
        raise ValueError("Control Tower unit does not belong to the selected run")
    apps = unit.get("apps")
    apps = apps if isinstance(apps, list) else []
    terminal = [app for app in apps if _object(app).get("app_kind") == "workshop_terminal"]
    paired = [app for app in apps if _object(app).get("app_kind") == "omnigent"]
    if len(terminal) != 1 or len(paired) > 1:
        raise ValueError("evaluation requires one exact Workshop Terminal and at most one paired Omnigent")
    terminal = _object(terminal[0])
    deployment = _object(_object(terminal.get("resource_state")).get("deployment"))
    release = {}
    digest = terminal.get("release_digest") or deployment.get("release_digest")
    if deployment.get("mode") == "package" and isinstance(digest, str) and _DIGEST.fullmatch(digest):
        if deployment.get("release_digest") and deployment["release_digest"] != digest:
            raise ValueError("Control Tower recorded conflicting package releases")
        release = {"source_kind": "deployed-package", "source_digest": digest}
    elif deployment.get("source_ref"):
        release = {"source_kind": "git", "source_ref": deployment["source_ref"],
                   "source_ref_immutable": deployment.get("source_ref_immutable") is True}
    raw = {
        "name": terminal.get("app_name"), "url": terminal.get("app_url"),
        "workspace_host": unit.get("workspace_url"), "workspace_id": unit.get("workspace_id"),
        "ct_run_id": run_id, "ct_unit_id": _identifier(unit.get("unit_id")),
        "disposable": True, "attendee_email": unit.get("lab_user_email"),
        "expires_at": ct_run.get("termination_at"), "schema": unit.get("schema_name"),
        "release": release, "resources": {"catalog": unit.get("catalog_name")},
    }
    for key in ("deployment_id",):
        if deployment.get(key):
            raw[key] = deployment[key]
    if terminal.get("service_principal_id"):
        raw["service_principal_id"] = terminal["service_principal_id"]
    if paired:
        handoff = _object(paired[0]).get("omnigent_handoff") or _object(_object(paired[0]).get("resource_state")).get("handoff")
        if not handoff:
            raise ValueError("paired Omnigent has no recorded Control Tower handoff")
        raw["omnigent"] = _handoff(handoff)
    return select_target({"apps": [raw]}, instance_name=str(raw["name"]))


def verify_ownership(
    detail: Mapping, ownership: Mapping, *, allow_expired: bool = False,
    now: datetime | None = None,
) -> dict:
    """Verify a CT observation against an exact bounded evaluation receipt."""
    run = _object(detail.get("run"))
    marker = ownership.get("marker")
    run_id = _identifier(ownership.get("run_id"))
    ttl = ownership.get("ttl_seconds")
    cost = ownership.get("cost_budget_usd")
    if (
        not isinstance(marker, str) or not marker.startswith("wt-eval-")
        or not _ID.fullmatch(marker)
        or run.get("run_id") != run_id or run.get("lab_name") != marker
        or type(run.get("requested_user_count")) is not int or run["requested_user_count"] != 1
        or type(ttl) is not int or not 60 <= ttl <= 4 * 3600
        or isinstance(cost, bool) or not isinstance(cost, (int, float))
        or not math.isfinite(cost) or cost <= 0 or run.get("cost_budget_usd") != cost
    ):
        raise ValueError("Control Tower run does not match bounded evaluation ownership")
    expiry = _timestamp(run.get("termination_at"))
    declared_expiry = _timestamp(ownership.get("termination_at"))
    created = _timestamp(run.get("created_at"))
    if expiry != declared_expiry or not 0 < (expiry - created).total_seconds() <= ttl:
        raise ValueError("Control Tower expiry does not match evaluation ownership TTL")
    if not allow_expired and expiry <= (now or datetime.now(timezone.utc)):
        raise ValueError("evaluation run has expired")
    if ownership.get("control_tower_url") and (
        _url(ownership["control_tower_url"]) != _url(detail.get("control_tower_url"))
    ):
        raise ValueError("Control Tower origin does not match evaluation ownership")
    return {"verified": True, "run_id": run_id, "marker": marker,
            "ttl_seconds": ttl, "cost_budget_usd": cost, "termination_at": ownership["termination_at"]}


class ControlTowerClient:
    def __init__(self, base_url: str, profile: str, transport=None, *, timeout: float = 30):
        self.base_url = _url(base_url)
        self.profile = _identifier(profile)
        if not 0 < timeout <= 60:
            raise ValueError("Control Tower request timeout must be bounded")
        self.timeout = timeout
        self._transport = transport

    def _request(self, method: str, path: str, body: dict | None = None) -> object:
        try:
            url = f"{self.base_url}{path}"
            if self._transport is not None:
                status, payload = self._transport(method, url, json=body, timeout=self.timeout)
            else:
                from databricks.sdk.core import Config

                headers = Config(profile=self.profile).authenticate()
                headers["Accept"] = "application/json"
                data = json.dumps(body).encode() if body is not None else None
                if data is not None:
                    headers["Content-Type"] = "application/json"
                request = Request(url, data=data, headers=headers, method=method)
                # An authenticated API probe must not follow SSO/foreign redirects.
                with build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
                    status = response.status
                    payload = json.load(response)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise ControlTowerError("control_tower_request_failed")
            return payload
        except (ControlTowerError, HTTPError, URLError, OSError, ValueError, TypeError):
            raise ControlTowerError("control_tower_request_failed") from None
        except Exception:
            # SDK/custom transports must not expose credential-bearing diagnostics.
            raise ControlTowerError("control_tower_request_failed") from None

    def discover(self) -> dict:
        settings = _object(self._request("GET", "/api/settings"))
        labs = self._request("GET", "/api/labs")
        if not isinstance(labs, list):
            raise ControlTowerError("control_tower_runs_shape_invalid")
        return {
            "control_tower_url": self.base_url, "profile": self.profile,
            "settings": {
                "app": _pick(settings.get("app"), ("app_name", "version", "build_sha", "python_version")),
                "provisioning": _pick(settings.get("provisioning"), ("cloud", "default_region", "max_workspaces_per_run")),
                "safety": _pick(settings.get("safety"), ("require_dry_run_before_destroy", "default_expiry_hours", "max_expiry_hours")),
                "flags": _pick(settings.get("flags"), ("enable_workshop_apps", "enable_content_seeding", "enable_ai_gateway_budgets")),
            },
            "runs": [_run(item) for item in labs if isinstance(item, Mapping)],
            "workshop_release": _release_status(self._request("GET", "/api/workshop-release")),
            "existing_runs_disposable": False,
        }

    def fetch_run(self, run_id: str) -> dict:
        run_id = _identifier(run_id)
        raw = _object(self._request("GET", f"/api/labs/{quote(run_id, safe='')}"))
        run = _run(raw.get("run"))
        if run.get("run_id") != run_id:
            raise ControlTowerError("control_tower_run_identity_mismatch")
        units = raw.get("units")
        apps = raw.get("apps")
        return {
            "run": run,
            "units": [_unit(item) for item in units if isinstance(item, Mapping)] if isinstance(units, list) else [],
            "apps": [_app(item) for item in apps if isinstance(item, Mapping)] if isinstance(apps, list) else [],
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "proof_source": "authenticated_control_tower_run_api",
            "control_tower_url": self.base_url,
            "disposable": False,
        }

    def fetch_unit(self, run_id: str, unit_id: str) -> dict:
        unit_id = _identifier(unit_id)
        detail = self.fetch_run(run_id)
        units = [unit for unit in detail["units"] if unit.get("unit_id") == unit_id and unit.get("run_id") == run_id]
        if len(units) != 1:
            raise ControlTowerError("control_tower_unit_identity_mismatch")
        return {key: value for key, value in detail.items() if key not in {"units", "apps"}} | {"unit": units[0]}

    def export_target(self, ct_run: Mapping, unit: Mapping, *, disposable: bool = False) -> dict:
        return export_target(ct_run, unit, disposable=disposable)

    @staticmethod
    def verify_ownership(detail: Mapping, ownership: Mapping, *, allow_expired: bool = False, now: datetime | None = None) -> dict:
        return verify_ownership(detail, ownership, allow_expired=allow_expired, now=now)

    def owned_fetch_run(self, ownership: Mapping, *, allow_expired: bool = False) -> dict:
        detail = self.fetch_run(_identifier(ownership.get("run_id")))
        proof = verify_ownership(detail, ownership, allow_expired=allow_expired)
        return detail | {"evaluation_ownership": proof}

    @staticmethod
    def _owned_payload(payload: Mapping, ownership: Mapping, *, now: datetime) -> dict:
        if not isinstance(payload, Mapping) or set(payload) - _CREATE_FIELDS:
            raise ValueError("evaluation create accepts only the bounded CT default topology")
        if type(payload.get("requested_user_count")) is not int or payload["requested_user_count"] != 1:
            raise ValueError("evaluation create requires exactly one seat")
        marker = ownership.get("marker")
        if not isinstance(marker, str) or not marker.startswith("wt-eval-") or payload.get("lab_name") != marker:
            raise ValueError("evaluation create requires an exact ownership marker")
        _identifier(marker)
        ttl = ownership.get("ttl_seconds")
        cost = ownership.get("cost_budget_usd")
        if type(ttl) is not int or not 60 <= ttl <= 4 * 3600:
            raise ValueError("evaluation ownership requires a bounded TTL")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost <= 0 or payload.get("cost_budget_usd") != cost:
            raise ValueError("evaluation ownership requires an explicit matching cost budget")
        remaining = (_timestamp(payload.get("termination_at")) - now).total_seconds()
        if not 0 < remaining <= ttl:
            raise ValueError("evaluation termination must fit its ownership TTL")
        for key in ("workspace_prefix", "catalog_prefix", "target_region"):
            _identifier(payload.get(key))
        _create_extras(payload, cost=cost, model_budget_policy=ownership.get("model_budget_policy"))
        return dict(payload)

    def create_draft(self, payload: Mapping, ownership: Mapping, *, execute: bool = False, now: datetime | None = None) -> dict:
        body = self._owned_payload(payload, ownership, now=now or datetime.now(timezone.utc))
        owned = _pick(ownership, ("marker", "ttl_seconds", "cost_budget_usd", "run_id", "model_budget_policy")) | {
            "termination_at": body["termination_at"],
            "control_tower_url": self.base_url,
        }
        plan = {"operation": "create_draft", "method": "POST", "path": "/api/labs", "payload": body,
                "ownership": owned, "executed": False}
        if execute:
            result = _pick(self._request("POST", "/api/labs", body), ("run_id", "status"))
            run_id = _identifier(result.get("run_id"))
            plan.update(executed=True, result=result, ownership=owned | {"run_id": run_id})
        return plan

    def _owned_operation(self, operation: str, run_id: str, ownership: Mapping, *, execute: bool, body: dict | None = None) -> dict:
        run_id = _identifier(run_id)
        if ownership.get("run_id") != run_id or not str(ownership.get("marker", "")).startswith("wt-eval-"):
            raise ValueError("evaluation operation requires exact owned run evidence")
        path = f"/api/labs/{quote(run_id, safe='')}/{operation}"
        plan = {"operation": operation, "method": "POST", "path": path, "payload": body, "executed": False}
        if execute:
            detail = self.fetch_run(run_id)
            verify_ownership(detail, ownership, allow_expired=operation.startswith("teardown"))
            response = _object(self._request("POST", path, body))
            result = _pick(response, ("run_id", "status", "ok", "blocking_failures", "warnings"))
            if operation == "preflight":
                checks = response.get("checks")
                result["checks"] = [_pick(item, ("name", "status")) for item in checks] if isinstance(checks, list) else []
            if operation == "teardown/dry-run":
                blockers = response.get("blockers")
                result["blocker_count"] = len(blockers) if isinstance(blockers, list) else None
            if response.get("run_id") != run_id:
                raise ControlTowerError("control_tower_operation_identity_mismatch")
            plan.update(executed=True, result=result)
        return plan

    def preflight(self, run_id: str, ownership: Mapping, *, execute: bool = False) -> dict:
        return self._owned_operation("preflight", run_id, ownership, execute=execute)

    def provision(self, run_id: str, ownership: Mapping, *, execute: bool = False) -> dict:
        return self._owned_operation("provision", run_id, ownership, execute=execute, body={})

    def teardown_dry_run(self, run_id: str, ownership: Mapping, *, execute: bool = False) -> dict:
        return self._owned_operation("teardown/dry-run", run_id, ownership, execute=execute)

    def teardown(self, run_id: str, ownership: Mapping, *, execute: bool = False, dry_run_receipt: Mapping | None = None) -> dict:
        if execute and not (
            dry_run_receipt and dry_run_receipt.get("executed") is True
            and dry_run_receipt.get("path") == f"/api/labs/{run_id}/teardown/dry-run"
            and _object(dry_run_receipt.get("result")).get("run_id") == run_id
            and _object(dry_run_receipt.get("result")).get("blocker_count") == 0
        ):
            raise ValueError("evaluation teardown requires its exact dry-run receipt")
        return self._owned_operation("teardown", run_id, ownership, execute=execute,
                                     body={"confirmation": "DESTROY " + str(ownership.get("marker")), "force": False})
