"""CT evaluation boundaries: no inferred ownership, identity, or readiness."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evals.generated_apps.adapters.control_tower import (
    REQUIRED_HARD_CHECKS,
    assess_readiness,
    assess_target,
    expected_omnigent_host_id,
    select_target,
)


COMMIT = "a" * 40
NOW = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)


def inventory():
    return {"apps": [{
        "name": "novice-eval-unit", "url": "https://terminal.example.com",
        "workspace_host": "https://labs.example.com", "disposable": True,
        "ct_run_id": "generated-app-eval-1", "ct_unit_id": "unit-1",
        "attendee_email": "synthetic@example.com", "token_env": "EVAL_OPERATOR_TOKEN",
        "attendee_token_env": "EVAL_ATTENDEE_TOKEN",
        "release": {"source_ref": COMMIT, "source_ref_immutable": True},
        "resources": {"catalog": "eval_catalog", "resources": [{
            "type": "warehouses", "id": "wh-1", "required_permission": "CAN_MANAGE",
        }]},
    }]}


def target():
    return select_target(inventory(), instance_name="novice-eval-unit")


def readiness():
    checks = {name: {"ok": True, "state": "green"} for name in REQUIRED_HARD_CHECKS}
    checks["attendee_identity"].update(source="control-tower", configured=True)
    checks["credentials"].update(credential_state="rotating", source="app_identity_oauth")
    checks["app_sp_binding"].update(
        expected_service_principal_id="123", observed_service_principal_id="123",
        expected_application_id="app-client", observed_application_id="app-client",
        validation_result="matched",
    )
    return {"ready": True, "status": "ready", "checks": checks, "release_manifest": {
        "claude": {"enabled": True, "expected": "2.1.237", "actual": "2.1.237", "match": True},
        "databricks_agent_skills": {
            "enabled": True, "expected": "v1.0.0", "actual": "v1.0.0", "match": True,
            "resolved_commit": COMMIT, "checksum": "b" * 64, "source": "network",
        },
    }}


def evidence():
    ready = readiness()
    return {
        "readyz": ready,
        "setup": {
            "steps": {"claude": {"status": "complete"}},
            "release_manifest": copy.deepcopy(ready["release_manifest"]),
        },
        "config": {
            "user": {"email": "synthetic@example.com"},
            "workspace_url": "https://labs.example.com",
            "omnigent_remote": {"enabled": False, "url": ""},
        },
        "attendee_current_user": {"id": "456", "userName": "synthetic@example.com"},
        "attendee_scim": {"id": "456", "userName": "synthetic@example.com"},
        "resources": {
            "catalog": "eval_catalog", "catalog_owner": True, "all_privileges": True,
            "attendee_access": True, "attendee_identity_bound": True, "resources": [{
                "type": "warehouses", "id": "wh-1", "state": "handed_off", "permission_level": "CAN_MANAGE",
            }],
        },
        "terminal_deployment": {"source_ref": COMMIT, "source_ref_immutable": True},
    }


def paired_inventory():
    value = inventory()
    pair = json.loads((Path(__file__).parents[1] / "docs/examples/omnigent-control-tower-payload.json").read_text())
    pair["environment"]["WORKSHOP_ATTENDEE_EMAIL"] = "synthetic@example.com"
    pair["deployment"]["server_version"] = json.loads((Path(__file__).parents[1] / "assets/artifacts/manifest.json").read_text())["artifacts"]["omnigent_lock"]["version"]
    value["apps"][0]["omnigent_compatibility"] = {
        "policy_id": "ct-reviewed-fixture", "contract_version": "1.6",
        "server_version": pair["deployment"]["server_version"],
    }
    pair["remote_host"]["expected_host_id"] = expected_omnigent_host_id(
        pair["deployment"]["app_url"], "synthetic@example.com"
    )
    value["apps"][0]["omnigent"] = pair
    return value


def paired_evidence(selected):
    value = evidence()
    pair = selected["omnigent"]
    value["config"]["omnigent_remote"] = {
        "enabled": True, "url": pair["deployment"]["app_url"]
    }
    host_id = pair["remote_host"]["expected_host_id"]
    value["omnigent"] = {
        "health": {"status": "ok"}, "version": {"version": pair["deployment"]["server_version"]},
        "deployment": copy.deepcopy(pair["deployment"]),
        "resources": {"lakebase_ready": True, "artifact_volume_ready": True},
        "host_readiness": {
            "status": "running", "connected": True, "expected_host_id": host_id,
            "host_id": host_id, "last_seen_at": "2026-10-08T05:00:00Z",
        },
    }
    return value


def test_exact_selection_never_guesses_disposability_from_name():
    value = inventory()
    del value["apps"][0]["disposable"]
    with pytest.raises(ValueError, match="explicitly disposable"):
        select_target(value, instance_name="novice-eval-unit")
    with pytest.raises(ValueError, match="not found"):
        select_target(inventory(), instance_name="NOVICE-EVAL-UNIT")


@pytest.mark.parametrize("url", [
    "http://localhost:8000", "https://token@terminal.example.com",
    "https://terminal.example.com?token=secret", "https://terminal.example.com#secret",
])
def test_inventory_rejects_credential_or_insecure_urls(url):
    value = inventory()
    value["apps"][0]["url"] = url
    with pytest.raises(ValueError):
        select_target(value, instance_name="novice-eval-unit")


def test_inventory_normalizes_https_and_rejects_duplicates():
    value = inventory()
    value["apps"][0]["url"] = "https://TERMINAL.example.com:443/"
    assert select_target(value, instance_name="novice-eval-unit")["url"] == "https://terminal.example.com"
    value["apps"].append({**value["apps"][0], "name": "other"})
    with pytest.raises(ValueError, match="duplicate"):
        select_target(value, instance_name="novice-eval-unit")


def test_inventory_retains_credential_references_never_raw_values():
    selected = target()
    assert selected["token_env"] == "EVAL_OPERATOR_TOKEN"
    value = inventory()
    value["apps"][0]["token"] = "private-value"
    with pytest.raises(ValueError, match="secret-free") as error:
        select_target(value, instance_name="novice-eval-unit")
    assert "private-value" not in str(error.value)
    value = inventory()
    value["apps"][0]["attendee_token_env"] = "EVAL_OPERATOR_TOKEN"
    with pytest.raises(ValueError, match="must differ"):
        select_target(value, instance_name="novice-eval-unit")


def test_pre_attendee_admission_waits_for_flagged_obo_but_execution_does_not():
    value = readiness()
    value["ready"] = False
    value["checks"]["obo"] = {"ok": False, "state": "red", "attendee_dependent": True}
    preflight = assess_readiness(value)
    assert preflight["admitted"] is True
    assert preflight["execution_ready"] is False
    assert "obo:awaiting_attendee" in preflight["warnings"]
    assert assess_readiness(value, attendee_present=True)["admitted"] is False


def test_unknown_new_hard_check_fails_closed_and_soft_failure_warns():
    value = readiness()
    value["checks"]["future_boundary"] = {"ok": False}
    value["checks"]["insight_capture"] = {"ok": False, "soft": True}
    result = assess_readiness(value)
    assert "future_boundary:red" in result["blockers"]
    assert "insight_capture:red" in result["warnings"]
    del value["checks"]["future_boundary"]
    assert assess_readiness(value)["execution_ready"] is True


@pytest.mark.parametrize("field,change,expected", [
    ("attendee_identity", {"source": "self-bound"}, "control_tower_binding_unverified"),
    ("credentials", {"source": "workshop_pat"}, "direct_oauth_unverified"),
    ("credentials", {"credential_state": "degraded"}, "direct_oauth_unverified"),
    ("app_sp_binding", {"observed_service_principal_id": "999"}, "identity_unverified"),
])
def test_ready_true_cannot_hide_wrong_ct_or_credential_plane(field, change, expected):
    value = readiness()
    value["checks"][field].update(change)
    result = assess_readiness(value)
    assert result["admitted"] is False
    assert f"{field}:{expected}" in result["blockers"]


def test_missing_required_check_cannot_be_reclassified_as_soft():
    value = readiness()
    value["checks"]["secret_protection"] = {"ok": True, "soft": True}
    assert "secret_protection:unverified" in assess_readiness(value)["blockers"]
    del value["checks"]["app_sp_binding"]
    assert "app_sp_binding:unverified" in assess_readiness(value)["blockers"]


def test_ready_single_run_never_claims_ct_or_fleet_qualification():
    result = assess_target(target(), evidence(), attendee_present=True)
    assert result["qualification_ready"] is True
    assert result["execution_ready"] is True
    assert result["provenance"]["control_tower_integration_qualified"] is False
    assert result["provenance"]["fleet_qualified"] is False


@pytest.mark.parametrize("mutation", ["wrong_user", "sp_identity", "scim_mismatch", "config_operator"])
def test_operator_auth_does_not_stand_in_for_the_assigned_attendee(mutation):
    value = evidence()
    if mutation == "wrong_user":
        value["attendee_current_user"]["userName"] = "operator@example.com"
    elif mutation == "sp_identity":
        value["attendee_current_user"]["applicationId"] = "app-client"
    elif mutation == "scim_mismatch":
        value["attendee_scim"]["id"] = "777"
    else:
        value["config"]["user"]["email"] = "operator@example.com"
    result = assess_target(target(), value, attendee_present=True)
    assert result["proofs"]["attendee_identity"] == "unverified"
    assert result["execution_ready"] is False


def test_resource_declarations_do_not_prove_grants_and_permissions_must_match():
    value = evidence()
    value["resources"]["resources"][0]["permission_level"] = "CAN_USE"
    result = assess_target(target(), value)
    assert result["proofs"]["resource_grants"] == "unverified"
    assert "resource_grants:unverified" in result["blockers"]
    del value["resources"]
    assert assess_target(target(), value)["execution_ready"] is False


def test_unknown_source_release_is_valid_baseline_but_not_build_qualification():
    selected = target()
    selected["release"] = {}
    result = assess_target(selected, evidence())
    assert result["execution_ready"] is True
    assert result["qualification_ready"] is False
    assert "release:baseline_revision_unknown" in result["warnings"]
    raw = inventory()
    raw["apps"][0]["release"] = {}
    assert select_target(raw, instance_name="novice-eval-unit")["release"] == {}


def test_setup_release_drift_fails_without_false_network_prewarm_drift():
    value = evidence()
    value["setup"]["release_manifest"]["databricks_agent_skills"]["source"] = "prewarmed"
    assert assess_target(target(), value)["proofs"]["setup"] == "verified"
    value["setup"]["release_manifest"]["claude"]["actual"] = "2.0.0"
    assert assess_target(target(), value)["proofs"]["setup"] == "unverified"


def test_paired_contract_preserves_resources_and_rejects_identity_substitution():
    value = paired_inventory()
    selected = select_target(value, instance_name="novice-eval-unit")
    assert selected["omnigent"]["resources"]["app_resource_keys"]["lakebase"] == "postgres"
    assert selected["omnigent"]["remote_host"]["authentication"]["static_secret_required"] is False
    value["apps"][0]["omnigent"]["environment"]["WORKSHOP_ATTENDEE_EMAIL"] = "other@example.com"
    with pytest.raises(ValueError, match="does not match"):
        select_target(value, instance_name="novice-eval-unit")


def test_paired_running_is_only_ready_with_exact_recent_attendee_host_proof():
    selected = select_target(paired_inventory(), instance_name="novice-eval-unit")
    value = paired_evidence(selected)
    assert assess_target(selected, value, now=NOW)["qualification_ready"] is True
    host = value["omnigent"]["host_readiness"]
    host["connected"] = False
    assert "omnigent:attendee_host_unverified" in assess_target(selected, value, now=NOW)["blockers"]
    host["connected"] = True
    host["host_id"] = "f" * 32
    assert assess_target(selected, value, now=NOW)["execution_ready"] is False


def test_paired_stale_or_unknown_resource_proof_is_unverified():
    selected = select_target(paired_inventory(), instance_name="novice-eval-unit")
    value = paired_evidence(selected)
    value["omnigent"]["host_readiness"]["last_seen_at"] = "2026-10-08T04:00:00Z"
    assert assess_target(selected, value, now=NOW)["execution_ready"] is False
    value = paired_evidence(selected)
    value["omnigent"]["resources"]["artifact_volume_ready"] = False
    assert "omnigent:resources_unverified" in assess_target(selected, value, now=NOW)["blockers"]


def test_paired_source_unknown_is_reported_separately_from_wt_source():
    selected = select_target(paired_inventory(), instance_name="novice-eval-unit")
    value = paired_evidence(selected)
    del value["omnigent"]["deployment"]
    result = assess_target(selected, value, now=NOW)
    assert result["execution_ready"] is True
    assert result["qualification_ready"] is False
    assert result["proofs"]["source_release"] == "verified"
    assert result["proofs"]["paired_source_release"] == "unverified"


def test_ct_recorded_newer_pair_version_needs_explicit_release_compatibility():
    value = paired_inventory()
    value["apps"][0]["omnigent"]["deployment"]["server_version"] = "0.99.0"
    selected = select_target(value, instance_name="novice-eval-unit")
    observed = paired_evidence(selected)
    observed["omnigent"]["version"]["version"] = "0.99.0"
    result = assess_target(selected, observed, now=NOW)
    assert result["execution_ready"] is True
    assert result["qualification_ready"] is False
    assert "omnigent:release_compatibility_unverified" in result["qualification_blockers"]
    value["apps"][0]["omnigent_compatibility"] = {
        "policy_id": "ct-reviewed-test-version", "contract_version": "1.6", "server_version": "0.99.0",
    }
    selected = select_target(value, instance_name="novice-eval-unit")
    assert assess_target(selected, observed, now=NOW)["qualification_ready"] is True


def test_deployed_package_digest_is_a_release_identity_separate_from_git():
    selected = target()
    selected["release"] = {"source_kind": "deployed-package", "source_digest": "c" * 64}
    observed = evidence()
    observed["terminal_deployment"] = dict(selected["release"])
    assert assess_target(selected, observed)["proofs"]["source_release"] == "verified"
    observed["terminal_deployment"]["source_digest"] = "d" * 64
    assert assess_target(selected, observed)["qualification_ready"] is False


def test_unrecorded_pair_is_not_ignored_for_a_bare_harness_cell():
    value = evidence()
    value["config"]["omnigent_remote"]["enabled"] = True
    assert "omnigent:unrecorded_pair" in assess_target(target(), value)["blockers"]


def test_endpoint_detail_and_auth_material_never_enter_output():
    value = evidence()
    private = "SYNTHETIC-private-bearer-value"
    value["readyz"]["checks"]["obo"]["detail"] = private
    value["readyz"]["access_token"] = private
    value["config"]["credential"] = {"token": private}
    value["setup"]["steps"]["claude"]["error"] = private
    serialized = json.dumps(assess_target(target(), value))
    assert private not in serialized
    assert "synthetic@example.com" not in serialized
