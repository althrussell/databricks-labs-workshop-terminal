#!/usr/bin/env python3
"""Validate reviewed Codex launcher/native tarballs before CT staging."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.bootstrap.codex_artifacts import (
    CodexArtifactError,
    validate_codex_tarballs,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--native", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--launcher-sha256", required=True)
    parser.add_argument("--native-package-sha256", required=True)
    parser.add_argument("--native-executable-sha256", required=True)
    args = parser.parse_args()
    try:
        proof = validate_codex_tarballs(
            args.launcher, args.native, args.version
        )
        expected = {
            "launcher_package_sha256": args.launcher_sha256,
            "native_package_sha256": args.native_package_sha256,
            "native_executable_sha256": args.native_executable_sha256,
        }
        verified = all(
            hmac.compare_digest(str(proof[key]), value)
            for key, value in expected.items()
        )
    except (CodexArtifactError, OSError):
        verified = False
        proof = {}
    result = {
        "status": "verified" if verified else "rejected",
        "version": args.version,
        "alias": proof.get("alias"),
        "alias_target": proof.get("alias_target"),
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
