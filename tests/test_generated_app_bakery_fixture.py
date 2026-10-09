"""External bakery fixture boundaries; every SQL/catalog client is synthetic."""
import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from databricks.sdk.errors import NotFound

from evals.generated_apps import bakery_fixture as fixture
from scripts import seed_generated_app_fixture as runner


def binding():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {"marker": "wt-eval-seed-1", "workspace_host": "https://labs.example.com", "workspace_id": "123",
            "profile": "labs", "deployment_id": "wt-deploy-1", "created_at": (now - timedelta(minutes=2)).timestamp(),
            "expires_at": (now + timedelta(minutes=30)).timestamp(), "attendee": {"email": "operator@example.com"},
            "app": {"id": "terminal-app-1", "name": "wt-eval-seed-1-wt",
                    "service_principal_client_id": "11111111-1111-4111-8111-111111111111"},
            "environment": {"WORKSHOP_CATALOG": "wt_eval_seed_1", "WORKSHOP_SCHEMA": "generated_apps"}}


def plan(value=None):
    value = value or binding()
    return fixture.build_plan(value, deployment_receipt_sha256="a" * 64, epoch=datetime.now(timezone.utc))


def response(statement_id, rows=None, state="SUCCEEDED", **manifest_options):
    result = None
    manifest = None
    if rows is not None:
        columns = list(rows[0]) if rows else []
        manifest = NS(schema=NS(columns=[NS(name=name) for name in columns]), truncated=False,
                      total_chunk_count=1, total_row_count=len(rows))
        for key, value in manifest_options.items():
            setattr(manifest, key, value)
        result = NS(data_array=[[row[column] for column in columns] for row in rows], next_chunk_index=None)
    return NS(statement_id=statement_id, status=NS(state=state), manifest=manifest, result=result)


class Client:
    def __init__(self, setup, *, existing=None, fail=None, wrong_grants=False, ignore_owner=None):
        self.setup, self.existing, self.fail = setup, existing, fail
        self.wrong_grants = wrong_grants
        self.calls = []
        self.created = set()
        self.owners = {"schema": "operator@example.com", "table": "operator@example.com"}
        self.ignore_owner = ignore_owner
        self.config = NS(host=setup["workspace_host"])
        self.catalogs = NS(get=lambda name: NS(name=name, owner=setup["attendee_email"]))
        self.schemas = NS(get=lambda name: self.get_resource("schema", name))
        self.tables = NS(get=lambda name: self.get_resource("table", name))
        self.statement_execution = NS(execute_statement=self.execute, get_statement=self.poll, cancel_execution=self.cancel)
        self.grants = NS(get=self.get_grants)
        self.rows = fixture.expected_rows(setup)
        self.pending = None
        self.generated = NS(id="generated-app-1", name="bakery", creator=setup["principal"],
                            create_time=datetime.now(timezone.utc).isoformat(),
                            active_deployment=NS(deployment_id="app-deploy-1"))
        self.deployment = NS(deployment_id="app-deploy-1", status=NS(state="SUCCEEDED"),
                             create_time=datetime.now(timezone.utc).isoformat(), source_code_path="/Workspace/Users/operator/bakery")
        self.apps = NS(get=lambda _name: self.generated, get_deployment=lambda *_args: self.deployment)
        self.terminal = NS(id=setup["terminal_app_id"], service_principal_client_id=setup["principal"],
                           active_deployment=NS(deployment_id=setup["terminal_deployment_id"]))
        self.apps.get = lambda name: self.terminal if name == setup["terminal_app_name"] else self.generated

    def get_resource(self, kind, name):
        self.calls.append(("get_" + kind, name))
        if self.existing == kind:
            return NS(name=name)
        if kind in self.created:
            return NS(name=self.setup["schema"] if kind == "schema" else name, full_name=name,
                      catalog_name=self.setup["catalog"], schema_name=self.setup["schema"],
                      table_id="seed-table-id", owner=self.owners[kind])
        raise NotFound("missing")

    def execute(self, **kwargs):
        statement = kwargs["statement"]
        self.calls.append(("sql", statement))
        assert kwargs["warehouse_id"] == "warehouse-1"
        assert kwargs["wait_timeout"] == "10s" and kwargs["row_limit"] == 100
        identifier = "query-" + str(len(self.calls))
        if self.fail and self.fail in statement:
            return response(identifier, state="FAILED")
        if statement.startswith("CREATE SCHEMA"): self.created.add("schema")
        if statement.startswith("CREATE TABLE"): self.created.add("table")
        for kind in ("schema", "table"):
            if statement.startswith("ALTER " + kind.upper() + " "):
                name = self.setup["catalog"] + "." + self.setup["schema"] if kind == "schema" else self.setup["table"]
                target = ".".join(map(fixture.quote_identifier, name.split(".")))
                assert statement == f"ALTER {kind.upper()} {target} OWNER TO {fixture.quote_principal(self.setup['attendee_email'])}"
                if kind != self.ignore_owner:
                    self.owners[kind] = self.setup["attendee_email"]
        rows = self.rows if statement.startswith("SELECT") else None
        if rows is not None and "WHERE order_id" in statement:
            rows = [row for row in rows if row["order_id"] == "BK-1001"]
        return response(identifier, rows)

    def get_grants(self, kind, name):
        self.calls.append(("grants", kind, name))
        principal = "unrelated-sp" if self.wrong_grants else self.setup["principal"]
        return NS(privilege_assignments=[NS(principal=principal, privileges=["USE_SCHEMA"] if kind == "schema" else ["SELECT", "MODIFY"])])

    def poll(self, statement_id):
        return self.pending

    def cancel(self, statement_id):
        self.calls.append(("cancel", statement_id))


def verified_seed(setup):
    return {"schema_version": 1, "status": "seed_verified", "executed": True, "accepted": False,
            "control_tower_requests": 0, "plan": setup, "warehouse_id": "warehouse-1",
            "seeded_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
            "data_readback": {"verified": True}, "grant_readback": {"table": {"verified": True}}}


def candidate(setup):
    return {"app_id": "generated-app-1", "app_name": "bakery", "deployment_id": "app-deploy-1",
            "source_code_path": "/Workspace/Users/operator/bakery", "creator": setup["principal"]}


def make_oracle(client, setup):
    client.created.update({"schema", "table"})
    client.owners.update(schema=setup["attendee_email"], table=setup["attendee_email"])
    seed = verified_seed(setup)
    seed["table_identity"] = runner.verify_table_identity(client, setup)
    return runner.SqlBackendOracle(client, seed, candidate(setup))


def test_fixture_is_deterministic_ordinary_data_with_stable_lateness_and_safe_sql():
    value = binding()
    epoch = datetime.now(timezone.utc).replace(microsecond=0)
    setup = fixture.build_plan(value, deployment_receipt_sha256="a" * 64, epoch=epoch)
    assert setup == fixture.build_plan(value, deployment_receipt_sha256="a" * 64, epoch=epoch)
    assert len(setup["rows"]) == 20 and len({row["order_id"] for row in setup["rows"]}) == 20
    for row in setup["rows"]:
        offset = row["due_at_epoch"] - int(epoch.timestamp())
        assert offset <= -86400 or offset >= 259200
    assert {row["packed"] for row in setup["rows"]} == {True, False}
    assert [row["due_at_epoch"] - int(epoch.timestamp()) for row in setup["rows"][:3]] == [-86400, 259200, -172800]
    insert = next(item["statement"] for item in setup["statements"] if item["name"] == "insert_orders")
    assert insert.startswith("INSERT INTO `wt_eval_seed_1`.`generated_apps`.`bakery_orders`\nSELECT ")
    assert " VALUES " not in insert and insert.count("\nUNION ALL\n") == 19
    assert len(re.findall(r"^SELECT ", insert, re.MULTILINE)) == 20
    assert fixture.literal("Sam O'Brien") in insert
    assert fixture.literal("Elena García") in insert
    assert fixture.literal("Morning tea assortment for the volunteer thank-you gathering") in insert
    sql = "\n".join(item["statement"] for item in setup["statements"])
    assert "IF NOT EXISTS" not in sql and "DROP " not in sql and "REPLACE " not in sql
    grants = [item["statement"] for item in setup["statements"] if item["name"].startswith("grant_")]
    assert len(grants) == 2 and all(setup["principal"] in statement for statement in grants)
    assert all("ALL PRIVILEGES" not in statement and "CATALOG" not in statement for statement in grants)
    assert [item["name"] for item in setup["statements"]][-2:] == ["own_table", "own_schema"]
    assert setup["accepted"] is False
    assert setup["provisioning_attribution"] == "external_setup_fixture_not_app_created"


@pytest.mark.parametrize("mutation", ["catalog", "schema", "marker", "epoch", "receipt"])
def test_plan_rejects_unbound_or_expired_scope(mutation):
    value = binding()
    epoch = datetime.now(timezone.utc)
    receipt = "a" * 64
    if mutation == "catalog": value["environment"]["WORKSHOP_CATALOG"] = "production"
    elif mutation == "schema": value["environment"]["WORKSHOP_SCHEMA"] = "shared"
    elif mutation == "marker": value["marker"] = "production"
    elif mutation == "epoch": epoch += timedelta(days=1)
    elif mutation == "receipt": receipt = ""
    with pytest.raises(ValueError):
        fixture.build_plan(value, deployment_receipt_sha256=receipt, epoch=epoch)


def test_seed_requires_exact_data_not_just_count_or_plausible_rows():
    setup = plan()
    actual = fixture.expected_rows(setup)
    assert fixture.verify_seed_rows(setup, actual)["verified"]
    actual[0]["packed"] = True
    with pytest.raises(ValueError, match="readback_mismatch"):
        fixture.verify_seed_rows(setup, actual)


@pytest.mark.parametrize("value", ["Sam O'Brien", "Lee's Corner Store", "Elena García", r"A backslash \ and quote '", ""])
def test_databricks_literal_preserves_utf8_without_adjacent_quote_or_backslash_rules(value):
    expression = fixture.literal(value)
    match = re.fullmatch(r"decode\(unhex\('([0-9a-f]*)'\), 'UTF-8'\)", expression)
    assert match is not None
    assert bytes.fromhex(match.group(1)).decode("utf-8") == value


def test_v1_plan_remains_readable_but_correction_is_scoped_and_versioned():
    old_binding = binding()
    old = fixture.build_plan(old_binding, deployment_receipt_sha256="a" * 64, epoch=datetime.now(timezone.utc),
                             fixture_version=fixture.LEGACY_VERSION)
    assert fixture.validate_plan(old) == old
    assert "Sam O''Brien" in old["statements"][2]["statement"]
    actual = fixture.expected_rows(old)
    for row in actual:
        if row["order_id"] in {"BK-1006", "BK-1009"}:
            row["customer_name"] = row["customer_name"].replace("'", "")
    rebound = copy.deepcopy(old_binding)
    rebound["attendee"]["email"] = "labuser+1@example.com"
    rebound["deployment_id"] = "wt-deploy-2"
    correction = fixture.plan_literal_correction(old, actual, rebound, deployment_receipt_sha256="b" * 64)
    assert correction["plan"]["fixture_version"] == fixture.HEX_VALUES_VERSION
    assert correction["plan"]["epoch"] == old["epoch"]
    assert correction["plan"]["attendee_email"] == "labuser+1@example.com"
    assert correction["plan"]["terminal_deployment_id"] == "wt-deploy-2"
    assert len(correction["statements"]) == 2 and correction["accepted"] is False
    for change, statement in zip(correction["changes"], correction["statements"]):
        assert statement["statement"].startswith("UPDATE `wt_eval_seed_1`.`generated_apps`.`bakery_orders`")
        assert fixture.literal(change["order_id"]) in statement["statement"]
        assert fixture.literal(change["before"]) in statement["statement"]
        assert fixture.literal(change["after"]) in statement["statement"]
    assert fixture.verify_seed_rows(correction["plan"], fixture.expected_rows(old))["verified"]


def historical_binding():
    value = binding()
    value.update(created_at=1780358400, expires_at=1780362000)
    value["attendee"]["email"] = "labuser+1@example.com"
    return value


@pytest.mark.parametrize("version,expected_digest", [
    (fixture.LEGACY_VERSION, "74ed906817085bbbb471c3cfc4422c5ee9289d292a34c46916e19cbdb3ad1c83"),
    (fixture.HEX_VALUES_VERSION, "d5bc1588a4a69ef9a93930f81f0138df33c0deff0a02a7963be43d0b1eac0dd0"),
])
def test_historical_plans_reconstruct_exact_pre_v3_receipt_bytes(version, expected_digest):
    old = fixture.build_plan(historical_binding(), deployment_receipt_sha256="a" * 64,
                             epoch="2026-06-02T00:10:00+00:00", fixture_version=version)
    assert fixture.validate_plan(old) == old and old["plan_digest"] == expected_digest
    assert [item["name"] for item in old["statements"]] == ["create_schema", "create_table", "insert_orders", "grant_schema", "grant_table"]
    assert " VALUES " in old["statements"][2]["statement"]
    assert fixture.read_statement(old).startswith("SELECT ")


def test_historical_literal_correction_reconstructs_exact_pre_v3_digest():
    value = historical_binding()
    old = fixture.build_plan(value, deployment_receipt_sha256="a" * 64,
                             epoch="2026-06-02T00:10:00+00:00", fixture_version=fixture.LEGACY_VERSION)
    actual = fixture.expected_rows(old)
    for row in actual:
        if row["order_id"] in {"BK-1006", "BK-1009"}:
            row["customer_name"] = row["customer_name"].replace("'", "")
    correction = fixture.plan_literal_correction(old, actual, dict(value, deployment_id="wt-deploy-2"),
                                                deployment_receipt_sha256="b" * 64)
    assert correction["plan"]["fixture_version"] == fixture.HEX_VALUES_VERSION
    assert correction["plan"]["plan_digest"] == "44f6f6ab5c2fc5b8f6d2e8a1c40ff3202796c378864e7b25eeac8ac4ff7b6df6"
    assert fixture.digest(correction) == "52bfbcfdf87e779573cec4da0fc69a1dc606c70dac5d19e176f368782872ba80"


@pytest.mark.parametrize("version", [fixture.LEGACY_VERSION, fixture.HEX_VALUES_VERSION])
def test_historical_plan_is_readable_but_cannot_start_new_seed(tmp_path, version):
    value = binding()
    old = fixture.build_plan(value, deployment_receipt_sha256="a" * 64,
                             epoch=datetime.now(timezone.utc), fixture_version=version)
    client = Client(old)
    output = tmp_path / "historical-new-seed.json"
    with pytest.raises(runner.SeedError, match="new_seed_requires_current_fixture_version"):
        runner.seed(value, old, output, client=client, warehouse_id="warehouse-1")
    assert not client.calls and not output.exists()


def test_owner_principal_is_safely_quoted_and_scoped_only_to_seeded_objects():
    value = binding()
    value["attendee"]["email"] = "lab`user+1@example.com"
    setup = plan(value)
    owner_sql = {operation["name"]: operation["statement"] for operation in setup["statements"] if operation["name"].startswith("own_")}
    assert owner_sql == {
        "own_table": "ALTER TABLE `wt_eval_seed_1`.`generated_apps`.`bakery_orders` OWNER TO `lab``user+1@example.com`",
        "own_schema": "ALTER SCHEMA `wt_eval_seed_1`.`generated_apps` OWNER TO `lab``user+1@example.com`",
    }


@pytest.mark.parametrize("principal", ["", " user@example.com", "user@example.com\n", "user\x00@example.com", "x" * 321])
def test_new_owner_principal_must_be_bounded_and_without_control_characters(principal):
    value = binding()
    value["attendee"]["email"] = principal
    with pytest.raises(fixture.FixtureValidationError, match="bounded_principal_required"):
        plan(value)


@pytest.mark.parametrize("mutation", ["third_record", "packed", "already_correct", "another_terminal", "another_catalog", "another_workspace"])
def test_literal_correction_cannot_rewrite_unexpected_data_or_resources(mutation):
    value = binding()
    old = fixture.build_plan(value, deployment_receipt_sha256="a" * 64, epoch=datetime.now(timezone.utc),
                             fixture_version=fixture.LEGACY_VERSION)
    actual = fixture.expected_rows(old)
    for row in actual:
        if row["order_id"] in {"BK-1006", "BK-1009"}:
            row["customer_name"] = row["customer_name"].replace("'", "")
    rebound = copy.deepcopy(value)
    if mutation == "third_record": actual[0]["customer_name"] = "Different name"
    elif mutation == "packed": actual[0]["packed"] = True
    elif mutation == "already_correct": actual = fixture.expected_rows(old)
    elif mutation == "another_terminal": rebound["app"]["id"] = "other-terminal"
    elif mutation == "another_catalog": rebound["environment"]["WORKSHOP_CATALOG"] = "global"
    elif mutation == "another_workspace": rebound["workspace_host"] = "https://other.example.com"
    with pytest.raises(fixture.FixtureValidationError):
        fixture.plan_literal_correction(old, actual, rebound, deployment_receipt_sha256="b" * 64)


@pytest.mark.parametrize("mutation", ["global_catalog", "changed_row", "extra_sql", "digest"])
def test_imported_plan_cannot_change_seed_scope_or_operations(mutation):
    setup = plan()
    if mutation == "global_catalog":
        setup["catalog"] = "global"
        setup["table"] = "global.generated_apps.bakery_orders"
    elif mutation == "changed_row": setup["rows"][0]["packed"] = True
    elif mutation == "extra_sql": setup["statements"].append({"name": "extra", "statement": "DROP TABLE other.table"})
    elif mutation == "digest": setup["plan_digest"] = "0" * 64
    with pytest.raises(ValueError): fixture.validate_plan(setup)


@pytest.mark.parametrize("existing", ["schema", "table"])
def test_existing_resource_blocks_every_sql_mutation(tmp_path, monkeypatch, existing):
    value = binding()
    setup = plan(value)
    client = Client(setup, existing=existing)
    monkeypatch.setattr(runner, "verify_deployment", lambda *_args: {"verified": True})
    with pytest.raises(runner.SeedError, match="fresh_"):
        runner.seed(value, setup, tmp_path / "receipt.json", client=client, warehouse_id="warehouse-1")
    assert not [call for call in client.calls if call[0] == "sql"]
    evidence = json.loads((tmp_path / "receipt.json").read_text())
    assert evidence["status"] == "failed" and not evidence["created_resources"]


def test_successful_seed_retains_resource_statement_and_grant_readback(tmp_path, monkeypatch):
    value = binding()
    setup = plan(value)
    client = Client(setup)
    monkeypatch.setattr(runner, "verify_deployment", lambda *_args: {"verified": True})
    evidence = runner.seed(value, setup, tmp_path / "receipt.json", client=client, warehouse_id="warehouse-1")
    assert evidence["status"] == "seed_verified" and evidence["accepted"] is False
    assert [resource["kind"] for resource in evidence["created_resources"]] == ["schema", "table"]
    assert all(resource["state"] == "created" for resource in evidence["created_resources"])
    assert len(evidence["statements"]) == 8 and all(item["statement_id"] for item in evidence["statements"])
    assert evidence["data_readback"]["row_count"] == 20 and set(evidence["grant_readback"]) == {"schema", "table"}


@pytest.mark.parametrize("changed_target", [False, True])
def test_fixture_observer_continuation_is_read_only_and_cannot_change_target(tmp_path, monkeypatch, changed_target):
    value = binding(); setup = plan(value); client = Client(setup)
    monkeypatch.setattr(runner, "verify_deployment", lambda *_args: {"verified": True})
    seed = runner.seed(value, setup, tmp_path / "seed.json", client=client, warehouse_id="warehouse-1")
    rebound = copy.deepcopy(value); rebound["deployment_id"] = "wt-deploy-2"
    if changed_target: rebound["app"]["service_principal_client_id"] = "other-sp"
    monkeypatch.setattr(runner, "validate_receipt", lambda receipt: rebound)
    receipt = {"observer_update": {"package_bytes_unchanged": True},
               "previous_deployment": {"deployment_id": "wt-deploy-1"}}
    before = len(client.calls)
    if changed_target:
        with pytest.raises(runner.SeedError, match="target_changed"):
            runner.rebind_seed(receipt, b"new deployment", seed, b"previous seed", tmp_path / "rebind.json", client=client)
        assert len(client.calls) == before
    else:
        result = runner.rebind_seed(receipt, b"new deployment", seed, b"previous seed", tmp_path / "rebind.json", client=client)
        assert result["status"] == "seed_verified" and result["table_identity"] == seed["table_identity"]
        assert result["plan"]["epoch"] == setup["epoch"] and result["plan"]["expires_at_epoch"] == setup["expires_at_epoch"]
        assert result["fixture_continuation"]["workspace_mutations"] == 0
        assert all(call[1].startswith("SELECT ") for call in client.calls[before:] if call[0] == "sql")


def test_operator_created_seed_transfers_table_and_schema_to_distinct_attendee(tmp_path, monkeypatch):
    value = binding()
    value["attendee"]["email"] = "labuser+1@example.com"
    setup = plan(value)
    client = Client(setup)
    assert client.owners == {"schema": "operator@example.com", "table": "operator@example.com"}
    monkeypatch.setattr(runner, "verify_deployment", lambda *_args: {"verified": True})
    evidence = runner.seed(value, setup, tmp_path / "receipt.json", client=client, warehouse_id="warehouse-1")
    assert evidence["status"] == "seed_verified"
    assert client.owners == {"schema": "labuser+1@example.com", "table": "labuser+1@example.com"}
    assert evidence["table_identity"]["owner"] == evidence["schema_identity"]["owner"] == "labuser+1@example.com"
    assert [item["name"] for item in evidence["statements"]][-3:] == ["own_table", "own_schema", "read_seed"]


@pytest.mark.parametrize("kind", ["table", "schema"])
def test_unapplied_ownership_transfer_cannot_be_marked_verified(tmp_path, monkeypatch, kind):
    value = binding()
    value["attendee"]["email"] = "labuser+1@example.com"
    setup = plan(value)
    client = Client(setup, ignore_owner=kind)
    monkeypatch.setattr(runner, "verify_deployment", lambda *_args: {"verified": True})
    output = tmp_path / "receipt.json"
    with pytest.raises(runner.SeedError, match="seed_" + kind + "_identity_unverified"):
        runner.seed(value, setup, output, client=client, warehouse_id="warehouse-1")
    evidence = json.loads(output.read_text())
    assert evidence["status"] == "failed" and evidence["accepted"] is False
    assert evidence["data_readback"]["verified"] is True
    assert client.owners[kind] == "operator@example.com"


def test_partial_insert_failure_preserves_exact_cleanup_ownership(tmp_path, monkeypatch):
    value = binding()
    setup = plan(value)
    client = Client(setup, fail="INSERT INTO")
    monkeypatch.setattr(runner, "verify_deployment", lambda *_args: {"verified": True})
    with pytest.raises(runner.SeedError, match="statement_failed"):
        runner.seed(value, setup, tmp_path / "receipt.json", client=client, warehouse_id="warehouse-1")
    evidence = json.loads((tmp_path / "receipt.json").read_text())
    assert evidence["status"] == "failed" and len(evidence["created_resources"]) == 2
    assert evidence["statements"][-1]["name"] == "insert_orders"
    assert evidence["statements"][-1]["statement_id"] and evidence["statements"][-1]["status"] == "FAILED"
    assert not [call for call in client.calls if call[0] == "grants"]


def test_wrong_sp_grants_cannot_be_marked_verified(tmp_path, monkeypatch):
    value = binding()
    setup = plan(value)
    monkeypatch.setattr(runner, "verify_deployment", lambda *_args: {"verified": True})
    with pytest.raises(runner.SeedError, match="grant_readback"):
        runner.seed(value, setup, tmp_path / "receipt.json", client=Client(setup, wrong_grants=True), warehouse_id="warehouse-1")


def test_async_statement_deadline_retains_and_cancels_only_its_own_id():
    setup = plan()
    client = Client(setup)
    pending = response("owned-query", state="RUNNING")
    client.statement_execution.execute_statement = lambda **_kwargs: pending
    client.pending = pending
    elapsed = [0.0]
    events = []
    sql = runner.StatementRunner(client, "warehouse-1", record=lambda event: events.append(dict(event)), timeout_seconds=1,
                                clock=lambda: elapsed[0], sleep=lambda amount: elapsed.__setitem__(0, elapsed[0] + amount))
    with pytest.raises(runner.SeedError, match="deadline"):
        sql.run("owned_select", "SELECT 1")
    assert ("cancel", "owned-query") in client.calls
    assert events[-1]["statement_id"] == "owned-query" and events[-1]["status"] == "deadline_cancel_requested"


@pytest.mark.parametrize("change", ["truncated", "total_chunk_count", "total_row_count"])
def test_incomplete_backend_result_cannot_supply_truth(change):
    options = {"truncated": True} if change == "truncated" else {change: 2}
    with pytest.raises(runner.SeedError):
        runner.rows_from_response(response("read-1", [{"order_id": "BK-1001"}], **options))


def test_oracle_reads_real_values_before_and_after_and_rejects_noop():
    setup = plan()
    client = Client(setup)
    oracle = make_oracle(client, setup)
    before = oracle.read("BK-1001")
    unchanged = oracle.read("BK-1001")
    assert not fixture.assess_pack_transition(before, unchanged)["passed"]
    client.rows[0]["packed"] = True  # Synthetic stand-in for the visible app's actual write.
    after = oracle.read("BK-1001")
    assert fixture.assess_pack_transition(before, after)["passed"]
    assert fixture.assess_pack_transition(before, after)["accepted"] is False
    assert all("WHERE order_id = " + fixture.literal("BK-1001") in call[1] for call in client.calls if call[0] == "sql")


def test_table_replacement_and_terminal_redeployment_invalidate_oracle():
    setup = plan()
    client = Client(setup)
    client.created.update({"schema", "table"})
    receipt = verified_seed(setup)
    receipt["table_identity"] = runner.verify_table_identity(client, setup)
    client.terminal.active_deployment.deployment_id = "unreviewed-terminal-version"
    with pytest.raises(runner.SeedError, match="terminal_receipt_changed"):
        runner.SqlBackendOracle(client, receipt, candidate(setup))
    client.terminal.active_deployment.deployment_id = setup["terminal_deployment_id"]
    oracle = runner.SqlBackendOracle(client, receipt, candidate(setup))
    original = client.tables.get
    def replacement(name):
        table = original(name)
        table.table_id = "replacement-table-id"
        return table
    client.tables.get = replacement
    with pytest.raises(runner.SeedError, match="table_identity_changed"):
        oracle.read("BK-1001")


@pytest.mark.parametrize("mutation", ["app", "deployment", "source", "old_app", "table", "fake_source", "reuse_read", "record", "unrelated_field"])
def test_oracle_or_transition_rejects_misattribution(mutation):
    setup = plan()
    client = Client(setup)
    oracle = make_oracle(client, setup)
    before = oracle.read("BK-1001")
    client.rows[0]["packed"] = True
    if mutation == "app": client.generated.id = "another-app"
    elif mutation == "deployment": client.generated.active_deployment.deployment_id = "another-deployment"
    elif mutation == "source": client.deployment.source_code_path = "/Workspace/unrelated/source"
    elif mutation == "old_app": client.generated.create_time = "2000-01-01T00:00:00+00:00"
    if mutation in {"app", "deployment", "source", "old_app"}:
        with pytest.raises(runner.SeedError): oracle.read("BK-1001")
        return
    after = oracle.read("BK-1001")
    if mutation == "table": after["binding"]["table"] = "global.shared.orders"
    elif mutation == "fake_source": after["source"] = "model says packed"
    elif mutation == "reuse_read": after["statement_id"] = before["statement_id"]
    elif mutation == "record": after["rows"][0]["order_id"] = "BK-1002"
    elif mutation == "unrelated_field": after["rows"][0]["customer_name"] = "changed customer"
    with pytest.raises(ValueError): fixture.assess_pack_transition(before, after)


def test_plan_cli_is_offline_and_execute_requires_reviewed_exact_receipt(tmp_path, monkeypatch):
    value = binding()
    receipt = tmp_path / "deployment.json"
    receipt.write_text('{"synthetic":true}\n')
    monkeypatch.setattr(runner, "validate_receipt", lambda _receipt: value)
    planned = tmp_path / "plan.json"
    assert runner.main(["--receipt", str(receipt), "--output", str(planned)]) == 0
    result = json.loads(planned.read_text())
    assert result["executed"] is False and result["control_tower_requests"] == 0
    receipt.write_text('{"synthetic":true,"changed":true}\n')
    blocked = tmp_path / "blocked.json"
    assert runner.main(["--receipt", str(receipt), "--reviewed-plan", str(planned), "--output", str(blocked), "--execute"]) == 2
    assert json.loads(blocked.read_text())["status"] == "failed"


def test_cli_refuses_overwriting_receipt_or_old_output(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    with pytest.raises(SystemExit): runner.main(["--receipt", str(receipt), "--output", str(receipt)])
    with pytest.raises(SystemExit): runner.main(["--receipt", str(receipt), "--output", str(tmp_path / "new.json"), "--execute"])
    assert receipt.read_text() == "{}"


def test_real_installed_sdk_constructor_accepts_config_timeouts_offline(tmp_path, monkeypatch):
    """Keep real Config/WorkspaceClient constructors; stub only host discovery I/O."""
    import databricks.sdk.config as sdk_config
    from databricks.sdk import WorkspaceClient
    for name in list(os.environ):
        if name.startswith("DATABRICKS_"):
            monkeypatch.delenv(name)
    config_file = tmp_path / "fake-databrickscfg"
    config_file.write_text("[offline]\nhost = https://sdk-offline.example.invalid\ntoken = dummy-offline-token\nauth_type = pat\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(sdk_config, "get_host_metadata", lambda _host: NS(
        account_id=None, workspace_id=None, oidc_endpoint=None, cloud=None,
        token_federation_default_oidc_audiences=[]))
    client = runner.workspace_client("offline")
    assert isinstance(client, WorkspaceClient)
    assert client.config.host == "https://sdk-offline.example.invalid"
    assert client.config.http_timeout_seconds == 10 and client.config.retry_timeout_seconds == 15
