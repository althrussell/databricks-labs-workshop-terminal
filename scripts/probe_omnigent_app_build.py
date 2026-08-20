#!/usr/bin/env python3
"""Prove the Omnigent App's uv.lock installs 0.10.0 on Databricks Apps.

Mode 3 acceptance needs a dedicated Lakebase and UC Volume, which is real
persistent infrastructure. The dependency resolution is the part the version
bump actually changed, and it can be checked without any of that: deploy the
App source with the two resource bindings stubbed out, then read the build log.

Startup is expected to fail at Lakebase. That failure is the success signal —
reaching it means Python 3.12 was provisioned and the locked 0.10.0 wheel set
installed from files.pythonhosted.org.

  DATABRICKS_CONFIG_PROFILE=labs python scripts/probe_omnigent_app_build.py
"""

from __future__ import annotations

import argparse
import io
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "deploy", "omnigent-app")
SKIP = {"__pycache__"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="omnigent-app-buildprobe")
    args = ap.parse_args()

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.apps import App, AppDeployment
    from databricks.sdk.service.workspace import ImportFormat

    w = WorkspaceClient()
    me = w.current_user.me().user_name
    target = f"/Workspace/Users/{me}/apps/{args.name}"

    print(f"Uploading Omnigent App source to {target}")
    for root, dirs, files in os.walk(os.path.normpath(SRC)):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, os.path.normpath(SRC))
            with open(path, "rb") as f:
                content = f.read()
            if rel == "app.yaml":
                # The probe has no bound resources, so the valueFrom entries
                # would fail deploy validation before a single wheel installs.
                content = content.replace(
                    b"  - name: AP_LAKEBASE_ENDPOINT\n    valueFrom: postgres\n"
                    b"  - name: AP_ARTIFACT_VOLUME_PATH\n    valueFrom: artifact_volume\n",
                    b"  - name: AP_LAKEBASE_ENDPOINT\n    value: \"probe-unbound\"\n"
                    b"  - name: AP_ARTIFACT_VOLUME_PATH\n    value: \"/Volumes/probe/unbound/path\"\n",
                )
                if b"valueFrom" in content:
                    print("  WARNING: valueFrom entries still present; deploy may fail early")
            ws_path = f"{target}/{rel}".replace("\\", "/")
            w.workspace.mkdirs(os.path.dirname(ws_path))
            w.workspace.upload(ws_path, io.BytesIO(content), format=ImportFormat.AUTO, overwrite=True)

    print(f"Creating app {args.name}")
    try:
        w.apps.create_and_wait(App(name=args.name))
    except Exception as e:
        if "already exists" not in str(e).lower():
            raise
        print("App exists — redeploying")

    try:
        d = w.apps.deploy_and_wait(
            app_name=args.name,
            app_deployment=AppDeployment(source_code_path=target),
        )
        print(f"Deployment state: {d.status.state if d.status else 'unknown'}")
        if d.status and d.status.message:
            print(f"Message: {d.status.message}")
    except Exception as e:
        print(f"Deploy ended with: {str(e)[:600]}")

    app = w.apps.get(args.name)
    print(f"URL: {app.url}")
    if app.app_status:
        print(f"App status: {app.app_status.state} — {app.app_status.message}")
    if app.compute_status:
        print(f"Compute status: {app.compute_status.state} — {app.compute_status.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
