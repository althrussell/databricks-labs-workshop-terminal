"""Independent qualification and receipt-bound teardown for CT-compatible WT."""
from __future__ import annotations
import hashlib
import json
import time
from pathlib import Path

import requests
from databricks.sdk.errors import NotFound

from .ct_deployment import ensure_live, identity, privileges, verify_package
from .report import write_evidence


def canonical_rows(rows):
    """Compare ACL sets independently of API row/nested-list ordering."""
    def canonical(value):
        if isinstance(value, list):
            return sorted((canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        return value
    return canonical(rows)


def remove_owned_acl(client, object_type, object_id, principal, expected_permission):
    """Permissions PATCH cannot delete a grant; SET preserves all other direct ACLs.

    Read twice before SET to reject a concurrent edit, retain inherited ACLs via
    their parent, and independently compare every other principal afterwards.
    """
    from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel
    before = client.permissions.get(object_type, object_id).as_dict().get("access_control_list", [])
    others = [row for row in before if row.get("service_principal_name") != principal]
    target = [row for row in before if row.get("service_principal_name") == principal]
    direct = {p["permission_level"] for row in target for p in row.get("all_permissions", []) if not p.get("inherited", False)}
    if not direct <= {expected_permission}:
        raise ValueError("Test SP shared ACL was expanded after provisioning")
    if not direct:
        return
    remaining = []
    for row in others:
        levels = {p["permission_level"] for p in row.get("all_permissions", []) if not p.get("inherited", False)}
        if len(levels) > 1:
            raise ValueError("Ambiguous shared direct ACL; cleanup refused")
        if levels:
            remaining.append(AccessControlRequest(**{key: row[key] for key in (
                "user_name", "group_name", "service_principal_name") if row.get(key)},
                permission_level=PermissionLevel(next(iter(levels)))))
    check = client.permissions.get(object_type, object_id).as_dict().get("access_control_list", [])
    if canonical_rows(before) != canonical_rows(check):
        raise ValueError("Shared ACL changed before cleanup")
    client.permissions.set(object_type, object_id, access_control_list=remaining)
    after = client.permissions.get(object_type, object_id).as_dict().get("access_control_list", [])
    if any(p for row in after if row.get("service_principal_name") == principal
           for p in row.get("all_permissions", []) if not p.get("inherited", False)):
        raise RuntimeError("Exact SP direct ACL removal did not verify")
    if canonical_rows(others) != canonical_rows([row for row in after if row.get("service_principal_name") != principal]):
        raise RuntimeError("Other principals changed during shared ACL removal")


def wait_for_deployment(client, receipt, *, deadline):
    expected = receipt["deployment"]["deployment_id"]
    while True:
        ensure_live(receipt["plan"])
        app = identity(client, receipt)
        deployment = client.apps.get_deployment(app.name, expected)
        state = getattr(getattr(deployment, "status", None), "state", None)
        state = getattr(state, "value", state)
        if state in {"FAILED", "CANCELLED"}:
            raise RuntimeError("The recorded WT deployment failed")
        if (getattr(app.active_deployment, "deployment_id", None) == expected and state == "SUCCEEDED"
                and getattr(getattr(app.app_status, "state", None), "value", None) == "RUNNING"):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("The recorded WT deployment did not become active")
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def qualify(client, receipt_path, output, *, timeout_seconds=900):
    from scripts.run_generated_app_journey import validate_receipt, verify_deployment
    raw = Path(receipt_path).read_bytes()
    receipt = json.loads(raw)
    binding = validate_receipt(receipt)
    contract = receipt["plan"]["ct_compatible_contract"]
    result = {"schema_version": 1, "scope": "simulated_control_tower", "control_tower_requests": 0,
        "deployment_receipt_sha256": hashlib.sha256(raw).hexdigest(), "accepted": False,
        "status": "waiting_for_package_bootstrap", "calls": []}
    write_evidence(output, result)
    deadline = time.monotonic() + timeout_seconds
    wait_for_deployment(client, receipt, deadline=deadline)
    # Deliver policy only after independently verifying the exact active source.
    result["deployment_verification"] = verify_deployment(binding, client)
    def http(method, path, payload=None):
        ensure_live(receipt["plan"])
        identity(client, receipt)
        response = requests.request(method, binding["app"]["url"] + path, headers=client.config.authenticate(),
            json=payload, timeout=40, allow_redirects=False)
        return response
    while True:
        response = http("PUT", "/api/admin/model-policy", contract["model_policy"])
        if response.status_code == 200:
            acknowledged = response.json()
            expected = sorted(entry["service_name"] for entry in contract["model_policy"]["pool"])
            if (acknowledged.get("revision") != contract["model_policy"]["revision"]
                    or acknowledged.get("applied") is not True or acknowledged.get("verified") is not True
                    or acknowledged.get("positive_checks") != expected
                    or acknowledged.get("negative_checks") != contract["model_policy"]["denied_models"]
                    or acknowledged.get("processes_restarted") is not False):
                raise ValueError("Real WT policy acknowledgement mismatch")
            result["model_policy_acknowledgement"] = acknowledged
            break
        result["bootstrap_http_status"] = response.status_code
        write_evidence(output, result)
        if response.status_code not in {404, 502, 503} or time.monotonic() >= deadline:
            raise RuntimeError("Package bootstrap/policy delivery did not qualify")
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    result["status"] = "checking_release_and_app_identity"; write_evidence(output, result)
    observation = http("GET", "/api/admin/evaluation/sessions/00000000-0000-0000-0000-000000000000/messages")
    if (observation.status_code != 404 or not observation.headers.get("Content-Type", "").startswith("application/json")
            or observation.json().get("detail") != "Evaluation session not found"):
        raise ValueError("External transcript observer is hidden or not correctly routed")
    result["observer_route_verified"] = True
    with client.files.download(receipt["staged_package"]["path"]).contents as handle:
        artifact = handle.read(contract["package_manifest"]["size_bytes"] + 1)
    with client.workspace.download(binding["source_path"] + "/release-manifest.json") as handle:
        manifest_bytes = handle.read(1024 * 1024 + 1)
    manifest, _ = verify_package(manifest_bytes, artifact, contract["manifest_sha256"])
    result["package_readback"] = {"sha256": manifest["sha256"], "wt_git_sha": manifest["wt_git_sha"],
        "size_bytes": len(artifact), "logical_content_verified": True}
    app = identity(client, receipt)
    sp = app.service_principal_client_id
    demo = receipt.get("shared_demo_catalog")
    if demo:
        catalog = client.catalogs.get(demo["name"])
        if (demo["name"] != contract.get("demo_catalog") or demo["principal"] != sp
                or demo.get("read_only") is not True or demo.get("state") != "independently_verified"
                or any(getattr(catalog, key) != demo[key] for key in ("name", "created_at", "metastore_id"))
                or not {"USE_CATALOG", "USE_SCHEMA", "SELECT", "READ_VOLUME"} <=
                    privileges(client.grants.get_effective("CATALOG", demo["name"], principal=sp), sp)):
            raise ValueError("Shared demo catalog read access did not qualify")
        result["shared_demo_catalog"] = {"name": demo["name"], "read_only": True,
            "identity_and_effective_access_verified": True}
    result["effective_model_permissions"] = []
    for delta in receipt["permission_deltas"]:
        if delta["kind"] in {"MODEL_SERVICE", "SCHEMA"} and delta["name"].startswith("system.ai"):
            observed = privileges(client.grants.get_effective(delta["kind"], delta["name"], principal=sp), sp)
            if not set(delta["required"]) <= observed:
                raise ValueError("Model privileges changed before qualification")
            result["effective_model_permissions"].append({"kind": delta["kind"], "name": delta["name"], "privileges": sorted(observed)})
    for role in ("driver", "codex", "wizard"):
        row = {"role": role, "state": "invocation_requested"}
        result["calls"].append(row); write_evidence(output, result)
        response = http("POST", "/api/admin/evaluation/model-canary", {"role": role})
        row["http_status"] = response.status_code
        if response.status_code == 200:
            body = response.json()
            if (body.get("marker") != receipt["plan"]["marker"] or body.get("application_id") != sp
                    or body.get("service_principal_id") != str(app.service_principal_id)):
                raise ValueError("Canary returned a different app identity")
            row["result"] = body
        write_evidence(output, result)
    result["status"] = "qualified" if all(row.get("result", {}).get("invocation_verified") is True for row in result["calls"]) else "model_invocation_failed"
    result["novice_journey_eligible"] = result["status"] == "qualified"
    result["control_tower_integration_qualified"] = False
    write_evidence(output, result)
    return result


def cleanup(client, receipt_path, output):
    """Delete exact owned resources; shared objects retain every other ACL."""
    from databricks.sdk.service.catalog import PermissionsChange, Privilege
    raw = Path(receipt_path).read_bytes()
    receipt = json.loads(raw)
    plan = receipt["plan"]
    if (receipt.get("scope") != "simulated_control_tower" or receipt.get("control_tower_requests") != 0
            or plan.get("ct_compatible_contract", {}).get("version") != 2 or plan.get("disposable") is not True
            or client.config.host.rstrip("/") != plan["workspace_host"]
            or plan["names"]["app_name"] != plan["marker"] + "-wt"
            or plan["names"]["catalog"] != plan["marker"].replace("-", "_")):
        raise ValueError("Exact disposable CT-compatible receipt required")
    result = {"schema_version": 1, "scope": "simulated_control_tower", "control_tower_requests": 0,
        "deployment_receipt_sha256": hashlib.sha256(raw).hexdigest(), "operations": [], "status": "cleaning", "accepted": False}
    write_evidence(output, result)
    owned = {entry["kind"]: entry for entry in receipt["created_resources"]}
    app_owned = owned.get("app", {})
    sp = app_owned.get("service_principal_client_id")
    if not sp:
        raise ValueError("App identity did not converge; manual receipt-bound recovery is required")
    try:
        app = identity(client, receipt)
    except NotFound:
        app = None
    # Preflight all existing targets before performing any teardown mutation.
    try:
        catalog = client.catalogs.get(plan["names"]["catalog"])
    except NotFound:
        catalog = None
    if catalog and (catalog.created_by != receipt["operator"]["email"] or catalog.owner != receipt["operator"]["email"]
            or catalog.created_at != owned["catalog"].get("created_at")
            or catalog.metastore_id != owned["catalog"].get("metastore_id")):
        raise ValueError("Catalog identity or owner changed; cleanup refused")
    group_owned = owned.get("group")
    try:
        group = client.groups.get(group_owned["id"]) if group_owned else None
    except NotFound:
        group = None
    if group:
        members = {m.value for m in (group.members or [])}
        expected = {receipt["operator"]["id"], str(app_owned["service_principal_id"])}
        if group.display_name != group_owned["name"] or members not in (
                expected, {receipt["operator"]["id"]}):
            raise ValueError("Group identity or membership changed; cleanup refused")
        if members != expected:
            # Apps deletion also removes its SP and that membership. A cleanup
            # retry may accept only that exact shrink after both absence checks.
            if app is not None:
                raise ValueError("Group membership changed while app still exists")
            try:
                client.service_principals.get(str(app_owned["service_principal_id"]))
            except NotFound:
                pass
            else:
                raise ValueError("Group membership changed while principal still exists")
    source_owned = owned.get("workspace_source")
    try:
        source = client.workspace.get_status(source_owned["path"]) if source_owned else None
    except NotFound:
        source = None
    if source and str(source.object_id) != source_owned.get("id"):
        raise ValueError("Source directory identity changed; cleanup refused")
    if source:
        from scripts.run_generated_app_journey import verify_snapshot
        verify_snapshot(client, source_owned["path"], receipt["uploaded_source"])
    demo = receipt.get("shared_demo_catalog")
    if demo:
        if (demo["name"] != plan["ct_compatible_contract"].get("demo_catalog")
                or demo["principal"] != sp or demo.get("read_only") is not True
                or demo["name"] == plan["names"]["catalog"]):
            raise ValueError("Shared demo catalog receipt changed; cleanup refused")
        actual = client.catalogs.get(demo["name"])
        if any(getattr(actual, key) != demo[key] for key in ("name", "created_at", "metastore_id")):
            raise ValueError("Shared demo catalog identity changed; cleanup refused")
    # Validate all shared deltas before any mutation, including partial runs.
    for delta in receipt.get("permission_deltas", []):
        if delta["name"] == plan["names"]["catalog"] or delta["name"].startswith(plan["names"]["catalog"] + ".") or not delta["added"]:
            continue
        demo_delta = (demo and delta["kind"] == "CATALOG" and delta["name"] == demo["name"]
                      and set(delta["added"]) <= {"USE_CATALOG", "USE_SCHEMA", "SELECT", "READ_VOLUME"})
        if (delta["principal"] != sp or not (
                delta["kind"] == "SCHEMA" and delta["name"] == "system.ai"
                or delta["kind"] == "MODEL_SERVICE" and delta["name"].startswith("system.ai.")
                or demo_delta)):
            raise ValueError("Shared privilege delta is outside the test principal")
    for key, object_type in (("warehouse_access", "sql/warehouses"), ("work_sync", "directories")):
        delta = receipt.get(key)
        if not delta:
            continue
        if delta["principal"] != sp or delta["added"] != ("CAN_USE" if key == "warehouse_access" else "CAN_EDIT"):
            raise ValueError("Shared ACL delta changed")
        if object_type == "directories" and str(client.workspace.get_status(delta["path"]).object_id) != delta["id"]:
            raise ValueError("Work-sync directory identity changed")
        before = client.permissions.get(object_type, delta["id"]).as_dict()
        actual = {p["permission_level"] for row in before.get("access_control_list", [])
                  if row.get("service_principal_name") == sp for p in row.get("all_permissions", [])
                  if not p.get("inherited", False)}
        if not actual <= {delta["added"]}:
            raise ValueError("Test SP shared ACL was expanded after provisioning")
    def action(kind, name, operation):
        row = {"kind": kind, "identity": name, "state": "requested"}
        result["operations"].append(row); write_evidence(output, result)
        operation(); row["state"] = "verified"; write_evidence(output, result)
    for delta in receipt.get("permission_deltas", []):
        if delta["name"] == plan["names"]["catalog"] or delta["name"].startswith(plan["names"]["catalog"] + ".") or not delta["added"]:
            continue
        # Every shared delta was identity/privilege checked above before mutation.
        def revoke(delta=delta):
            before = client.grants.get(delta["kind"], delta["name"]).as_dict()
            others = [row for row in before.get("privilege_assignments", []) if row.get("principal") != sp]
            client.grants.update(delta["kind"], delta["name"], changes=[PermissionsChange(principal=sp, remove=[Privilege(v) for v in delta["added"]])])
            after = client.grants.get(delta["kind"], delta["name"])
            if set(delta["added"]) & privileges(after, sp):
                raise RuntimeError("Grant removal did not verify")
            if canonical_rows(others) != canonical_rows([row for row in after.as_dict().get("privilege_assignments", []) if row.get("principal") != sp]):
                raise RuntimeError("Other principals changed during shared grant removal")
        action("shared_privilege_delta", delta["name"], revoke)
    for key, object_type in (("warehouse_access", "sql/warehouses"), ("work_sync", "directories")):
        delta = receipt.get(key)
        if not delta: continue
        if delta["principal"] != sp:
            raise ValueError("Shared ACL principal changed")
        def revoke_acl(delta=delta, object_type=object_type):
            if object_type == "directories":
                actual = client.workspace.get_status(delta["path"])
                if str(actual.object_id) != delta["id"]:
                    raise ValueError("Work-sync directory identity changed")
            remove_owned_acl(client, object_type, delta["id"], sp, delta["added"])
        action("shared_acl_delta", delta["id"], revoke_acl)
    def absent(lookup):
        try: lookup()
        except NotFound: return
        raise RuntimeError("Deletion absence is not verified")
    if app:
        def delete_app():
            client.apps.delete(app.name)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                try: client.apps.get(app.name)
                except NotFound: return
                time.sleep(min(10, max(0, deadline - time.monotonic())))
            raise RuntimeError("App deletion did not converge")
        action("app", app.id, delete_app)
    if source:
        def delete_source():
            client.workspace.delete(source_owned["path"], recursive=True)
            absent(lambda: client.workspace.get_status(source_owned["path"]))
        action("workspace_source", source_owned["id"], delete_source)
    if catalog:
        def delete_catalog():
            client.catalogs.delete(catalog.name, force=True)
            absent(lambda: client.catalogs.get(catalog.name))
        action("catalog", catalog.name, delete_catalog)
    if group:
        def delete_group():
            client.groups.delete(group.id); absent(lambda: client.groups.get(group.id))
        action("group", group.id, delete_group)
    result["status"] = "cleanup_verified"; write_evidence(output, result)
    return result
