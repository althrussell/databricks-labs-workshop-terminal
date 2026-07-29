"""Per-user workshop content: instructions, subagents, skills, git, MCP.

Runs once per user (on their first session) after their HOME is bootstrapped:

- ~/.claude/CLAUDE.md       — workshop instructions (+ lab coach when enabled)
- ~/.codex/AGENTS.md        — the same instructions adapted for Codex
- ~/.local/bin/workshop-init-project — project bootstrap that commits the AppKit
                              mandate as project-level CLAUDE.md + AGENTS.md (the
                              only channel Omnigent's worktree-bound Codex worker
                              reads), backed by ~/.config/workshop/project-memory.md
- ~/.claude/agents/         — TDD subagent definitions (prd-writer, implementer, ...)
- ~/.claude/skills          — per-skill symlinks into the shared skills library
                              (reviewed databricks-agent-skills, fetched at boot);
                              ~/.codex/skills gets the same set, which is where
                              Codex and Omnigent's Codex worker discover them
- ~/.claude.json            — onboarding skipped + MCP servers (DeepWiki, Exa)
- ~/.gitconfig + hooks      — attendee git identity and a post-commit hook that
                              syncs ~/projects repos to the attendee's
                              Workspace home (work survives teardown)
"""

from __future__ import annotations

import json
import hmac
import logging
import os
import re
import secrets
import shutil

from . import config
from .users import User

logger = logging.getLogger(__name__)

_ASSETS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets"))
_COACH_MARKER = "<!-- workshop-lab-coach -->"
_CALLBACK_CAPABILITY = os.path.join(".config", "workshop", "callback-capability")

DEFAULT_DEEPWIKI_MCP = "https://mcp.deepwiki.com/mcp"
DEFAULT_EXA_MCP = "https://mcp.exa.ai/mcp"

_provisioned: set[str] = set()


def provision(user: User) -> None:
    """Idempotent per-user setup; never fatal to a session launch."""
    if user.email in _provisioned:
        return
    for step in (
        _write_callback_capability,
        _write_instructions,
        _install_project_helper,
        _install_cli_helpers,
        _install_subagents,
        _link_skills,
        _write_claude_json,
        _write_git_setup,
    ):
        try:
            step(user)
        except Exception as e:  # noqa: BLE001 — content must never block a terminal
            logger.warning("user content step %s failed for %s: %s",
                           step.__name__, user.email, e)
    _provisioned.add(user.email)


def callback_capability_path(user: User) -> str:
    return os.path.join(user.home, _CALLBACK_CAPABILITY)


def _write_callback_capability(user: User) -> None:
    """Create the attendee-bound loopback callback capability once.

    The helper reads this file directly; the value is deliberately absent from
    the PTY environment and never logged.
    """
    path = callback_capability_path(user)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
        return
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(32))
        f.write("\n")
    os.chmod(path, 0o600)


def verify_callback_capability(email: str, supplied: str) -> bool:
    """Constant-time verification against an already-provisioned attendee."""
    from .users import user_manager

    user = user_manager.peek(email)
    if user is None or not supplied:
        return False
    try:
        with open(callback_capability_path(user)) as f:
            expected = f.read().strip()
    except OSError:
        return False
    return bool(expected) and hmac.compare_digest(expected, supplied)


# -- instructions (CLAUDE.md / AGENTS.md + lab coach) --

def _base_instructions() -> str:
    with open(os.path.join(_ASSETS, "instructions", "CLAUDE.md")) as f:
        text = f.read()
    if config.lab_coach_enabled():
        with open(os.path.join(_ASSETS, "instructions", "lab_coach.md")) as f:
            coach = f.read()
        if _COACH_MARKER not in text:
            text = f"{text}\n\n{coach}"
    return text


def _write_instructions(user: User) -> None:
    text = _base_instructions()

    claude_md = os.path.join(user.home, ".claude", "CLAUDE.md")
    os.makedirs(os.path.dirname(claude_md), exist_ok=True)
    with open(claude_md, "w") as f:
        f.write(text)

    # Codex reads AGENTS.md; same content, only the top header is swapped.
    agents_md = os.path.join(user.home, ".codex", "AGENTS.md")
    os.makedirs(os.path.dirname(agents_md), exist_ok=True)
    adapted = re.sub(r"^#\s+.*$", "# Codex Agent Instructions", text, count=1, flags=re.MULTILINE)
    with open(agents_md, "w") as f:
        f.write(adapted)


# -- project memory (project-level CLAUDE.md / AGENTS.md via a bootstrap helper) --

def _install_project_helper(user: User) -> None:
    """Install the project bootstrap helper + its project-memory template.

    Home-level ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md reach Claude, Codex,
    and Omnigent's polly brain — but polly's Codex sub-agent runs in an isolated
    CODEX_HOME inside a git worktree, so it never sees the global AGENTS.md. The
    only channel that survives that is a *committed, project-level* AGENTS.md,
    which `workshop-init-project` writes (and commits) into every new project so
    it propagates into worktrees. Defense-in-depth for the other agents too.
    """
    template_src = os.path.join(_ASSETS, "instructions", "project_memory.md")
    template_dst = os.path.join(user.home, ".config", "workshop", "project-memory.md")
    os.makedirs(os.path.dirname(template_dst), exist_ok=True)
    shutil.copy2(template_src, template_dst)

    helper_src = os.path.join(_ASSETS, "bin", "workshop-init-project")
    local_bin = os.path.join(user.home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)
    helper_dst = os.path.join(local_bin, "workshop-init-project")
    shutil.copy2(helper_src, helper_dst)
    os.chmod(helper_dst, 0o755)


def _install_cli_helpers(user: User) -> None:
    """Install the dual-identity CLI helpers.

    - ``databricks-me``: run the databricks CLI as the attendee (``[me]`` /OBO
      profile) with reactive token self-heal, so the agent can read the data the
      attendee is actually governed by.
    - ``workshop-grant-me``: trigger an immediate entitlement reconcile so a
      just-built non-UC resource is usable by the labuser without waiting for
      the next sweep.
    """
    local_bin = os.path.join(user.home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)
    for name in ("databricks-me", "workshop-grant-me"):
        src = os.path.join(_ASSETS, "bin", name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(local_bin, name)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)


# -- subagents --

def _install_subagents(user: User) -> None:
    source = os.path.join(_ASSETS, "agents")
    target = os.path.join(user.home, ".claude", "agents")
    os.makedirs(target, exist_ok=True)
    if not os.path.isdir(source):
        return
    for name in os.listdir(source):
        if name.endswith(".md"):
            shutil.copy2(os.path.join(source, name), os.path.join(target, name))


# -- skills (shared library, fetched latest at boot) --

# Each harness reads its own directory, and `databricks aitools install` is the
# reference for which: it keeps one canonical copy of the skills and symlinks
# each skill into every detected agent's real skills directory. Claude Code
# loads ~/.claude/skills; Codex CLI loads ~/.codex/skills, which is also the
# CODEX_HOME configure_codex() writes, so Omnigent's Codex worker inherits it.
HARNESS_SKILL_DIRS = {
    "claude": os.path.join(".claude", "skills"),
    "codex": os.path.join(".codex", "skills"),
}


def shared_skills_dir() -> str:
    return os.path.join(config.shared_prefix(), "skills")


def _link_skills(user: User) -> None:
    source = shared_skills_dir()
    if not os.path.isdir(source):
        source = os.path.join(_ASSETS, "skills")  # boot fetch not done yet — vendored
    names = sorted(
        name
        for name in os.listdir(source)
        if os.path.isdir(os.path.join(source, name))
    )
    for relative in HARNESS_SKILL_DIRS.values():
        _link_skill_set(source, os.path.join(user.home, relative), names)


def _link_skill_set(source: str, target: str, names: list[str]) -> None:
    """Symlink each skill into one harness directory, per-skill like the CLI.

    Per-skill links rather than one directory link: a harness or the attendee
    may put their own skill next to ours, and a whole-directory symlink would
    either shadow that or (once the directory exists for real) leave the
    Databricks skills unreachable.
    """
    if os.path.islink(target):
        os.unlink(target)
    os.makedirs(target, exist_ok=True)
    for name in names:
        link = os.path.join(target, name)
        if os.path.islink(link):
            os.unlink(link)
        elif os.path.exists(link):
            continue  # the attendee's own copy wins
        os.symlink(os.path.join(source, name), link)


# -- ~/.claude.json (onboarding + MCP servers) --

def _write_claude_json(user: User) -> None:
    # P1-21: public MCP servers are an indirect prompt-injection egress path for
    # the autonomous, token-bearing agent. Off by default; operators opt in per
    # event via ENABLE_PUBLIC_MCP. When off, the agent has no external MCP egress.
    mcp_servers = {}
    if config.enable_public_mcp():
        deepwiki = os.environ.get("DEEPWIKI_MCP_URL", DEFAULT_DEEPWIKI_MCP).strip()
        exa = os.environ.get("EXA_MCP_URL", DEFAULT_EXA_MCP).strip()
        if deepwiki:
            mcp_servers["deepwiki"] = {"type": "http", "url": deepwiki}
        if exa:
            mcp_servers["exa"] = {"type": "http", "url": exa}

    path = os.path.join(user.home, ".claude.json")
    try:
        with open(path) as f:
            existing = json.load(f)
    except (OSError, json.JSONDecodeError):
        existing = {}
    existing["hasCompletedOnboarding"] = True
    existing["mcpServers"] = mcp_servers
    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


# -- git identity + workspace-sync hook --

_POST_COMMIT = """#!/bin/bash
# Auto-sync committed work to the attendee's Databricks Workspace home so it
# survives workshop teardown. Only syncs repos inside ~/projects/.
SYNC_LOG="$HOME/.sync.log"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -z "$REPO_ROOT" ] && exit 0
case "$REPO_ROOT" in
  "$HOME/projects"/*) ;;
  *) exit 0 ;;
esac

DEST="/Workspace/Users/{email}/projects/$(basename "$REPO_ROOT")"
echo "[post-commit] $(date +%H:%M:%S) syncing $REPO_ROOT -> $DEST" >> "$SYNC_LOG"

# databricks sync respects .gitignore and never uploads .git. Strip app SP
# creds so the CLI authenticates from ~/.databrickscfg.
env -u DATABRICKS_CLIENT_ID -u DATABRICKS_CLIENT_SECRET -u DATABRICKS_HOST -u DATABRICKS_TOKEN \\
  nohup databricks sync "$REPO_ROOT" "$DEST" --watch=false >> "$SYNC_LOG" 2>&1 & disown
"""


def _write_git_setup(user: User) -> None:
    hooks_dir = os.path.join(user.home, ".githooks")
    os.makedirs(hooks_dir, exist_ok=True)

    display = user.email.split("@")[0].replace(".", " ").title()
    gitconfig = os.path.join(user.home, ".gitconfig")
    with open(gitconfig, "w") as f:
        f.write(
            "[user]\n"
            f"\temail = {user.email}\n"
            f"\tname = {display}\n"
            "[core]\n"
            f"\thooksPath = {hooks_dir}\n"
            "[init]\n"
            "\tdefaultBranch = main\n"
        )

    post_commit = os.path.join(hooks_dir, "post-commit")
    with open(post_commit, "w") as f:
        f.write(_POST_COMMIT.replace("{email}", user.email))
    os.chmod(post_commit, 0o755)
