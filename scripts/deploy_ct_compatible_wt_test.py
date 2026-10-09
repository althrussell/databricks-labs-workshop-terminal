#!/usr/bin/env python3
"""Plan/deploy/qualify/clean a fresh CT-compatible package test, without CT writes."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.generated_apps.ct_deployment import plan_package, deploy_package
from evals.generated_apps.report import write_evidence
from scripts.deploy_generated_app_test import workspace_client


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--spec", required=True)
    p.add_argument("--release-manifest", required=True)
    p.add_argument("--artifact", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)
    output = Path(args.output)
    if output.exists() or output.is_symlink() or output.resolve() in {Path(x).resolve() for x in (args.spec, args.release_manifest, args.artifact)}:
        p.error("A fresh distinct output receipt is required")
    raw = Path(args.release_manifest).read_bytes()
    artifact = Path(args.artifact).read_bytes()
    plan = plan_package(json.loads(Path(args.spec).read_text()), raw, artifact)
    write_evidence(output, {"plan": plan, "executed": False, "control_tower_requests": 0})
    try:
        if args.execute:
            result = deploy_package(workspace_client(plan["profile"], http_timeout_seconds=180), plan, raw, artifact, output)
            print(json.dumps({"output": str(output), "status": result["status"], "app_name": plan["names"]["app_name"], "control_tower_requests": 0}))
        else:
            print(json.dumps({"output": str(output), "executed": False, "control_tower_requests": 0}))
        return 0
    except Exception as error:
        result = json.loads(output.read_text())
        result.update(status="failed", error_type=type(error).__name__)
        write_evidence(output, result)
        # All actual SDK/provider detail stays in the private diagnostic, never
        # in a shared receipt. This prints no authentication material.
        print(json.dumps({"output": str(output), "status": "failed", "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
