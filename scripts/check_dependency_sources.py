#!/usr/bin/env python3
"""Bootstrap the source guard before resolving any project dependencies.

Only the guard's existing YAML parser comes from PyPI. Neither the checkout's
project/lock/config files nor inherited package-source settings select its
bootstrap dependencies.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PYPI = "https://pypi.org/simple"
YAML_REQUIREMENT = "PyYAML==6.0.3"


def main() -> int:
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("UV_", "PIP_", "NPM_CONFIG_", "BUN_CONFIG_"))
        and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    # An empty cwd adds protection against future changes to uv discovery.
    with tempfile.TemporaryDirectory(prefix="wt-source-preflight-") as directory:
        return subprocess.run([
            "uv", "run", "--no-project", "--no-config", "--isolated", "--no-env-file",
            "--no-python-downloads", "--no-managed-python", "--no-build",
            "--keyring-provider", "disabled", "--python", sys.executable,
            "--default-index", PUBLIC_PYPI, "--with", YAML_REQUIREMENT,
            "python", "-I", str(ROOT / "dependency_sources.py"), "--root", str(ROOT),
        ], cwd=directory, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
