#!/usr/bin/env python3
"""Run the static audit and configurable release gate."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run_json(command: list[str]) -> tuple[int, dict]:
    process = subprocess.run(command, text=True, capture_output=True, check=False)
    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(
            f"tool did not return JSON: {' '.join(command)}\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}\n{error}"
        )
    return process.returncode, data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    script_dir = Path(__file__).resolve().parent

    _, result = run_json(
        [sys.executable, str(script_dir / "audit_project.py"), "--root", str(root)]
    )

    failures = []
    for severity, maximum in config.get("max_findings", {}).items():
        actual = result["summary"].get(severity, 0)
        if actual > maximum:
            failures.append(f"{severity}: {actual} > {maximum}")

    codes = {finding["code"] for finding in result["findings"]}
    for code in config.get("blocked_codes", []):
        if code in codes:
            failures.append(f"blocked finding: {code}")

    contrast_result = None
    if config.get("check_contrast", True):
        system = root / ".design-studio" / "design-system.json"
        if not system.is_file():
            failures.append("design-system.json missing for contrast gate")
        else:
            contrast_code, contrast_result = run_json(
                [
                    sys.executable,
                    str(script_dir / "check_contrast.py"),
                    "--system",
                    str(system),
                    "--minimum",
                    str(config.get("minimum_contrast", 4.5)),
                ]
            )
            if contrast_code:
                failures.extend(
                    f"contrast: {item}" for item in contrast_result.get("failures", [])
                )

    output = {
        "ok": not failures,
        "failures": failures,
        "summary": result["summary"],
        "contrast": contrast_result,
    }
    print(json.dumps(output, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
