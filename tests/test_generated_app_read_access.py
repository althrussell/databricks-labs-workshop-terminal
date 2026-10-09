"""CT parity grants remain confined to a newly attributed app and owned data."""
import json
from types import SimpleNamespace as NS

import pytest

from evals.generated_apps.bakery_fixture import build_plan
from evals.generated_apps.generated_access import GeneratedAppReadAccess
from scripts import run_generated_app_journey as runner
from .test_generated_app_journey_command import fixture, Client, new_app, APP_ID


def seat(tmp_path):
    receipt, _, now = fixture()
    bound = runner.validate_receipt(receipt, now=now)
    bound["package"] = {"version": 2}
    bound["catalog_owner"] = bound["attendee"]["email"]
    receipt["operator"] = {"email": bound["catalog_owner"]}
    catalog = next(r for r in receipt["created_resources"] if r["kind"] == "catalog")
    catalog.update(owner=bound["catalog_owner"], created_by=bound["catalog_owner"], created_at=123, metastore_id="metastore")
    plan = build_plan(bound, deployment_receipt_sha256="a" * 64, epoch=now)
    schema = {"full_name": plan["catalog"] + "." + plan["schema"], "catalog_name": plan["catalog"],
              "name": plan["schema"], "owner": bound["attendee"]["email"]}
    table = {"full_name": plan["table"], "table_id": "table-uuid"}
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"scope": "simulated_control_tower", "control_tower_requests": 0,
                               "status": "seed_verified", "executed": True, "plan": plan,
                               "schema_identity": schema, "table_identity": table}))
    app = NS(id="33333333-3333-4333-8333-333333333333", name="bakery-orders", creator=APP_ID,
             create_time=now.isoformat(), service_principal_id=789,
             service_principal_client_id="44444444-4444-4444-8444-444444444444")
    state = {(kind, name): set() for kind, name in [("CATALOG", catalog["name"]), ("SCHEMA", schema["full_name"])]}
    updates = []
    effective = {"ok": True}
    def read(kind, name, *, principal=None):
        assert (kind, name) in state
        values = state[kind, name] if principal is None or effective["ok"] else set()
        return NS(privilege_assignments=[NS(principal=app.service_principal_client_id, privileges=list(values))])
    def update(kind, name, *, changes):
        assert (kind, name) in state and len(changes) == 1
        change = changes[0]
        assert change.principal == app.service_principal_client_id and not change.remove
        values = {p.value for p in change.add}
        updates.append((kind, name, change.principal, values))
        state[kind, name].update(values)
    client = NS(config=NS(host=bound["workspace_host"]), apps=NS(get=lambda name: app),
        service_principals=NS(get=lambda id: NS(id=str(app.service_principal_id), application_id=app.service_principal_client_id)),
        catalogs=NS(get=lambda name: NS(**{k: catalog[k] for k in ["owner", "created_by", "created_at", "metastore_id"]})),
        schemas=NS(get=lambda name: NS(**schema)), tables=NS(get=lambda name: NS(**table)),
        grants=NS(get=read, get_effective=read, update=update))
    access = GeneratedAppReadAccess(client, bound, receipt, seed, tmp_path / "access.json")
    return NS(access=access, client=client, bound=bound, app=app, catalog=catalog, schema=schema,
              table=table, updates=updates, effective=effective, start=now.timestamp() - 1)


def test_new_app_sp_gets_exact_ct_read_equivalence_before_successful_deploy(tmp_path):
    s = seat(tmp_path)
    assert s.access.qualify(s.app, started_at=s.start)
    assert s.updates == [("CATALOG", s.catalog["name"], s.app.service_principal_client_id, {"USE_CATALOG", "SELECT"}),
                         ("SCHEMA", s.schema["full_name"], s.app.service_principal_client_id, {"USE_SCHEMA", "SELECT"})]
    assert s.access.evidence["status"] == "qualified"
    assert all(row["state"] == "independently_verified" for row in s.access.evidence["permission_deltas"])
    assert "account users" not in json.dumps(s.access.evidence)
    assert s.access.qualify(s.app, started_at=s.start) and len(s.updates) == 2


@pytest.mark.parametrize("mutation", ["owner", "catalog_created", "metastore", "schema_owner", "table_uuid",
    "wrong_workspace", "expired", "foreign_creator", "old_app", "wt_principal", "sp_mismatch"])
def test_changed_or_unowned_identity_receives_no_grants(tmp_path, mutation):
    s = seat(tmp_path)
    if mutation == "owner": s.catalog["owner"] = "foreign@example.com"
    elif mutation == "catalog_created": s.catalog["created_at"] += 1
    elif mutation == "metastore": s.catalog["metastore_id"] = "other-metastore"
    elif mutation == "schema_owner": s.schema["owner"] = "foreign@example.com"
    elif mutation == "table_uuid":
        s.client.tables.get = lambda name: NS(full_name=s.table["full_name"], table_id="replacement")
    elif mutation == "wrong_workspace": s.client.config.host = "https://other.example.com"
    elif mutation == "expired": s.bound["expires_at"] = s.start - 1
    elif mutation == "foreign_creator": s.app.creator = "someone@example.com"
    elif mutation == "old_app": s.start += 2
    elif mutation == "wt_principal": s.app.service_principal_client_id = APP_ID
    elif mutation == "sp_mismatch": s.client.service_principals.get = lambda id: NS(id=id, application_id=APP_ID)
    with pytest.raises(runner.GateError): s.access.qualify(s.app, started_at=s.start)
    assert s.updates == []


def test_pending_sp_is_observed_without_granting_other_principals(tmp_path):
    s = seat(tmp_path); s.app.service_principal_client_id = None
    assert not s.access.qualify(s.app, started_at=s.start) and s.updates == []


def test_effective_readback_is_required_and_partial_grant_receipt_survives(tmp_path):
    s = seat(tmp_path); s.effective["ok"] = False
    with pytest.raises(RuntimeError, match="Effective grant readback failed"):
        s.access.qualify(s.app, started_at=s.start)
    assert len(s.updates) == 1
    result = json.loads(s.access.output.read_text())
    assert result["status"] == "granting" and result["permission_deltas"][0]["state"] == "grant_requested"


def test_discovery_never_provisions_read_access_for_ambiguous_apps():
    receipt, files, now = fixture(); client = Client(receipt, files, now)
    access = NS(qualify=lambda *args, **kwargs: pytest.fail("ambiguous app received grants"))
    discovery = runner.AppDiscovery(client, runner.validate_receipt(receipt, now=now), read_access=access)
    discovery.start()
    first = new_app(client, creator=APP_ID, marker=False)
    second = new_app(client, creator=APP_ID, marker=False, name="second"); second.id = "second-app-id"
    assert discovery.observe().status == "needs_review"


def test_discovery_provisions_before_app_build_finishes():
    receipt, files, now = fixture(); client = Client(receipt, files, now); calls = []
    access = NS(qualify=lambda app, **kwargs: calls.append(app.id) or True)
    discovery = runner.AppDiscovery(client, runner.validate_receipt(receipt, now=now), read_access=access)
    discovery.start(); new_app(client, creator=APP_ID, marker=False, state="IN_PROGRESS")
    assert discovery.observe().status == "awaiting" and calls == ["new-app-id"]
