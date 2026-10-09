"""Pure plans and comparisons for ordinary, discoverable bakery order data.

This module never imports a cloud SDK or writes data. Fixture creation is setup,
not app-created provisioning. Private record/task expectations stay in the
external evaluator; the builder discovers ordinary rows through catalog tools.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import PurePosixPath


VERSION = "bakery-orders-v3"
HEX_VALUES_VERSION = "bakery-orders-v2"
LEGACY_VERSION = "bakery-orders-v1"
TABLE = "bakery_orders"
SCHEMA = "generated_apps"
_ID = re.compile(r"[a-z][a-z0-9_]{0,127}")
_MARKER = re.compile(r"wt-eval-[a-z0-9](?:[a-z0-9-]{0,13}[a-z0-9])?")


class FixtureValidationError(ValueError):
    """Stable allowlisted validation code; never includes remote row values."""


def require(condition, code):
    if not condition:
        raise FixtureValidationError(code)


def aware_time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(isinstance(value, datetime) and value.tzinfo is not None, "aware_epoch_required")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def quote_identifier(value):
    require(isinstance(value, str) and _ID.fullmatch(value), "bounded_identifier_required")
    return "`" + value + "`"


def quote_principal(value):
    require(isinstance(value, str) and 0 < len(value) <= 320 and value.strip() == value
            and not any(ord(character) < 32 or ord(character) == 127 for character in value),
            "bounded_principal_required")
    return "`" + value.replace("`", "``") + "`"


def literal(value):
    require(isinstance(value, str), "string_literal_required")
    # Spark SQL concatenates adjacent quoted strings: 'O''Brien' loses its
    # apostrophe. Decode UTF-8 bytes rather than depending on quote/backslash
    # parser settings. The expression contains only bounded hex and fixed SQL.
    return "decode(unhex('" + value.encode("utf-8").hex() + "'), 'UTF-8')"


def _legacy_literal(value):
    """Reconstruct v1 receipts for readback only; never generate new v1 seeds."""
    return "'" + value.replace("'", "''") + "'"


def orders(epoch):
    """Materialize stable lateness at a recorded UTC epoch; no frozen app clock."""
    epoch = aware_time(epoch)
    customers = ["Maya Patel", "Riverbend Community Centre", "Noah Wilson", "Elena García",
                 "Northside Primary School", "Sam O'Brien", "Harbour Dental", "Ava Chen",
                 "Lee's Corner Store", "Lakeside Football Club", "Priya Shah", "Daniel Brooks",
                 "Neighbourhood Garden Volunteers", "Sofia Rossi", "Oliver Jones", "Mia Williams",
                 "Hillcrest Retirement Village", "Lucas Brown", "Amelia Davis", "Eastbank Book Club"]
    items = ["Birthday cake", "Morning tea assortment for the volunteer thank-you gathering",
             "Sourdough loaves", "Croissant box", "Mini muffins", "Chocolate celebration cake"]
    rows = []
    for index, customer in enumerate(customers):
        packed = index in {2, 12, 18}
        future = index in {1, 7, 13, 16, 19}
        offset = (3 + index % 4) * 86400 if future else -(1 + index % 3) * 86400
        if index < 3:
            offset = (-86400, 259200, -172800)[index]
        quantity = [1, 40, 6, 12, 30, 1][index % 6]
        rows.append({"order_id": f"BK-{1001 + index}", "customer_name": customer,
                     "items": items[index % len(items)], "quantity": quantity,
                     "order_total": str(Decimal(quantity) * Decimal("4.50") + Decimal("12.00")),
                     "due_at": (epoch + timedelta(seconds=offset)).isoformat(),
                     "due_at_epoch": int(epoch.timestamp()) + offset, "packed": packed,
                     "created_at": (epoch - timedelta(days=7)).isoformat(),
                     "created_at_epoch": int((epoch - timedelta(days=7)).timestamp())})
    return rows


def build_plan(binding, *, deployment_receipt_sha256, epoch, fixture_version=VERSION):
    require(fixture_version in {VERSION, HEX_VALUES_VERSION, LEGACY_VERSION}, "fixture_version_required")
    marker = binding.get("marker")
    require(isinstance(marker, str) and _MARKER.fullmatch(marker), "isolated_marker_required")
    require(isinstance(deployment_receipt_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", deployment_receipt_sha256),
            "exact_deployment_receipt_digest_required")
    environment = binding.get("environment", {})
    catalog = marker.replace("-", "_")
    require(environment.get("WORKSHOP_CATALOG") == catalog and environment.get("WORKSHOP_SCHEMA") == SCHEMA,
            "catalog_schema_marker_binding_required")
    app = binding.get("app", {})
    require(app.get("name") == marker + "-wt" and app.get("id") and app.get("service_principal_client_id"),
            "terminal_identity_required")
    epoch = aware_time(epoch)
    require(binding["created_at"] <= epoch.timestamp() < binding["expires_at"], "epoch_outside_receipt_bounds")
    table = ".".join((catalog, SCHEMA, TABLE))
    quoted_table = ".".join(map(quote_identifier, (catalog, SCHEMA, TABLE)))
    quoted_schema = ".".join(map(quote_identifier, (catalog, SCHEMA)))
    principal = "`" + app["service_principal_client_id"].replace("`", "``") + "`"
    rows = orders(epoch)
    sql_literal = _legacy_literal if fixture_version == LEGACY_VERSION else literal
    values = []
    for row in rows:
        expressions = ", ".join([
            sql_literal(row["order_id"]), sql_literal(row["customer_name"]), sql_literal(row["items"]),
            str(row["quantity"]), row["order_total"], "to_timestamp(" + sql_literal(row["due_at"]) + ")",
            "true" if row["packed"] else "false", "to_timestamp(" + sql_literal(row["created_at"]) + ")",
        ])
        values.append("SELECT " + expressions if fixture_version == VERSION else "(" + expressions + ")")
    # Databricks cannot evaluate decode/unhex inside an inline VALUES table.
    # SELECT supports these expressions while retaining their exact UTF-8 bytes.
    insert = (f"INSERT INTO {quoted_table}\n" + "\nUNION ALL\n".join(values) if fixture_version == VERSION
              else f"INSERT INTO {quoted_table} VALUES " + ",\n".join(values))
    statements = [
        {"name": "create_schema", "statement": f"CREATE SCHEMA {quoted_schema} COMMENT 'Isolated synthetic workshop evaluation data'"},
        {"name": "create_table", "statement": f"CREATE TABLE {quoted_table} (order_id STRING NOT NULL, customer_name STRING NOT NULL, items STRING NOT NULL, quantity INT NOT NULL, order_total DECIMAL(10,2) NOT NULL, due_at TIMESTAMP NOT NULL, packed BOOLEAN NOT NULL, created_at TIMESTAMP NOT NULL) USING DELTA COMMENT 'Sample bakery orders for workshop evaluation'"},
        {"name": "insert_orders", "statement": insert},
        {"name": "grant_schema", "statement": f"GRANT USE SCHEMA ON SCHEMA {quoted_schema} TO {principal}"},
        {"name": "grant_table", "statement": f"GRANT SELECT, MODIFY ON TABLE {quoted_table} TO {principal}"},
    ]
    if fixture_version == VERSION:
        attendee = quote_principal(binding["attendee"]["email"])
        # Finish setup/grants before transferring the operator-created objects.
        statements.extend([
            {"name": "own_table", "statement": f"ALTER TABLE {quoted_table} OWNER TO {attendee}"},
            {"name": "own_schema", "statement": f"ALTER SCHEMA {quoted_schema} OWNER TO {attendee}"},
        ])
    plan = {"schema_version": 1, "operation": "seed_bakery_fixture", "scope": "simulated_control_tower",
            "control_tower_requests": 0, "fixture_version": fixture_version, "marker": marker,
            "workspace_host": binding["workspace_host"], "workspace_id": binding["workspace_id"],
            "profile": binding["profile"], "terminal_app_id": app["id"], "terminal_app_name": app["name"],
            "terminal_deployment_id": binding["deployment_id"], "principal": app["service_principal_client_id"],
            "attendee_email": binding["attendee"]["email"], "terminal_created_at_epoch": binding["created_at"],
            "deployment_receipt_sha256": deployment_receipt_sha256, "catalog": catalog, "schema": SCHEMA,
            "table": table, "epoch": epoch.isoformat(), "expires_at_epoch": binding["expires_at"],
            "rows": rows, "rows_digest": digest(rows), "statements": statements,
            "provisioning_attribution": "external_setup_fixture_not_app_created", "accepted": False}
    plan["plan_digest"] = digest(plan)
    return plan


def validate_plan(plan):
    """Reject altered scope, SQL, rows, or digest in an imported setup receipt."""
    require(isinstance(plan, dict), "fixture_plan_required")
    binding = {"marker": plan.get("marker"), "workspace_host": plan.get("workspace_host"),
               "workspace_id": plan.get("workspace_id"), "profile": plan.get("profile"),
               "deployment_id": plan.get("terminal_deployment_id"), "created_at": plan.get("terminal_created_at_epoch"),
               "expires_at": plan.get("expires_at_epoch"), "attendee": {"email": plan.get("attendee_email")},
               "app": {"id": plan.get("terminal_app_id"), "name": plan.get("terminal_app_name"),
                       "service_principal_client_id": plan.get("principal")},
               "environment": {"WORKSHOP_CATALOG": plan.get("catalog"), "WORKSHOP_SCHEMA": plan.get("schema")}}
    expected = build_plan(binding, deployment_receipt_sha256=plan.get("deployment_receipt_sha256"), epoch=plan.get("epoch"),
                          fixture_version=plan.get("fixture_version"))
    require(expected == plan, "fixture_plan_changed")
    return plan


def read_statement(plan, order_id=None):
    validate_plan(plan)
    parts = plan["table"].split(".")
    require(parts == [plan["catalog"], SCHEMA, TABLE], "seed_table_binding_required")
    statement = ("SELECT order_id, customer_name, items, quantity, CAST(order_total AS STRING) AS order_total, "
                 "unix_timestamp(due_at) AS due_at_epoch, packed, unix_timestamp(created_at) AS created_at_epoch "
                 "FROM " + ".".join(map(quote_identifier, parts)))
    if order_id is not None:
        require(order_id in {row["order_id"] for row in plan["rows"]}, "fixture_record_id_required")
        statement += " WHERE order_id = " + literal(order_id)
    return statement + " ORDER BY order_id"


def normalize_rows(rows):
    normalized = []
    for row in rows:
        require(set(row) == {"order_id", "customer_name", "items", "quantity", "order_total",
                            "due_at_epoch", "packed", "created_at_epoch"}, "backend_columns_unverified")
        value = dict(row)
        for field in ("quantity", "due_at_epoch", "created_at_epoch"):
            require(not isinstance(value[field], bool), "backend_numeric_type_unverified")
            value[field] = int(value[field])
        if value["packed"] in ("true", "false"):
            value["packed"] = value["packed"] == "true"
        require(type(value["packed"]) is bool, "backend_boolean_type_unverified")
        value["order_total"] = str(Decimal(value["order_total"]).quantize(Decimal("0.01")))
        normalized.append(value)
    require(len({row["order_id"] for row in normalized}) == len(normalized), "duplicate_backend_records")
    return sorted(normalized, key=lambda row: row["order_id"])


def expected_rows(plan):
    return normalize_rows([{key: value for key, value in row.items() if key not in {"due_at", "created_at"}}
                           for row in plan["rows"]])


def verify_seed_rows(plan, observed):
    require(normalize_rows(observed) == expected_rows(plan), "seed_data_readback_mismatch")
    return {"verified": True, "row_count": len(observed), "normalized_rows_digest": digest(normalize_rows(observed)),
            "provisioning_attribution": "external_setup_fixture_not_app_created"}


def plan_literal_correction(old_plan, observed_rows, new_binding, *, deployment_receipt_sha256):
    """Plan only the two proved v1 apostrophe corrections; never execute SQL.

    Preserve the original failed receipt and materialized epoch. A caller must
    verify the rebound deployment and retain the same live table UUID before
    applying these setup corrections, then independently reread the whole table.
    """
    validate_plan(old_plan)
    require(old_plan["fixture_version"] == LEGACY_VERSION, "legacy_literal_fixture_required")
    # Historical correction receipts describe v1 -> v2, not a fresh seed. Pin
    # that version so their binding, SQL and digests never change with VERSION.
    new_plan = build_plan(new_binding, deployment_receipt_sha256=deployment_receipt_sha256, epoch=old_plan["epoch"],
                          fixture_version=HEX_VALUES_VERSION)
    for field in ("marker", "workspace_host", "workspace_id", "catalog", "schema", "table",
                  "terminal_app_id", "terminal_app_name", "principal", "epoch"):
        require(old_plan[field] == new_plan[field], "correction_target_binding_changed")
    expected = expected_rows(old_plan)
    observed = normalize_rows(observed_rows)
    require([row["order_id"] for row in expected] == [row["order_id"] for row in observed], "correction_record_set_changed")
    changes = []
    for wanted, actual in zip(expected, observed):
        differences = {key for key in wanted if wanted[key] != actual[key]}
        if differences:
            require(differences == {"customer_name"} and wanted["order_id"] in {"BK-1006", "BK-1009"}
                    and actual["customer_name"] == wanted["customer_name"].replace("'", ""),
                    "unanticipated_fixture_difference")
            changes.append({"order_id": wanted["order_id"], "field": "customer_name", "before": actual["customer_name"],
                            "after": wanted["customer_name"]})
    require({change["order_id"] for change in changes} == {"BK-1006", "BK-1009"}, "exact_two_literal_corrections_required")
    table = ".".join(map(quote_identifier, new_plan["table"].split(".")))
    statements = [{"name": "correct_customer_" + change["order_id"],
                   "statement": f"UPDATE {table} SET customer_name = {literal(change['after'])} "
                                f"WHERE order_id = {literal(change['order_id'])} AND customer_name = {literal(change['before'])}"}
                  for change in changes]
    return {"schema_version": 1, "operation": "repair_bakery_literal_fixture", "scope": "simulated_control_tower",
            "control_tower_requests": 0, "accepted": False, "read_only_plan": True, "plan": new_plan,
            "original_failed_plan_digest": old_plan["plan_digest"], "observed_before_digest": digest(observed),
            "changes": changes, "statements": statements,
            "provisioning_attribution": "external_setup_literal_correction_not_builder_repair",
            "required_proofs": ["same_live_table_id_before_and_after", "rebound_terminal_deployment",
                                "full_data_readback", "scoped_grant_readback", "preserved_original_failure_receipt"]}


def validate_generated_binding(candidate, plan):
    """Pin an actual discovery candidate; the live collector must verify it again."""
    require(isinstance(candidate, dict) and candidate.get("app_id") and candidate["app_id"] != plan["terminal_app_id"]
            and candidate.get("deployment_id") and candidate.get("app_name"), "generated_app_identity_required")
    source = candidate.get("source_code_path", "")
    require(isinstance(source, str) and isinstance(candidate.get("creator"), str), "generated_source_identity_required")
    path = PurePosixPath(source)
    require(source.startswith("/") and str(path) == source and ".." not in path.parts,
            "generated_source_path_required")
    require(candidate.get("creator", "").casefold() == plan["principal"].casefold()
            or candidate["app_name"].startswith(plan["marker"] + "-") or plan["marker"] in path.parts,
            "generated_app_marker_attribution_required")
    return {key: candidate[key] for key in ("app_id", "app_name", "deployment_id", "source_code_path", "creator")}


def assess_pack_transition(before, after):
    """Compare actual bound reads; this is one functional check, never full acceptance."""
    for read in (before, after):
        require(read.get("source") == "independent_sql_statement_execution" and read.get("live_binding_verified") is True
                and read.get("statement_id") and read.get("observed_at"), "live_backend_read_required")
        require(len(read.get("rows", [])) == 1, "exact_backend_record_required")
    require(before["binding"] == after["binding"] and before["binding"].get("table")
            and before["binding"].get("app_id") and before["binding"].get("deployment_id"), "backend_binding_changed")
    require(before["statement_id"] != after["statement_id"], "fresh_backend_read_required")
    require(aware_time(before["observed_at"]) <= aware_time(after["observed_at"]), "backend_read_order_unverified")
    old, new = normalize_rows(before["rows"])[0], normalize_rows(after["rows"])[0]
    require(old["order_id"] == new["order_id"], "backend_record_changed")
    unchanged = {key for key in old if key != "packed"}
    require(all(old[key] == new[key] for key in unchanged), "unrelated_order_fields_changed")
    return {"passed": old["packed"] is False and new["packed"] is True, "record_id": old["order_id"],
            "field": "packed", "before": old["packed"], "after": new["packed"],
            "evidence_references": [before["statement_id"], after["statement_id"]],
            "binding": before["binding"], "accepted": False,
            "limitations": ["Visible UI action, fresh-browser/reload/restart, and UX evidence are separate checks."]}
