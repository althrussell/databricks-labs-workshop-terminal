#!/usr/bin/env python3
"""Plan or seed ordinary bakery data only in an exact fresh isolated WT catalog.

Default makes no network calls. --execute requires a separately reviewed plan,
live deployment verification, absent schema/table, and a fresh evidence output.
No CT endpoint or existing global dataset is used. Partial creation receipts are
preserved for exact cleanup; no resume, replacement, or destructive cleanup runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.generated_apps import bakery_fixture as fixture
from evals.generated_apps.report import write_evidence
from scripts.deploy_ct_sim import _pick_warehouse
from scripts.run_generated_app_journey import canonical_path, enum, timestamp, validate_receipt, verify_deployment


class SeedError(ValueError):
    pass


def workspace_client(profile):
    """Timeout options belong to Config, not WorkspaceClient constructor kwargs."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.config import Config
    return WorkspaceClient(config=Config(profile=profile, http_timeout_seconds=10, retry_timeout_seconds=15))


def require(condition, reason):
    if not condition:
        raise SeedError(reason)


class StatementRunner:
    """Bounded inline SQL with statement IDs retained before waiting or parsing."""
    def __init__(self, client, warehouse_id, *, record=lambda _event: None, timeout_seconds=30,
                 clock=time.monotonic, sleep=time.sleep):
        require(isinstance(warehouse_id, str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}", warehouse_id), "warehouse_id_required")
        require(type(timeout_seconds) in {int, float} and 0 < timeout_seconds <= 60, "bounded_statement_timeout_required")
        self.client, self.warehouse_id, self.record = client, warehouse_id, record
        self.timeout, self.clock, self.sleep = timeout_seconds, clock, sleep

    def run(self, name, statement):
        deadline = self.clock() + self.timeout
        event = {"name": name, "statement_sha256": hashlib.sha256(statement.encode()).hexdigest(), "status": "submission_requested"}
        self.record(event)
        response = self.client.statement_execution.execute_statement(statement=statement, warehouse_id=self.warehouse_id,
                wait_timeout="10s", row_limit=100, byte_limit=200000)
        statement_id = response.statement_id
        require(isinstance(statement_id, str) and statement_id, "statement_identity_unverified")
        event.update(statement_id=statement_id, status="submitted")
        self.record(event)
        while enum(getattr(response.status, "state", None)) in {"PENDING", "RUNNING"}:
            remaining = deadline - self.clock()
            if remaining <= 0:
                self.client.statement_execution.cancel_execution(statement_id)
                event["status"] = "deadline_cancel_requested"
                self.record(event)
                raise SeedError("statement_deadline")
            self.sleep(min(1, remaining))
            response = self.client.statement_execution.get_statement(statement_id)
            require(response.statement_id == statement_id, "statement_identity_changed")
        state = enum(getattr(response.status, "state", None))
        event["status"] = state or "unknown"
        self.record(event)
        require(state == "SUCCEEDED", "statement_failed")
        require(self.clock() <= deadline, "statement_completed_after_deadline")
        return response


def rows_from_response(response):
    manifest, result = getattr(response, "manifest", None), getattr(response, "result", None)
    require(manifest is not None and result is not None and getattr(manifest, "truncated", False) is not True,
            "complete_inline_result_required")
    columns = [column.name for column in manifest.schema.columns]
    require(len(columns) == len(set(columns)) and getattr(manifest, "total_chunk_count", 1) == 1
            and getattr(result, "next_chunk_index", None) is None, "single_complete_result_required")
    data = result.data_array or []
    require(len(data) <= 100 and getattr(manifest, "total_row_count", len(data)) == len(data)
            and all(len(row) == len(columns) for row in data), "complete_inline_result_required")
    return [dict(zip(columns, row)) for row in data]


def require_absent(operation, kind):
    from databricks.sdk.errors import NotFound
    try:
        operation()
    except NotFound:
        return
    raise SeedError("fresh_" + kind + "_required")


def verify_grants(client, plan):
    required = {"schema": {"USE_SCHEMA"}, "table": {"SELECT", "MODIFY"}}
    evidence = {}
    for kind, privileges in required.items():
        name = plan["catalog"] + "." + plan["schema"] if kind == "schema" else plan["table"]
        result = client.grants.get(kind, name)
        observed = set()
        for assignment in result.privilege_assignments or []:
            if assignment.principal == plan["principal"]:
                observed.update(enum(value).upper().replace(" ", "_") for value in assignment.privileges or [])
        require(privileges <= observed, "seed_grant_readback_mismatch")
        evidence[kind] = {"full_name": name, "principal": plan["principal"], "required": sorted(privileges), "observed": sorted(observed)}
    return evidence


def verify_table_identity(client, plan):
    table = client.tables.get(plan["table"])
    require(table.full_name == plan["table"] and table.catalog_name == plan["catalog"]
            and table.schema_name == plan["schema"] and isinstance(table.table_id, str) and table.table_id
            and table.owner.casefold() == plan["attendee_email"].casefold(), "seed_table_identity_unverified")
    return {"table_id": table.table_id, "full_name": table.full_name, "catalog_name": table.catalog_name,
            "schema_name": table.schema_name, "owner": table.owner}


def verify_schema_identity(client, plan):
    name = plan["catalog"] + "." + plan["schema"]
    schema = client.schemas.get(name)
    require(schema.full_name == name and schema.catalog_name == plan["catalog"]
            and schema.name == plan["schema"]
            and schema.owner.casefold() == plan["attendee_email"].casefold(), "seed_schema_identity_unverified")
    return {"full_name": schema.full_name, "catalog_name": schema.catalog_name,
            "name": schema.name, "owner": schema.owner}


def rebind_seed(deployment_receipt, deployment_raw, previous_seed, previous_raw, output, *, client):
    """Read-only fixture continuation after an exact external-observer update."""
    binding = validate_receipt(deployment_receipt)
    require(previous_seed.get("status") == "seed_verified" and previous_seed.get("accepted") is False,
            "verified_seed_receipt_required")
    old = previous_seed["plan"]
    fixture.validate_plan(old)
    require(deployment_receipt.get("observer_update", {}).get("package_bytes_unchanged") is True
            and deployment_receipt.get("previous_deployment", {}).get("deployment_id") == old["terminal_deployment_id"],
            "external_observer_continuation_required")
    plan = fixture.build_plan(binding, deployment_receipt_sha256=hashlib.sha256(deployment_raw).hexdigest(), epoch=old["epoch"])
    allowed = {"terminal_deployment_id", "deployment_receipt_sha256", "plan_digest"}
    require({k:v for k,v in plan.items() if k not in allowed} == {k:v for k,v in old.items() if k not in allowed},
            "fixture_continuation_target_changed")
    result = {**previous_seed, "plan": plan, "operation": "rebind_bakery_fixture", "status": "verifying",
        "fixture_continuation": {"previous_seed_sha256": hashlib.sha256(previous_raw).hexdigest(),
            "previous_deployment_id": old["terminal_deployment_id"], "workspace_mutations": 0,
            "original_bounds_and_epoch_preserved": True}}
    write_evidence(output, result)
    result["terminal_verification"] = verify_deployment(binding, client)
    require(verify_table_identity(client, plan) == previous_seed["table_identity"], "seed_table_identity_changed")
    require(verify_schema_identity(client, plan) == previous_seed["schema_identity"], "seed_schema_identity_changed")
    sql = StatementRunner(client, previous_seed["warehouse_id"])
    response = sql.run("rebind_read_seed", fixture.read_statement(plan))
    observed = rows_from_response(response)
    result["data_readback"] = fixture.verify_seed_rows(plan, observed)
    result["grant_readback"] = verify_grants(client, plan)
    result["seed_observation"] = {"statement_id": response.statement_id, "observed_rows": observed,
                                  "observed_at": datetime.now(timezone.utc).isoformat()}
    result["status"] = "seed_verified"
    write_evidence(output, result)
    return result


def seed(binding, plan, output, *, client, warehouse_id=None):
    fixture.validate_plan(plan)
    require(plan["fixture_version"] == fixture.VERSION, "new_seed_requires_current_fixture_version")
    require(plan == fixture.build_plan(binding, deployment_receipt_sha256=plan["deployment_receipt_sha256"], epoch=plan["epoch"]),
            "seed_plan_target_binding_changed")
    receipt = {"schema_version": 1, "operation": "seed_bakery_fixture", "scope": "simulated_control_tower",
               "control_tower_requests": 0, "executed": True, "accepted": False, "status": "verifying",
               "plan": plan, "created_resources": [], "statements": []}
    write_evidence(output, receipt)
    try:
        receipt["terminal_verification"] = verify_deployment(binding, client)
        catalog = client.catalogs.get(plan["catalog"])
        require(catalog.name == plan["catalog"] and catalog.owner.casefold() == binding.get("catalog_owner", binding["attendee"]["email"]).casefold(),
                "catalog_receipt_owner_mismatch")
        schema_name = plan["catalog"] + "." + plan["schema"]
        require_absent(lambda: client.schemas.get(schema_name), "schema")
        require_absent(lambda: client.tables.get(plan["table"]), "table")
        warehouse_id = warehouse_id or _pick_warehouse(client)
        receipt["warehouse_id"] = warehouse_id
        def record(event):
            if receipt["statements"] and receipt["statements"][-1]["name"] == event["name"]:
                receipt["statements"][-1] = dict(event)
            else:
                receipt["statements"].append(dict(event))
            write_evidence(output, receipt)
        sql = StatementRunner(client, warehouse_id, record=record)
        for operation in plan["statements"]:
            require(datetime.now(timezone.utc).timestamp() < binding["expires_at"], "seed_receipt_expired")
            if operation["name"] in {"create_schema", "create_table"}:
                resource = {"kind": operation["name"].removeprefix("create_"), "name": schema_name if operation["name"] == "create_schema" else plan["table"], "state": "creation_requested"}
                receipt["created_resources"].append(resource)
                write_evidence(output, receipt)
            sql.run(operation["name"], operation["statement"])
            if operation["name"] in {"create_schema", "create_table"}:
                resource["state"] = "created"
                write_evidence(output, receipt)
        response = sql.run("read_seed", fixture.read_statement(plan))
        observed = rows_from_response(response)
        receipt["seed_observation"] = {"statement_id": response.statement_id, "observed_rows": observed,
                                       "observed_at": datetime.now(timezone.utc).isoformat()}
        write_evidence(output, receipt)  # Preserve actual sample-data readback before any comparison fails.
        receipt["data_readback"] = fixture.verify_seed_rows(plan, observed)
        receipt["grant_readback"] = verify_grants(client, plan)
        receipt["table_identity"] = verify_table_identity(client, plan)
        receipt["schema_identity"] = verify_schema_identity(client, plan)
        receipt.update(status="seed_verified", seeded_at=datetime.now(timezone.utc).isoformat(),
                       provisioning_attribution="external_setup_fixture_not_app_created")
        write_evidence(output, receipt)
        return receipt
    except Exception as error:
        receipt.update(status="failed", error_type=type(error).__name__)
        if isinstance(error, (SeedError, ValueError)):
            receipt["reason"] = str(error) if isinstance(error, (SeedError, fixture.FixtureValidationError)) else "fixture_validation_failed"
        write_evidence(output, receipt)
        raise


class SqlBackendOracle:
    """Read actual seeded storage bound to one discovered app/deployment/table.

    Reads never modify data or claim that the app provisioned the setup table.
    A UI action and fresh-context/restart proofs must be collected separately.
    """
    def __init__(self, client, seed_receipt, candidate, *, record=lambda _event: None):
        require(seed_receipt.get("status") == "seed_verified" and seed_receipt.get("executed") is True
                and seed_receipt.get("control_tower_requests") == 0 and seed_receipt.get("accepted") is False,
                "verified_seed_receipt_required")
        self.plan = seed_receipt["plan"]
        fixture.validate_plan(self.plan)
        self.seeded_at = timestamp(seed_receipt["seeded_at"])
        self.app = fixture.validate_generated_binding(candidate, self.plan)
        require(client.config.host.rstrip("/") == self.plan["workspace_host"], "oracle_workspace_mismatch")
        require(seed_receipt["data_readback"].get("verified") is True and seed_receipt.get("grant_readback"), "verified_seed_receipt_required")
        self.table_identity = seed_receipt["table_identity"]
        require(self.table_identity == verify_table_identity(client, self.plan), "oracle_seed_table_identity_changed")
        terminal = client.apps.get(self.plan["terminal_app_name"])
        require(terminal.id == self.plan["terminal_app_id"] and terminal.service_principal_client_id == self.plan["principal"]
                and terminal.active_deployment is not None and terminal.active_deployment.deployment_id == self.plan["terminal_deployment_id"],
                "oracle_terminal_receipt_changed")
        self.client = client
        self.sql = StatementRunner(client, seed_receipt["warehouse_id"], record=record)

    def read(self, order_id):
        require(datetime.now(timezone.utc).timestamp() < self.plan["expires_at_epoch"], "oracle_receipt_expired")
        require(self.table_identity == verify_table_identity(self.client, self.plan), "oracle_seed_table_identity_changed")
        app = self.client.apps.get(self.app["app_name"])
        require(app.id == self.app["app_id"] and app.creator == self.app["creator"]
                and app.active_deployment is not None and app.active_deployment.deployment_id == self.app["deployment_id"]
                and timestamp(app.create_time) >= self.seeded_at, "oracle_generated_app_binding_changed")
        deployed = self.client.apps.get_deployment(self.app["app_name"], self.app["deployment_id"])
        require(deployed.deployment_id == self.app["deployment_id"] and enum(deployed.status.state) == "SUCCEEDED"
                and canonical_path(deployed.source_code_path) == canonical_path(self.app["source_code_path"])
                and timestamp(deployed.create_time) >= self.seeded_at, "oracle_generated_deployment_changed")
        response = self.sql.run("read_order_" + order_id, fixture.read_statement(self.plan, order_id))
        rows = fixture.normalize_rows(rows_from_response(response))
        require(len(rows) == 1 and rows[0]["order_id"] == order_id, "oracle_exact_record_required")
        current = self.client.apps.get(self.app["app_name"])
        require(current.id == self.app["app_id"] and current.active_deployment is not None
                and current.active_deployment.deployment_id == self.app["deployment_id"], "oracle_generated_app_binding_changed")
        return {"source": "independent_sql_statement_execution", "live_binding_verified": True,
                "binding": {**self.app, "table": self.plan["table"], "workspace_id": self.plan["workspace_id"],
                            "table_id": self.table_identity["table_id"],
                            "fixture_plan_digest": self.plan["plan_digest"]}, "statement_id": response.statement_id,
                "observed_at": datetime.now(timezone.utc).isoformat(), "rows": rows,
                "provisioning_attribution": "external_setup_fixture_not_app_created"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--epoch", help="Aware UTC fixture epoch; defaults to current time for local planning")
    parser.add_argument("--reviewed-plan", help="Exact local plan output required for execution")
    parser.add_argument("--warehouse-id")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--rebind-seed", help="Read-only binding of an unchanged verified fixture to an external-observer continuation")
    args = parser.parse_args(argv)
    output = Path(args.output)
    inputs = [Path(args.receipt)] + ([Path(args.reviewed_plan)] if args.reviewed_plan else []) + ([Path(args.rebind_seed)] if args.rebind_seed else [])
    temporary = output.with_name(output.name + ".tmp")
    if any(path.exists() or path.is_symlink() for path in (output, temporary)) or any(
            path.resolve() in {value.resolve() for value in inputs} for path in (output, temporary)):
        parser.error("Fresh distinct evidence output required; preserve existing ownership receipts")
    if args.execute and not args.reviewed_plan:
        parser.error("Execution requires the exact separately reviewed local plan")
    if args.rebind_seed and (args.execute or args.reviewed_plan or args.epoch):
        parser.error("Read-only fixture rebinding cannot create/reseed data or change its epoch")
    report = {"schema_version": 1, "operation": "seed_bakery_fixture", "accepted": False, "executed": False,
              "control_tower_requests": 0, "status": "planning"}
    try:
        content = Path(args.receipt).read_bytes()
        binding = validate_receipt(json.loads(content))
        if args.rebind_seed:
            previous_raw = Path(args.rebind_seed).read_bytes()
            report = rebind_seed(json.loads(content), content, json.loads(previous_raw), previous_raw, output,
                                 client=workspace_client(binding["profile"]))
            print(json.dumps({"output": str(output), "status": report["status"], "workspace_mutations": 0}))
            return 0
        reviewed = json.loads(Path(args.reviewed_plan).read_text())["plan"] if args.reviewed_plan else None
        epoch = args.epoch or (reviewed["epoch"] if reviewed else datetime.now(timezone.utc))
        plan = fixture.build_plan(binding, deployment_receipt_sha256=hashlib.sha256(content).hexdigest(), epoch=epoch)
        require(reviewed is None or reviewed == plan, "reviewed_plan_or_receipt_changed")
        report.update(status="planned", plan=plan)
        write_evidence(output, report)
        if args.execute:
            report = seed(binding, plan, output, client=workspace_client(binding["profile"]), warehouse_id=args.warehouse_id)
    except Exception as error:
        # The executor already preserved resource and statement receipts on failure.
        if output.exists():
            report = json.loads(output.read_text())
        report.update(status="failed", error_type=type(error).__name__, accepted=False)
        write_evidence(output, report)
        print(json.dumps({"output": str(output), "status": "failed", "error_type": type(error).__name__, "accepted": False}))
        return 2
    print(json.dumps({"output": str(output), "status": report["status"], "control_tower_requests": 0, "accepted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
