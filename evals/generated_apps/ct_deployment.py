"""WT-owned implementation of CT's package provisioning contract.

No CT API or mutable CT resource is used. Shared-workspace simulation uses
exact app-SP grants in place of permanent account groups/workspace-wide grants.
The package is byte-for-byte unchanged; observation is an explicit external
module, with its own source manifest, enabled only for disposable evaluations.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .adapters.simulated_control_tower import plan_simulation, bind_created_app
from .report import write_evidence
from .test_baseline import validate_test_baseline

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_VERSION = 2
PACKAGE_FILES = frozenset({"app.yaml", "requirements.txt", "wt_bootstrap.py",
    "wt_evaluation_bootstrap.py", "wt_evaluation.py", "release-manifest.json"})


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def verify_package(manifest_bytes, artifact, manifest_digest):
    """The reviewed manifest digest is the root of trust, not a Git checkout."""
    if not re.fullmatch(r"[a-f0-9]{64}", manifest_digest) or sha256(manifest_bytes) != manifest_digest:
        raise ValueError("Release manifest digest mismatch")
    manifest = json.loads(manifest_bytes)
    expected = {"artifact_name": "workshop-terminal.pex", "artifact_type": "pex", "format_version": 1,
                "entry_point": "server.otel_bootstrap:main", "platform": "linux_x86_64",
                "python_abi": "cp311", "python_implementation": "cpython",
                "minimum_control_tower_contract_version": 1}
    if any(manifest.get(k) != v for k, v in expected.items()):
        raise ValueError("Unsupported CT package contract")
    if (type(manifest.get("size_bytes")) is not int or not 0 < len(artifact) <= 200 * 1024 * 1024
            or len(artifact) != manifest["size_bytes"] or sha256(artifact) != manifest.get("sha256")
            or not re.fullmatch(r"[a-f0-9]{40}", manifest.get("wt_git_sha", ""))):
        raise ValueError("Release artifact identity mismatch")
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("Duplicate package entries")
        content = json.loads(archive.read("runtime-content-manifest.json"))
        if (content.get("wt_git_sha") != manifest["wt_git_sha"]
                or len(content.get("files", [])) != manifest["logical_contents_file_count"]):
            raise ValueError("Logical content manifest identity mismatch")
        logical = hashlib.sha256()
        seen = set()
        for entry in content["files"]:
            name = entry["path"]
            if (name in seen or name.startswith("/") or ".." in Path(name).parts
                    or entry["mode"] not in {"644", "755"}):
                raise ValueError("Unsafe runtime content manifest")
            seen.add(name)
            data = archive.read(name)
            if len(data) != entry["size_bytes"] or sha256(data) != entry["sha256"]:
                raise ValueError("Package logical file mismatch")
            logical.update(name.encode() + b"\0" + entry["mode"].encode() + b"\0" + data + b"\0")
        if logical.hexdigest() != manifest["logical_contents_sha256"] or logical.hexdigest() != content["logical_contents_sha256"]:
            raise ValueError("Package logical content digest mismatch")
        artifacts = json.loads(archive.read("assets/artifacts/manifest.json"))
    return manifest, artifacts


def validate_policy(policy):
    if (not isinstance(policy, dict) or set(policy) != {"revision", "pool", "denied_models", "restart_processes"}
            or type(policy["revision"]) is not int or policy["revision"] < 1 or policy["restart_processes"] is not False
            or not isinstance(policy["pool"], list) or not 1 <= len(policy["pool"]) <= 32
            or not isinstance(policy["denied_models"], list)):
        raise ValueError("An explicit CT model-policy snapshot is required")
    names = []
    capabilities = set()
    for entry in policy["pool"]:
        if (not isinstance(entry, dict) or set(entry) != {"service_name", "enabled", "capabilities", "principal_classes", "limit_profile"}
                or not re.fullmatch(r"system\.ai\.[a-z0-9][a-z0-9._-]*", entry.get("service_name", ""))
                or entry["enabled"] is not True or "wt_sp" not in entry["principal_classes"]
                or not set(entry["principal_classes"]) <= {"lab_user", "wt_sp", "helper_sp"}
                or not entry["capabilities"] or not set(entry["capabilities"]) <= {"claude", "codex", "chat", "embedding"}
                or not isinstance(entry["limit_profile"], dict)):
            raise ValueError("Invalid CT model-policy entry")
        names.append(entry["service_name"])
        capabilities.update(entry["capabilities"])
    denied = policy["denied_models"]
    if (len(names) != len(set(names)) or denied != sorted(set(denied)) or set(names) & set(denied)
            or any(not re.fullmatch(r"system\.ai\.[a-z0-9][a-z0-9._-]*", name) for name in denied)
            or not {"claude", "codex", "chat"} <= capabilities):
        raise ValueError("Incomplete or overlapping CT model-policy snapshot")
    return policy


def plan_package(spec, manifest_bytes, artifact, *, now=None):
    spec = dict(spec)
    package_input = spec.pop("package")
    if set(package_input) != {"manifest_sha256"}:
        raise ValueError("Package declaration requires the reviewed manifest SHA256")
    policy = validate_policy(spec.pop("model_policy"))
    baseline_name = spec.pop("test_baseline", None)
    demo_catalog = spec.pop("demo_catalog", None)
    if demo_catalog is not None and (
            not isinstance(demo_catalog, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", demo_catalog)
            or demo_catalog.lower() in {"system", "hive_metastore"}):
        raise ValueError("Demo catalog requires an explicit existing Unity Catalog name")
    mirror = spec.pop("toolchain_mirror", None)
    if mirror is not None and (not isinstance(mirror, dict) or set(mirror) != {"source_volume", "strict"}
            or not re.fullmatch(r"/Volumes/[A-Za-z0-9_]+/[A-Za-z0-9_]+/[A-Za-z0-9_]+", mirror.get("source_volume", ""))
            or type(mirror.get("strict")) is not bool):
        raise ValueError("Toolchain mirror requires an exact read-only source volume and strict flag")
    manifest, artifacts = verify_package(manifest_bytes, artifact, package_input["manifest_sha256"])
    baseline = validate_test_baseline(baseline_name, artifacts, policy) if baseline_name is not None else None
    spec["release"] = {"source_kind": "runtime-snapshot", "source_digest": manifest["sha256"],
                       "parent_git_sha": manifest["wt_git_sha"], "instrumentation": True,
                       "prompt_policy_unchanged_asserted": True}
    plan = plan_simulation(spec, artifacts, now=now)
    plan["ct_compatible_contract"] = {
        "version": CONTRACT_VERSION, "deployment_mode": "package",
        "package_manifest": manifest, "manifest_sha256": package_input["manifest_sha256"],
        "model_policy": policy, "model_privilege_strategy": "exact_test_app_sp",
        "catalog_owner_strategy": "retain_operator", "attendee_app_permission": "CAN_MANAGE",
        "work_sync_path": f"/Workspace/Users/{plan['attendee']['email']}/projects",
        "toolchain_mirror": mirror,
        "demo_catalog": demo_catalog,
        "simulation_limits": ["Existing shared Labs workspace; no new workspace assignment",
            "Exact app-SP privileges replace permanent CT account group membership",
            "No workspace-wide account users grant; generated-app SPs need exact owned-catalog access",
            "CT callbacks and AI Gateway blocking budgets are not exercised",
            "Work-sync ACL is exercised on the attendee home; only the added test-SP ACL is removed"],
    }
    plan["grants"]["attendee"]["owner"] = False
    plan["environment"].update(WORKSHOP_MODEL_POLICY_REQUIRED="true", SKILLS_REF=artifacts["artifacts"]["databricks_agent_skills"]["version"],
        WORKSHOP_RELEASE_SHA=manifest["wt_git_sha"], WORKSHOP_PACKAGE_SHA256=manifest["sha256"],
        WORKSHOP_PACKAGE_SIZE_BYTES=str(manifest["size_bytes"]),
        WORKSHOP_PACKAGE_VOLUME_PATH=f"/Volumes/{plan['names']['catalog']}/_wt_runtime/packages/{manifest['sha256']}/workshop-terminal.pex",
        HOME=f"/app/python/source_code/data/{plan['marker']}/apphome",
        OTEL_TRACES_SAMPLER="always_on",
        OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS="authorization,cookie,set-cookie,x-api-key",
        OTEL_PYTHON_FASTAPI_EXCLUDED_URLS="/api/wizard.*,/api/certificate.*")
    if baseline:
        plan["ct_compatible_contract"]["test_baseline"] = baseline
        plan["environment"].update(ANTHROPIC_MODEL=baseline["models"]["driver"],
                                   CODEX_MODEL=baseline["models"]["codex"])
    if mirror:
        plan["environment"].update(WORKSHOP_TOOLCHAIN_MIRROR_PATH=f"/Volumes/{plan['names']['catalog']}/_wt_runtime/toolchain",
                                   WORKSHOP_TOOLCHAIN_MIRROR_STRICT=str(mirror["strict"]).lower())
    if demo_catalog:
        if demo_catalog.lower() == plan["names"]["catalog"].lower():
            raise ValueError("Shared demo catalog must be distinct from the attendee's owned catalog")
        plan["environment"]["WORKSHOP_DEMO_CATALOG"] = demo_catalog
    return plan


def ensure_live(plan):
    bounds = plan["bounds"]
    now = datetime.now(timezone.utc).timestamp()
    start = datetime.fromisoformat(bounds["created_at"].replace("Z", "+00:00")).timestamp()
    expiry = datetime.fromisoformat(bounds["expires_at"].replace("Z", "+00:00")).timestamp()
    if not start <= now < expiry or expiry - start != bounds["ttl_seconds"] or not 60 <= expiry - start <= 14400:
        raise ValueError("The original deployment bounds are invalid or expired")


def identity(client, receipt):
    plan = receipt["plan"]
    if (receipt.get("scope") != "simulated_control_tower" or receipt.get("control_tower_requests") != 0
            or client.config.host.rstrip("/") != plan["workspace_host"]):
        raise ValueError("Receipt/workspace identity mismatch")
    owned = next(x for x in receipt["created_resources"] if x["kind"] == "app")
    app = client.apps.get(owned["name"])
    if (app.id != owned["id"] or app.service_principal_id != owned["service_principal_id"]
            or app.service_principal_client_id != owned["service_principal_client_id"]):
        raise ValueError("The owned app identity changed")
    return app


def submit_deployment(client, receipt, receipt_path, *, timeout_seconds=120):
    """Start stopped compute and submit only the exact verified bootstrap tree.

    CT normally gets active compute from app creation. The Labs platform can
    return a fresh app with stopped compute; this bounded convergence handles
    that state without pretending its undeployed application is already ready.
    """
    from databricks.sdk.service.apps import AppDeployment, EnvVar
    from scripts.run_generated_app_journey import verify_snapshot
    ensure_live(receipt["plan"])
    app = identity(client, receipt)
    verify_snapshot(client, receipt["plan"]["names"]["source_path"], receipt["uploaded_source"],
                    expected_environment=receipt["bound_plan"]["environment"])
    state = getattr(getattr(app, "compute_status", None), "state", None)
    state = getattr(state, "value", state)
    receipt["compute_convergence"] = {"initial_state": state, "state": "checking"}
    write_evidence(receipt_path, receipt)
    if state in {"STOPPED", "STOPPING", "ERROR", "UNAVAILABLE"}:
        client.apps.start(app.name)
        receipt["compute_convergence"]["state"] = "start_requested"; write_evidence(receipt_path, receipt)
    deadline = time.monotonic() + timeout_seconds
    while True:
        ensure_live(receipt["plan"])
        app = identity(client, receipt)
        state = getattr(getattr(app, "compute_status", None), "state", None)
        state = getattr(state, "value", state)
        if state == "ACTIVE": break
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned app compute did not become active")
        time.sleep(2)
    receipt["compute_convergence"].update(state="active_independently_verified")
    write_evidence(receipt_path, receipt)
    pending = client.apps.deploy(app_name=app.name, app_deployment=AppDeployment(
        source_code_path=receipt["plan"]["names"]["source_path"],
        env_vars=[EnvVar(name=name, value=value) for name, value in sorted(receipt["bound_plan"]["environment"].items())]))
    receipt.update(deployment=pending.response.as_dict(), status="deployment_submitted")
    write_evidence(receipt_path, receipt)
    return receipt


def privileges(response, principal):
    result = set()
    for assignment in response.privilege_assignments or []:
        if str(assignment.principal).casefold() == principal.casefold():
            for entry in assignment.privileges or []:
                value = getattr(entry, "privilege", entry)
                result.add(str(getattr(value, "value", value)).upper().replace(" ", "_"))
    return result


def grant(client, receipt, path, kind, name, principal, required):
    """Record the delta before PATCH; independent readback is mandatory."""
    from databricks.sdk.service.catalog import PermissionsChange, Privilege
    before = privileges(client.grants.get(kind, name), principal)
    added = sorted(set(required) - before)
    row = {"kind": kind, "name": name, "principal": principal, "required": sorted(required),
           "added": added, "state": "grant_requested"}
    receipt.setdefault("permission_deltas", []).append(row)
    write_evidence(path, receipt)
    if added:
        client.grants.update(kind, name, changes=[PermissionsChange(principal=principal, add=[Privilege(v) for v in added])])
    if not set(required) <= privileges(client.grants.get(kind, name), principal):
        raise RuntimeError("Direct grant readback failed")
    if not set(required) <= privileges(client.grants.get_effective(kind, name, principal=principal), principal):
        raise RuntimeError("Effective grant readback failed")
    row["state"] = "independently_verified"
    write_evidence(path, receipt)


def grant_demo_read_access(client, receipt, path, principal):
    """Mirror CT's seeded read grants for only this temporary WT identity."""
    name = receipt["plan"]["ct_compatible_contract"].get("demo_catalog")
    if not name:
        return
    catalog = client.catalogs.get(name)
    identity = {key: getattr(catalog, key) for key in ("name", "created_at", "metastore_id")}
    receipt["shared_demo_catalog"] = {**identity, "owner": catalog.owner,
        "principal": principal, "read_only": True, "state": "granting"}
    write_evidence(path, receipt)
    grant(client, receipt, path, "CATALOG", name, principal,
          {"USE_CATALOG", "USE_SCHEMA", "SELECT", "READ_VOLUME"})
    observed = client.catalogs.get(name)
    if identity != {key: getattr(observed, key) for key in identity}:
        raise ValueError("Shared demo catalog identity changed during provisioning")
    receipt["shared_demo_catalog"]["state"] = "independently_verified"
    write_evidence(path, receipt)


def deploy_package(client, plan, manifest_bytes, artifact, receipt_path):
    from databricks.sdk.errors import NotFound
    from databricks.sdk.service.apps import App, AppDeployment
    from databricks.sdk.service.iam import ComplexValue
    from databricks.sdk.service.catalog import VolumeType
    from databricks.sdk.service.workspace import ImportFormat
    from scripts.deploy_generated_app_test import require_absent, await_app_identity, grant_attendee_app_access, source_manifest
    from scripts.deploy_ct_sim import _pick_warehouse, _sql, _quoted
    ensure_live(plan)
    manifest, _ = verify_package(manifest_bytes, artifact, plan["ct_compatible_contract"]["manifest_sha256"])
    if manifest != plan["ct_compatible_contract"]["package_manifest"]:
        raise ValueError("Package changed after planning")
    if client.config.host.rstrip("/") != plan["workspace_host"]:
        raise ValueError("Wrong workspace")
    names = plan["names"]
    operator = client.current_user.me()
    attendee = list(client.users.list(filter=f'userName eq "{plan["attendee"]["email"]}"'))
    if len(attendee) != 1 or not attendee[0].active:
        raise ValueError("Attendee must already be assigned to the Labs workspace")
    for lookup in (lambda: client.apps.get(names["app_name"]), lambda: client.catalogs.get(names["catalog"]),
                   lambda: client.workspace.get_status(names["source_path"])):
        require_absent(lookup)
    if list(client.groups.list(filter=f'displayName eq "{names["admin_group"]}"')):
        raise ValueError("The disposable admin group already exists")
    receipt = {"schema_version": 1, "scope": "simulated_control_tower", "control_tower_requests": 0,
               "accepted": False, "plan": plan, "created_resources": [], "status": "provisioning",
               "operator": {"id": str(operator.id), "email": operator.user_name},
               "assigned_attendee": {"id": attendee[0].id, "email": attendee[0].user_name}}
    write_evidence(receipt_path, receipt)
    def owned(kind, **fields):
        row = {"kind": kind, "state": "creation_requested", **fields}
        receipt["created_resources"].append(row); write_evidence(receipt_path, receipt)
        return row
    app_resource = owned("app", name=names["app_name"])
    submitted = client.apps.create(App(name=names["app_name"], description="Disposable CT-compatible WT E2E test",
        user_api_scopes=plan["app_resource"]["user_api_scopes"]), no_compute=False).response
    if submitted:
        app_resource["id"] = submitted.id; write_evidence(receipt_path, receipt)
    app, bound = await_app_identity(client, plan, app_resource, receipt, receipt_path)
    grant_attendee_app_access(client, plan, app_resource, receipt, receipt_path, minimum_permission="CAN_MANAGE")
    group_resource = owned("group", name=names["admin_group"])
    group = client.groups.create(display_name=names["admin_group"], members=[ComplexValue(value=str(operator.id)),
        ComplexValue(value=str(app.service_principal_id))])
    group_resource.update(id=group.id, state="created"); write_evidence(receipt_path, receipt)
    observed_group = client.groups.get(group.id)
    if observed_group.display_name != names["admin_group"] or {x.value for x in observed_group.members} != {str(operator.id), str(app.service_principal_id)}:
        raise RuntimeError("Isolated admin group membership readback failed")
    catalog_resource = owned("catalog", name=names["catalog"])
    warehouse = _pick_warehouse(client)
    if not warehouse:
        raise ValueError("A Labs SQL warehouse is required")
    _sql(client, warehouse, f"CREATE CATALOG {_quoted(names['catalog'])} COMMENT 'Disposable CT-compatible WT E2E test'")
    catalog = client.catalogs.get(names["catalog"])
    if catalog.owner != operator.user_name:
        raise ValueError("Catalog platform ownership was not retained")
    catalog_resource.update(owner=catalog.owner, created_at=catalog.created_at, created_by=catalog.created_by,
                            metastore_id=catalog.metastore_id, state="created"); write_evidence(receipt_path, receipt)
    sp = app.service_principal_client_id
    grant(client, receipt, receipt_path, "CATALOG", catalog.name, operator.user_name, {"ALL_PRIVILEGES", "MANAGE"})
    grant(client, receipt, receipt_path, "CATALOG", catalog.name, plan["attendee"]["email"], {"ALL_PRIVILEGES", "MANAGE"})
    grant(client, receipt, receipt_path, "CATALOG", catalog.name, sp, {"USE_CATALOG", "CREATE_SCHEMA", "MANAGE"})
    # CT provisions the attendee's default schema as well as the catalog.
    # Runtime/toolchain schemas do not replace that assigned build/export target.
    attendee_schema_name = f"{catalog.name}.{names['schema']}"
    try:
        attendee_schema = client.schemas.get(attendee_schema_name)
    except NotFound:
        attendee_schema = client.schemas.create(name=names["schema"], catalog_name=catalog.name)
    catalog_resource["attendee_schema"] = {"name": attendee_schema.full_name, "id": attendee_schema.schema_id}
    write_evidence(receipt_path, receipt)
    grant(client, receipt, receipt_path, "SCHEMA", attendee_schema.full_name,
          plan["attendee"]["email"], {"ALL_PRIVILEGES"})
    schema = client.schemas.create(name="_wt_runtime", catalog_name=catalog.name)
    catalog_resource["runtime_schema"] = {"name": schema.full_name, "id": schema.schema_id}
    write_evidence(receipt_path, receipt)
    volume = client.volumes.create(catalog_name=catalog.name, schema_name=schema.name, name="packages", volume_type=VolumeType.MANAGED)
    catalog_resource["runtime_volume"] = {"name": volume.full_name, "id": volume.volume_id}
    write_evidence(receipt_path, receipt)
    grant(client, receipt, receipt_path, "SCHEMA", schema.full_name, sp, {"USE_SCHEMA"})
    grant(client, receipt, receipt_path, "VOLUME", volume.full_name, sp, {"READ_VOLUME"})
    package_path = bound["environment"]["WORKSHOP_PACKAGE_VOLUME_PATH"]
    receipt["staged_package"] = {"path": package_path, "sha256": manifest["sha256"], "size": len(artifact), "state": "upload_requested"}
    write_evidence(receipt_path, receipt)
    client.files.create_directory(package_path.rsplit("/", 1)[0])
    client.files.upload(package_path, io.BytesIO(artifact), overwrite=False)
    with client.files.download(package_path).contents as handle:
        observed = handle.read(len(artifact) + 1)
    if sha256(observed) != manifest["sha256"] or len(observed) != len(artifact):
        raise ValueError("Staged package readback mismatch")
    receipt["staged_package"]["state"] = "independently_verified"
    catalog_resource["state"] = "created_and_grants_verified"; write_evidence(receipt_path, receipt)
    if plan["ct_compatible_contract"].get("toolchain_mirror"):
        stage_toolchain(client, receipt, receipt_path, artifact)
    grant_demo_read_access(client, receipt, receipt_path, sp)
    grant(client, receipt, receipt_path, "SCHEMA", "system.ai", sp, {"USE_SCHEMA"})
    for entry in plan["ct_compatible_contract"]["model_policy"]["pool"]:
        grant(client, receipt, receipt_path, "MODEL_SERVICE", entry["service_name"], sp, {"EXECUTE"})
    # Use the same warehouse as the fixture, without replacing its ACL.
    permissions = client.permissions.get("sql/warehouses", warehouse).as_dict()
    before = [x for x in permissions.get("access_control_list", []) if x.get("service_principal_name") == sp]
    if before:
        raise ValueError("Fresh app SP unexpectedly has a direct warehouse ACL")
    row = {"kind": "warehouse", "id": warehouse, "principal": sp, "added": "CAN_USE", "state": "grant_requested"}
    receipt["warehouse_access"] = row; write_evidence(receipt_path, receipt)
    from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel
    client.permissions.update("sql/warehouses", warehouse, access_control_list=[AccessControlRequest(service_principal_name=sp, permission_level=PermissionLevel.CAN_USE)])
    if not any(x.get("service_principal_name") == sp and any(p.get("permission_level") in {"CAN_USE", "CAN_MANAGE", "IS_OWNER"} for p in x.get("all_permissions", []))
               for x in client.permissions.get("sql/warehouses", warehouse).as_dict().get("access_control_list", [])):
        raise RuntimeError("Warehouse ACL readback failed")
    row["state"] = "independently_verified"; write_evidence(receipt_path, receipt)
    sync_path = plan["ct_compatible_contract"]["work_sync_path"]
    client.workspace.mkdirs(sync_path)
    directory = client.workspace.get_status(sync_path)
    existing = client.permissions.get("directories", str(directory.object_id)).as_dict()
    if any(x.get("service_principal_name") == sp for x in existing.get("access_control_list", [])):
        raise ValueError("Fresh app SP unexpectedly has a work-sync ACL")
    receipt["work_sync"] = {"path": sync_path, "id": str(directory.object_id), "principal": sp,
        "added": "CAN_EDIT", "directory_preserved_on_cleanup": True, "state": "grant_requested"}
    write_evidence(receipt_path, receipt)
    client.permissions.update("directories", str(directory.object_id), access_control_list=[AccessControlRequest(service_principal_name=sp, permission_level=PermissionLevel.CAN_EDIT)])
    if not any(x.get("service_principal_name") == sp and any(p.get("permission_level") in {"CAN_EDIT", "CAN_MANAGE"} for p in x.get("all_permissions", []))
               for x in client.permissions.get("directories", str(directory.object_id)).as_dict().get("access_control_list", [])):
        raise RuntimeError("Work-sync ACL readback failed")
    receipt["work_sync"]["state"] = "independently_verified"; write_evidence(receipt_path, receipt)
    files = package_sources(bound, manifest_bytes)
    snapshot = source_manifest(files)
    receipt.update(bound_plan=bound, uploaded_source=snapshot, source_identity={
        "planned_unpatched_digest": manifest["sha256"], "uploaded_runtime_digest": snapshot["digest"],
        "runtime_environment_patched": True, "instrumented_release": True,
        "package_bytes_unchanged": True, "package_sha256": manifest["sha256"]})
    source_resource = owned("workspace_source", path=names["source_path"])
    client.workspace.mkdirs(names["source_path"])
    source_resource.update(state="created", id=str(client.workspace.get_status(names["source_path"]).object_id))
    write_evidence(receipt_path, receipt)
    for name, data in sorted(files.items()):
        client.workspace.upload(names["source_path"] + "/" + name, io.BytesIO(data), format=ImportFormat.AUTO, overwrite=False)
        with client.workspace.download(names["source_path"] + "/" + name) as handle:
            if handle.read(len(data) + 1) != data:
                raise ValueError("Bootstrap source upload readback mismatch")
    ensure_live(plan)
    identity(client, receipt)
    return submit_deployment(client, receipt, receipt_path)


def package_sources(bound, manifest_bytes):
    files = {name: (ROOT / "evals/generated_apps/runtime" / name).read_bytes()
             for name in ("wt_bootstrap.py", "wt_evaluation_bootstrap.py")}
    files["wt_evaluation.py"] = (ROOT / "server/evaluation.py").read_bytes()
    files["release-manifest.json"] = manifest_bytes
    files["requirements.txt"] = b"# The CT-pinned PEX contains all runtime dependencies.\n"
    files["app.yaml"] = yaml.safe_dump({"command": ["python", "wt_bootstrap.py"], "env": [
        {"name": name, "value": value} for name, value in sorted({**bound["environment"],
            "CONTROL_TOWER_URL": "", "CONTROL_TOWER_INGEST_URL": "", "CONTROL_TOWER_INGEST_TOKEN": "",
            "WORKSHOP_PAT": "", "OMNIGENT_APP_URL": ""}.items())]}, sort_keys=False).encode()
    return files


def update_observer(client, receipt, receipt_path):
    """Refresh the external observer/launcher only, preserving package bytes."""
    from databricks.sdk.service.workspace import ImportFormat
    from scripts.deploy_generated_app_test import source_manifest
    from scripts.run_generated_app_journey import verify_snapshot
    ensure_live(receipt["plan"])
    identity(client, receipt)
    source = receipt["plan"]["names"]["source_path"]
    verify_snapshot(client, source, receipt["uploaded_source"], expected_environment=receipt["bound_plan"]["environment"])
    files = {}
    for entry in receipt["uploaded_source"]["files"]:
        with client.workspace.download(source + "/" + entry["path"]) as handle:
            files[entry["path"]] = handle.read(entry["size"] + 1)
    updates = {"wt_evaluation.py": (ROOT / "server/evaluation.py").read_bytes(),
               "wt_evaluation_bootstrap.py": (ROOT / "evals/generated_apps/runtime/wt_evaluation_bootstrap.py").read_bytes()}
    receipt["observer_update"] = {"files": [{"path": name, "before_sha256": sha256(files[name]), "after_sha256": sha256(data)}
        for name, data in updates.items()], "state": "update_requested", "package_bytes_unchanged": True}
    write_evidence(receipt_path, receipt)
    for name, data in updates.items():
        files[name] = data
        client.workspace.upload(source + "/" + name, io.BytesIO(data), format=ImportFormat.AUTO, overwrite=True)
    receipt["uploaded_source"] = source_manifest(files)
    receipt["source_identity"]["uploaded_runtime_digest"] = receipt["uploaded_source"]["digest"]
    receipt["observer_update"]["state"] = "updated"
    receipt["previous_deployment"] = receipt.pop("deployment")
    receipt.pop("error_type", None)
    return submit_deployment(client, receipt, receipt_path)


def stream_digest(handle, *, limit, target=None):
    """Hash bounded Files transfers without holding native binaries in memory."""
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(min(1024 * 1024, limit + 1 - size)):
        size += len(chunk)
        if size > limit:
            raise ValueError("Toolchain transfer exceeds its size bound")
        digest.update(chunk)
        if target is not None:
            target.write(chunk)
    return digest.hexdigest(), size


def stage_toolchain(client, receipt, receipt_path, artifact):
    """Copy only package-pinned blobs into owned storage; no CT grant/write."""
    from databricks.sdk.service.catalog import VolumeType
    from databricks.sdk.errors import NotFound
    plan = receipt["plan"]
    mirror = plan["ct_compatible_contract"]["toolchain_mirror"]
    catalog = plan["names"]["catalog"]
    if (not mirror or plan["environment"]["WORKSHOP_TOOLCHAIN_MIRROR_PATH"] != f"/Volumes/{catalog}/_wt_runtime/toolchain"):
        raise ValueError("Owned toolchain volume binding mismatch")
    app = identity(client, receipt)
    owned = next(x for x in receipt["created_resources"] if x["kind"] == "catalog")
    if owned.get("toolchain_volume"):
        volume = client.volumes.read(owned["toolchain_volume"]["name"])
        if volume.volume_id != owned["toolchain_volume"]["id"]:
            raise ValueError("Owned mirror volume identity changed")
    else:
        volume = client.volumes.create(catalog_name=catalog, schema_name="_wt_runtime", name="toolchain", volume_type=VolumeType.MANAGED)
    owned["toolchain_volume"] = {"name": volume.full_name, "id": volume.volume_id}
    report = {"source_volume": mirror["source_volume"], "destination_volume": plan["environment"]["WORKSHOP_TOOLCHAIN_MIRROR_PATH"],
        "strict": mirror["strict"], "state": "staging", "blobs": [], "control_tower_mutations": 0}
    receipt["toolchain_staging"] = report; write_evidence(receipt_path, receipt)
    grant(client, receipt, receipt_path, "VOLUME", volume.full_name, app.service_principal_client_id, {"READ_VOLUME"})
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        entries = json.loads(archive.read("assets/artifacts/manifest.json"))["artifacts"]
    for name, entry in sorted(entries.items()):
        if "sha256" not in entry or not entry.get("source", "").startswith("https://"):
            continue
        ensure_live(plan)
        digest = entry["sha256"]
        row = {"artifact": name, "sha256": digest, "state": "checking_source"}
        report["blobs"].append(row); write_evidence(receipt_path, receipt)
        with tempfile.TemporaryFile() as data:
            try:
                with client.files.download(mirror["source_volume"] + "/" + digest).contents as handle:
                    observed, size = stream_digest(handle, limit=512 * 1024 * 1024, target=data)
                if observed != digest:
                    raise ValueError("Source toolchain blob failed checksum verification")
            except (NotFound, ValueError) as error:
                row["state"] = ("source_missing_network_fallback" if isinstance(error, NotFound)
                                else "source_checksum_or_size_mismatch_network_fallback")
                write_evidence(receipt_path, receipt)
                if mirror["strict"]:
                    raise ValueError("Strict mirror source is missing or invalid") from error
                continue
            ensure_live(plan)
            destination = report["destination_volume"] + "/" + digest
            try:
                with client.files.download(destination).contents as handle:
                    if stream_digest(handle, limit=size) != (digest, size):
                        raise ValueError("Existing owned mirror blob has different bytes")
            except NotFound:
                data.seek(0)
                client.files.upload(destination, data, overwrite=False)
            with client.files.download(destination).contents as handle:
                if stream_digest(handle, limit=size) != (digest, size):
                    raise ValueError("Staged toolchain blob failed independent readback")
            row.update(state="independently_verified", size_bytes=size); write_evidence(receipt_path, receipt)
    report["state"] = "complete" if all(row["state"] == "independently_verified" for row in report["blobs"]) else "partial_with_declared_network_fallback"
    write_evidence(receipt_path, receipt)


def update_mirror(client, receipt, receipt_path, source_volume, *, strict=False):
    """Add the CT-configured mirror to an existing isolated qualification run."""
    from databricks.sdk.service.workspace import ImportFormat
    from scripts.deploy_generated_app_test import source_manifest
    from scripts.run_generated_app_journey import verify_snapshot
    ensure_live(receipt["plan"])
    app = identity(client, receipt)
    existing_mirror = receipt["plan"]["ct_compatible_contract"].get("toolchain_mirror")
    resuming = existing_mirror == {"source_volume": source_volume, "strict": strict} and receipt.get("toolchain_staging", {}).get("state") == "staging"
    if (not re.fullmatch(r"/Volumes/[A-Za-z0-9_]+/[A-Za-z0-9_]+/[A-Za-z0-9_]+", source_volume)
            or existing_mirror is not None and not resuming):
        raise ValueError("Mirror upgrade requires an exact source volume and no existing mirror")
    source = receipt["plan"]["names"]["source_path"]
    previous_env = {k:v for k,v in receipt["bound_plan"]["environment"].items()
                    if not resuming or k not in {"WORKSHOP_TOOLCHAIN_MIRROR_PATH", "WORKSHOP_TOOLCHAIN_MIRROR_STRICT"}}
    verify_snapshot(client, source, receipt["uploaded_source"], expected_environment=previous_env)
    with client.files.download(receipt["staged_package"]["path"]).contents as handle:
        artifact = handle.read(receipt["staged_package"]["size"] + 1)
    if sha256(artifact) != receipt["staged_package"]["sha256"]:
        raise ValueError("Staged package changed before mirror upgrade")
    plan = receipt["plan"]
    plan["ct_compatible_contract"]["toolchain_mirror"] = {"source_volume": source_volume, "strict": strict}
    plan["environment"].update(WORKSHOP_TOOLCHAIN_MIRROR_PATH=f"/Volumes/{plan['names']['catalog']}/_wt_runtime/toolchain",
                               WORKSHOP_TOOLCHAIN_MIRROR_STRICT=str(strict).lower())
    receipt["bound_plan"] = bind_created_app(plan, {"name": app.name, "service_principal_id": app.service_principal_id, "url": app.url})
    write_evidence(receipt_path, receipt)
    stage_toolchain(client, receipt, receipt_path, artifact)
    with client.workspace.download(source + "/release-manifest.json") as handle:
        manifest_bytes = handle.read(1024 * 1024 + 1)
    files = package_sources(receipt["bound_plan"], manifest_bytes)
    for name, data in files.items():
        client.workspace.upload(source + "/" + name, io.BytesIO(data), format=ImportFormat.AUTO, overwrite=True)
    receipt["uploaded_source"] = source_manifest(files)
    receipt["source_identity"]["uploaded_runtime_digest"] = receipt["uploaded_source"]["digest"]
    receipt["previous_deployment"] = receipt.pop("deployment")
    return submit_deployment(client, receipt, receipt_path)
