#!/usr/bin/env python3
"""Refresh/check the fallback through upstream's real supported installer.

Uses the reviewed launcher/bundle and the same engine release for this laptop's
platform. Proxy selection is local tooling only; shipped sources remain public.
No real harness home or project is modified. Native binaries are never vendored.
"""
from pathlib import Path
import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.bootstrap import impeccable  # noqa: E402
from server.bootstrap.artifacts import directory_checksum, load_manifest  # noqa: E402


def refresh(write: bool) -> int:
    entries = load_manifest("")["artifacts"]
    host = "darwin" if platform.system() == "Darwin" else "linux"
    arch = "arm64" if platform.machine() in {"arm64", "aarch64"} else "x64"
    host_platform = f"{host}-{arch}"
    npm = subprocess.run(["npm", "config", "get", "registry"], check=True,
                         capture_output=True, text=True).stdout.strip().rstrip("/")
    with tempfile.TemporaryDirectory(prefix="wt-impeccable-") as temporary:
        root = Path(temporary)
        (root / "bin").mkdir()
        node = shutil.which("node")
        if not node:
            raise SystemExit("Node is required to run the supported npm launcher")
        (root / "bin/node").symlink_to(node)

        def fetch(name):
            entry = dict(entries[name])
            source = entry["source"]
            if name == "impeccable_npm_launcher":
                source = source.replace("https://registry.npmjs.org", npm, 1)
            if name == "impeccable_engine_linux_x64" and host_platform != "linux-x64":
                source = source.replace("impeccable-linux-x64", f"impeccable-{host_platform}")
                with urllib.request.urlopen(source + ".sha256", timeout=60) as response:
                    entry["sha256"] = response.read().decode().split()[0]
            path = root / name
            with urllib.request.urlopen(source, timeout=120) as response:
                path.write_bytes(response.read())
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise RuntimeError(f"{name} checksum differs from reviewed release")
            return str(path), entry

        env = {key: value for key, value in os.environ.items() if not key.startswith("DATABRICKS_")}
        env["PATH"] = str(root / "bin") + os.pathsep + env.get("PATH", "")
        target = root / "skills"
        target.mkdir()
        impeccable.install_skill(str(target), str(root), fetch, env, platform=host_platform)
        actual = target / "impeccable"
        probe = subprocess.run([str(actual / "scripts/impeccable"), "engine-probe"],
                               env=env, cwd=root, capture_output=True, text=True, check=True)
        if probe.stdout.strip() != f"impeccable-engine {impeccable.ENGINE_VERSION}":
            raise RuntimeError("Installed skill cannot resolve its reviewed native engine")
        vendored = ROOT / "assets/skills/impeccable"
        if write:
            shutil.rmtree(vendored, ignore_errors=True)
            shutil.copytree(actual, vendored)
        elif directory_checksum(actual) != directory_checksum(vendored):
            raise SystemExit("Impeccable fallback differs from supported install; run --write")
    print(f"Impeccable {impeccable.SKILL_VERSION}: supported installer and fallback agree ({host_platform})")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    raise SystemExit(refresh(args.write))
