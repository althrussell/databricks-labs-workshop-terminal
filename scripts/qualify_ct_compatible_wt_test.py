#!/usr/bin/env python3
"""Qualify or clean an exact standalone CT-compatible deployment receipt."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.generated_apps.ct_lifecycle import qualify, cleanup
from evals.generated_apps.ct_deployment import submit_deployment, update_observer, update_mirror
from evals.generated_apps.report import write_evidence
from scripts.deploy_generated_app_test import workspace_client


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=["qualify", "cleanup", "resume-deployment", "update-observer", "add-mirror"])
    p.add_argument("--receipt", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--mirror-source")
    p.add_argument("--mirror-strict", action="store_true")
    args = p.parse_args(argv)
    output = Path(args.output)
    if output.exists() or output.is_symlink() or output.resolve() == Path(args.receipt).resolve():
        p.error("A fresh distinct result path is required; preserve the deployment receipt")
    receipt = json.loads(Path(args.receipt).read_text())
    try:
        client = workspace_client(receipt["plan"]["profile"], http_timeout_seconds=180)
        if args.operation == "add-mirror":
            if receipt.get("status") not in {"deployment_submitted", "failed"} or not args.mirror_source:
                raise ValueError("Mirror addition requires a recorded deployment and exact source volume")
            result = update_mirror(client, receipt, output,
                args.mirror_source, strict=args.mirror_strict)
            print(json.dumps({"output": str(output), "status": result["status"]}))
            return 0
        if args.operation == "update-observer":
            if receipt.get("status") != "deployment_submitted":
                raise ValueError("Observer update requires the exact recorded deployment")
            result = update_observer(client, receipt, output)
            print(json.dumps({"output": str(output), "status": result["status"]}))
            return 0
        if args.operation == "resume-deployment":
            if (receipt.get("status") != "failed" or "deployment" in receipt
                    or receipt.get("staged_package", {}).get("state") != "independently_verified"
                    or receipt.get("work_sync", {}).get("state") != "independently_verified"):
                raise ValueError("Resume is limited to the completed provisioning stage before first deployment")
            receipt["resume_provenance"] = {"previous_receipt": str(Path(args.receipt).resolve()),
                "previous_receipt_sha256": hashlib.sha256(Path(args.receipt).read_bytes()).hexdigest(),
                "previous_error_type": receipt.pop("error_type", None), "bounds_preserved": True}
            result = submit_deployment(client, receipt, output)
            print(json.dumps({"output": str(output), "status": result["status"]}))
            return 0
        operation = qualify if args.operation == "qualify" else cleanup
        result = operation(client, args.receipt, output)
    except Exception as error:
        result = json.loads(output.read_text()) if output.exists() else {"accepted": False, "control_tower_requests": 0}
        result.update(status="failed", error_type=type(error).__name__)
        write_evidence(output, result)
        print(json.dumps({"output": str(output), "status": "failed", "error_type": type(error).__name__}))
        return 2
    print(json.dumps({"output": str(output), "status": result["status"], "accepted": False, "control_tower_requests": 0}))
    return 0 if result["status"] in {"qualified", "cleanup_verified"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
