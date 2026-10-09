#!/usr/bin/env python3
"""Deploy a fresh WT test instance directly, with a simulated CT handoff.

No Control Tower client, URL, deployment, settings or existing event is used.
Only runtime sources are uploaded; novice private facts and judges stay outside.
The receipt distinguishes simulation from actual CT integration qualification.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml

from evals.generated_apps.adapters.simulated_control_tower import (
    bind_created_app, deployment_path_allowed, plan_simulation,
)
from evals.generated_apps.report import write_evidence
from scripts.deploy_ct_sim import (
    _catalog_sql_plan, _pick_warehouse, _quoted, _scim_literal, _sql, _verify_catalog_access,
)


def runtime_sources(root: Path) -> dict[str, bytes]:
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if not deployment_path_allowed(relative):
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Runtime source links are not deployable")
        # rglob does not recurse through links; also reject a linked ancestor.
        if any(parent.is_symlink() for parent in path.parents if parent != root.parent):
            raise ValueError("Runtime source links are not deployable")
        result[relative] = path.read_bytes()
    validate_source_bundle(result)
    return result


def validate_source_bundle(files):
    if not isinstance(files, dict) or any(not deployment_path_allowed(name) or not isinstance(content, bytes)
                                         for name, content in files.items()):
        raise ValueError("Only runtime files may be deployed")
    if not {"app.yaml", "requirements.txt", "server/main.py", "static/index.html", "content/default_pack.json",
            "assets/artifacts/manifest.json"} <= files.keys():
        raise ValueError("Runtime source bundle is incomplete")


def source_manifest(files: dict[str, bytes]) -> dict:
    records = [{"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
               for name, content in sorted(files.items())]
    digest = hashlib.sha256(json.dumps(records, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    return {"digest": digest, "digest_kind": "sorted-runtime-file-manifest-sha256", "files": records}


def app_yaml(content: bytes, environment: dict) -> bytes:
    value = yaml.safe_load(content)
    existing = value.get("env", [])
    names = [entry.get("name") for entry in existing]
    if len(set(names)) != len(names):
        raise ValueError("Runtime environment contains duplicate names")
    patches = dict(environment)
    # CT delivery is simulated, not pointed at any real CT endpoint.
    patches.update(CONTROL_TOWER_URL="", CONTROL_TOWER_INGEST_URL="", CONTROL_TOWER_INGEST_TOKEN="",
                   WORKSHOP_PAT="", OMNIGENT_APP_URL="")
    for entry in existing:
        if entry["name"] in patches:
            entry.pop("valueFrom", None)
            entry["value"] = patches.pop(entry["name"])
    existing.extend({"name": key, "value": str(val)} for key, val in sorted(patches.items()))
    value["env"] = existing
    return yaml.safe_dump(value, sort_keys=False).encode()


def require_absent(operation):
    from databricks.sdk.errors import NotFound
    try:
        operation()
    except NotFound:
        return
    raise ValueError("Simulation requires a fresh resource; existing resources cannot be reused")


def validate_resume_receipt(plan: dict, previous: dict) -> dict:
    """Resume only the exact app-identity stage; never reuse other resources."""
    if (not isinstance(previous, dict) or previous.get("schema_version") != 1
            or previous.get("scope") != "simulated_control_tower"
            or type(previous.get("control_tower_requests")) is not int
            or previous["control_tower_requests"] != 0 or previous.get("accepted") is not False):
        raise ValueError("Resume requires a simulated deployment ownership receipt")
    old = previous.get("plan")
    if not isinstance(old, dict) or any(old.get(key) != plan.get(key) for key in (
        "marker", "profile", "workspace_host", "names", "run_id", "unit_id",
        "attendee", "app_resource", "bounds", "artifact_manifest_identity", "environment",
    )):
        raise ValueError("Resume receipt differs from the reviewed simulation")
    resources = previous.get("created_resources")
    if (not isinstance(resources, list) or len(resources) != 1
            or not isinstance(resources[0], dict) or resources[0].get("kind") != "app"
            or resources[0].get("name") != plan["names"]["app_name"]
            or not isinstance(resources[0].get("id"), str) or not resources[0]["id"]
            or resources[0].get("state") not in {"created", "identity_pending", "creation_requested"}
            or any(key in previous for key in ("bound_plan", "uploaded_source", "deployment", "source_identity"))):
        raise ValueError("Resume is restricted to the app identity stage with an exact app ID")
    if ({key: value for key, value in old.get("release", {}).items() if key != "source_digest"}
            != {key: value for key, value in plan.get("release", {}).items() if key != "source_digest"}):
        raise ValueError("Resume cannot change the runtime release policy")
    return copy.deepcopy(resources[0])


def await_app_identity(client, plan, app_resource, receipt, receipt_path, *, timeout_seconds=60):
    """Boundedly wait for app identity and requested scopes to become visible."""
    from databricks.sdk.errors import NotFound

    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 60):
        raise ValueError("App identity wait must be bounded to 60 seconds")
    deadline = time.monotonic() + timeout_seconds
    expected_id = app_resource.get("id")
    while True:
        try:
            app = client.apps.get(plan["names"]["app_name"])
        except NotFound:
            app = None
        if app is not None:
            if app.name != plan["names"]["app_name"] or (expected_id and app.id != expected_id):
                raise ValueError("App identity does not match the exact creation receipt")
            if not app.id:
                raise ValueError("Created app is missing its immutable app ID")
            expected_id = app.id
            app_resource.update(id=app.id, state="identity_pending",
                                service_principal_id=app.service_principal_id,
                                service_principal_client_id=app.service_principal_client_id,
                                url=app.url)
            write_evidence(receipt_path, receipt)
            # The platform may expose the SP before the requested OBO scopes.
            # Keep the app-only receipt resumable while that state converges.
            scopes_ready = set(plan["app_resource"]["user_api_scopes"]) <= set(app.user_api_scopes or [])
            if (app.service_principal_id is not None and app.service_principal_client_id
                    and app.url and app.url != "Unavailable" and scopes_ready):
                bound = bind_created_app(plan, {
                    "name": app.name, "service_principal_id": app.service_principal_id, "url": app.url,
                })
                app_resource["state"] = "created"
                write_evidence(receipt_path, receipt)
                return app, bound
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Created app identity or OBO scopes are still unavailable; preserve receipt for bounded resume")
        time.sleep(min(2, remaining))


def grant_attendee_app_access(client, plan, app_resource, receipt, receipt_path, *, timeout_seconds=60,
                              minimum_permission="CAN_USE"):
    """Add attendee access to the owned app and verify a separate ACL read."""
    from databricks.sdk.service.apps import AppAccessControlRequest, AppPermissionLevel

    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 60):
        raise ValueError("App permission verification must be bounded to 60 seconds")
    app_name, app_id = plan["names"]["app_name"], app_resource.get("id")
    if app_resource.get("name") != app_name or not isinstance(app_id, str) or not app_id:
        raise ValueError("App permission changes require the exact created app ID")
    attendee = plan["attendee"]["email"]
    if minimum_permission not in {"CAN_USE", "CAN_MANAGE"}:
        raise ValueError("Unsupported attendee app permission")
    required_level = AppPermissionLevel(minimum_permission)

    def verify_identity():
        actual = client.apps.get(app_name)
        if actual.name != app_name or actual.id != app_id:
            raise ValueError("App permission target differs from the exact creation receipt")

    def has_access(permissions):
        # CAN_MANAGE already includes use; never downgrade an operator-bound
        # attendee's existing owner permission just to request CAN_USE.
        return any(
            entry.user_name and entry.user_name.casefold() == attendee.casefold()
            and any(permission.permission_level in ({AppPermissionLevel.CAN_MANAGE} if minimum_permission == "CAN_MANAGE"
                                                    else {AppPermissionLevel.CAN_USE, AppPermissionLevel.CAN_MANAGE})
                    for permission in entry.all_permissions or [])
            for entry in permissions.access_control_list or []
        )

    verify_identity()
    existing = client.apps.get_permissions(app_name)
    update_required = not has_access(existing)
    access = {"app_name": app_name, "app_id": app_id, "attendee_email": attendee,
              "minimum_permission": minimum_permission, "update_required": update_required,
              "state": "grant_requested" if update_required else "readback_pending"}
    receipt["attendee_app_access"] = access
    write_evidence(receipt_path, receipt)
    if update_required:
        verify_identity()
        # PATCH only this user; replacing the ACL would erase owner/inherited
        # grants. The update response is not accepted as verification evidence.
        client.apps.update_permissions(app_name, access_control_list=[
            AppAccessControlRequest(user_name=attendee, permission_level=required_level),
        ])
        access["state"] = "readback_pending"
        write_evidence(receipt_path, receipt)
    deadline = time.monotonic() + timeout_seconds
    while True:
        verify_identity()
        if has_access(client.apps.get_permissions(app_name)):
            access.update(state="independently_verified", effective_permission_verified=True)
            write_evidence(receipt_path, receipt)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Attendee CAN_USE app permission did not verify; preserve the owned app receipt")
        time.sleep(min(2, remaining))


def provision_isolated_catalog(client, plan, app_resource, operator, receipt, receipt_path):
    """Keep verified operator cleanup rights before handing off a fresh catalog."""
    names = plan["names"]
    catalog = names["catalog"]
    resources = [resource for resource in receipt.get("created_resources", [])
                 if resource.get("kind") == "catalog"]
    apps = [resource for resource in receipt.get("created_resources", []) if resource.get("kind") == "app"]
    if (receipt.get("scope") != "simulated_control_tower" or receipt.get("control_tower_requests") != 0
            or client.config.host.rstrip("/") != plan["workspace_host"]
            or not plan["marker"].startswith("wt-eval-") or catalog != plan["marker"].replace("-", "_")
            or len(resources) != 1 or resources[0].get("name") != catalog
            or resources[0].get("state") != "creation_requested"
            or len(apps) != 1 or apps[0] != app_resource or app_resource.get("state") != "created"
            or app_resource.get("name") != names["app_name"] or not app_resource.get("id")
            or not app_resource.get("service_principal_client_id")
            or "catalog_provisioning" in receipt):
        raise ValueError("Catalog provisioning requires the exact fresh isolated resource receipt")
    actual_app = client.apps.get(names["app_name"])
    if (actual_app.name != app_resource["name"] or actual_app.id != app_resource["id"]
            or actual_app.service_principal_client_id != app_resource["service_principal_client_id"]
            or actual_app.service_principal_id != app_resource.get("service_principal_id")):
        raise ValueError("Catalog app grants must target the independently verified owned app identity")
    actual = client.current_user.me()
    principal = getattr(actual, "user_name", None)
    identity_id = getattr(actual, "id", None)
    if (not principal or not identity_id or str(identity_id) != str(getattr(operator, "id", ""))
            or principal.casefold() != str(getattr(operator, "user_name", "")).casefold()):
        raise ValueError("Catalog cleanup grants must target the independently verified current operator")
    cat, operator_sql = _quoted(catalog), _quoted(principal)
    attendee, app_id = plan["attendee"]["email"], app_resource["service_principal_client_id"]
    require_absent(lambda: client.catalogs.get(catalog))
    warehouse_id = _pick_warehouse(client)
    if not warehouse_id:
        raise RuntimeError("Isolated catalog provisioning requires an existing SQL warehouse")
    progress = {"catalog": catalog, "warehouse_id": warehouse_id, "status": "provisioning", "steps": []}
    operator_access = {"catalog": catalog, "principal": principal, "identity_id": str(identity_id),
                       "privileges": ["ALL_PRIVILEGES", "MANAGE"], "state": "grant_requested",
                       "verified_before_owner_transfer": False, "verified_after_owner_transfer": False}
    receipt["catalog_provisioning"], receipt["catalog_operator_access"] = progress, operator_access
    write_evidence(receipt_path, receipt)

    def execute(operation, statement):
        step = {"operation": operation, "state": "requested"}
        progress["steps"].append(step)
        write_evidence(receipt_path, receipt)
        result = _sql(client, warehouse_id, statement)
        step["state"] = "succeeded"
        if getattr(result, "statement_id", None):
            step["statement_id"] = result.statement_id
        write_evidence(receipt_path, receipt)

    def verify_operator():
        response = client.grants.get(securable_type="catalog", full_name=catalog)
        privileges = set()
        for assignment in response.privilege_assignments or []:
            if str(assignment.principal or "").casefold() == principal.casefold():
                privileges.update(str(getattr(privilege, "value", privilege)).upper().replace(" ", "_")
                                  for privilege in assignment.privileges or [])
        if not {"ALL_PRIVILEGES", "MANAGE"} <= privileges:
            raise RuntimeError("Isolated operator catalog cleanup grants failed independent readback")

    try:
        # A fresh-only CREATE fails closed if another catalog appeared since
        # preflight; IF NOT EXISTS must never permit granting on such a catalog.
        execute("create", f"CREATE CATALOG {cat} COMMENT 'Disposable Workshop Terminal E2E catalog'")
        resources[0]["state"] = "created"
        write_evidence(receipt_path, receipt)
        execute("operator_all_privileges", f"GRANT ALL PRIVILEGES ON CATALOG {cat} TO {operator_sql}")
        execute("operator_manage", f"GRANT MANAGE ON CATALOG {cat} TO {operator_sql}")
        verify_operator()
        operator_access.update(state="verified_before_owner_transfer", verified_before_owner_transfer=True)
        write_evidence(receipt_path, receipt)
        statements = _catalog_sql_plan(catalog, attendee, app_id, create=False)
        for operation, statement in zip(("attendee_all_privileges", "attendee_manage", "app_access", "attendee_owner"), statements):
            execute(operation, statement)
        _verify_catalog_access(client, catalog, attendee, app_id, None)
        verify_operator()
        operator_access.update(state="independently_verified", verified_after_owner_transfer=True)
        progress["status"] = "created_and_grants_verified"
        write_evidence(receipt_path, receipt)
        return True
    except Exception as error:
        progress.update(status="failed", error_type=type(error).__name__)
        write_evidence(receipt_path, receipt)
        raise


def upload_runtime_sources(client, plan, files, receipt, receipt_path, *, timeout_seconds=900,
                           pace_seconds=1.1, max_attempts=4):
    """Pace owned no-overwrite uploads; reconcile ambiguous responses by bytes.

    The phase deadline is checked between SDK calls, whose request/retry times
    are separately bounded by ``workspace_client``. This is not a resume path.
    """
    from databricks.sdk.errors import (
        DeadlineExceeded, InternalError, NotFound, OperationTimeout,
        ResourceExhausted, TemporarilyUnavailable,
    )
    from databricks.sdk.service.workspace import ImportFormat
    from requests.exceptions import ConnectionError as RequestConnectionError, Timeout as RequestTimeout

    validate_source_bundle(files)
    for name, value, maximum in (("timeout", timeout_seconds, 900), ("pace", pace_seconds, 10)):
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum):
            raise ValueError(f"Source upload {name} must be finite, positive and bounded")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 4:
        raise ValueError("Source upload attempts must be bounded to four per file")
    source_path = plan["names"]["source_path"]
    ownership = [resource for resource in receipt.get("created_resources", [])
                 if resource.get("kind") == "workspace_source"]
    if (receipt.get("scope") != "simulated_control_tower" or receipt.get("control_tower_requests") != 0
            or len(ownership) != 1 or ownership[0].get("path") != source_path
            or ownership[0].get("state") != "created" or receipt.get("uploaded_source") != source_manifest(files)
            or "source_upload_progress" in receipt):
        raise ValueError("Source upload requires the exact fresh owned source and manifest receipt")
    retryable = (DeadlineExceeded, InternalError, OperationTimeout, ResourceExhausted,
                 TemporarilyUnavailable, RequestConnectionError, RequestTimeout, TimeoutError)
    started = time.monotonic()
    deadline = started + timeout_seconds
    last_request = None
    progress = {"status": "uploading", "source_path": source_path, "total_files": len(files),
                "uploaded_files": 0, "timeout_seconds": timeout_seconds, "pace_seconds": pace_seconds,
                "max_attempts_per_file": max_attempts, "files": []}
    receipt["source_upload_progress"] = progress
    write_evidence(receipt_path, receipt)

    def checkpoint():
        progress["elapsed_seconds"] = max(0, time.monotonic() - started)
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned source upload exceeded its bounded phase deadline")

    def pause(seconds):
        checkpoint()
        time.sleep(min(seconds, max(0, deadline - time.monotonic())))
        checkpoint()

    def pace():
        checkpoint()
        if last_request is not None:
            remaining = pace_seconds - (time.monotonic() - last_request)
            if remaining > 0:
                pause(remaining)

    def readback(path, content, record):
        nonlocal last_request
        for attempt in range(1, max_attempts + 1):
            record["readback_attempts"] = record.get("readback_attempts", 0) + 1
            write_evidence(receipt_path, receipt)
            pace()
            try:
                with client.workspace.download(path) as stream:
                    actual = stream.read(len(content) + 1)
                checkpoint()
            except NotFound:
                checkpoint()
                return False
            except retryable:
                checkpoint()
                if attempt == max_attempts:
                    record["state"] = "readback_unverified"
                    raise TimeoutError("Ambiguous source upload could not be independently read back") from None
                pause(2 ** attempt)
                continue
            finally:
                last_request = time.monotonic()
            if len(actual) != len(content) or hashlib.sha256(actual).digest() != hashlib.sha256(content).digest():
                record["state"] = "source_conflict"
                raise ValueError("Owned source bytes differ from the reviewed upload manifest")
            return True

    try:
        for name, content in sorted(files.items()):
            record = {"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                      "state": "pending", "attempts": 0}
            progress["files"].append(record)
            path = source_path + "/" + name
            for attempt in range(1, max_attempts + 1):
                pace()
                record.update(state="upload_requested", attempts=attempt)
                write_evidence(receipt_path, receipt)
                try:
                    client.workspace.upload(path, io.BytesIO(content), format=ImportFormat.AUTO, overwrite=False)
                    checkpoint()
                except Exception as error:
                    record.update(state="readback_pending", error_type=type(error).__name__)
                    write_evidence(receipt_path, receipt)
                    if readback(path, content, record):
                        record["state"] = "verified_after_ambiguous_response"
                        break
                    record["state"] = "upload_not_present"
                    write_evidence(receipt_path, receipt)
                    if not isinstance(error, retryable) or attempt == max_attempts:
                        raise
                    pause(2 ** attempt)
                else:
                    record["state"] = "upload_acknowledged"
                    break
                finally:
                    last_request = time.monotonic()
            progress["uploaded_files"] += 1
            write_evidence(receipt_path, receipt)
        checkpoint()
        progress["status"] = "uploaded"
        write_evidence(receipt_path, receipt)
    except Exception as error:
        progress.update(status="failed", error_type=type(error).__name__,
                        elapsed_seconds=max(0, time.monotonic() - started))
        write_evidence(receipt_path, receipt)
        raise


def deploy(plan: dict, files: dict[str, bytes], receipt_path: str, *, client, resume_receipt=None):
    from databricks.sdk.service.apps import App, AppDeployment
    from databricks.sdk.service.iam import ComplexValue

    validate_source_bundle(files)
    resumed_app = validate_resume_receipt(plan, resume_receipt) if resume_receipt is not None else None
    receipt = {"schema_version": 1, "scope": "simulated_control_tower", "control_tower_requests": 0,
               "plan": plan, "created_resources": [resumed_app] if resumed_app else [], "accepted": False}
    if resumed_app:
        receipt["resume_provenance"] = {"previous_app_id": resumed_app["id"],
                                        "previous_status": resume_receipt.get("status"),
                                        "app_identity_independently_verified": False}
        write_evidence(receipt_path, receipt)
    w = client
    if w.config.host.rstrip("/") != plan["workspace_host"]:
        raise ValueError("Profile host differs from the reviewed simulation")
    me = w.current_user.me()
    if plan["attendee"]["mode"] == "operator_bound" and me.user_name.casefold() != plan["attendee"]["email"]:
        raise ValueError("Operator identity differs from the reviewed browser principal")
    names = plan["names"]
    if resumed_app:
        from databricks.sdk.errors import NotFound
        try:
            existing_app = w.apps.get(names["app_name"])
        except NotFound as error:
            raise ValueError("Resume app no longer exists; no replacement will be created") from error
        if existing_app.id != resumed_app["id"] or existing_app.name != names["app_name"]:
            raise ValueError("Resume app differs from the exact previous app ID")
        receipt["resume_provenance"]["app_identity_independently_verified"] = True
        write_evidence(receipt_path, receipt)
    else:
        require_absent(lambda: w.apps.get(names["app_name"]))
    require_absent(lambda: w.catalogs.get(names["catalog"]))
    require_absent(lambda: w.workspace.get_status(names["source_path"]))
    if list(w.groups.list(filter=f'displayName eq "{_scim_literal(names["admin_group"])}"')):
        raise ValueError("Simulation operator group already exists")
    write_evidence(receipt_path, receipt)  # Prove destination before creation.

    app_resource = resumed_app
    if app_resource is None:
        app_resource = {"kind": "app", "name": names["app_name"], "state": "creation_requested"}
        receipt["created_resources"].append(app_resource)
        write_evidence(receipt_path, receipt)
        # Compute must start for the platform to assign its SP and app URL.
        # Consume the creation response; never block on the SDK's ACTIVE waiter.
        pending_app = w.apps.create(App(name=names["app_name"],
            description="Disposable Workshop Terminal E2E evaluation; simulated CT handoff",
            user_api_scopes=plan["app_resource"]["user_api_scopes"]), no_compute=False)
        submitted_app = pending_app.response
        if submitted_app is not None:
            if submitted_app.name != names["app_name"]:
                raise ValueError("Creation response does not match the isolated app")
            app_resource.update(id=submitted_app.id,
                                service_principal_id=submitted_app.service_principal_id,
                                service_principal_client_id=submitted_app.service_principal_client_id,
                                url=submitted_app.url)
            write_evidence(receipt_path, receipt)
    app, bound = await_app_identity(w, plan, app_resource, receipt, receipt_path)
    if not set(plan["app_resource"]["user_api_scopes"]) <= set(app.user_api_scopes or []):
        raise ValueError("Platform did not preserve requested OBO scopes")
    grant_attendee_app_access(w, plan, app_resource, receipt, receipt_path)
    group_resource = {"kind": "group", "name": names["admin_group"], "state": "creation_requested"}
    receipt["created_resources"].append(group_resource)
    write_evidence(receipt_path, receipt)
    group = w.groups.create(display_name=names["admin_group"],
        members=[ComplexValue(value=str(me.id)), ComplexValue(value=str(app.service_principal_id))])
    group_resource.update(id=group.id, state="created")
    write_evidence(receipt_path, receipt)
    catalog_resource = {"kind": "catalog", "name": names["catalog"], "state": "creation_requested"}
    receipt["created_resources"].append(catalog_resource)
    write_evidence(receipt_path, receipt)
    if not provision_isolated_catalog(w, plan, app_resource, me, receipt, receipt_path):
        raise RuntimeError("Isolated catalog provisioning did not verify")
    catalog_resource["state"] = "created_and_grants_verified"
    write_evidence(receipt_path, receipt)
    files = dict(files)
    files["app.yaml"] = app_yaml(files["app.yaml"], bound["environment"])
    manifest = source_manifest(files)
    receipt["uploaded_source"] = manifest
    receipt["source_identity"] = {"planned_unpatched_digest": plan["release"]["source_digest"],
        "uploaded_runtime_digest": manifest["digest"], "runtime_environment_patched": True,
        "instrumented_release": plan["release"].get("instrumentation", False)}
    receipt["bound_plan"] = bound
    write_evidence(receipt_path, receipt)
    receipt["created_resources"].append({"kind": "workspace_source", "path": names["source_path"],
                                         "state": "creation_requested"})
    write_evidence(receipt_path, receipt)
    directories = sorted({str(Path(name).parent) for name in files})
    for directory in directories:
        w.workspace.mkdirs(names["source_path"] + ("" if directory == "." else "/" + directory))
    receipt["created_resources"][-1]["state"] = "created"
    write_evidence(receipt_path, receipt)
    upload_runtime_sources(w, plan, files, receipt, receipt_path)
    # Async submission; caller polls this exact app, without blocking a chat.
    pending = w.apps.deploy(app_name=app.name,
                          app_deployment=AppDeployment(source_code_path=names["source_path"]))
    deployment = pending.response
    receipt["deployment"] = deployment.as_dict() if deployment else {"submitted": True}
    receipt["status"] = "deployment_submitted"
    write_evidence(receipt_path, receipt)
    return receipt


def workspace_client(profile, *, http_timeout_seconds=10):
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config

    return WorkspaceClient(config=Config(profile=profile, http_timeout_seconds=http_timeout_seconds, retry_timeout_seconds=15))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume-receipt", help="Resume only the exact previously created app's pending identity")
    args = parser.parse_args(argv)
    if Path(args.spec).resolve() == Path(args.output).resolve():
        parser.error("Output must differ from the simulation input")
    if Path(args.output).exists() or Path(args.output).is_symlink():
        parser.error("A fresh deployment requires a new receipt path; preserve earlier cleanup ownership")
    previous = json.loads(Path(args.resume_receipt).read_text()) if args.resume_receipt else None
    resume_start = None
    if previous is not None:
        try:
            resume_start = datetime.fromisoformat(previous["plan"]["bounds"]["created_at"].replace("Z", "+00:00"))
            expiry = datetime.fromisoformat(previous["plan"]["bounds"]["expires_at"].replace("Z", "+00:00"))
            if resume_start.tzinfo is None or expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise ValueError("expired")
        except (KeyError, TypeError, ValueError) as error:
            parser.error("Resume requires valid, unexpired original deployment bounds")
    files = runtime_sources(ROOT)
    spec = json.loads(Path(args.spec).read_text())
    raw_manifest = source_manifest(files)
    spec["release"]["source_digest"] = raw_manifest["digest"]
    plan = plan_simulation(spec, json.loads((ROOT / "assets/artifacts/manifest.json").read_text()), now=resume_start)
    prior_app = validate_resume_receipt(plan, previous) if previous is not None else None
    initial = {"plan": plan, "source_manifest": raw_manifest, "executed": False}
    if prior_app is not None:
        initial.update(schema_version=1, scope="simulated_control_tower", control_tower_requests=0,
                       created_resources=[prior_app], accepted=False, resume_receipt=args.resume_receipt)
    write_evidence(args.output, initial)
    if not args.execute:
        print(json.dumps({"output": args.output, "control_tower_requests": 0, "executed": False}))
        return 0
    try:
        result = deploy(plan, files, args.output, client=workspace_client(plan["profile"]),
                        resume_receipt=previous)
    except Exception as error:
        # Keep the exact created-resource receipt; errors must not erase cleanup ownership.
        result = json.loads(Path(args.output).read_text())
        result.update(status="failed", error_type=type(error).__name__)
        write_evidence(args.output, result)
        print(json.dumps({"output": args.output, "status": "failed", "error_type": type(error).__name__}))
        return 2
    print(json.dumps({"output": args.output, "status": result["status"], "control_tower_requests": 0,
                      "app_name": plan["names"]["app_name"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
