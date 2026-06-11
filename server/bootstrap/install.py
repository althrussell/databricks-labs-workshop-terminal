"""Boot-time installer orchestration with per-agent readiness.

CLIs are installed once into the shared prefix (one copy on disk for all
users). The UI is served immediately; launch buttons enable as each binary
lands. Everything is idempotent so Control Tower redeploys are cheap.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .. import config

logger = logging.getLogger(__name__)

# Pinned versions — bump deliberately per release.
CODEX_VERSION = os.environ.get("CODEX_CLI_VERSION", "0.46.0")
CLAUDE_INSTALLER_URL = os.environ.get(
    "CLAUDE_INSTALLER_URL", "https://claude.ai/install.sh"
)

_state_lock = threading.Lock()
_state: dict[str, dict] = {}


def _set(step: str, status: str, error: str | None = None) -> None:
    with _state_lock:
        _state[step] = {"status": status, "error": error, "at": time.time()}


def status() -> dict:
    with _state_lock:
        steps = dict(_state)
    ready = {
        "bash": True,
        "claude": steps.get("claude", {}).get("status") == "complete",
        "codex": steps.get("codex", {}).get("status") == "complete",
    }
    installing = any(s.get("status") in (None, "pending", "running") for s in steps.values())
    return {"steps": steps, "ready": ready, "installing": installing}


def _install_env() -> dict:
    env = os.environ.copy()
    prefix = config.shared_prefix()
    env["PATH"] = f"{prefix}/bin:{env.get('PATH', '')}"
    # Installers must not see app SP credentials.
    env.pop("DATABRICKS_CLIENT_ID", None)
    env.pop("DATABRICKS_CLIENT_SECRET", None)
    return env


def _install_node() -> None:
    _set("node", "running")
    script = os.path.join(os.path.dirname(__file__), "install_node.sh")
    result = subprocess.run(
        ["bash", script, config.shared_prefix()],
        capture_output=True, text=True, timeout=300, env=_install_env(),
    )
    if result.returncode == 0:
        _set("node", "complete")
    else:
        _set("node", "error", (result.stderr or result.stdout)[-500:])
        raise RuntimeError("node install failed")


def _install_claude() -> None:
    _set("claude", "running")
    prefix = config.shared_prefix()
    claude_bin = os.path.join(prefix, "bin", "claude")
    if os.path.exists(claude_bin):
        _set("claude", "complete")
        return
    env = _install_env()
    # The installer targets $HOME/.local — point HOME at the shared prefix's
    # parent so the binary lands in the shared tree, then link it into bin/.
    staging = os.path.join(prefix, "claude-home")
    os.makedirs(staging, exist_ok=True)
    env["HOME"] = staging
    try:
        curl = subprocess.Popen(
            ["curl", "-fsSL", CLAUDE_INSTALLER_URL], stdout=subprocess.PIPE, env=env,
        )
        result = subprocess.run(
            ["bash"], stdin=curl.stdout, capture_output=True, text=True,
            timeout=300, env=env,
        )
        curl.stdout.close()
        curl.wait()
        installed = os.path.join(staging, ".local", "bin", "claude")
        if result.returncode == 0 and os.path.exists(installed):
            os.makedirs(os.path.dirname(claude_bin), exist_ok=True)
            if os.path.lexists(claude_bin):
                os.unlink(claude_bin)
            os.symlink(installed, claude_bin)
            _set("claude", "complete")
        else:
            _set("claude", "error", (result.stderr or result.stdout)[-500:])
    except (subprocess.TimeoutExpired, OSError) as e:
        _set("claude", "error", str(e))


def _install_codex() -> None:
    _set("codex", "running")
    prefix = config.shared_prefix()
    codex_bin = os.path.join(prefix, "bin", "codex")
    if os.path.exists(codex_bin):
        _set("codex", "complete")
        return
    env = _install_env()
    npm = os.path.join(prefix, "bin", "npm")
    if not os.path.exists(npm):
        npm = "npm"
    for attempt in range(1, 4):
        try:
            result = subprocess.run(
                [npm, "install", "-g", f"--prefix={prefix}", f"@openai/codex@{CODEX_VERSION}"],
                capture_output=True, text=True, timeout=300, env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            result = None
            error = str(e)
        else:
            error = (result.stderr or result.stdout)[-500:]
        if result and result.returncode == 0 and os.path.exists(codex_bin):
            _set("codex", "complete")
            return
        logger.warning("codex install attempt %d/3 failed", attempt)
        time.sleep(5)
    _set("codex", "error", error)


def _install_databricks_cli() -> None:
    _set("databricks", "running")
    prefix = config.shared_prefix()
    target = os.path.join(prefix, "bin", "databricks")
    if os.path.exists(target):
        _set("databricks", "complete")
        return
    existing = shutil.which("databricks")
    if existing:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.symlink(existing, target)
        _set("databricks", "complete")
        return
    try:
        result = subprocess.run(
            ["bash", "-c",
             f"curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh "
             f"| sh -s -- --target {os.path.join(prefix, 'bin')}"],
            capture_output=True, text=True, timeout=300, env=_install_env(),
        )
        if result.returncode == 0 and os.path.exists(target):
            _set("databricks", "complete")
        else:
            _set("databricks", "error", (result.stderr or result.stdout)[-500:])
    except (subprocess.TimeoutExpired, OSError) as e:
        _set("databricks", "error", str(e))


def run_in_background() -> None:
    for step in ("node", "claude", "codex", "databricks"):
        _set(step, "pending")

    def orchestrate():
        os.makedirs(os.path.join(config.shared_prefix(), "bin"), exist_ok=True)
        try:
            _install_node()
        except RuntimeError:
            # Without node, codex can't install; claude's installer is
            # self-contained, so still try it.
            _set("codex", "error", "skipped: node install failed")
            _install_claude()
            _install_databricks_cli()
            return
        with ThreadPoolExecutor(max_workers=3) as pool:
            pool.submit(_install_claude)
            pool.submit(_install_codex)
            pool.submit(_install_databricks_cli)

    threading.Thread(target=orchestrate, daemon=True, name="bootstrap").start()
