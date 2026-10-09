#!/usr/bin/env python3
"""Independently observe one generated bakery app; full acceptance stays false.

Default validates local receipts and an optional reviewed actual-UI task plan.
--execute reverifies workspace/app identities, clicks the one reviewed packed
control, reads actual seeded UC storage, and checks reload/fresh-context state.
No Control Tower, builder prompt, data reset, app deployment or restart is used.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.generated_apps import bakery_fixture as fixture
from evals.generated_apps.adapters.browser import AuthenticatedBrowser, BrowserAction, BrowserJourneyError, GeneratedAppBrowserVerifier, RoleLocator
from evals.generated_apps.report import write_evidence
from scripts.run_generated_app_journey import canonical_path, enum, timestamp, validate_receipt, verify_deployment, workspace_client
from scripts.seed_generated_app_fixture import SqlBackendOracle, verify_grants


class ResultError(ValueError):
    """Stable code only; never disclose SDK bodies or authentication state."""


def require(condition, code):
    if not condition:
        raise ResultError(code)


def load_json(path, *, limit=2 * 1024 * 1024):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= limit, "bounded_regular_input_required")
    content = path.read_bytes()
    payload = json.loads(content)
    require(isinstance(payload, dict), "object_input_required")
    return payload, hashlib.sha256(content).hexdigest()


def generated_url(candidate, binding):
    value = candidate.get("url", "")
    require(isinstance(value, str), "generated_app_url_workspace_mismatch")
    parsed = urlsplit(value)
    require(isinstance(value, str) and len(value) <= 2048 and parsed.scheme == "https"
            and parsed.hostname and parsed.hostname.endswith(".databricksapps.com")
            and ("-" + binding["workspace_id"] + ".") in parsed.hostname
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path in {"", "/"}, "generated_app_url_workspace_mismatch")
    return value.rstrip("/")


def load_inputs(terminal_path, journey_path, seed_path):
    terminal, terminal_hash = load_json(terminal_path)
    binding = validate_receipt(terminal)
    journey, journey_hash = load_json(journey_path)
    seed, seed_hash = load_json(seed_path)
    require(type(seed.get("schema_version")) is int and seed["schema_version"] == 1 and seed.get("operation") in {"seed_bakery_fixture", "rebind_bakery_fixture"}
            and seed.get("scope") == "simulated_control_tower" and seed.get("control_tower_requests") == 0
            and seed.get("status") == "seed_verified" and seed.get("executed") is True
            and seed.get("accepted") is False, "verified_seed_receipt_required")
    if seed["operation"] == "rebind_bakery_fixture":
        continuation = seed.get("fixture_continuation", {})
        require(type(continuation.get("workspace_mutations")) is int and continuation["workspace_mutations"] == 0
                and continuation.get("original_bounds_and_epoch_preserved") is True
                and isinstance(continuation.get("previous_seed_sha256"), str)
                and re.fullmatch(r"[a-f0-9]{64}", continuation["previous_seed_sha256"])
                and isinstance(continuation.get("previous_deployment_id"), str) and continuation["previous_deployment_id"]
                and continuation.get("previous_deployment_id") == terminal.get("previous_deployment", {}).get("deployment_id")
                and terminal.get("observer_update", {}).get("package_bytes_unchanged") is True,
                "read_only_seed_continuation_required")
    plan = fixture.validate_plan(seed.get("plan"))
    require(plan == fixture.build_plan(binding, deployment_receipt_sha256=terminal_hash, epoch=plan["epoch"],
                                      fixture_version=plan["fixture_version"]),
            "seed_terminal_receipt_binding_changed")
    require(binding["created_at"] <= timestamp(seed.get("seeded_at")) < binding["expires_at"], "seed_time_binding_unverified")
    require(seed.get("data_readback", {}).get("verified") is True and seed.get("grant_readback")
            and isinstance(seed.get("warehouse_id"), str) and seed["warehouse_id"]
            and isinstance(seed.get("table_identity"), dict) and seed["table_identity"].get("full_name") == plan["table"]
            and seed["table_identity"].get("table_id"), "verified_seed_storage_binding_required")
    require(type(journey.get("schema_version")) is int and journey["schema_version"] == 1 and journey.get("scope") == "simulated_control_tower"
            and journey.get("control_tower_requests") == 0 and journey.get("accepted") is False
            and journey.get("mode") == "build" and journey.get("status") == "journey_finished", "actual_build_journey_required")
    target = journey.get("target", {})
    for key in ("marker", "workspace_host", "workspace_id", "deployment_id", "source_path", "attendee"):
        require(target.get(key) == binding[key], "journey_terminal_binding_changed")
    require(target.get("app_id") == binding["app"]["id"] and target.get("app_name") == binding["app"]["name"]
            and target.get("app_url") == binding["app"]["url"], "journey_terminal_binding_changed")
    observed = journey.get("deployment_verification", {})
    require(observed.get("verified") is True and observed.get("app_id") == binding["app"]["id"]
            and observed.get("deployment_id") == binding["deployment_id"]
            and observed.get("runtime_snapshot", {}).get("manifest_digest") == binding["manifest"]["digest"],
            "journey_terminal_verification_required")
    discovery = journey.get("app_discovery", {})
    require(discovery.get("status") == "deployment_observed" and discovery.get("unattributed_new_attendee_apps") == 0
            and isinstance(discovery.get("candidates"), list) and len(discovery["candidates"]) == 1,
            "one_observed_generated_app_required")
    candidate = discovery["candidates"][0]
    fixture.validate_generated_binding(candidate, plan)
    creator = candidate["creator"].casefold()
    marker_bound = candidate["app_name"].startswith(binding["marker"] + "-") or binding["marker"] in PurePosixPath(candidate["source_code_path"]).parts
    require(creator == plan["principal"].casefold() or creator == plan["attendee_email"].casefold() and marker_bound,
            "generated_app_creator_unverified")
    require(candidate.get("deployment_state") == "SUCCEEDED" and candidate.get("app_state") == "RUNNING",
            "observed_successful_generated_deployment_required")
    generated_url(candidate, binding)
    app_observations = journey.get("journey", {}).get("app_observations", [])
    require(any(item.get("status") == "deployment_observed" and item.get("app_url") == candidate["url"]
                and item.get("artifacts") == ["workspace-app:" + candidate["app_id"]]
                and item.get("source") == "live workspace app inventory difference" for item in app_observations),
            "journey_discovery_correlation_required")
    return {"binding": binding, "journey": journey, "seed": seed, "candidate": candidate,
            "input_sha256": {"terminal_receipt": terminal_hash, "journey": journey_hash, "seed_receipt": seed_hash}}


def locator(value, *, roles):
    require(isinstance(value, dict) and set(value) == {"role", "name", "exact"}
            and value["role"] in roles and value["exact"] is True
            and isinstance(value["name"], str) and 0 < len(value["name"]) <= 500 and value["name"].strip(),
            "exact_bounded_role_locator_required")
    return RoleLocator(value["role"], value["name"], exact=True)


def validate_task_plan(task, inputs):
    required = {"schema_version", "operation", "backend_kind", "input_sha256", "ui_calibration", "record_id",
                "page_ready", "record_scope", "before_status", "packed_action", "after_condition"}
    require(isinstance(task, dict) and set(task) == required and type(task["schema_version"]) is int and task["schema_version"] == 1
            and task["operation"] == "generated_app_packed_task"
            and task["backend_kind"] in {"seeded_unity_catalog", "unsupported"}
            and task["input_sha256"] == inputs["input_sha256"], "reviewed_task_input_binding_changed")
    seed, candidate = inputs["seed"], inputs["candidate"]
    calibration = task["ui_calibration"]
    require(isinstance(calibration, dict) and set(calibration) == {"source", "reviewed_by", "observed_at", "app_id", "deployment_id", "artifacts"}
            and calibration["source"] == "actual_generated_app_ui"
            and isinstance(calibration["reviewed_by"], str) and 0 < len(calibration["reviewed_by"].strip()) <= 200
            and calibration["app_id"] == candidate["app_id"] and calibration["deployment_id"] == candidate["deployment_id"]
            and timestamp(seed["seeded_at"]) <= timestamp(calibration["observed_at"]) <= datetime.now(timezone.utc).timestamp() + 5,
            "actual_ui_calibration_binding_required")
    references = calibration["artifacts"]
    require(isinstance(references, list) and 1 <= len(references) <= 6, "actual_ui_calibration_artifacts_required")
    kinds, paths = set(), set()
    for artifact in references:
        require(isinstance(artifact, dict) and set(artifact) == {"kind", "path", "sha256"}
                and artifact["kind"] in {"screenshot", "accessibility_snapshot"}
                and isinstance(artifact["path"], str) and Path(artifact["path"]).is_absolute()
                and re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"]), "actual_ui_calibration_artifacts_required")
        path = Path(artifact["path"])
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 10 * 1024 * 1024
                and path.resolve() not in paths and hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"],
                "ui_calibration_artifact_changed")
        kinds.add(artifact["kind"])
        paths.add(path.resolve())
    require(kinds == {"screenshot", "accessibility_snapshot"}, "actual_ui_calibration_artifacts_required")
    rows = {row["order_id"]: row for row in seed["plan"]["rows"]}
    require(task["record_id"] in rows, "seeded_record_required")
    row = rows[task["record_id"]]
    require(row["packed"] is False and row["due_at_epoch"] < timestamp(seed["seeded_at"]), "late_unpacked_fixture_record_required")
    locator(task["page_ready"], roles={"heading", "region"})
    scope = task["record_scope"]
    require(isinstance(scope, dict) and set(scope) == {"role", "anchor"} and scope["role"] in {"row", "group", "listitem", "article"},
            "bounded_record_scope_required")
    locator(scope["anchor"], roles={"cell", "rowheader", "heading"})
    require(scope["anchor"]["name"] in {row["order_id"], row["customer_name"]}, "scope_must_anchor_exact_fixture_record")
    locator(task["before_status"], roles={"cell", "status", "heading", "rowheader"})
    locator(task["packed_action"], roles={"button", "checkbox", "switch"})
    after = task["after_condition"]
    require(isinstance(after, dict) and after.get("kind") in {"visible_status", "record_absent"}, "reviewed_postcondition_required")
    if after["kind"] == "visible_status":
        require(set(after) == {"kind", "target"}, "reviewed_postcondition_required")
        locator(after["target"], roles={"cell", "status", "heading", "rowheader"})
        require(after["target"]["name"] != task["before_status"]["name"], "distinct_packed_postcondition_required")
    else:
        require(set(after) == {"kind"}, "reviewed_postcondition_required")
    return task


@dataclass(frozen=True)
class ScopedTarget:
    scope: dict
    target: dict

    def on(self, page):
        return record_scope(page, self.scope).get_by_role(self.target["role"], name=self.target["name"], exact=True)


def record_scope(page, scope):
    anchor = scope["anchor"]
    return page.get_by_role(scope["role"]).filter(has=page.get_by_role(anchor["role"], name=anchor["name"], exact=True))


def verify_generated_app(inputs, client):
    binding, candidate, seed = inputs["binding"], inputs["candidate"], inputs["seed"]
    now, seeded = datetime.now(timezone.utc).timestamp(), timestamp(seed["seeded_at"])
    require(now < binding["expires_at"], "result_receipt_expired")
    expected_ids, unattributed = set(), 0
    for index, app in enumerate(client.apps.list(page_size=100)):
        require(index < 2000, "generated_inventory_budget")
        if app.id == binding["app"]["id"] or not isinstance(app.creator, str) or app.creator.casefold() not in {
                seed["plan"]["principal"].casefold(), seed["plan"]["attendee_email"].casefold()} or timestamp(app.create_time) < seeded:
            continue
        source = getattr(getattr(app, "active_deployment", None), "source_code_path", "") or ""
        marked = app.name.startswith(binding["marker"] + "-") or binding["marker"] in PurePosixPath(source).parts
        if app.creator.casefold() == seed["plan"]["principal"].casefold() or marked:
            expected_ids.add(app.id)
        else:
            unattributed += 1
    require(expected_ids == {candidate["app_id"]} and unattributed == 0, "live_generated_inventory_ambiguous")
    app = client.apps.get(candidate["app_name"])
    require(app.id == candidate["app_id"] and app.name == candidate["app_name"] and app.creator == candidate["creator"]
            and app.url.rstrip("/") == generated_url(candidate, binding)
            and seeded <= timestamp(app.create_time) <= now + 5
            and app.active_deployment is not None and app.active_deployment.deployment_id == candidate["deployment_id"]
            and enum(getattr(app.app_status, "state", None)) == "RUNNING"
            and enum(getattr(app.compute_status, "state", None)) == "ACTIVE", "live_generated_app_binding_changed")
    deployed = client.apps.get_deployment(candidate["app_name"], candidate["deployment_id"])
    require(deployed.deployment_id == candidate["deployment_id"] and enum(deployed.status.state) == "SUCCEEDED"
            and canonical_path(deployed.source_code_path) == canonical_path(candidate["source_code_path"])
            and seeded <= timestamp(deployed.create_time) <= now + 5, "live_generated_deployment_changed")
    current = client.apps.get(candidate["app_name"])
    require(current.id == app.id and current.creator == app.creator and current.url == app.url and current.active_deployment is not None
            and current.active_deployment.deployment_id == deployed.deployment_id, "live_generated_app_changed_during_read")
    return {"new_app_verified": True, **fixture.validate_generated_binding(candidate, seed["plan"]),
            "url": generated_url(candidate, binding), "create_time": app.create_time, "deployment_create_time": deployed.create_time,
            "source_identity_verified": "current_successful_deployment_source_path", "process_restart_verified": False}


def fresh_paths(output, artifacts, inputs):
    output, artifacts = Path(output), Path(artifacts)
    temporary = output.with_name(output.name + ".tmp")
    paths = [Path(value).resolve() for value in inputs]
    require(not any(path.exists() or path.is_symlink() for path in (output, temporary, artifacts))
            and not any(path.resolve() in paths for path in (output, temporary, artifacts))
            and not output.resolve().is_relative_to(artifacts.resolve())
            and not any(path.is_relative_to(artifacts.resolve()) for path in paths), "fresh_distinct_output_and_artifacts_required")


def private_browser_state(path):
    value = Path(path) if path else None
    require(value is not None and value.is_file() and not value.is_symlink(), "genuine_private_browser_state_required")
    details = value.stat()
    require(stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid() and details.st_nlink == 1
            and details.st_mode & 0o077 == 0 and not value.resolve().is_relative_to(ROOT), "genuine_private_browser_state_required")


def actions_passed(results, expected):
    return len(results) == expected and all(result.get("passed") is True for result in results)


def assess_transition(before, after):
    """Preserve proved target corruption; unknown read/binding faults still raise."""
    try:
        return fixture.assess_pack_transition(before, after)
    except ValueError as error:
        if str(error) != "unrelated_order_fields_changed":
            raise
        old, new = fixture.normalize_rows(before["rows"])[0], fixture.normalize_rows(after["rows"])[0]
        return {"passed": False, "reason": "unrelated_order_fields_changed", "record_id": old["order_id"], "field": "packed",
                "before": old["packed"], "after": new["packed"], "binding": before["binding"], "accepted": False,
                "changed_unrelated_fields": sorted(key for key in old if key != "packed" and old[key] != new[key]),
                "evidence_references": [before["statement_id"], after["statement_id"]]}


async def postcondition(verifier, task):
    start = len(verifier.task_results)
    after = task["after_condition"]
    actions = [BrowserAction("app ready", "expect_visible", locator(task["page_ready"], roles={"heading", "region"}))]
    if after["kind"] == "visible_status":
        actions.append(BrowserAction("target packed status", "expect_visible", ScopedTarget(task["record_scope"], after["target"])))
    await verifier.run_actions(actions)
    results = verifier.task_results[start:]
    if after["kind"] == "record_absent" and actions_passed(results, 1):
        result = {"name": "target removed from attention queue", "operation": "expect_record_absent", "passed": False}
        try:
            await record_scope(verifier.page, task["record_scope"]).wait_for(state="hidden", timeout=verifier.timeout_ms)
            result["passed"] = True
        except Exception as error:
            result["error_type"] = type(error).__name__
        verifier.task_results.append(result)
        results = verifier.task_results[start:]
    return results, actions_passed(results, 2)


async def captures(verifier, phase, app_url):
    results = []
    for name, width, height in (("desktop", 1440, 900), ("tablet", 768, 1024), ("mobile", 390, 844)):
        try:
            actual, expected = urlsplit(verifier.page.url), urlsplit(app_url)
            require((actual.scheme, actual.netloc) == (expected.scheme, expected.netloc)
                    and await verifier.page.locator("input[type='password']").count() == 0,
                    "generated_browser_origin_or_password_unverified")
            observed = await verifier.capture_viewport(phase + "-" + name, width, height)
            screenshot = verifier.artifact_dir / observed["screenshot"]
            aria = verifier.artifact_dir / observed["accessibility_snapshot"]
            screenshot.chmod(0o600)
            aria.chmod(0o600)
            text = aria.read_text(encoding="utf-8")
            require(len(text) <= 12_000, "quality_accessibility_text_budget")
            results.append({**observed, "state": phase, "screenshot": str(screenshot.resolve()),
                            "accessibility_snapshot": str(aria.resolve()), "accessibility_snapshot_text": text,
                            "horizontal_overflow_observed": observed["layout_observation"]["horizontalOverflow"]})
        except Exception as error:
            failure = {"name": phase + "-" + name, "width": width, "height": height,
                       "status": "capture_failed", "error_type": type(error).__name__}
            if isinstance(error, ResultError):
                failure["reason"] = str(error)
            for kind, suffix in (("screenshot", ".png"), ("accessibility_snapshot", ".aria.txt")):
                path = verifier.artifact_dir / (phase + "-" + name + suffix)
                if path.is_file() and not path.is_symlink():
                    path.chmod(0o600)
                    failure[kind] = str(path.resolve())
            results.append(failure)
    return results


def assess_observations(report):
    stages = report.get("observations", {})
    task = stages.get("packed_action", {})
    transition = stages.get("pack_transition", {})
    persistence = stages.get("persistence", {})
    failed = any(item.get("passed") is False for stage in ("before", "packed_action", "after")
                 for item in stages.get(stage, {}).get("task_results", []))
    failed = failed or transition.get("passed") is False or any(
        value is False for key, value in persistence.items() if key in {
            "reload_passed", "fresh_context_passed", "reload_backend_verified", "fresh_context_backend_verified", "backend_verified"})
    failed = failed or stages.get("precondition", {}).get("passed") is False
    return {"verdict": "failed" if failed else "unverified", "critical_user_task_verified": bool(
                report.get("binding_current_at_end_verified") is True and task.get("passed") is True
                and stages.get("after", {}).get("passed") is True and transition.get("passed") is True),
            "reload_and_fresh_context_persistence_verified": bool(report.get("binding_current_at_end_verified") is True
                                                                   and persistence.get("persistence_verified") is True),
            "process_restart_verified": False, "calibrated_visual_quality_verified": False, "accepted": False}


async def execute(args, inputs, task, report, *, client=None):
    require(task is not None, "reviewed_actual_ui_task_plan_required")
    require(task["backend_kind"] == "seeded_unity_catalog", "backend_oracle_unsupported_unverified")
    report["binding_current_at_end_verified"] = False
    started = time.monotonic()
    deadline = started + min(args.timeout_seconds, inputs["binding"]["expires_at"] - datetime.now(timezone.utc).timestamp())
    async def bounded(operation, *values, sync=False, maximum=60):
        remaining = deadline - time.monotonic()
        require(remaining > 0, "result_deadline")
        return await asyncio.wait_for(asyncio.to_thread(operation, *values) if sync else operation(*values), timeout=min(remaining, maximum))
    if client is None:
        client = await bounded(workspace_client, inputs["binding"]["profile"], sync=True, maximum=30)
    report["terminal_verification"] = await bounded(verify_deployment, inputs["binding"], client, sync=True, maximum=180)
    report["deployment"] = await bounded(verify_generated_app, inputs, client, sync=True)
    require(timestamp(task["ui_calibration"]["observed_at"]) >= timestamp(report["deployment"]["deployment_create_time"]),
            "ui_calibration_precedes_current_deployment")
    report["live_seed_grants"] = await bounded(verify_grants, client, inputs["seed"]["plan"], sync=True)
    def record(event):
        report.setdefault("sql_statements", []).append(dict(event))
        write_evidence(args.output, report)
    oracle = await bounded(lambda: SqlBackendOracle(client, inputs["seed"], inputs["candidate"], record=record), sync=True)
    observations = report.setdefault("observations", {})
    reads = observations.setdefault("backend_reads", [])
    async def read(phase):
        value = await bounded(oracle.read, task["record_id"], sync=True)
        reads.append({"phase": phase, "observation": value})
        write_evidence(args.output, report)
        return value
    before = await read("before")
    expected = next(row for row in fixture.expected_rows(inputs["seed"]["plan"]) if row["order_id"] == task["record_id"])
    observations["precondition"] = {"passed": fixture.normalize_rows(before["rows"]) == [expected] and expected["packed"] is False,
                                   "source": "actual_bound_seeded_sql_record"}
    require(observations["precondition"]["passed"],
            "actual_seeded_record_precondition_changed")
    origin = urlsplit(report["deployment"]["url"])
    artifact_dir = Path(args.artifacts)
    artifact_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    browser = AuthenticatedBrowser(args.browser_state, headless=not args.show_browser,
                                   clear_app_storage_origins=[origin.scheme + "://" + origin.netloc])
    verifier = None
    try:
        await bounded(browser.__aenter__, maximum=30)
        verifier = GeneratedAppBrowserVerifier(browser.page, artifact_dir=artifact_dir, timeout_seconds=args.action_timeout_seconds)
        await bounded(verifier.open, report["deployment"]["url"])
        scoped = record_scope(browser.page, task["record_scope"])
        require(await bounded(scoped.count) == 1, "one_visible_target_record_required")
        start = len(verifier.task_results)
        await bounded(verifier.run_actions, [BrowserAction("app ready", "expect_visible", locator(task["page_ready"], roles={"heading", "region"})),
            BrowserAction("target before packing", "expect_visible", ScopedTarget(task["record_scope"], task["before_status"]))])
        initial = verifier.task_results[start:]
        observations["before"] = {"task_results": initial, "passed": actions_passed(initial, 2)}
        report["first_preview"] = {"independently_observed": True, "viewports": await bounded(captures, verifier, "first-preview", report["deployment"]["url"]), "task_results": initial}
        await bounded(browser.page.set_viewport_size, {"width": 1440, "height": 900})
        if actions_passed(initial, 2):
            target = ScopedTarget(task["record_scope"], task["packed_action"])
            require(await bounded(target.on(browser.page).count) == 1, "one_packed_control_required")
            start = len(verifier.task_results)
            await bounded(verifier.run_actions, [BrowserAction("pack target order once", "click", target)])
            clicked = verifier.task_results[start:]
            observations["packed_action"] = {"task_results": clicked, "passed": actions_passed(clicked, 1), "click_attempts": 1}
            if actions_passed(clicked, 1):
                post, post_passed = await bounded(postcondition, verifier, task)
                observations["after"] = {"task_results": post, "passed": post_passed}
                poll_deadline = min(deadline, time.monotonic() + args.action_timeout_seconds)
                while True:
                    after = await read("after_action")
                    transition = assess_transition(before, after)
                    observations["pack_transition"] = transition
                    if transition["passed"] or transition.get("reason") or time.monotonic() >= poll_deadline:
                        break
                    await bounded(asyncio.sleep, .25)
                if post_passed and transition["passed"]:
                    start = len(verifier.task_results)
                    await bounded(verifier.run_actions, [BrowserAction("reload app", "reload")])
                    reload_results = verifier.task_results[start:]
                    reload_post, reload_visible = await bounded(postcondition, verifier, task)
                    persistence = {"reload_passed": actions_passed(reload_results, 1) and reload_visible,
                                   "reload_tasks": reload_results + reload_post, "reload_backend_verified": None,
                                   "fresh_context_passed": None, "fresh_context_backend_verified": None, "backend_verified": None,
                                   "app_cache_cleared": True, "process_restart_verified": False, "persistence_verified": False}
                    observations["persistence"] = persistence
                    reload_read = await read("after_reload")
                    reload_assessment = assess_transition(before, reload_read)
                    reload_backend = reload_assessment["passed"]
                    persistence["reload_backend_verified"] = reload_backend
                    persistence["reload_backend_assessment"] = reload_assessment
                    second = await bounded(browser.new_context)
                    try:
                        require(second is not browser.context, "fresh_context_not_independent")
                        page = await bounded(second.new_page)
                        fresh = GeneratedAppBrowserVerifier(page, artifact_dir=artifact_dir / "fresh-context", timeout_seconds=args.action_timeout_seconds)
                        fresh.artifact_dir.chmod(0o700)
                        await bounded(fresh.open, report["deployment"]["url"])
                        fresh_results, fresh_visible = await bounded(postcondition, fresh, task)
                        persistence.update(fresh_context_passed=fresh_visible, fresh_context_tasks=fresh_results)
                        report["fresh_context_page_errors"] = fresh.page_errors
                        # Keep known UI failures even if the independent SQL collector stalls.
                        report["final"] = {"independently_observed": True, "viewports": [],
                                           "task_results": clicked + post + reload_results + reload_post + fresh_results}
                        report["final"]["viewports"] = await bounded(captures, fresh, "final", report["deployment"]["url"])
                        fresh_read = await read("fresh_context")
                        fresh_assessment = assess_transition(before, fresh_read)
                        fresh_backend = fresh_assessment["passed"]
                        persistence.update(fresh_context_backend_verified=fresh_backend,
                                           fresh_context_backend_assessment=fresh_assessment,
                                           backend_verified=bool(reload_backend and fresh_backend),
                                           persistence_verified=bool(persistence["reload_passed"] and fresh_visible and reload_backend and fresh_backend))
                    finally:
                        if second is not browser.context:
                            await asyncio.wait_for(second.close(), timeout=10)
        if "final" not in report:
            report["final"] = {"independently_observed": True, "viewports": await bounded(captures, verifier, "final", report["deployment"]["url"]),
                               "task_results": verifier.task_results}
        report["page_errors"] = verifier.page_errors
        report["final_deployment_verification"] = await bounded(verify_generated_app, inputs, client, sync=True)
        report["binding_current_at_end_verified"] = True
        report["assessment"] = assess_observations(report)
        report.update(status="observations_finished", verdict=report["assessment"]["verdict"], accepted=False)
    finally:
        if verifier is not None and "final" not in report:
            try:
                report["final"] = {"independently_observed": True,
                                   "viewports": await asyncio.wait_for(captures(verifier, "final", report["deployment"]["url"]), timeout=10),
                                   "task_results": verifier.task_results}
            except Exception as error:
                report["final"] = {"independently_observed": False, "status": "capture_failed", "error_type": type(error).__name__,
                                   "task_results": verifier.task_results, "viewports": []}
        await asyncio.wait_for(browser.__aexit__(None, None, None), timeout=10)
        report["elapsed_seconds"] = time.monotonic() - started


async def observe_only(args, inputs, report):
    """Calibrate the actual UI before choosing controls or a backend oracle."""
    client = await asyncio.wait_for(asyncio.to_thread(workspace_client, inputs["binding"]["profile"]), timeout=30)
    report["terminal_verification"] = await asyncio.wait_for(asyncio.to_thread(verify_deployment, inputs["binding"], client), timeout=180)
    report["deployment"] = await asyncio.wait_for(asyncio.to_thread(verify_generated_app, inputs, client), timeout=60)
    origin = urlsplit(report["deployment"]["url"])
    directory = Path(args.artifacts)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    async with AuthenticatedBrowser(args.browser_state, headless=not args.show_browser,
            clear_app_storage_origins=[origin.scheme + "://" + origin.netloc]) as browser:
        verifier = GeneratedAppBrowserVerifier(browser.page, artifact_dir=directory, timeout_seconds=args.action_timeout_seconds)
        await verifier.open(report["deployment"]["url"])
        # Let the first asynchronous data render settle; this is observation,
        # never a claim that loading or any task has succeeded.
        await asyncio.sleep(1)
        report["first_preview"] = {"independently_observed": True, "task_results": [],
            "viewports": await captures(verifier, "first-preview", report["deployment"]["url"])}
        report["page_errors"] = verifier.page_errors
    report["final_deployment_verification"] = await asyncio.wait_for(asyncio.to_thread(verify_generated_app, inputs, client), timeout=60)
    report.update(status="ui_observed", binding_current_at_end_verified=True, verdict="unverified", accepted=False,
                  observation_only=True, app_tasks_executed=False, backend_persistence_verified=False)


def quality_export(report, evidence_reference):
    """Actual text/task observations for existing native MLflow input adapters."""
    export = {"source": "independent_generated_app_browser", "synthetic_attendee": True,
              "deployment": {"new_app_verified": bool(report.get("deployment", {}).get("new_app_verified") is True
                                                        and report.get("binding_current_at_end_verified") is True),
                             "app_name": report.get("deployment", {}).get("app_name", ""),
                             "deployment_revision": report.get("deployment", {}).get("deployment_id", ""),
                             "evidence_reference": str(evidence_reference)}, "accepted": False}
    for phase in ("first_preview", "final"):
        value = report.get(phase)
        if value:
            export[phase] = {**value, "task_results": [{**task, "evidence_reference": str(evidence_reference),
                                                       "observed_result": "observed task completed" if task["passed"] else "observed task failed"}
                                                      for task in value["task_results"]]}
    transition = report.get("observations", {}).get("pack_transition", {})
    if "final" in export and type(transition.get("passed")) is bool:
        observed = "actual seeded target packed changed from false to true" if transition["passed"] else "actual seeded target packed did not change from false to true"
        if transition.get("reason") == "unrelated_order_fields_changed":
            observed = "packing changed unrelated target-order fields: " + ", ".join(transition["changed_unrelated_fields"])
        export["final"]["task_results"].append({"name": "independent seeded storage packed update", "operation": "independent_sql_read",
            "passed": transition["passed"], "observed_result": observed, "evidence_reference": str(evidence_reference)})
    if "final" in export:
        persistence = report.get("observations", {}).get("persistence", {})
        for field, name in (("reload_backend_verified", "independent storage after reload"),
                            ("fresh_context_backend_verified", "independent storage in fresh context"),
                            ("backend_verified", "independent reload and fresh-context storage outcome")):
            if type(persistence.get(field)) is bool:
                export["final"]["task_results"].append({"name": name, "operation": "independent_sql_read", "passed": persistence[field],
                    "observed_result": "actual seeded target update persisted without changing unrelated order details" if persistence[field]
                        else "actual seeded target update failed independent persistence checks",
                    "evidence_reference": str(evidence_reference)})
    return export


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--terminal-receipt", required=True)
    value.add_argument("--journey", required=True)
    value.add_argument("--seed-receipt", required=True)
    value.add_argument("--task-plan", help="Private role-based task reviewed against this exact actual app UI")
    value.add_argument("--browser-state", help="Genuine private generated-app browser authentication; never copied")
    value.add_argument("--output", required=True)
    value.add_argument("--artifacts", required=True, help="Fresh private screenshot/ARIA directory, separate from inputs/output")
    value.add_argument("--timeout-seconds", type=float, default=300)
    value.add_argument("--action-timeout-seconds", type=float, default=15)
    value.add_argument("--show-browser", action="store_true")
    value.add_argument("--observe-only", action="store_true", help="Read-only actual UI capture for task calibration; does not execute a task or assert persistence")
    value.add_argument("--execute", action="store_true")
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    input_paths = [args.terminal_receipt, args.journey, args.seed_receipt] + ([args.task_plan] if args.task_plan else []) + ([args.browser_state] if args.browser_state else [])
    try:
        fresh_paths(args.output, args.artifacts, input_paths)
    except ResultError as error:
        print(json.dumps({"status": "blocked", "reason": str(error), "accepted": False}))
        return 2
    report = {"schema_version": 1, "operation": "verify_generated_app_result", "scope": "simulated_control_tower",
              "control_tower_requests": 0, "accepted": False, "verdict": "unverified", "status": "planning", "executed": False,
              "unverified_checks": ["app_process_restart", "calibrated_first_preview_visual_quality", "calibrated_final_visual_quality",
                                    "event_attendee_equivalence", "other_backend_or_other_record_changes", "budget_usage", "resource_cleanup"],
              "limitations": ["The backend oracle supports this exact externally seeded UC table and one target record only.",
                              "Screenshot paths, actual ARIA text and overflow observations do not establish visual quality.",
                              "Fixture setup is external; this command does not establish app-created provisioning."]}
    try:
        require(math.isfinite(args.timeout_seconds) and 0 < args.timeout_seconds <= 600
                and math.isfinite(args.action_timeout_seconds) and 0 < args.action_timeout_seconds <= 30,
                "bounded_result_deadlines_required")
        inputs = load_inputs(args.terminal_receipt, args.journey, args.seed_receipt)
        task = None
        require(not args.observe_only or not args.task_plan, "observation_cannot_execute_reviewed_task")
        if args.task_plan:
            task, task_hash = load_json(args.task_plan, limit=64 * 1024)
            validate_task_plan(task, inputs)
            calibration_paths = [Path(artifact["path"]).resolve() for artifact in task["ui_calibration"]["artifacts"]]
            require(not set(calibration_paths).intersection(Path(path).resolve() for path in input_paths),
                    "calibration_must_be_distinct_from_receipts_and_auth")
            fresh_paths(args.output, args.artifacts, input_paths + [artifact["path"] for artifact in task["ui_calibration"]["artifacts"]])
            report["task_plan_sha256"] = task_hash
        report.update(status="planned", input_sha256=inputs["input_sha256"], target=inputs["candidate"],
                      backend_kind=task["backend_kind"] if task else "unverified_without_task_plan",
                      task_record_id=task["record_id"] if task else None,
                      reviewed_actual_ui_task_available=task is not None)
        if args.execute:
            require(args.observe_only or task is not None, "reviewed_actual_ui_task_plan_required")
            private_browser_state(args.browser_state)
        write_evidence(args.output, report)
        if args.execute:
            report["executed"] = True
            asyncio.run(observe_only(args, inputs, report) if args.observe_only else execute(args, inputs, task, report))
            report["mlflow_browser_input"] = quality_export(report, Path(args.output).resolve())
    except Exception as error:
        report.update(status="blocked", error_type=type(error).__name__, accepted=False)
        if isinstance(error, ResultError):
            report["reason"] = str(error)
        elif isinstance(error, BrowserJourneyError):
            report["reason"] = error.code
        report["assessment"] = assess_observations(report)
        report["verdict"] = report["assessment"]["verdict"]
        if report.get("deployment", {}).get("new_app_verified") is True:
            report["mlflow_browser_input"] = quality_export(report, Path(args.output).resolve())
        write_evidence(args.output, report)
        print(json.dumps({"output": args.output, "status": report["status"], "reason": report.get("reason"), "error_type": type(error).__name__, "accepted": False}))
        return 2
    write_evidence(args.output, report)
    print(json.dumps({"output": args.output, "status": report["status"], "verdict": report["verdict"], "accepted": False, "control_tower_requests": 0}))
    return 1 if args.execute else 0


if __name__ == "__main__":
    raise SystemExit(main())
