"""Local CT-contract plans cannot masquerade as live CT integration evidence."""

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evals.generated_apps.adapters.control_tower import expected_omnigent_host_id
from evals.generated_apps.adapters.simulated_control_tower import (
    OBO_SCOPES,
    bind_created_app,
    deployment_path_allowed,
    plan_simulation,
)


ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)


def manifest():
    return json.loads((ROOT / "assets/artifacts/manifest.json").read_text())


def spec():
    return {
        "marker": "wt-eval-bakery-1", "disposable": True,
        "workspace_host": "https://LABS.example.com:443/", "workspace_id": "12345",
        "profile": "labs", "attendee_email": "Operator@example.com",
        "attendee_mode": "operator_bound", "ttl_seconds": 3600, "cost_budget_usd": 10,
        "release": {"source_kind": "deployed-package", "source_digest": "a" * 64},
        "harnesses": ["claude", "codex", "omnigent"], "evaluation_observation": True,
    }


def plan(value=None):
    return plan_simulation(value or spec(), manifest(), now=NOW)


def test_agentbricks_opt_in_uses_reviewed_lock_without_ct_requests():
    value = spec()
    value["agentbricks"] = True
    result = plan(value)
    assert result["environment"]["AGENTBRICKS_ENABLED"] == "true"
    assert result["environment"]["AGENTBRICKS_VERSION"] == manifest()["artifacts"]["agentbricks_lock"]["version"]
    assert result["control_tower_requests"] == 0
    assert plan()["environment"]["AGENTBRICKS_ENABLED"] == "false"


def test_simulation_accepts_current_ux_pins_and_older_complete_releases():
    current = manifest()
    assert plan_simulation(spec(), current, now=NOW)["control_tower_requests"] == 0
    historical = copy.deepcopy(current)
    names = {"impeccable_npm_launcher", "impeccable_engine_linux_x64", "impeccable_skill_bundle"}
    for name in names:
        historical["artifacts"].pop(name)
    historical["artifacts"]["databricks_agent_skills"].pop("effective_content_sha256")
    assert plan_simulation(spec(), historical, now=NOW)["control_tower_requests"] == 0
    current["artifacts"].pop("impeccable_skill_bundle")
    with pytest.raises(ValueError, match="all pinned Impeccable"):
        plan_simulation(spec(), current, now=NOW)


def test_historical_manifest_remains_supported_without_agentbricks():
    reviewed = manifest()
    del reviewed["artifacts"]["agentbricks_lock"]
    assert plan_simulation(spec(), reviewed, now=NOW)["environment"]["AGENTBRICKS_ENABLED"] == "false"
    value = spec()
    value["agentbricks"] = True
    with pytest.raises(ValueError, match="requires its reviewed artifact lock"):
        plan_simulation(value, reviewed, now=NOW)


@pytest.mark.parametrize("mutation", ["unknown_artifact", "lock_digest"])
def test_optional_artifact_does_not_relax_manifest_validation(mutation):
    reviewed = manifest()
    if mutation == "unknown_artifact":
        reviewed["artifacts"]["unknown"] = reviewed["artifacts"]["agentbricks_lock"]
    else:
        reviewed["artifacts"]["agentbricks_lock"]["lock_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        plan_simulation(spec(), reviewed, now=NOW)


def pair():
    value = json.loads((ROOT / "docs/examples/omnigent-control-tower-payload.json").read_text())
    value["environment"]["WORKSHOP_ATTENDEE_EMAIL"] = "operator@example.com"
    value["remote_host"]["expected_host_id"] = expected_omnigent_host_id(
        value["deployment"]["app_url"], "operator@example.com",
    )
    return value


def test_single_attendee_plan_has_scoped_fresh_names_and_no_ct_actions():
    result = plan()
    assert result["scope"] == "simulated_control_tower"
    assert result["mode"] == "plan_only"
    assert result["control_tower_requests"] == 0
    assert result["workspace_host"] == "https://labs.example.com"
    assert result["names"] == {
        "app_name": "wt-eval-bakery-1-wt", "admin_group": "wt-eval-bakery-1-operators",
        "catalog": "wt_eval_bakery_1", "schema": "generated_apps",
        "resource_prefix": "wt-eval-bakery-1-",
        "source_path": "/Workspace/Shared/wt-eval-bakery-1/workshop-terminal",
    }
    assert result["app_resource"]["must_be_absent"] is True
    assert result["grants"]["must_be_absent"] is True
    assert result["grants"]["operator_group"]["must_be_absent"] is True
    assert result["grants"]["applied"] is False
    assert result["grants"]["catalog"] == result["names"]["catalog"]
    assert result["grants"]["app_service_principal"]["principal"] is None
    assert result["grants"]["app_service_principal"]["privileges"] == [
        "MANAGE", "USE_CATALOG", "CREATE_SCHEMA",
    ]
    assert all(value is False for value in result["provenance"].values())


def test_operator_browser_principal_is_never_described_as_event_attendee_evidence():
    result = plan()
    assert result["attendee"] == {
        "email": "operator@example.com", "mode": "operator_bound",
        "persona": "synthetic_nontechnical", "identity_verified": False,
        "event_attendee_equivalence_verified": False,
    }
    value = spec()
    value["attendee_mode"] = "synthetic_attendee"
    assert plan(value)["attendee"]["identity_verified"] is False


def test_environment_matches_wt_contract_without_fabricating_oauth_or_sp_identity():
    result = plan()
    env = result["environment"]
    assert env["WORKSHOP_ATTENDEE_EMAIL"] == "operator@example.com"
    assert env["ALLOW_SHARED_TOPOLOGY"] == "false"
    assert env["MAX_SESSIONS_PER_USER"] == env["MAX_SESSIONS_GLOBAL"] == "1"
    assert env["ENABLE_OBO"] == env["ENABLE_ENTITLEMENTS"] == "true"
    assert env["SESSION_STATE_PATH"].endswith("/wt-eval-bakery-1/sessions.json")
    assert env["WORKSHOP_INSIGHT_CAPTURE"] == env["DISCOVERY_ENABLED"] == "false"
    assert env["WORKSHOP_ONBOARDING_WIZARD"] == env["WORKSHOP_LLM_WIZARD"] == "true"
    assert env["WORKSHOP_DEMO_CATALOG"] == ""
    assert "WORKSHOP_APP_SP_ID" not in env
    assert "SKILLS_REF" not in env
    assert "WORKSHOP_PAT" not in env
    assert "DATABRICKS_TOKEN" not in env
    assert "CONTROL_TOWER_URL" not in env
    assert result["pending_runtime_patches"][0]["required_before_deploy"] is True
    assert result["app_resource"]["user_api_scopes"] == list(OBO_SCOPES)
    assert env["OBO_SCOPES"].split(",") == list(OBO_SCOPES)
    assert result["provenance"]["obo_consent_verified"] is False


def test_bound_evaluation_observation_uses_actual_injected_simulation_ids(monkeypatch):
    from server import config

    result = plan()
    for key, value in result["environment"].items():
        monkeypatch.setenv(key, value)
    assert config.evaluation_observation_binding() == {
        "marker": result["marker"], "attendee_email": "operator@example.com",
        "run_id": result["run_id"], "unit_id": result["unit_id"],
    }
    monkeypatch.setenv("WORKSHOP_UNIT_ID", "some-other-unit")
    assert config.evaluation_observation_binding() is None


def test_observation_is_explicit_opt_in_and_does_not_enable_commercial_capture():
    value = spec()
    del value["evaluation_observation"]
    env = plan(value)["environment"]
    assert env["WORKSHOP_EVALUATION_ENABLED"] == "false"
    assert "WORKSHOP_EVALUATION_ATTENDEE_EMAIL" not in env
    assert env["WORKSHOP_INSIGHT_CAPTURE"] == "false"


def test_ttl_and_cost_are_declared_bounds_not_claimed_applied_policy():
    result = plan()
    bounds = result["bounds"]
    assert bounds["attendee_count"] == 1
    assert datetime.fromisoformat(bounds["expires_at"].replace("Z", "+00:00")) == (
        NOW + timedelta(hours=1)
    )
    assert int(result["environment"]["WORKSHOP_EVENT_ENDS_AT"]) == int(NOW.timestamp()) + 3600
    assert bounds["budget_enforcement"] == "external_runner_required"
    assert bounds["native_gateway_budget_applied"] is False
    assert bounds["automatic_expiry_applied"] is False


@pytest.mark.parametrize("key,value", [
    ("marker", "normal-event"), ("marker", "wt-eval-X"),
    ("marker", "wt-eval-" + "a" * 60), ("disposable", False),
    ("attendee_mode", "event_attendee"), ("attendee_email", "not-an-email"),
    ("profile", "labs; echo secret"), ("ttl_seconds", True),
    ("ttl_seconds", 59), ("ttl_seconds", 14401), ("cost_budget_usd", True),
    ("cost_budget_usd", 0), ("cost_budget_usd", float("nan")),
    ("cost_budget_usd", float("inf")), ("workspace_id", "0"),
    ("harnesses", []), ("harnesses", ["codex", "codex"]),
    ("harnesses", ["unknown"]), ("harnesses", [["claude"]]),
    ("release", {"source_ref": "main"}), ("release", {}),
    ("onboarding_wizard", "true"), ("evaluation_observation", 1),
])
def test_invalid_or_unbounded_spec_is_rejected(key, value):
    raw = spec()
    raw[key] = value
    with pytest.raises(ValueError):
        plan(raw)


@pytest.mark.parametrize("url", [
    "http://labs.example.com", "https://secret@labs.example.com",
    "https://labs.example.com?token=private", "https://labs.example.com/path",
])
def test_workspace_identity_is_a_clean_https_origin(url):
    raw = spec()
    raw["workspace_host"] = url
    with pytest.raises(ValueError):
        plan(raw)


def test_spec_rejects_raw_secrets_and_arbitrary_environment_overrides_without_echoing():
    raw = spec()
    raw["release"]["client_secret"] = "private-credential"
    with pytest.raises(ValueError) as error:
        plan(raw)
    assert "private-credential" not in str(error.value)
    raw = spec()
    raw["environment"] = {"WORKSHOP_PAT": "private-credential"}
    with pytest.raises(ValueError):
        plan(raw)


def test_git_source_identity_and_reviewed_pins_are_independent():
    raw = spec()
    raw["release"] = {"source_kind": "git", "source_ref": "b" * 40, "source_ref_immutable": True}
    result = plan(raw)
    reviewed = manifest()
    assert result["release"] == raw["release"]
    assert result["environment"]["CLAUDE_CODE_VERSION"] == reviewed["artifacts"]["claude_binary"]["version"]
    assert result["environment"]["CODEX_CLI_VERSION"] == reviewed["artifacts"]["codex_npm_launcher_package"]["version"]
    assert result["artifact_manifest_identity"]["skills_commit"] == reviewed["artifacts"]["databricks_agent_skills"]["commit"]
    assert result["artifact_manifest_identity"]["digest_kind"] == "canonical-json-sha256"


def test_dirty_instrumented_upload_has_runtime_snapshot_identity_not_git_or_package():
    raw = spec()
    raw["release"] = {
        "source_kind": "runtime-snapshot", "source_digest": "c" * 64,
        "parent_git_sha": "b" * 40, "instrumentation": True,
        "prompt_policy_unchanged_asserted": True,
    }
    result = plan(raw)
    assert result["release"] == raw["release"]
    assert "source_ref" not in result["release"]
    assert result["provenance"]["prompt_policy_unchanged_verified"] is False
    assert result["provenance"]["deployment_applied"] is False


@pytest.mark.parametrize("field,value", [
    ("source_digest", "HEAD"), ("parent_git_sha", "main"),
    ("instrumentation", "true"), ("prompt_policy_unchanged_asserted", False),
])
def test_runtime_snapshot_cannot_replace_digest_or_silently_modify_prompt_policy(field, value):
    raw = spec()
    raw["release"] = {
        "source_kind": "runtime-snapshot", "source_digest": "c" * 64,
        "parent_git_sha": "b" * 40, "instrumentation": True,
        "prompt_policy_unchanged_asserted": True,
    }
    raw["release"][field] = value
    with pytest.raises(ValueError):
        plan(raw)


def test_app_name_budget_accepts_exact_limit_and_rejects_one_extra_character():
    raw = spec()
    raw["marker"] = "wt-eval-" + "a" * 15
    assert len(plan(raw)["names"]["app_name"]) == 26
    raw["marker"] += "a"
    with pytest.raises(ValueError):
        plan(raw)


@pytest.mark.parametrize("mutation", [
    "not_reviewed", "missing_artifact", "checksum", "source", "skills_commit",
    "claude_version", "node_version", "codex_version", "executable_checksum", "archive_path",
])
def test_incomplete_or_conflicting_reviewed_manifest_is_rejected(mutation):
    reviewed = manifest()
    if mutation == "not_reviewed":
        reviewed["reviewed"] = False
    elif mutation == "missing_artifact":
        del reviewed["artifacts"]["node_linux_x64"]
    elif mutation == "checksum":
        del reviewed["artifacts"]["tmux_linux_x64"]["sha256"]
    elif mutation == "source":
        reviewed["artifacts"]["claude_binary"]["source"] = "https://secret@host.invalid/artifact"
    elif mutation == "skills_commit":
        reviewed["artifacts"]["databricks_agent_skills"]["commit"] = "main"
    elif mutation == "claude_version":
        reviewed["artifacts"]["claude_installer"]["version"] = "1.0.0"
    elif mutation == "node_version":
        reviewed["artifacts"]["node_linux_arm64"]["version"] = "22.0.0"
    elif mutation == "codex_version":
        reviewed["artifacts"]["codex_native_package_linux_x64"]["version"] = "1.0.0-linux-x64"
    elif mutation == "executable_checksum":
        del reviewed["artifacts"]["codex_native_package_linux_x64"]["executable_sha256"]
    else:
        reviewed["artifacts"]["uv_binary"]["executable_relative_path"] = "../private"
    with pytest.raises(ValueError):
        plan_simulation(spec(), reviewed, now=NOW)


def test_plan_does_not_mutate_supplied_spec_or_manifest():
    raw, reviewed = spec(), manifest()
    original = copy.deepcopy((raw, reviewed))
    plan_simulation(raw, reviewed, now=NOW)
    assert (raw, reviewed) == original


@pytest.mark.parametrize("path", [
    "server/main.py", "server/bootstrap/artifacts.py", "static/app.js",
    "assets/artifacts/manifest.json", "assets/skills/databricks-apps/SKILL.md",
    "content/demo_seed_manifest.json", "app.yaml", "requirements.txt", "requirements.in",
])
def test_source_allowlist_keeps_required_runtime_files(path):
    assert deployment_path_allowed(path) is True


@pytest.mark.parametrize("path", [
    "evals/private.json", "tests/fixture.json", "docs/evidence/actual.json",
    "scripts/evaluate_generated_apps.py", ".env", ".git/config", "server/.env",
    "server/__pycache__/main.pyc", "assets/.git/config", "static/node_modules/foo.js",
    "requirements-dev.txt", "server/../private.json", "./server/main.py",
    "/server/main.py", "server//main.py", "server\\main.py", "server", "",
])
def test_source_allowlist_excludes_evaluation_data_private_paths_and_caches(path):
    assert deployment_path_allowed(path) is False


def test_optional_pair_preserves_observed_handoff_version_and_reports_compatibility_gap():
    raw = spec()
    raw["omnigent"] = pair()
    assert plan(raw)["omnigent_compatibility"] == "reviewed_version_match"
    raw["omnigent"]["deployment"]["server_version"] = "0.99.0"
    result = plan(raw)
    assert result["omnigent"]["deployment"]["server_version"] == "0.99.0"
    assert result["environment"]["OMNIGENT_VERSION"] == manifest()["artifacts"]["omnigent_lock"]["version"]
    assert result["omnigent_compatibility"] == "unverified"
    raw["omnigent_compatibility"] = {
        "policy_id": "reviewed-test-pair", "contract_version": "1.6", "server_version": "0.99.0",
    }
    result = plan(raw)
    assert result["omnigent_compatibility"] == "explicit_policy_asserted"
    assert result["provenance"]["control_tower_integration_qualified"] is False


def test_paired_attendee_must_match_and_harness_must_be_enabled():
    raw = spec()
    raw["omnigent"] = pair()
    raw["omnigent"]["environment"]["WORKSHOP_ATTENDEE_EMAIL"] = "another@example.com"
    with pytest.raises(ValueError):
        plan(raw)
    raw["omnigent"] = pair()
    raw["harnesses"] = ["claude", "codex"]
    with pytest.raises(ValueError):
        plan(raw)


def test_bind_numeric_app_id_uses_exact_name_and_leaves_readiness_unverified():
    original = plan()
    bound = bind_created_app(original, {
        "name": original["names"]["app_name"], "service_principal_id": 123,
        "url": "https://wt.example.com", "service_principal_client_id": "a-client-uuid",
    })
    assert bound["environment"]["WORKSHOP_APP_SP_ID"] == "123"
    assert bound["grants"]["app_service_principal"]["principal"] == "123"
    assert bound["pending_runtime_patches"] == []
    assert bound["created_app_receipt"]["independently_verified"] is False
    assert all(value is False for value in bound["provenance"].values())
    assert "WORKSHOP_APP_SP_ID" not in original["environment"]


@pytest.mark.parametrize("sp_id", [None, True, False, 0, "0", -1, "uuid", "123.0"])
def test_app_receipt_cannot_fabricate_numeric_sp_binding(sp_id):
    original = plan()
    with pytest.raises(ValueError):
        bind_created_app(original, {
            "name": original["names"]["app_name"], "service_principal_id": sp_id,
        })


def test_existing_neighbor_app_receipt_cannot_patch_simulated_target():
    with pytest.raises(ValueError, match="does not match"):
        bind_created_app(plan(), {"name": "Ai-Demo-01", "service_principal_id": 123})


def test_naive_start_time_is_rejected():
    with pytest.raises(ValueError, match="aware"):
        plan_simulation(spec(), manifest(), now=NOW.replace(tzinfo=None))
