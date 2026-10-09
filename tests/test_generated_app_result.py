"""Synthetic SQL/UI result-command boundaries; no workspace or real app calls."""
import asyncio
import copy
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from evals.generated_apps import bakery_fixture as fixture
from evals.generated_apps.mlflow_evaluation import plan_quality_evaluation
from evals.generated_apps.adapters.browser import GeneratedAppBrowserVerifier
from scripts import verify_generated_app_result as runner
from tests.test_generated_app_journey_command import Client as TerminalClient, fixture as terminal_fixture


GENERATED_ID = "33333333-3333-4333-8333-333333333333"


def documents(tmp_path, *, fixture_version=fixture.VERSION):
    terminal, files, now = terminal_fixture()
    terminal_path = tmp_path / "terminal.json"
    terminal_path.write_text(json.dumps(terminal))
    binding = runner.validate_receipt(terminal, now=now)
    plan = fixture.build_plan(binding, deployment_receipt_sha256=hashlib.sha256(terminal_path.read_bytes()).hexdigest(),
                              epoch=now - timedelta(seconds=30), fixture_version=fixture_version)
    seed = {"schema_version": 1, "operation": "seed_bakery_fixture", "scope": "simulated_control_tower", "control_tower_requests": 0,
            "status": "seed_verified", "executed": True, "accepted": False, "plan": plan, "warehouse_id": "warehouse-1",
            "seeded_at": (now - timedelta(seconds=10)).isoformat(), "data_readback": {"verified": True},
            "grant_readback": {"table": {"principal": plan["principal"]}},
            "table_identity": {"table_id": "seed-table-id", "full_name": plan["table"], "catalog_name": plan["catalog"],
                               "schema_name": plan["schema"], "owner": plan["attendee_email"]}}
    candidate = {"app_id": GENERATED_ID, "app_name": "bakery", "deployment_id": "generated-deploy-1", "creator": plan["principal"],
                 "namespace_marker_observed": False, "unique_terminal_sp_creator": True,
                 "source_code_path": "/Workspace/Users/operator/bakery", "deployment_state": "SUCCEEDED", "app_state": "RUNNING",
                 "url": "https://bakery-123.aws.databricksapps.com"}
    target = {key: binding[key] for key in ("marker", "workspace_host", "workspace_id", "deployment_id", "source_path", "attendee")}
    target.update(app_id=binding["app"]["id"], app_name=binding["app"]["name"], app_url=binding["app"]["url"])
    journey = {"schema_version": 1, "scope": "simulated_control_tower", "control_tower_requests": 0, "accepted": False,
               "mode": "build", "status": "journey_finished", "target": target,
               "deployment_verification": {"verified": True, "app_id": binding["app"]["id"], "deployment_id": binding["deployment_id"],
                                           "runtime_snapshot": {"manifest_digest": binding["manifest"]["digest"]}},
               "app_discovery": {"status": "deployment_observed", "unattributed_new_attendee_apps": 0, "candidates": [candidate]},
               "journey": {"app_observations": [{"status": "deployment_observed", "app_url": candidate["url"],
                                                 "artifacts": ["workspace-app:" + GENERATED_ID], "source": "live workspace app inventory difference"}]}}
    journey_path, seed_path = tmp_path / "journey.json", tmp_path / "seed.json"
    journey_path.write_text(json.dumps(journey))
    seed_path.write_text(json.dumps(seed))
    inputs = runner.load_inputs(terminal_path, journey_path, seed_path)
    aria, screenshot = tmp_path / "calibration.aria.txt", tmp_path / "calibration.png"
    aria.write_text("- heading: Bakery orders\n- row: BK-1001 Maya Patel Unpacked Pack")
    screenshot.write_bytes(b"synthetic actual-UI-calibration test image")
    def role(name, kind="cell"):
        return {"role": kind, "name": name, "exact": True}
    task = {"schema_version": 1, "operation": "generated_app_packed_task", "backend_kind": "seeded_unity_catalog",
            "input_sha256": inputs["input_sha256"], "record_id": "BK-1001", "page_ready": role("Bakery orders", "heading"),
            "record_scope": {"role": "row", "anchor": role("BK-1001")}, "before_status": role("Unpacked"),
            "packed_action": role("Pack", "button"), "after_condition": {"kind": "visible_status", "target": role("Packed")},
            "ui_calibration": {"source": "actual_generated_app_ui", "reviewed_by": "synthetic operator", "observed_at": (now - timedelta(seconds=1)).isoformat(),
                               "app_id": GENERATED_ID, "deployment_id": candidate["deployment_id"],
                               "artifacts": [{"kind": kind, "path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                             for kind, path in (("screenshot", screenshot), ("accessibility_snapshot", aria))]}}
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps(task))
    return NS(terminal=terminal, files=files, now=now, inputs=inputs, task=task, terminal_path=terminal_path,
              journey=journey, journey_path=journey_path, seed_path=seed_path, task_path=task_path)


def arguments(value, tmp_path, *, task=True):
    args = ["--terminal-receipt", str(value.terminal_path), "--journey", str(value.journey_path), "--seed-receipt", str(value.seed_path),
            "--output", str(tmp_path / "result.json"), "--artifacts", str(tmp_path / "artifacts")]
    return args + (["--task-plan", str(value.task_path)] if task else [])


def test_default_plan_uses_only_local_actual_bound_inputs(tmp_path, monkeypatch):
    value = documents(tmp_path)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("default plan must not construct SDK/browser")
    monkeypatch.setattr(runner, "workspace_client", forbidden)
    monkeypatch.setattr(runner, "AuthenticatedBrowser", forbidden)
    assert runner.main(arguments(value, tmp_path)) == 0
    report = json.loads((tmp_path / "result.json").read_text())
    assert report["status"] == "planned" and report["executed"] is False and report["accepted"] is False
    assert report["input_sha256"] == value.inputs["input_sha256"]
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize("version", [fixture.LEGACY_VERSION, fixture.HEX_VALUES_VERSION, fixture.VERSION])
def test_verified_seed_receipt_binds_its_explicit_fixture_version(tmp_path, version):
    value = documents(tmp_path, fixture_version=version)
    inputs = runner.load_inputs(value.terminal_path, value.journey_path, value.seed_path)
    assert inputs["seed"]["plan"]["fixture_version"] == version
    assert inputs["seed"]["plan"] == value.inputs["seed"]["plan"]


@pytest.mark.parametrize("change", [None, "mutations", "bounds", "previous_deployment", "package_changed", "boolean_mutations", "missing_previous_deployment", "invalid_digest"])
def test_read_only_seed_continuation_preserves_qualified_observer_binding(tmp_path, change):
    value = documents(tmp_path)
    terminal = value.terminal
    terminal.update(previous_deployment={"deployment_id": "previous-observer"},
                    observer_update={"package_bytes_unchanged": change != "package_changed"})
    value.terminal_path.write_text(json.dumps(terminal))
    binding = runner.validate_receipt(terminal, now=value.now)
    seed = value.inputs["seed"]
    seed["plan"] = fixture.build_plan(binding, deployment_receipt_sha256=hashlib.sha256(value.terminal_path.read_bytes()).hexdigest(),
                                      epoch=seed["plan"]["epoch"], fixture_version=seed["plan"]["fixture_version"])
    seed.update(operation="rebind_bakery_fixture", fixture_continuation={
        "previous_seed_sha256": "a" * 64, "previous_deployment_id": "other" if change == "previous_deployment" else "previous-observer",
        "workspace_mutations": 1 if change == "mutations" else 0, "original_bounds_and_epoch_preserved": change != "bounds"})
    if change == "boolean_mutations": seed["fixture_continuation"]["workspace_mutations"] = False
    if change == "missing_previous_deployment":
        seed["fixture_continuation"].pop("previous_deployment_id")
    if change == "invalid_digest": seed["fixture_continuation"]["previous_seed_sha256"] = "unverified"
    value.seed_path.write_text(json.dumps(seed))
    if change is None:
        assert runner.load_inputs(value.terminal_path, value.journey_path, value.seed_path)["seed"]["plan"] == seed["plan"]
    else:
        with pytest.raises(runner.ResultError, match="read_only_seed_continuation_required"):
            runner.load_inputs(value.terminal_path, value.journey_path, value.seed_path)


@pytest.mark.parametrize("change,reason", [("terminal_bytes", "seed_terminal_receipt_binding_changed"),
    ("multiple_candidates", "one_observed_generated_app_required"), ("unrelated_creator", "generated_app_creator_unverified"),
    ("wrong_workspace_url", "generated_app_url_workspace_mismatch"), ("uncorrelated_discovery", "journey_discovery_correlation_required")])
def test_receipt_discovery_and_exact_byte_bindings_reject_replacements(tmp_path, change, reason):
    value = documents(tmp_path)
    if change == "terminal_bytes": value.terminal_path.write_text(value.terminal_path.read_text() + "\n")
    else:
        journey = value.journey
        candidate = journey["app_discovery"]["candidates"][0]
        if change == "multiple_candidates": journey["app_discovery"]["candidates"].append(dict(candidate))
        if change == "unrelated_creator":
            candidate["creator"] = "someone@example.com"
            candidate["app_name"] = value.inputs["binding"]["marker"] + "-bakery"
        if change == "wrong_workspace_url": candidate["url"] = "https://bakery-999.aws.databricksapps.com"
        if change == "uncorrelated_discovery": journey["journey"]["app_observations"] = []
        value.journey_path.write_text(json.dumps(journey))
    with pytest.raises(runner.ResultError, match=reason):
        runner.load_inputs(value.terminal_path, value.journey_path, value.seed_path)


@pytest.mark.parametrize("change", ["input_digest", "deployment", "unseeded_record", "packed_record", "scope", "nonexact_status",
    "action_script", "button_postcondition", "same_before_after", "changed_calibration", "extra_private_field"])
def test_private_reviewed_task_requires_actual_calibration_and_exact_scoped_action(tmp_path, change):
    value = documents(tmp_path)
    task = copy.deepcopy(value.task)
    if change == "input_digest": task["input_sha256"]["seed_receipt"] = "a" * 64
    if change == "deployment": task["ui_calibration"]["deployment_id"] = "other"
    if change == "unseeded_record": task["record_id"] = "unrelated"
    if change == "packed_record": task["record_id"] = "BK-1003"
    if change == "scope": task["record_scope"]["anchor"]["name"] = "all orders"
    if change == "nonexact_status": task["after_condition"]["target"]["exact"] = False
    if change == "action_script": task["packed_action"]["operation"] = "evaluate_script"
    if change == "button_postcondition": task["after_condition"]["target"]["role"] = "button"
    if change == "same_before_after": task["after_condition"]["target"]["name"] = "Unpacked"
    if change == "changed_calibration": Path(task["ui_calibration"]["artifacts"][0]["path"]).write_bytes(b"changed")
    if change == "extra_private_field": task["builder_prompt"] = "Repair the app"
    with pytest.raises((runner.ResultError, ValueError)):
        runner.validate_task_plan(task, value.inputs)


@pytest.mark.parametrize("collision", ["output", "temporary", "artifacts", "auth_alias", "calibration_alias"])
def test_fresh_outputs_never_overwrite_inputs_auth_or_calibration(tmp_path, collision):
    value = documents(tmp_path)
    output, artifacts = tmp_path / "result.json", tmp_path / "artifacts"
    paths = [value.seed_path]
    if collision == "output": output.write_text("keep")
    if collision == "temporary": output.with_name(output.name + ".tmp").write_text("keep")
    if collision == "artifacts": artifacts.mkdir()
    if collision == "auth_alias": output.symlink_to(value.seed_path)
    if collision == "calibration_alias": artifacts = Path(value.task["ui_calibration"]["artifacts"][0]["path"])
    with pytest.raises(runner.ResultError, match="fresh_distinct"):
        runner.fresh_paths(output, artifacts, paths)
    assert json.loads(value.seed_path.read_text())["status"] == "seed_verified"


def test_execute_without_genuine_private_browser_state_blocks_before_sdk(tmp_path, monkeypatch):
    value = documents(tmp_path)
    monkeypatch.setattr(runner, "workspace_client", lambda *_: (_ for _ in ()).throw(AssertionError("no SDK")))
    assert runner.main(arguments(value, tmp_path) + ["--execute"]) == 2
    report = json.loads((tmp_path / "result.json").read_text())
    assert report["reason"] == "genuine_private_browser_state_required" and report["accepted"] is False


@pytest.mark.parametrize("change", ["public_permissions", "symlink", "hardlink", "inside_repository"])
def test_authentication_state_must_be_private_distinct_external_file(tmp_path, monkeypatch, change):
    state = tmp_path / "auth.json"
    state.write_text('{"cookies": [{"value": "private-auth-cookie"}]}')
    state.chmod(0o600)
    runner.private_browser_state(state)
    selected = state
    if change == "public_permissions": state.chmod(0o644)
    if change == "symlink": selected = tmp_path / "alias.json"; selected.symlink_to(state)
    if change == "hardlink": os.link(state, tmp_path / "alias.json")
    if change == "inside_repository": monkeypatch.setattr(runner, "ROOT", tmp_path)
    with pytest.raises(runner.ResultError, match="genuine_private_browser_state_required"):
        runner.private_browser_state(selected)
    assert json.loads(state.read_text())["cookies"][0]["value"] == "private-auth-cookie"


class Client(TerminalClient):
    def __init__(self, value):
        super().__init__(value.terminal, value.files, value.now)
        self.plan = value.inputs["seed"]["plan"]
        self.row = next(row for row in fixture.expected_rows(self.plan) if row["order_id"] == "BK-1001")
        c = value.inputs["candidate"]
        self.generated_deployment = NS(deployment_id=c["deployment_id"], status=NS(state="SUCCEEDED"),
            create_time=(value.now - timedelta(seconds=4)).isoformat(), source_code_path=c["source_code_path"])
        self.generated = NS(id=c["app_id"], name=c["app_name"], creator=c["creator"], url=c["url"],
            create_time=(value.now - timedelta(seconds=5)).isoformat(), active_deployment=self.generated_deployment,
            app_status=NS(state="RUNNING"), compute_status=NS(state="ACTIVE"))
        self.app_by_name[c["app_name"]] = self.generated
        self.inventory.append(self.generated)
        original = self.get_deployment
        self.apps.get_deployment = lambda name, deployment_id: self.generated_deployment if name == self.generated.name else original(name, deployment_id)
        identity = value.inputs["seed"]["table_identity"]
        self.tables = NS(get=lambda name: NS(**identity))
        self.grants = NS(get=lambda kind, name: NS(privilege_assignments=[NS(principal=self.plan["principal"],
            privileges=["USE_SCHEMA"] if kind == "schema" else ["SELECT", "MODIFY"])]))
        self.queries = []
        self.revert_on_read = None
        self.timeout_on_read = None
        self.corrupt_on_read = None
        self.statement_execution = NS(execute_statement=self.query)

    def query(self, **kwargs):
        assert kwargs["statement"].startswith("SELECT") and "WHERE order_id = " in kwargs["statement"]
        self.queries.append(kwargs["statement"])
        if len(self.queries) == self.timeout_on_read:
            raise TimeoutError("private SQL timeout body")
        if len(self.queries) == self.corrupt_on_read:
            self.row["quantity"] += 1
        if self.revert_on_read is not None and len(self.queries) >= self.revert_on_read:
            self.row["packed"] = False
        row = dict(self.row)
        return NS(statement_id="sql-" + str(len(self.queries)), status=NS(state="SUCCEEDED"),
            manifest=NS(schema=NS(columns=[NS(name=key) for key in row]), truncated=False, total_chunk_count=1, total_row_count=1),
            result=NS(data_array=[list(row.values())], next_chunk_index=None))


class Locator:
    def __init__(self, page, role, name=None, exact=True):
        self.page, self.role, self.name, self.exact = page, role, name, exact
    def filter(self, *, has):
        assert has.name == "BK-1001" and has.exact is True
        return self
    def get_by_role(self, role, *, name, exact):
        assert self.role == "row" and exact is True
        return Locator(self.page, role, name, exact)
    async def count(self):
        if self.role == "password": return 0
        if self.role == "row" and self.page.browser.remove_packed and self.page.ui_packed: return 0
        return self.page.browser.scope_count if self.role == "row" else 1
    async def wait_for(self, *, state, **_kwargs):
        if self.role == "heading" and self.name == "Bakery orders": return
        if self.role == "row" and state == "hidden" and self.page.browser.remove_packed and self.page.ui_packed: return
        visible = self.name == ("Packed" if self.page.ui_packed else "Unpacked")
        if state != "visible" or not visible:
            raise TimeoutError("synthetic assertion absent")
    async def click(self, **_kwargs):
        assert self.role == "button" and self.name == "Pack" and self.exact is True
        self.page.browser.clicks += 1
        self.page.ui_packed = self.page.browser.ui_updates
        if self.page.browser.backend_updates:
            self.page.browser.client.row["packed"] = True
    async def aria_snapshot(self, **_kwargs):
        if self.page.browser.remove_packed and self.page.ui_packed: return "- heading: Bakery orders"
        return "- heading: Bakery orders\n- row: BK-1001 Maya Patel " + ("Packed" if self.page.ui_packed else "Unpacked")


class Page:
    def __init__(self, browser, context):
        self.browser, self.context = browser, context
        self.ui_packed = browser.client.row["packed"] or browser.optimistic_persistence and browser.clicks > 0
        if context is not browser.context and browser.fresh_ui_failure:
            self.ui_packed = False
        self.url, self.width = browser.client.generated.url, 1440
    def on(self, *_args): pass
    def get_by_role(self, role, *, name=None, exact=True): return Locator(self, role, name, exact)
    def locator(self, name):
        if name == "input[type='password']": return Locator(self, "password")
        assert name == "body"
        return Locator(self, "body")
    async def goto(self, url, **_kwargs): self.url = url; return NS(status=200)
    async def reload(self, **_kwargs):
        self.ui_packed = (self.browser.client.row["packed"] or self.browser.optimistic_persistence and self.browser.clicks > 0) and not self.browser.reload_ui_failure
    async def set_viewport_size(self, value): self.width = value["width"]
    async def screenshot(self, *, path, **_kwargs): Path(path).write_bytes(b"synthetic rendered viewport")
    async def evaluate(self, _script): return {"viewport": self.width, "content": self.width, "horizontalOverflow": False}


class Browser:
    def __init__(self, client, *, backend_updates=True, ui_updates=True, scope_count=1, remove_packed=False, optimistic_persistence=False,
                 reload_ui_failure=False, fresh_ui_failure=False):
        self.client, self.backend_updates, self.ui_updates, self.scope_count = client, backend_updates, ui_updates, scope_count
        self.clicks, self.contexts, self.closed = 0, [], False
        self.remove_packed = remove_packed
        self.optimistic_persistence = optimistic_persistence
        self.reload_ui_failure, self.fresh_ui_failure = reload_ui_failure, fresh_ui_failure
        self.context = NS()
        self.page = Page(self, self.context)
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): self.closed = True
    async def new_context(self):
        context = NS()
        async def new_page(): return Page(self, context)
        async def close(): pass
        context.new_page, context.close = new_page, close
        self.contexts.append(context)
        return context


def run_result(value, tmp_path, monkeypatch, **browser_options):
    client = Client(value)
    browser = Browser(client, **browser_options)
    kwargs = []
    def create_browser(state, **options):
        assert state == "private-state-outside-evidence"
        kwargs.append(options)
        return browser
    monkeypatch.setattr(runner, "AuthenticatedBrowser", create_browser)
    args = NS(output=str(tmp_path / "result.json"), artifacts=str(tmp_path / "artifacts"), browser_state="private-state-outside-evidence",
              show_browser=False, timeout_seconds=30, action_timeout_seconds=.001)
    report = {"accepted": False}
    return client, browser, args, report, kwargs


def test_actual_task_transition_reload_and_fresh_context_still_cannot_claim_acceptance(tmp_path, monkeypatch):
    value = documents(tmp_path)
    client, browser, args, report, kwargs = run_result(value, tmp_path, monkeypatch)
    asyncio.run(runner.execute(args, value.inputs, value.task, report, client=client))
    assert browser.clicks == 1 and browser.closed and len(browser.contexts) == 1
    assert kwargs[0]["clear_app_storage_origins"] == ["https://bakery-123.aws.databricksapps.com"]
    assert report["observations"]["pack_transition"]["passed"] is True
    assert report["assessment"]["critical_user_task_verified"] is True
    assert report["assessment"]["reload_and_fresh_context_persistence_verified"] is True
    assert report["assessment"]["process_restart_verified"] is False and report["accepted"] is False
    assert report["verdict"] == "unverified" and client.queries and all(query.startswith("SELECT") for query in client.queries)
    for phase in ("first_preview", "final"):
        assert [capture["width"] for capture in report[phase]["viewports"]] == [1440, 768, 390]
        assert all(Path(capture["screenshot"]).is_file() and capture["accessibility_snapshot_text"] for capture in report[phase]["viewports"])
    assert "private-state-outside-evidence" not in json.dumps(report)
    exported = runner.quality_export(report, args.output)
    quality = plan_quality_evaluation(run_id="test-result", scenario_id="novice-bakery-order-queue-v1", experiment_id="123",
        dataset_name="bakery_result", judge_model="databricks:/judge", public_input={"message": "Help with bakery orders", "persona": "business", "entry_path": "skip_wizard"},
        app_evidence=exported)
    assert {batch.family for batch in quality.batches} == {"first_preview", "final"}
    assert "final_visual_quality" in quality.unverified_checks


@pytest.mark.parametrize("backend_updates,ui_updates", [(False, True), (True, False)])
def test_ui_and_backend_contradiction_is_preserved_as_failed_task(tmp_path, monkeypatch, backend_updates, ui_updates):
    value = documents(tmp_path)
    client, browser, args, report, _ = run_result(value, tmp_path, monkeypatch, backend_updates=backend_updates, ui_updates=ui_updates)
    asyncio.run(runner.execute(args, value.inputs, value.task, report, client=client))
    assert browser.clicks == 1 and report["verdict"] == "failed" and report["accepted"] is False
    assert report["assessment"]["critical_user_task_verified"] is False
    assert report["observations"]["backend_reads"][0]["observation"]["rows"][0]["packed"] is False
    export = runner.quality_export(report, args.output)
    assert any(task["passed"] is False for task in export["final"]["task_results"])


@pytest.mark.parametrize("backend_updates", [True, False])
def test_reviewed_attention_queue_removal_still_requires_real_storage_update(tmp_path, monkeypatch, backend_updates):
    value = documents(tmp_path)
    value.task["after_condition"] = {"kind": "record_absent"}
    client, browser, args, report, _ = run_result(value, tmp_path, monkeypatch, remove_packed=True, backend_updates=backend_updates)
    asyncio.run(runner.execute(args, value.inputs, value.task, report, client=client))
    assert browser.clicks == 1 and report["accepted"] is False
    assert report["assessment"]["critical_user_task_verified"] is backend_updates
    assert report["verdict"] == ("unverified" if backend_updates else "failed")


@pytest.mark.parametrize("revert_on_read", [3, 4])
def test_mlflow_export_keeps_reload_or_fresh_backend_loss_when_ui_stays_packed(tmp_path, monkeypatch, revert_on_read):
    value = documents(tmp_path)
    client, browser, args, report, _ = run_result(value, tmp_path, monkeypatch, optimistic_persistence=True)
    client.revert_on_read = revert_on_read
    asyncio.run(runner.execute(args, value.inputs, value.task, report, client=client))
    assert browser.clicks == 1 and report["observations"]["pack_transition"]["passed"] is True
    assert all(task["passed"] for task in report["final"]["task_results"])
    assert report["observations"]["persistence"]["fresh_context_backend_verified"] is False
    assert report["verdict"] == "failed" and report["accepted"] is False
    export = runner.quality_export(report, args.output)
    failed = {task["name"] for task in export["final"]["task_results"] if task["passed"] is False}
    assert "independent storage in fresh context" in failed
    assert ("independent storage after reload" in failed) is (revert_on_read == 3)
    assert "independent reload and fresh-context storage outcome" in failed


def test_end_binding_invalidation_blocks_current_task_persistence_and_mlflow_claims(tmp_path, monkeypatch):
    value = documents(tmp_path)
    client, browser, args, report, _ = run_result(value, tmp_path, monkeypatch)
    original = runner.verify_generated_app
    calls = []
    def change_at_end(inputs, sdk):
        calls.append(True)
        if len(calls) > 1:
            raise runner.ResultError("live_generated_deployment_changed")
        return original(inputs, sdk)
    monkeypatch.setattr(runner, "verify_generated_app", change_at_end)
    with pytest.raises(runner.ResultError, match="live_generated_deployment_changed"):
        asyncio.run(runner.execute(args, value.inputs, value.task, report, client=client))
    assert browser.clicks == 1 and report["observations"]["persistence"]["persistence_verified"] is True
    assessment = runner.assess_observations(report)
    assert assessment["critical_user_task_verified"] is False and assessment["reload_and_fresh_context_persistence_verified"] is False
    exported = runner.quality_export(report, args.output)
    assert exported["deployment"]["new_app_verified"] is False and exported["accepted"] is False


@pytest.mark.parametrize("phase,timeout_on_read", [("reload", 3), ("fresh_context", 4)])
def test_known_ui_failure_survives_following_independent_sql_timeout(tmp_path, monkeypatch, phase, timeout_on_read):
    value = documents(tmp_path)
    client, browser, args, report, _ = run_result(value, tmp_path, monkeypatch,
        reload_ui_failure=phase == "reload", fresh_ui_failure=phase == "fresh_context")
    client.timeout_on_read = timeout_on_read
    with pytest.raises(TimeoutError):
        asyncio.run(runner.execute(args, value.inputs, value.task, report, client=client))
    persistence = report["observations"]["persistence"]
    assert persistence["reload_passed" if phase == "reload" else "fresh_context_passed"] is False
    assert persistence["reload_backend_verified" if phase == "reload" else "fresh_context_backend_verified"] is None
    assert runner.assess_observations(report)["verdict"] == "failed" and report["accepted"] is False
    export = runner.quality_export(report, args.output)
    assert any(task["passed"] is False for task in export["final"]["task_results"])
    assert any("Unpacked" in viewport.get("accessibility_snapshot_text", "") for viewport in report["final"]["viewports"])
    assert browser.clicks == 1 and browser.closed and "private SQL timeout body" not in json.dumps(report)


def test_packing_that_corrupts_unrelated_order_details_is_a_preserved_failed_observation(tmp_path, monkeypatch):
    value = documents(tmp_path)
    client, browser, args, report, _ = run_result(value, tmp_path, monkeypatch)
    client.corrupt_on_read = 2
    asyncio.run(runner.execute(args, value.inputs, value.task, report, client=client))
    transition = report["observations"]["pack_transition"]
    assert transition["passed"] is False and transition["reason"] == "unrelated_order_fields_changed"
    assert transition["before"] is False and transition["after"] is True and transition["changed_unrelated_fields"] == ["quantity"]
    assert browser.clicks == 1 and len(client.queries) == 2 and report["verdict"] == "failed" and report["accepted"] is False
    exported = runner.quality_export(report, args.output)
    storage = next(task for task in exported["final"]["task_results"] if task["name"] == "independent seeded storage packed update")
    assert storage["passed"] is False and "unrelated target-order fields: quantity" in storage["observed_result"]


@pytest.mark.parametrize("change", ["already_packed", "ambiguous_scope", "changed_source", "replaced_deployment"])
def test_changed_or_ambiguous_preconditions_never_trigger_repair_or_click(tmp_path, monkeypatch, change):
    value = documents(tmp_path)
    client, browser, args, report, _ = run_result(value, tmp_path, monkeypatch, scope_count=2 if change == "ambiguous_scope" else 1)
    if change == "already_packed": client.row["packed"] = True
    if change == "changed_source": client.generated_deployment.source_code_path = "/Workspace/Shared/unrelated"
    if change == "replaced_deployment": client.generated.active_deployment = NS(deployment_id="replacement")
    with pytest.raises((runner.ResultError, ValueError)):
        asyncio.run(runner.execute(args, value.inputs, value.task, report, client=client))
    assert browser.clicks == 0 and report["accepted"] is False
    assert all(query.startswith("SELECT") for query in client.queries)


def test_empty_or_early_stopped_task_results_cannot_pass():
    assert not runner.actions_passed([], 2)
    assert not runner.actions_passed([{"passed": True}], 2)
    assert not runner.actions_passed([{"passed": True}, {"passed": False}], 2)


def test_unsupported_backend_stays_unverified_before_browser_or_sql(tmp_path, monkeypatch):
    value = documents(tmp_path)
    value.task["backend_kind"] = "unsupported"
    client, browser, args, report, _ = run_result(value, tmp_path, monkeypatch)
    with pytest.raises(runner.ResultError, match="backend_oracle_unsupported_unverified"):
        asyncio.run(runner.execute(args, value.inputs, value.task, report, client=client))
    assert browser.clicks == 0 and not client.queries and report["accepted"] is False


@pytest.mark.parametrize("login_state", ["external_origin", "password_field"])
def test_capture_does_not_record_auth_pages_or_password_fields(tmp_path, login_state):
    value = documents(tmp_path)
    client = Client(value)
    browser = Browser(client)
    page = browser.page
    if login_state == "external_origin": page.url = "https://login.example.invalid/?private=password"
    else:
        original = page.locator
        class Password:
            async def count(self): return 1
        page.locator = lambda name: Password() if name == "input[type='password']" else original(name)
    verifier = GeneratedAppBrowserVerifier(page, artifact_dir=tmp_path / "captures")
    observed = asyncio.run(runner.captures(verifier, "final", client.generated.url))
    assert len(observed) == 3 and all(item["status"] == "capture_failed" for item in observed)
    assert all(item["reason"] == "generated_browser_origin_or_password_unverified" for item in observed)
    assert not list((tmp_path / "captures").iterdir()) and "private=password" not in json.dumps(observed)


def test_failure_report_preserves_actual_task_contradiction_and_sanitized_mlflow_input(tmp_path, monkeypatch):
    value = documents(tmp_path)
    state = tmp_path / "private-auth.json"
    state.write_text('{"cookies": [{"value": "private-auth-cookie"}]}')
    state.chmod(0o600)
    async def fail_after_observations(args, inputs, task, report):
        report["deployment"] = {"new_app_verified": True, "app_name": "bakery", "deployment_id": "generated-deploy-1"}
        report["observations"] = {"pack_transition": {"passed": False}}
        report["final"] = {"independently_observed": True, "viewports": [], "task_results": [{"name": "actual UI packed status", "passed": True}]}
        raise RuntimeError("Bearer private-auth-error-body")
    monkeypatch.setattr(runner, "execute", fail_after_observations)
    assert runner.main(arguments(value, tmp_path) + ["--execute", "--browser-state", str(state)]) == 2
    report = json.loads((tmp_path / "result.json").read_text())
    assert report["verdict"] == "failed" and report["accepted"] is False
    assert report["observations"]["pack_transition"]["passed"] is False
    assert report["mlflow_browser_input"]["final"]["task_results"][-1]["passed"] is False
    assert "private-auth-cookie" not in json.dumps(report) and "private-auth-error-body" not in json.dumps(report)
    assert str(state) not in json.dumps(report)
