import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from evals.generated_apps.adapters.ct_client import ControlTowerClient, ControlTowerError, export_target, verify_ownership
from evals.generated_apps.adapters.control_tower import expected_omnigent_host_id


BASE = "https://ct.example.com"
NOW = datetime.now(timezone.utc)
EXPIRES = (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
DIGEST = "c" * 64
RUN_ID = "eval-run-1"
UNIT_ID = "unit-1"
MARKER = "wt-eval-bakery-1"


def run_detail():
    pair_url = "https://omni-child.example.com"
    email = "synthetic@example.com"
    pair = {
        "contract_version": "1.6",
        "deployment": {"app_name": "omni", "app_url": pair_url, "deployment_id": "omni-deployment",
            "server_version": "0.15.0", "source_repo": "https://github.com/althrussell/databricks-labs-workshop-terminal",
            "source_ref": "a" * 40, "source_ref_immutable": True, "source_subdir": "deploy/omnigent-app"},
        "resources": {"lakebase_endpoint": "projects/eval/branches/production/endpoints/primary",
            "lakebase_database": "projects/eval/branches/production/databases/default",
            "artifact_volume": "eval_catalog.default.artifacts",
            "app_resource_keys": {"lakebase": "postgres", "artifact_volume": "artifact_volume"}},
        "environment": {"OMNIGENT_APP_URL": pair_url, "WORKSHOP_ATTENDEE_EMAIL": email,
            "ALLOW_SHARED_TOPOLOGY": "false", "MAX_SESSIONS_PER_USER": "1", "MAX_SESSIONS_GLOBAL": "1"},
        "remote_host": {"enabled": True, "status": "waiting_for_token",
            "expected_host_id": expected_omnigent_host_id(pair_url, email),
            "host_id_derivation": "sha256(databricks-workshop-terminal/omnigent-host-id/v1\\0<normalized-server-url>\\0<normalized-attendee-email>)[:32]",
            "authentication": {"kind": "obo_token_mirror", "owner_identity": "attendee",
                "refresh_trigger": "authenticated_browser_request", "static_secret_required": False},
            "verified_commands": {"host": "omnigent host --server <OMNIGENT_APP_URL> --non-interactive",
                "terminal": "omnigent polly --server <OMNIGENT_APP_URL>"}},
    }
    return {
        "run": {"run_id": RUN_ID, "lab_name": MARKER, "requested_user_count": 1,
            "created_at": NOW.isoformat(),
            "termination_at": EXPIRES, "status": "ready", "cost_budget_usd": 10,
            "unit_counts": {"ready": 1}, "last_error": "Bearer private-credential"},
        "units": [{"unit_id": UNIT_ID, "run_id": RUN_ID, "lab_user_email": email,
            "workspace_id": "12345", "workspace_url": "https://child-workspace.example.com",
            "catalog_name": "eval_catalog", "schema_name": "default", "status": "ready",
            "apps": [
                {"app_kind": "workshop_terminal", "app_name": "wt", "app_url": "https://wt-child.example.com",
                 "release_digest": DIGEST, "resource_state": {"deployment": {
                    "mode": "package", "release_digest": DIGEST, "deployment_id": "wt-deployment"}}},
                {"app_kind": "omnigent", "app_name": "omni", "app_url": pair_url,
                 "version": "0.15.0", "resource_state": {"handoff": pair}},
            ]}],
        "apps": [], "admin_principals": [{"token": "private-credential"}],
    }


def responses():
    return {
        "/api/settings": {"app": {"version": "0.0.0+build", "password": "private-credential"},
            "database": {"password": "private-credential"}, "identity": {"token": "private-credential"}},
        "/api/labs": [run_detail()["run"]],
        "/api/workshop-release": {"config": {"configured": True, "deployment_mode": "package", "manifest_sha256": DIGEST},
            "job": {"log": ["private-credential"]}},
        f"/api/labs/{RUN_ID}": run_detail(),
    }


def client(overrides=None):
    data = responses() | (overrides or {})
    calls = []
    def transport(method, url, **kwargs):
        path = url.removeprefix(BASE)
        calls.append((method, path, kwargs.get("json")))
        return 200, copy.deepcopy(data[path])
    return ControlTowerClient(BASE, "labs", transport), calls


def payload():
    return {"lab_name": MARKER, "workspace_prefix": "eval", "catalog_prefix": "eval",
        "requested_user_count": 1, "target_region": "ap-southeast-2", "termination_at": EXPIRES,
        "cost_budget_usd": 10, "pair_omnigent_app": True}


def ownership():
    return {"marker": MARKER, "ttl_seconds": 7200, "cost_budget_usd": 10}


def test_discovery_performs_only_three_reads_and_removes_unrestricted_fields():
    ct, calls = client()
    discovered = ct.discover()
    assert [method for method, _, _ in calls] == ["GET"] * 3
    assert discovered["existing_runs_disposable"] is False
    assert discovered["runs"][0]["unit_counts"] == {"ready": 1}
    assert "private-credential" not in json.dumps(discovered)
    assert "database" not in discovered["settings"]


def test_exact_unit_discovery_retains_assignment_pair_and_package_provenance():
    ct, calls = client()
    proof = ct.fetch_unit(RUN_ID, UNIT_ID)
    assert proof["disposable"] is False
    with pytest.raises(ValueError, match="not automatically disposable"):
        ct.export_target(proof["run"], proof["unit"])
    target = ct.export_target(proof["run"], proof["unit"], disposable=True)
    assert target["workspace_host"] == "https://child-workspace.example.com"
    assert target["attendee_email"] == "synthetic@example.com"
    assert target["expires_at"] == EXPIRES
    assert target["release"] == {"source_kind": "deployed-package", "source_digest": DIGEST}
    assert target["omnigent"]["deployment"]["server_version"] == "0.15.0"
    assert target["omnigent"]["remote_host"]["status"] == "waiting_for_token"
    assert calls == [("GET", f"/api/labs/{RUN_ID}", None)]


def test_exact_identity_mismatch_never_selects_another_run_or_unit():
    ct, _ = client()
    with pytest.raises(ControlTowerError, match="unit_identity_mismatch"):
        ct.fetch_unit(RUN_ID, "other-unit")
    bad = run_detail()
    bad["run"]["run_id"] = "another-run"
    ct, _ = client({f"/api/labs/{RUN_ID}": bad})
    with pytest.raises(ControlTowerError, match="run_identity_mismatch"):
        ct.fetch_run(RUN_ID)


def test_conflicting_package_release_is_rejected():
    detail = run_detail()
    detail["units"][0]["apps"][0]["resource_state"]["deployment"]["release_digest"] = "d" * 64
    with pytest.raises(ValueError, match="conflicting package"):
        export_target(detail["run"], detail["units"][0], disposable=True)


def test_duplicate_terminal_not_inferred_by_url_or_name():
    detail = run_detail()
    detail["units"][0]["apps"].append(copy.deepcopy(detail["units"][0]["apps"][0]))
    with pytest.raises(ValueError, match="one exact Workshop Terminal"):
        export_target(detail["run"], detail["units"][0], disposable=True)


def test_mutations_default_to_plans_without_even_a_read_request():
    ct, calls = client()
    draft = ct.create_draft(payload(), ownership(), now=NOW)
    owned = draft["ownership"] | {"run_id": RUN_ID}
    for plan in (draft, ct.preflight(RUN_ID, owned), ct.provision(RUN_ID, owned),
                 ct.teardown_dry_run(RUN_ID, owned), ct.teardown(RUN_ID, owned)):
        assert plan["executed"] is False
    assert calls == []


@pytest.mark.parametrize("change", [
    {"requested_user_count": 2}, {"cost_budget_usd": 100}, {"lab_name": "Ai-Demo-01"},
    {"termination_at": (NOW + timedelta(hours=5)).isoformat()}, {"token": "private-credential"},
])
def test_create_cannot_escape_owned_single_seat_ttl_and_budget(change):
    ct, calls = client()
    with pytest.raises(ValueError):
        ct.create_draft(payload() | change, ownership(), execute=True, now=NOW)
    assert calls == []


def test_explicit_create_returns_exact_owned_run_then_provision_revalidates_it():
    ct, calls = client({"/api/labs": {"run_id": RUN_ID, "status": "draft"},
        f"/api/labs/{RUN_ID}/provision": {"run_id": RUN_ID, "status": "provisioning"}})
    created = ct.create_draft(payload(), ownership(), execute=True, now=NOW)
    assert created["ownership"]["run_id"] == RUN_ID
    provisioned = ct.provision(RUN_ID, created["ownership"], execute=True)
    assert provisioned["executed"] is True
    assert [item[:2] for item in calls] == [
        ("POST", "/api/labs"), ("GET", f"/api/labs/{RUN_ID}"), ("POST", f"/api/labs/{RUN_ID}/provision")]


def test_owned_operation_stops_before_post_when_ct_marker_changed():
    detail = run_detail()
    detail["run"]["lab_name"] = "someone-elses-event"
    ct, calls = client({f"/api/labs/{RUN_ID}": detail})
    owned = ownership() | {"run_id": RUN_ID, "termination_at": EXPIRES}
    with pytest.raises(ValueError, match="does not match"):
        ct.provision(RUN_ID, owned, execute=True)
    assert all(method == "GET" for method, _, _ in calls)


@pytest.mark.parametrize("change", [
    {"ttl_seconds": 0}, {"ttl_seconds": 5 * 3600}, {"cost_budget_usd": 100},
    {"marker": "Ai-Demo-01"}, {"run_id": "another-run"},
])
def test_public_owned_snapshot_validation_rejects_unbounded_or_wrong_receipts(change):
    detail = run_detail() | {"control_tower_url": BASE}
    owned = ownership() | {"run_id": RUN_ID, "termination_at": EXPIRES}
    with pytest.raises(ValueError):
        verify_ownership(detail, owned | change, now=NOW)


def test_public_ownership_verifier_compares_semantic_expiry_and_allows_expired_teardown():
    detail = run_detail() | {"control_tower_url": BASE}
    owned = ownership() | {"run_id": RUN_ID, "termination_at": EXPIRES}
    detail["run"]["termination_at"] = EXPIRES.replace("Z", "+00:00")
    assert verify_ownership(detail, owned, now=NOW)["verified"] is True
    future = NOW + timedelta(hours=3)
    with pytest.raises(ValueError, match="expired"):
        verify_ownership(detail, owned, now=future)
    assert verify_ownership(detail, owned, allow_expired=True, now=future)["verified"] is True


def test_required_model_budget_failure_remains_named_in_preflight_result():
    ct, calls = client({f"/api/labs/{RUN_ID}/preflight": {
        "run_id": RUN_ID, "status": "failed", "blocking_failures": 1, "warnings": 0,
        "checks": [{"name": "ai_gateway_budget", "status": "fail", "message": "private-credential"}],
    }})
    owned = ownership() | {"run_id": RUN_ID, "termination_at": EXPIRES}
    result = ct.preflight(RUN_ID, owned, execute=True)["result"]
    assert result["blocking_failures"] == 1
    assert result["checks"] == [{"name": "ai_gateway_budget", "status": "fail"}]
    assert "private-credential" not in json.dumps(result)
    assert calls[-1][0] == "POST"


def test_teardown_requires_exact_executed_unblocked_dry_run():
    ct, calls = client({f"/api/labs/{RUN_ID}/teardown/dry-run": {"run_id": RUN_ID, "blockers": []},
        f"/api/labs/{RUN_ID}/teardown": {"run_id": RUN_ID, "status": "destroying"}})
    owned = ownership() | {"run_id": RUN_ID, "termination_at": EXPIRES}
    with pytest.raises(ValueError, match="dry-run receipt"):
        ct.teardown(RUN_ID, owned, execute=True)
    assert calls == []
    dry_run = ct.teardown_dry_run(RUN_ID, owned, execute=True)
    assert ct.teardown(RUN_ID, owned, execute=True, dry_run_receipt=dry_run)["executed"] is True
    assert calls[-1] == ("POST", f"/api/labs/{RUN_ID}/teardown", {"confirmation": "DESTROY " + MARKER, "force": False})


def test_transport_failure_never_prints_exception_credentials():
    def fail(*args, **kwargs):
        raise RuntimeError("Bearer private-credential")
    ct = ControlTowerClient(BASE, "labs", fail)
    with pytest.raises(ControlTowerError) as error:
        ct.discover()
    assert str(error.value) == "control_tower_request_failed"


def app_spec():
    return {"git_url": "https://github.com/althrussell/databricks-labs-workshop-terminal",
        "app_kind": "workshop_terminal", "app_name_template": "wt", "required": True,
        "enable_obo": True, "enable_entitlements": True, "sequence_number": 1,
        "env_overrides": {"WORKSHOP_INSIGHT_CAPTURE": "false", "WORKSHOP_ONBOARDING_WIZARD": "true", "WORKSHOP_LLM_WIZARD": "true"}}


def test_explicit_eval_spec_retains_obo_grants_privacy_and_required_model_cap():
    ct, calls = client()
    body = payload() | {"apps": [app_spec()],
        "admin_principals": [{"principal_type": "user", "principal_identifier": "operator@example.com"}],
        "ai_gateway_budget": {"mode": "required", "per_user_monthly_usd": 10, "block_usage": True}}
    plan = ct.create_draft(body, ownership(), now=NOW)
    assert plan["payload"]["apps"][0]["enable_entitlements"] is True
    assert plan["payload"]["ai_gateway_budget"]["mode"] == "required"
    assert calls == []
    for key, value in (("WORKSHOP_PAT", "secret"), ("WORKSHOP_INSIGHT_CAPTURE", "true")):
        modified = copy.deepcopy(body)
        modified["apps"][0]["env_overrides"][key] = value
        with pytest.raises(ValueError, match="not allowlisted"):
            ct.create_draft(modified, ownership(), execute=True, now=NOW)
    assert calls == []


def test_event_default_disabled_budget_requires_explicit_recorded_acknowledgement():
    ct, calls = client()
    body = payload() | {"ai_gateway_budget": {"mode": "disabled"}}
    with pytest.raises(ValueError, match="policy acknowledgement"):
        ct.create_draft(body, ownership(), now=NOW)
    owned = ownership() | {"model_budget_policy": "event_default_unenforced"}
    planned = ct.create_draft(body, owned, now=NOW)
    assert planned["payload"]["ai_gateway_budget"] == {"mode": "disabled"}
    assert planned["ownership"]["model_budget_policy"] == "event_default_unenforced"
    assert planned["ownership"]["cost_budget_usd"] == 10
    assert planned["ownership"]["ttl_seconds"] == 7200
    assert calls == []


def test_event_default_acknowledgement_never_silently_downgrades_required_payload():
    ct, calls = client()
    body = payload() | {"ai_gateway_budget": {"mode": "required", "per_user_monthly_usd": 10, "block_usage": True}}
    owned = ownership() | {"model_budget_policy": "event_default_unenforced"}
    with pytest.raises(ValueError, match="conflicts"):
        ct.create_draft(body, owned, now=NOW)
    assert body["ai_gateway_budget"]["mode"] == "required"
    assert calls == []
