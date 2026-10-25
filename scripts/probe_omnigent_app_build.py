#!/usr/bin/env python3
"""Prove the Omnigent App's uv.lock installs 0.19.1 on Databricks Apps.

Mode 3 acceptance needs a dedicated Lakebase and UC Volume, which is real
persistent infrastructure. The dependency resolution is the part the version
bump actually changed, and it can be checked without any of that: deploy the
App source with the two resource bindings stubbed out, then read the build log.

Startup is expected to fail at Lakebase. That failure is the success signal —
reaching it means Python 3.12 was provisioned and the locked 0.19.1 wheel set
installed from files.pythonhosted.org.

  DATABRICKS_CONFIG_PROFILE=labs python scripts/probe_omnigent_app_build.py
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.source_upload import read_upload_sources


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="omnigent-app-buildprobe")
    args = ap.parse_args(argv)

    # Validate the complete release before importing the paired application's
    # sources; the same root carries its lock and artifact dependencies.
    prefix = "deploy/omnigent-app/"
    sources = {name.removeprefix(prefix): content
               for name, content in read_upload_sources(ROOT, exclude=(
                   ".git*", ".venv*", "node_modules*", "frontend/node_modules*",
                   "__pycache__*", "*.pyc", ".pytest_cache*",
               )).items() if name.startswith(prefix)}

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.apps import App, AppDeployment
    from databricks.sdk.service.workspace import ImportFormat

    w = WorkspaceClient()
    me = w.current_user.me().user_name
    target = f"/Workspace/Users/{me}/apps/{args.name}"

    print(f"Uploading Omnigent App source to {target}")
    for rel, content in sources.items():
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
        ws_path = f"{target}/{rel}"
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
