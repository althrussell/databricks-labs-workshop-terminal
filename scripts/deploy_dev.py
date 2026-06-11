#!/usr/bin/env python3
"""Deploy this repo to a dev workspace for smoke testing.

Mirrors Control Tower's deploy mechanics (workspace import + apps.deploy)
and sets user_api_scopes=["all-apis"] so per-user PAT minting works.

  export DATABRICKS_CONFIG_PROFILE=my-dev-workspace   # or DATABRICKS_HOST/TOKEN
  python scripts/deploy_dev.py [--name workshop-terminal-dev]

Requires: databricks-sdk (pip install databricks-sdk).
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import os
import sys

EXCLUDE = ["node_modules*", ".venv*", "__pycache__*", ".git*", "frontend/node_modules*", "*.pyc"]
REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="workshop-terminal-dev")
    args = parser.parse_args()

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.apps import App
    from databricks.sdk.service.workspace import ImportFormat

    w = WorkspaceClient()
    me = w.current_user.me().user_name
    target = f"/Workspace/Users/{me}/apps/{args.name}"
    print(f"Importing source to {target}")

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
            path = os.path.join(root, name)
            ws_path = f"{target}/{rel}".replace("\\", "/")
            w.workspace.mkdirs(os.path.dirname(ws_path))
            with open(path, "rb") as f:
                w.workspace.upload(ws_path, io.BytesIO(f.read()), format=ImportFormat.AUTO, overwrite=True)

    print(f"Creating app {args.name} (user_api_scopes=['all-apis'])")
    try:
        w.apps.create_and_wait(App(name=args.name, user_api_scopes=["all-apis"]))
    except Exception as e:
        if "already exists" not in str(e).lower():
            raise
        print("App exists — redeploying")

    deployment = w.apps.deploy_and_wait(app_name=args.name, source_code_path=target)
    app = w.apps.get(args.name)
    print(f"Deployed: {deployment.status.state if deployment.status else 'unknown'}")
    print(f"URL: {app.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
