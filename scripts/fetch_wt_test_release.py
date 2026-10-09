#!/usr/bin/env python3
"""Copy a checksum-pinned WT release from existing CT storage, with no CT writes."""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.generated_apps.ct_deployment import verify_package, sha256
from evals.generated_apps.report import write_evidence
from scripts.deploy_generated_app_test import workspace_client


def fetch(client, volume_path, pin, output):
    if (not re.fullmatch(r"/Volumes/[A-Za-z0-9_]+/[A-Za-z0-9_]+/[A-Za-z0-9_]+", volume_path)
            or not re.fullmatch(r"[a-f0-9]{64}", pin)):
        raise ValueError("Exact release volume and reviewed manifest SHA256 required")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Fresh release output directory required")
    output.mkdir(mode=0o700, parents=True)
    with client.files.download(volume_path + "/index.json").contents as handle:
        index_raw = handle.read(1024 * 1024 + 1)
    if len(index_raw) > 1024 * 1024:
        raise ValueError("Release index size limit")
    matches = []
    def search(value):
        if isinstance(value, dict):
            if value.get("manifest_sha256") == pin and value.get("artifact_sha256"):
                matches.append(value)
            for nested in value.values(): search(nested)
        elif isinstance(value, list):
            for nested in value: search(nested)
    search(json.loads(index_raw))
    if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{64}", matches[0]["artifact_sha256"]):
        raise ValueError("Release index did not bind the exact reviewed pin uniquely")
    path = volume_path + "/" + matches[0]["artifact_sha256"]
    with client.files.download(path + "/release-manifest.json").contents as handle:
        raw = handle.read(1024 * 1024 + 1)
    if sha256(raw) != pin:
        raise ValueError("Staged manifest differs from the reviewed pin")
    with client.files.download(path + "/workshop-terminal.pex").contents as handle:
        artifact = handle.read(200 * 1024 * 1024 + 1)
    manifest, _ = verify_package(raw, artifact, pin)
    (output / "release-manifest.json").write_bytes(raw)
    (output / "workshop-terminal.pex").write_bytes(artifact)
    result = {"schema_version": 1, "operation": "read_only_copy_pinned_wt_release", "control_tower_requests": 0,
        "control_tower_mutations": 0, "workspace_host": client.config.host.rstrip("/"), "source_path": path,
        "manifest_sha256": pin, "package_manifest": manifest, "status": "verified"}
    write_evidence(output / "fetch-receipt.json", result)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", required=True)
    p.add_argument("--volume", required=True)
    p.add_argument("--manifest-sha256", required=True)
    p.add_argument("--output-dir", required=True)
    args = p.parse_args(argv)
    result = fetch(workspace_client(args.profile), args.volume, args.manifest_sha256, args.output_dir)
    print(json.dumps({"output_dir": args.output_dir, "status": result["status"], "control_tower_mutations": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
