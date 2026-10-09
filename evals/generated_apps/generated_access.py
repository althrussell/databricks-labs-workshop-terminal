"""Exact generated-SP read grants replacing CT's contained workspace grant.

External isolated-test provisioning only. No account groups, shared catalogs,
warehouse ACLs or WT instructions are changed. A recorded fresh app and the
receipt-owned catalog/fixture schema must all match before a grant is issued.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .ct_deployment import grant
from .report import write_evidence


class GeneratedAppReadAccess:
    def __init__(self, client, binding, receipt, seed_path, output):
        from scripts.run_generated_app_journey import require
        from .bakery_fixture import validate_plan
        self.client, self.binding, self.output = client, binding, Path(output)
        require(not self.output.exists() and not self.output.is_symlink(), "fresh_generated_access_evidence_required")
        raw = Path(seed_path).read_bytes()
        seed = json.loads(raw)
        plan = validate_plan(seed.get("plan"))
        require((binding.get("package") or {}).get("version") == 2
                and receipt.get("scope") == "simulated_control_tower" and receipt.get("control_tower_requests") == 0
                and seed.get("scope") == "simulated_control_tower" and seed.get("control_tower_requests") == 0
                and seed.get("status") == "seed_verified" and seed.get("executed") is True
                and plan["terminal_deployment_id"] == binding["deployment_id"]
                and plan["terminal_app_id"] == binding["app"]["id"]
                and plan["workspace_host"] == binding["workspace_host"] and plan["workspace_id"] == binding["workspace_id"]
                and plan["expires_at_epoch"] == binding["expires_at"]
                and plan["principal"] == binding["app"]["service_principal_client_id"], "generated_access_binding_unverified")
        owned = [r for r in receipt["created_resources"] if r["kind"] == "catalog"]
        require(len(owned) == 1 and owned[0]["name"] == plan["catalog"] == binding["marker"].replace("-", "_")
                and owned[0]["owner"] == binding["catalog_owner"] == receipt["operator"]["email"], "generated_access_catalog_unverified")
        self.catalog, self.schema, self.table = dict(owned[0]), seed["schema_identity"], seed["table_identity"]
        require(self.schema["full_name"] == plan["catalog"] + "." + plan["schema"], "generated_access_schema_unverified")
        require(self.table["full_name"] == plan["table"] and self.table.get("table_id"), "generated_access_table_unverified")
        self.verified = {}
        self.evidence = {"schema_version": 1, "operation": "qualify_exact_generated_app_read_access",
            "scope": "simulated_control_tower", "control_tower_mutations": 0,
            "strategy": "exact_generated_sp_on_receipt_owned_catalog_and_fixture_schema",
            "seed_receipt_sha256": hashlib.sha256(raw).hexdigest(), "catalog": self.catalog["name"],
            "schema": self.schema["full_name"], "status": "awaiting_generated_app", "apps": [], "permission_deltas": []}
        write_evidence(self.output, self.evidence)

    def qualify(self, app, *, started_at):
        from scripts.run_generated_app_journey import require, timestamp, canonical_uuid
        bound, client = self.binding, self.client
        now = datetime.now(timezone.utc)
        require(now.timestamp() < bound["expires_at"] and client.config.host.rstrip("/") == bound["workspace_host"],
                "generated_access_expired_or_wrong_workspace")
        owned_creator = app.creator == bound["app"]["service_principal_client_id"]
        marked_attendee = app.creator == bound["attendee"]["email"] and app.name.startswith(bound["marker"] + "-")
        require(canonical_uuid(app.id) and app.id != bound["app"]["id"] and (owned_creator or marked_attendee)
                and started_at <= timestamp(app.create_time) <= now.timestamp() + 5, "generated_access_app_attribution_unverified")
        principal = getattr(app, "service_principal_client_id", None)
        numeric = getattr(app, "service_principal_id", None)
        if not canonical_uuid(principal) or type(numeric) is not int or numeric <= 0:
            return False
        require(principal != bound["app"]["service_principal_client_id"]
                and numeric != bound["app"]["service_principal_id"], "generated_access_principal_reused")
        identity = (app.id, app.name, app.creator, app.create_time, principal, numeric)
        if app.id in self.verified:
            require(self.verified[app.id] == identity, "generated_access_app_identity_changed")
            return True
        actual = client.apps.get(app.name)
        require((actual.id, actual.name, actual.creator, actual.create_time,
                 actual.service_principal_client_id, actual.service_principal_id) == identity,
                "generated_access_app_identity_changed")
        sp = client.service_principals.get(str(numeric))
        require(sp.application_id == principal and str(sp.id) == str(numeric), "generated_access_sp_identity_unverified")
        catalog = client.catalogs.get(self.catalog["name"])
        require(catalog.owner == self.catalog["owner"] and catalog.created_at == self.catalog["created_at"]
                and catalog.created_by == self.catalog["created_by"] and catalog.metastore_id == self.catalog["metastore_id"],
                "generated_access_catalog_identity_changed")
        schema = client.schemas.get(self.schema["full_name"])
        require(schema.full_name == self.schema["full_name"] and schema.catalog_name == self.schema["catalog_name"]
                and schema.name == self.schema["name"] and schema.owner == self.schema["owner"],
                "generated_access_schema_identity_changed")
        table = client.tables.get(self.table["full_name"])
        require(table.table_id == self.table["table_id"] and table.full_name == self.table["full_name"],
                "generated_access_table_identity_changed")
        entry = {"app_id": app.id, "app_name": app.name, "creator": app.creator,
            "create_time": app.create_time, "principal": principal, "service_principal_id": numeric, "state": "granting"}
        self.evidence["apps"].append(entry)
        self.evidence["status"] = "granting"
        write_evidence(self.output, self.evidence)
        grant(client, self.evidence, self.output, "CATALOG", self.catalog["name"], principal, {"USE_CATALOG", "SELECT"})
        require(datetime.now(timezone.utc).timestamp() < bound["expires_at"], "generated_access_expired_or_wrong_workspace")
        grant(client, self.evidence, self.output, "SCHEMA", self.schema["full_name"], principal, {"USE_SCHEMA", "SELECT"})
        entry.update(state="independently_verified", verified_at=datetime.now(timezone.utc).isoformat())
        self.evidence["status"] = "qualified"
        write_evidence(self.output, self.evidence)
        self.verified[app.id] = identity
        return True
