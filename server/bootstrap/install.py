"""Boot-time installer orchestration with per-agent readiness.

CLIs are installed once into the shared prefix (one copy on disk for all
users). The UI is served immediately; launch buttons enable as each binary
lands. Everything is idempotent so Control Tower redeploys are cheap.
"""

from __future__ import annotations

import logging
import os
import re
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
# Skills are overlaid from ai-dev-kit at boot; the vendored copy in assets/skills
# is the offline fallback (and carries the workflow skills that don't come from
# ai-dev-kit). P1-21: the ref is pinnable (AI_DEV_KIT_REF) so an event runs a
# known, reviewed skills version rather than whatever is on the branch tip at
# boot. Default "main"; events should pin a tag or commit SHA.
AI_DEV_KIT_REPO = os.environ.get(
    "AI_DEV_KIT_REPO", "https://github.com/databricks-solutions/ai-dev-kit.git"
)
AI_DEV_KIT_REF = os.environ.get("AI_DEV_KIT_REF", "main").strip() or "main"
_ASSETS_SKILLS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "assets", "skills")
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


# AppKit scaffolding (`databricks apps init`) needs 0.295+; the Apps runtime
# image ships a much older CLI, so we always install the latest release.
_DATABRICKS_CLI_MIN = (0, 295)


def _databricks_cli_current(path: str) -> bool:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return False
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out.stdout or "")
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= _DATABRICKS_CLI_MIN


def _install_databricks_cli() -> None:
    _set("databricks", "running")
    prefix = config.shared_prefix()
    bin_dir = os.path.join(prefix, "bin")
    target = os.path.join(bin_dir, "databricks")
    if os.path.exists(target) and _databricks_cli_current(target):
        _set("databricks", "complete")
        return
    if os.path.lexists(target):
        os.unlink(target)  # stale or runtime-bundled old version
    try:
        result = subprocess.run(
            ["bash", "-c",
             f"curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh "
             f"| sh -s -- --target {bin_dir}"],
            capture_output=True, text=True, timeout=300, env=_install_env(),
        )
        if result.returncode == 0 and os.path.exists(target):
            _set("databricks", "complete")
            return
        error = (result.stderr or result.stdout)[-500:]
    except (subprocess.TimeoutExpired, OSError) as e:
        error = str(e)
    # Download failed — the runtime's bundled CLI (old but functional) is
    # better than nothing for basic auth/workspace commands.
    existing = shutil.which("databricks")
    if existing:
        os.makedirs(bin_dir, exist_ok=True)
        os.symlink(existing, target)
        _set("databricks", "complete", error=f"latest install failed, using runtime CLI: {error}")
    else:
        _set("databricks", "error", error)


def _install_skills() -> None:
    """Build the shared skills library: vendored base + latest ai-dev-kit.

    1. Copy assets/skills (superpowers/bdd workflow skills + vendored
       databricks skills as the offline fallback).
    2. Shallow-clone ai-dev-kit and overlay databricks-skills/* so attendees
       always run the absolute latest published skills.
    """
    _set("skills", "running")
    prefix = config.shared_prefix()
    skills_dir = os.path.join(prefix, "skills")

    try:
        os.makedirs(skills_dir, exist_ok=True)
        if os.path.isdir(_ASSETS_SKILLS):
            for name in os.listdir(_ASSETS_SKILLS):
                source = os.path.join(_ASSETS_SKILLS, name)
                target = os.path.join(skills_dir, name)
                if os.path.isdir(source) and not os.path.exists(target):
                    shutil.copytree(source, target)
    except OSError as e:
        _set("skills", "error", f"vendored copy failed: {e}")
        return

    clone_dir = os.path.join(prefix, "ai-dev-kit")
    try:
        # Pinned-ref clone (P1-21): --branch accepts a tag or branch; for a
        # full-SHA pin we clone then checkout. Re-clone each boot so the pinned
        # ref is authoritative rather than whatever a stale checkout holds.
        shutil.rmtree(clone_dir, ignore_errors=True)
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", AI_DEV_KIT_REF,
             AI_DEV_KIT_REPO, clone_dir],
            capture_output=True, text=True, timeout=300, env=_install_env(),
        )
        if result.returncode != 0:
            # --branch rejects raw commit SHAs; fall back to clone + checkout.
            shutil.rmtree(clone_dir, ignore_errors=True)
            subprocess.run(
                ["git", "clone", AI_DEV_KIT_REPO, clone_dir],
                capture_output=True, text=True, timeout=300, env=_install_env(),
            )
            result = subprocess.run(
                ["git", "-C", clone_dir, "checkout", AI_DEV_KIT_REF],
                capture_output=True, text=True, timeout=120, env=_install_env(),
            )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout)[-300:])

        upstream = os.path.join(clone_dir, "databricks-skills")
        updated = 0
        if os.path.isdir(upstream):
            for name in os.listdir(upstream):
                source = os.path.join(upstream, name)
                if not os.path.isdir(source):
                    continue
                target = os.path.join(skills_dir, name)
                shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(source, target)
                updated += 1
        logger.info("skills: %d refreshed from ai-dev-kit@%s", updated, AI_DEV_KIT_REF)
        _set("skills", "complete")
    except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
        # Network/git failure — the vendored copy still serves.
        logger.warning("ai-dev-kit fetch failed (%s) — using vendored skills", e)
        _set("skills", "complete", error=f"vendored fallback: {e}")


def run_in_background() -> None:
    for step in ("node", "claude", "codex", "databricks", "skills"):
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
            _install_skills()
            return
        with ThreadPoolExecutor(max_workers=4) as pool:
            pool.submit(_install_claude)
            pool.submit(_install_codex)
            pool.submit(_install_databricks_cli)
            pool.submit(_install_skills)

    threading.Thread(target=orchestrate, daemon=True, name="bootstrap").start()
