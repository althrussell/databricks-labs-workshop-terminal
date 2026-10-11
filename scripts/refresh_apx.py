#!/usr/bin/env python3
"""Refresh/check official APX skill bytes at the workshop's reviewed commit."""
import argparse
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.bootstrap import apx
from server.bootstrap.artifacts import directory_checksum, load_manifest

FILES = ("SKILL.md", "backend-patterns.md", "frontend-patterns.md")


def fetch_skill(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base = f"https://raw.githubusercontent.com/databricks-solutions/apx/{apx.SOURCE_COMMIT}/skills/apx/"
    for name in FILES:
        with urllib.request.urlopen(base + name, timeout=60) as response:
            (destination / name).write_bytes(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    entry = load_manifest("")["artifacts"]["apx_wheel_linux_x64"]
    if entry["skill_commit"] != apx.SOURCE_COMMIT:
        raise SystemExit("APX skill commit differs from the reviewed CLI release")
    with tempfile.TemporaryDirectory(prefix="wt-apx-skills-") as temporary:
        source = Path(temporary) / "apx"
        fetch_skill(source)
        if directory_checksum(source) != entry["skill_content_sha256"]:
            raise SystemExit("APX upstream skill differs from the reviewed digest")
        target = ROOT / "assets/skills/apx"
        if args.write:
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(source, target)
        elif directory_checksum(target) != directory_checksum(source):
            raise SystemExit("APX fallback differs; run refresh_apx.py --write")
    print(f"APX {apx.VERSION}: official skill matches {apx.SOURCE_COMMIT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
