#!/usr/bin/env python3
"""Simulate a full Control Tower deployment of the Workshop Terminal app.

This goes beyond ``deploy_dev.py`` (source import + deploy) by reproducing the
Control-Tower contract documented in ``docs/control-tower-implementation.md``,
including the OBO dual-profile feature (§8) and entitlement provisioning (§9):

  1. Import source to the deploying user's workspace home.
  2. Edit the *uploaded* app.yaml in place (never the git copy) to vend a
     WORKSHOP_PAT, set optional model defaults, and turn on ENABLE_OBO /
     ENABLE_ENTITLEMENTS / WORKSHOP_CATALOG.
  3. Create the app, declaring user_api_scopes (catalog.*:read + sql) on the app
     resource (the OBO scope ceiling). NOTE: there is no `unity-catalog` scope —
     use the granular `catalog.catalogs:read` / `catalog.schemas:read` /
     `catalog.tables:read` scopes (list UC metadata) + `sql` (query data).
  4. Grant the app's service principal ``token CAN_USE`` (the §2 critical grant).
  5. Provision the per-attendee catalog: labuser OWNER + ALL_PRIVILEGES, app SP
     USE_CATALOG + CREATE_SCHEMA (§9).
  6. Deploy and print an acceptance summary.

Each CT step is fail-soft: a step that needs an admin grant the deployer lacks
logs a warning and the deploy proceeds, so you always get a running app plus a
clear report of what still needs a workspace admin.

  export DATABRICKS_CONFIG_PROFILE=labs
  python scripts/deploy_ct_sim.py --name workshop-terminal-ct

Requires: databricks-sdk.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import os
import sys
import time

EXCLUDE = ["node_modules*", ".venv*", "__pycache__*", ".git*", "frontend/node_modules*", "*.pyc", "uploads*"]
REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _log(step: str, msg: str) -> None:
    print(f"[ct-sim] {step:<14} {msg}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="workshop-terminal-ct")
    parser.add_argument("--attendee", default="",
                        help="labuser email used for OBO + entitlement grants "
                             "(default: the deploying user)")
    parser.add_argument("--catalog", default="workshop_ct_sim",
                        help="WORKSHOP_CATALOG to provision (labuser owner + ALL PRIVILEGES)")
    parser.add_argument(
        "--scopes",
        default="catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql",
        help="OBO user_api_scopes declared on the app resource (comma-separated). "
             "No 'unity-catalog' scope exists — use the granular 'catalog.*:read' scopes.")
    parser.add_argument("--no-pat", action="store_true",
                        help="skip the vended WORKSHOP_PAT (rely on the SP token CAN_USE grant)")
    parser.add_argument("--no-obo", action="store_true", help="leave ENABLE_OBO off")
    parser.add_argument("--no-entitlements", action="store_true", help="leave ENABLE_ENTITLEMENTS off")
    parser.add_argument(
        "--anthropic-model",
        default="",
        help="Databricks serving endpoint to prefer as the Claude default",
    )
    parser.add_argument(
        "--codex-model",
        default="",
        help="Databricks serving endpoint to prefer as the Codex default",
    )
    args = parser.parse_args()

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.apps import App, AppDeployment
    from databricks.sdk.service.workspace import ImportFormat

    w = WorkspaceClient()
    me = w.current_user.me().user_name
    attendee = args.attendee.strip() or me
    enable_obo = not args.no_obo
    enable_ent = not args.no_entitlements
    target = f"/Workspace/Users/{me}/apps/{args.name}"
    _log("identity", f"deploying as {me} -> app '{args.name}' (attendee/labuser={attendee})")

    # --- vended credential (CT vends its own; here a 12h PAT as the deployer) ---
    workshop_pat = ""
    if not args.no_pat:
        workshop_pat = w.tokens.create(
            comment=f"{args.name} ct-sim vended credential", lifetime_seconds=43200
        ).token_value
        _log("credential", "minted 12h WORKSHOP_PAT (vended credential)")

    # --- 1+2. import source, editing the uploaded app.yaml in place ---
    _log("import", f"uploading source to {target}")
    n_files = 0
    for root, dirs, files in os.walk(REPO_ROOT):
        rel_root = os.path.relpath(root, REPO_ROOT)
        dirs[:] = [
            d for d in dirs
            if not any(fnmatch.fnmatch(os.path.normpath(os.path.join(rel_root, d)), p) for p in EXCLUDE)
        ]
        for name in files:
            rel = os.path.normpath(os.path.join(rel_root, name))
            if any(fnmatch.fnmatch(rel, p) for p in EXCLUDE):
                continue
            with open(os.path.join(root, name), "rb") as f:
                content = f.read()
            if rel == "app.yaml":
                content = _patch_app_yaml(
                    content, workshop_pat, enable_obo, enable_ent,
                    args.catalog if enable_ent else "", args.scopes,
                    args.anthropic_model.strip(), args.codex_model.strip(),
                )
            ws_path = f"{target}/{rel}".replace("\\", "/")
            w.workspace.mkdirs(os.path.dirname(ws_path))
            w.workspace.upload(ws_path, io.BytesIO(content), format=ImportFormat.AUTO, overwrite=True)
            n_files += 1
    _log("import", f"uploaded {n_files} files")

    # --- 3. create app, declaring OBO scopes on the app resource ---
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()] if enable_obo else None
    app = _create_app(w, App, args.name, scopes)
    sp_id = getattr(app, "service_principal_client_id", None)
    _log("create", f"app SP client id = {sp_id}")

    # --- 4. grant the app SP token CAN_USE (the critical §2 grant) ---
    if sp_id:
        _grant_token_can_use(w, sp_id)
    else:
        _log("token-grant", "WARN no service_principal_client_id on app — skipping token CAN_USE")

    # --- 5. provision the per-attendee catalog (§9) ---
    if enable_ent:
        _provision_catalog(w, args.catalog, attendee, sp_id)

    # --- 6. deploy ---
    _log("deploy", "deploying source bundle ...")
    deployment = w.apps.deploy_and_wait(
        app_name=args.name, app_deployment=AppDeployment(source_code_path=target)
    )
    app = w.apps.get(args.name)
    state = deployment.status.state if deployment.status else "unknown"
    _log("deploy", f"deployment state = {state}")

    applied_scopes = getattr(app, "user_api_scopes", None) or []
    _print_summary(args, me, attendee, app, sp_id, enable_obo, enable_ent,
                   bool(workshop_pat), applied_scopes)
    return 0


def _patch_app_yaml(
    content,
    pat,
    enable_obo,
    enable_ent,
    catalog,
    scopes,
    anthropic_model="",
    codex_model="",
):
    def sub(b, k, v):
        return b.replace(
            f'- name: {k}\n    value: ""'.encode(),
            f'- name: {k}\n    value: "{v}"'.encode(),
        )

    if pat:
        content = sub(content, "WORKSHOP_PAT", pat)
    if enable_obo:
        content = content.replace(
            b'- name: ENABLE_OBO\n    value: "false"',
            b'- name: ENABLE_OBO\n    value: "true"',
        )
        content = sub(content, "OBO_SCOPES", scopes) if b'OBO_SCOPES\n    value: ""' in content else content
    if enable_ent:
        content = content.replace(
            b'- name: ENABLE_ENTITLEMENTS\n    value: "false"',
            b'- name: ENABLE_ENTITLEMENTS\n    value: "true"',
        )
        if catalog:
            content = sub(content, "WORKSHOP_CATALOG", catalog)
    if anthropic_model:
        content = sub(content, "ANTHROPIC_MODEL", anthropic_model)
    if codex_model:
        content = sub(content, "CODEX_MODEL", codex_model)
    return content


def _create_app(w, App, name, scopes):
    kwargs = {"name": name}
    if scopes:
        kwargs["user_api_scopes"] = scopes
    try:
        w.apps.create_and_wait(App(**kwargs))
        _log("create", f"app created (user_api_scopes={scopes})")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "already exists" in msg.lower():
            _log("create", "app exists — updating scopes + redeploying")
            try:
                if scopes:
                    w.apps.update(name, App(**kwargs))
            except Exception as ue:  # noqa: BLE001
                _log("create", f"WARN could not update user_api_scopes: {ue}")
        elif scopes:
            _log("create", f"WARN create with scopes failed ({msg.splitlines()[0]}); retrying without scopes")
            w.apps.create_and_wait(App(name=name))
        else:
            raise
    return w.apps.get(name)


def _grant_token_can_use(w, sp_id):
    from databricks.sdk.service.settings import TokenAccessControlRequest, TokenPermissionLevel

    try:
        w.token_management.update_permissions(
            access_control_list=[
                TokenAccessControlRequest(
                    service_principal_name=sp_id,
                    permission_level=TokenPermissionLevel.CAN_USE,
                )
            ]
        )
        _log("token-grant", f"granted token CAN_USE to app SP {sp_id}")
    except Exception as e:  # noqa: BLE001
        _log("token-grant", f"WARN could not grant token CAN_USE (needs workspace admin): {e}")


def _pick_warehouse(w):
    """First STARTING/RUNNING/STOPPED warehouse (prefer serverless), or None."""
    whs = list(w.warehouses.list())
    if not whs:
        return None
    whs.sort(key=lambda x: (not getattr(x, "enable_serverless_compute", False),))
    return whs[0].id


def _sql(w, wh, stmt):
    from databricks.sdk.service.sql import StatementState
    r = w.statement_execution.execute_statement(statement=stmt, warehouse_id=wh, wait_timeout="50s")
    if r.status.state == StatementState.FAILED:
        raise RuntimeError(r.status.error.message if r.status.error else "statement failed")
    return r


def _provision_catalog(w, catalog, attendee, sp_id):
    """CT §9: per-attendee catalog, labuser OWNER + ALL PRIVILEGES, app SP USE/CREATE.

    Tries the SDK create; on a Default-Storage metastore (no storage root) the
    REST create is rejected, so we fall back to a SQL ``CREATE CATALOG`` via a
    warehouse (which resolves default storage). Grants are issued via SQL too —
    the typed ``grants.update`` enum mis-serializes on some SDK builds.
    """
    exists = False
    try:
        w.catalogs.get(catalog)
        exists = True
        _log("catalog", f"catalog '{catalog}' exists — reusing")
    except Exception:  # noqa: BLE001 — not found / no access
        try:
            w.catalogs.create(name=catalog, comment="Workshop CT-sim per-attendee catalog")
            exists = True
            _log("catalog", f"created catalog '{catalog}' (SDK)")
        except Exception as e:  # noqa: BLE001
            _log("catalog", f"SDK create blocked ({str(e).splitlines()[0]}); trying SQL")

    wh = _pick_warehouse(w)
    if not exists:
        if not wh:
            _log("catalog", "WARN no warehouse available to CREATE CATALOG via SQL — skipping")
            return
        try:
            _sql(w, wh, f"CREATE CATALOG IF NOT EXISTS {catalog} "
                        f"COMMENT 'Workshop CT-sim per-attendee catalog'")
            _log("catalog", f"created catalog '{catalog}' (SQL/default-storage)")
        except Exception as e:  # noqa: BLE001
            _log("catalog", f"WARN could not create catalog '{catalog}': {e}")
            return

    if not wh:
        _log("catalog", "WARN no warehouse to issue GRANTs — set owner/grants manually")
        return
    try:
        _sql(w, wh, f"GRANT ALL PRIVILEGES ON CATALOG {catalog} TO `{attendee}`")
        if sp_id:
            _sql(w, wh, f"GRANT USE CATALOG, CREATE SCHEMA ON CATALOG {catalog} TO `{sp_id}`")
        _log("catalog", f"granted ALL PRIVILEGES to {attendee}; USE/CREATE to app SP")
    except Exception as e:  # noqa: BLE001
        _log("catalog", f"WARN could not set catalog grants: {e}")
    try:
        w.catalogs.update(name=catalog, owner=attendee)
        _log("catalog", f"set catalog owner = {attendee}")
    except Exception as e:  # noqa: BLE001
        _log("catalog", f"WARN could not set catalog owner: {e}")


def _print_summary(args, me, attendee, app, sp_id, enable_obo, enable_ent, vended, applied_scopes):
    print("\n" + "=" * 72)
    print("CT-SIM DEPLOYMENT SUMMARY")
    print("=" * 72)
    print(f"  app name        : {args.name}")
    print(f"  app url         : {app.url}")
    print(f"  deployed by     : {me}")
    print(f"  attendee/labuser: {attendee}")
    print(f"  app SP id       : {sp_id}")
    print(f"  vended PAT      : {'yes (degraded — prefer SP grant)' if vended else 'no (SP token CAN_USE)'}")
    scope_str = ",".join(applied_scopes) if applied_scopes else "(none applied — defaults iam.* only)"
    print(f"  OBO             : {'ENABLED, user_api_scopes=' + scope_str if enable_obo else 'off'}")
    print(f"  entitlements    : {'ENABLED, catalog=' + args.catalog if enable_ent else 'off'}")
    print("\nAcceptance checks (open the app URL in a browser first to mint an OBO token):")
    print(f"  curl -s {app.url}/api/config -H 'Authorization: Bearer <admin>' | jq '.credential,.obo,.entitlements'")
    print("  In a terminal session:")
    print("    databricks current-user me            # -> the app service principal")
    print("    databricks-me current-user me         # -> the attendee (OBO)")
    print(f"    databricks-me catalogs list           # -> includes {args.catalog}")
    print("\nNOTE: workspace 'User authorization' must be enabled and consent granted")
    print("      for the OBO token to be forwarded; otherwise obo.present stays false.")
    print("=" * 72)


if __name__ == "__main__":
    sys.exit(main())
