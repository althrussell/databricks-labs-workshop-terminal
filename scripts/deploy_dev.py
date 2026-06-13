#!/usr/bin/env python3
"""Deploy this repo to a dev workspace for smoke testing.

Mirrors Control Tower's deploy mechanics: workspace import + apps.deploy,
with a vended credential injected as WORKSHOP_PAT in the uploaded app.yaml
(here: a 12h PAT minted as the deploying user; Control Tower vends its own).

  export DATABRICKS_CONFIG_PROFILE=my-dev-workspace   # or DATABRICKS_HOST/TOKEN
  python scripts/deploy_dev.py [--name workshop-terminal-dev] [--no-pat]

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
    parser.add_argument("--no-pat", action="store_true",
                        help="skip minting/injecting the WORKSHOP_PAT dev credential")
    parser.add_argument("--omnigent-wheels", default="",
                        help="dir of omnigent wheels to stage with the app (pre-PyPI: "
                             "uploads *.whl and points OMNIGENT_PIP_SPEC/UV_FIND_LINKS at them)")
    args = parser.parse_args()

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.apps import App, AppDeployment
    from databricks.sdk.service.workspace import ImportFormat

    w = WorkspaceClient()
    me = w.current_user.me().user_name
    target = f"/Workspace/Users/{me}/apps/{args.name}"

    workshop_pat = ""
    if not args.no_pat:
        print("Minting 12h dev credential (WORKSHOP_PAT)")
        workshop_pat = w.tokens.create(
            comment=f"{args.name} vended credential", lifetime_seconds=43200
        ).token_value

    # Air-gap / pre-release escape hatch: omnigent is GA on PyPI and installs
    # from there by default, so this is only needed to test a local wheel build
    # (or where PyPI is unreachable). Stage wheels alongside the source; the
    # Apps runtime mounts it at /app/python/source_code, so that's where
    # UV_FIND_LINKS must point.
    omnigent_spec = ""
    wheels: list[str] = []
    if args.omnigent_wheels:
        wheels = sorted(
            f for f in os.listdir(args.omnigent_wheels) if f.endswith(".whl")
        )
        main_wheel = next((f for f in wheels if f.startswith("omnigent-")), None)
        if not main_wheel:
            print(f"no omnigent-*.whl in {args.omnigent_wheels}", file=sys.stderr)
            return 1
        omnigent_spec = f"omnigent=={main_wheel.split('-')[1]}"

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
            with open(path, "rb") as f:
                content = f.read()
            if rel == "app.yaml" and workshop_pat:
                # Same mechanism Control Tower uses: vend the credential by
                # editing the deployed app.yaml in place (never the git copy).
                content = content.replace(
                    b"- name: WORKSHOP_PAT\n    value: \"\"",
                    f"- name: WORKSHOP_PAT\n    value: \"{workshop_pat}\"".encode(),
                )
            if rel == "app.yaml" and omnigent_spec:
                # Point the (already-on) omnigent install at the staged wheels:
                # a pinned spec + UV_FIND_LINKS make uv resolve from the volume
                # instead of PyPI. OMNIGENT_ENABLED already ships "true".
                content = content.replace(
                    b"- name: OMNIGENT_PIP_SPEC\n    value: \"\"",
                    f"- name: OMNIGENT_PIP_SPEC\n    value: \"{omnigent_spec}\"".encode(),
                ).replace(
                    b"- name: UV_FIND_LINKS\n    value: \"\"",
                    b"- name: UV_FIND_LINKS\n    value: \"/app/python/source_code/wheels\"",
                )
            ws_path = f"{target}/{rel}".replace("\\", "/")
            w.workspace.mkdirs(os.path.dirname(ws_path))
            w.workspace.upload(ws_path, io.BytesIO(content), format=ImportFormat.AUTO, overwrite=True)

    for name in wheels:
        print(f"Staging wheel {name}")
        with open(os.path.join(args.omnigent_wheels, name), "rb") as f:
            w.workspace.mkdirs(f"{target}/wheels")
            w.workspace.upload(
                f"{target}/wheels/{name}", io.BytesIO(f.read()),
                format=ImportFormat.AUTO, overwrite=True,
            )

    print(f"Creating app {args.name}")
    try:
        w.apps.create_and_wait(App(name=args.name))
    except Exception as e:
        if "already exists" not in str(e).lower():
            raise
        print("App exists — redeploying")

    deployment = w.apps.deploy_and_wait(
        app_name=args.name,
        app_deployment=AppDeployment(source_code_path=target),
    )
    app = w.apps.get(args.name)
    print(f"Deployed: {deployment.status.state if deployment.status else 'unknown'}")
    print(f"URL: {app.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
