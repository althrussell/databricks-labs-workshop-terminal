"""Per-user workshop content: instructions, subagents, skills, git, MCP.

Runs once per user (on their first session) after their HOME is bootstrapped:

- ~/.claude/CLAUDE.md       — workshop instructions (+ lab coach when enabled)
- ~/.codex/AGENTS.md        — the same instructions adapted for Codex
- ~/.local/bin/workshop-init-project — project bootstrap that commits the AppKit
                              mandate as project-level CLAUDE.md + AGENTS.md (the
                              only channel Omnigent's worktree-bound Codex worker
                              reads), backed by ~/.config/workshop/project-memory.md
- ~/.claude/agents/         — kept empty; the TDD subagent chain was removed
- ~/.claude/skills          — per-skill symlinks into the shared skills library
                              (reviewed databricks-agent-skills, fetched at boot);
                              ~/.codex/skills gets the same set, which is where
                              Codex and Omnigent's Codex worker discover them
- ~/.claude.json            — onboarding skipped + MCP servers (DeepWiki, Exa)
- ~/.gitconfig + hooks      — attendee git identity and a post-commit hook that
                              syncs ~/projects repos to the attendee's
                              Workspace home (survives an app restart, not the
                              workshop — the workspace is deleted at teardown)
"""

from __future__ import annotations

import json
import hmac
import logging
import os
import re
from datetime import datetime, timezone
import secrets
import shutil

from . import config
from .users import User

logger = logging.getLogger(__name__)

_ASSETS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets"))
_COACH_MARKER = "<!-- workshop-lab-coach -->"
_DISCOVERY_MARKER = "<!-- workshop-discovery -->"
# Placeholder inside CLAUDE.md's ship gate, swapped for the anchor when the
# discovery tier is on and removed entirely when it is off.
_DISCOVERY_ANCHOR_SLOT = "<!-- discovery-anchor -->"
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
        _write_persona,
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


# -- persona (technical vs business) --

# The coach adapts its whole vocabulary to this, so it used to be the agent's
# first job: read ~/.workshop/persona, and if it was empty, ask. That question
# cost an attendee a full round trip -- file read, an interactive prompt, their
# answer, a file write -- before anything they came here for started happening,
# and it landed on someone who had just been told to type "hi" and had no idea
# why the machine wanted to know.
#
# The web UI can ask it for free while they are still reading the landing page,
# so the file is seeded before the first token and the agent never has to.
PERSONAS = ("technical", "business")

# Someone who did not pick is far likelier to be non-technical -- the engineers
# are the ones who notice a toggle and set it. Guessing business is also the
# cheaper error: jargon aimed at someone who does not want it loses them, while
# plain language aimed at an engineer is merely brief.
DEFAULT_PERSONA = "business"

_PERSONA_RELATIVE = os.path.join(".workshop", "persona")
_PERSONA_MARKER = "<!-- workshop-persona -->"


def persona_path(user: User) -> str:
    return os.path.join(user.home, _PERSONA_RELATIVE)


def read_persona(user: User) -> str | None:
    """The attendee's stored persona, or None when they never chose one."""
    try:
        with open(persona_path(user)) as f:
            value = f.read().strip().lower()
    except OSError:
        return None
    return value if value in PERSONAS else None


def _write_persona(user: User) -> None:
    """Seed the persona file so the agent never has to ask for it.

    Only writes the default when nothing is set, so a choice made on the
    landing page survives provisioning regardless of which happened first.
    """
    if read_persona(user) is not None:
        return
    _store_persona(user, DEFAULT_PERSONA)


def _store_persona(user: User, persona: str) -> None:
    path = persona_path(user)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"{persona}\n")


def set_persona(user: User, persona: str) -> str:
    """Record a persona chosen in the web UI and refresh the instructions.

    Rewriting the instructions matters: the persona is inlined into them, and
    the attendee can pick it either before their first session (nothing written
    yet) or after (already written with the default). Without the rewrite, the
    second case would leave the agent reading a stale line.
    """
    persona = persona.strip().lower()
    if persona not in PERSONAS:
        raise ValueError(f"unknown persona {persona!r}")
    _store_persona(user, persona)
    _write_instructions(user)
    return persona


# -- instructions (CLAUDE.md / AGENTS.md + lab coach) --

def _overlay(text: str, name: str, marker: str) -> str:
    """Append an instruction overlay once, keyed by its marker comment."""
    if marker in text:
        return text
    with open(os.path.join(_ASSETS, "instructions", name)) as f:
        return f"{text}\n\n{f.read()}"


def _base_instructions() -> str:
    with open(os.path.join(_ASSETS, "instructions", "CLAUDE.md")) as f:
        text = f.read()
    if config.lab_coach_enabled():
        text = _overlay(text, "lab_coach.md", _COACH_MARKER)
    # C6: the agent is the only thing positioned to notice what an attendee is
    # trying to build, so discovery is an instruction overlay rather than a form.
    # Absent entirely when capture is off — instructions that told the agent to
    # record against a disabled endpoint would just produce failed calls and a
    # confused explanation to the attendee.
    #
    # The overlay alone was not enough to make it happen. Appended after ~360
    # lines of "build fast, never announce process", it read as ceremony and got
    # skipped, and sessions reached account teams as "no use case recorded". So
    # the shipping moment — which always happens — carries a pointer to it.
    #
    # The pointer is substituted rather than written into CLAUDE.md so that
    # turning discovery off still removes every instruction to elicit, which is
    # the consent boundary DISCOVERY_ENABLED exists to draw.
    if config.discovery_enabled():
        with open(os.path.join(_ASSETS, "instructions", "discovery_anchor.md")) as f:
            anchor = f.read().strip()
        text = text.replace(_DISCOVERY_ANCHOR_SLOT, anchor)
        text = _overlay(text, "discovery.md", _DISCOVERY_MARKER)
    else:
        # Take the surrounding blank line with it rather than leaving a gap in
        # the middle of the ship gate.
        text = text.replace(f"\n{_DISCOVERY_ANCHOR_SLOT}\n", "")
    return text


def _persona_overlay(user: User) -> str:
    """The attendee's persona, stated in the instructions themselves.

    Inlined rather than left in a file for the agent to go and read: a file read
    is a tool call, and on the first turn that call is the difference between an
    agent that starts building and one that starts doing admin. The agent is
    told the answer before it is asked the question.
    """
    persona = read_persona(user) or DEFAULT_PERSONA
    if persona == "technical":
        described = (
            "**technical** — they write code or know the Databricks components. "
            "Use real names (AppKit, Lakebase, SQL warehouse, Unity Catalog) and "
            "explain the architecture choices you make."
        )
    else:
        described = (
            "**business-oriented** — they care about the outcome, not the "
            "plumbing. Talk about what their product does for them, and keep "
            "Databricks component names out of it unless they ask."
        )
    return (
        f"{_PERSONA_MARKER}\n"
        "## Who you are working with\n\n"
        f"This attendee is {described}\n\n"
        "This is already settled — never ask them whether they are technical or "
        "business, and never read it from a file. If the conversation shows the "
        "guess was wrong, just adjust how you talk and carry on.\n"
    )


def _write_instructions(user: User) -> None:
    text = f"{_base_instructions()}\n\n{_persona_overlay(user)}"

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
    - ``workshop-discovery``: record what the agent learned about the attendee's
      use case (contract C6). Installed unconditionally — the endpoint answers
      ``captured: false`` when capture is off, and a helper that exists but
      declines is a much better failure than ``command not found`` mid-session
      if an operator enables capture on a running instance.

    There is deliberately no design-gate helper. Design quality is applied while
    the components are written, not audited afterwards by a command the attendee
    waits on — see ``assets/skills/workshop-design-studio``.
    """
    local_bin = os.path.join(user.home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)
    for name in (
        "databricks-me",
        "workshop-grant-me",
        "workshop-discovery",
    ):
        src = os.path.join(_ASSETS, "bin", name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(local_bin, name)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)


# -- subagents --

def _install_subagents(user: User) -> None:
    """Install no subagents, and make sure none survive from an earlier build.

    This used to copy a PRD -> failing-tests -> implement -> review chain into
    every attendee's ~/.claude/agents. Claude reaches for those on any "build me
    X", which turns a ten-minute app into an interview plus a test suite. The
    workshop's whole proposition is idea to live URL fast, so the chain is gone.

    The directory is still created (and emptied) because a HOME can outlive a
    deploy: leaving a stale prd-writer.md behind would keep the old behaviour
    running for exactly the attendees who already have a session open.
    """
    target = os.path.join(user.home, ".claude", "agents")
    os.makedirs(target, exist_ok=True)
    for name in os.listdir(target):
        if name.endswith(".md"):
            os.remove(os.path.join(target, name))


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
    _write_aitools_state(user, source, names)


# The CLI tracks which skills it installed in its own state file, and reports
# "no skills installed. Run 'databricks aitools install'" purely from that file
# — it never looks at the harness directories. Because we place skills
# ourselves (pinned + checksum-verified via the artifact manifest) rather than
# shelling out to `aitools install`, that file did not exist, so the CLI told
# every attendee their skills were missing while Claude and Codex were loading
# them just fine. Writing it keeps the CLI's view consistent with reality.
_AITOOLS_STATE_RELATIVE = os.path.join(
    ".databricks", "aitools", "skills", ".state.json"
)

_SKILL_VERSION_RE = re.compile(
    r"^metadata:\s*$.*?^\s+version:\s*[\"']?([^\"'\s]+)", re.MULTILINE | re.DOTALL
)


def _skill_version(source: str, name: str) -> str | None:
    """Version from a skill's SKILL.md frontmatter (``metadata.version``).

    A few upstream skills omit it (``databricks-dabs``); the CLI records those
    as ``0.0.1``, which the caller mirrors.
    """
    try:
        with open(os.path.join(source, name, "SKILL.md"), encoding="utf-8") as f:
            head = f.read(4096)
    except OSError:
        return None
    _, _, rest = head.partition("---")
    frontmatter, _, _ = rest.partition("\n---")
    match = _SKILL_VERSION_RE.search(frontmatter)
    return match.group(1) if match else None


def _write_aitools_state(user: User, source: str, names: list[str]) -> None:
    from .bootstrap.install import SKILLS_REF, skills_provenance

    managed = skills_provenance()
    # No provenance yet (vendored fallback, or skills still installing) means we
    # cannot tell upstream skills from our vendored workflow ones, and declaring
    # the wrong set is worse than declaring none.
    if not managed:
        return
    versions = {
        name: _skill_version(source, name) or "0.0.1"
        for name in names
        if name in managed
    }
    if not versions:
        return

    path = os.path.join(user.home, _AITOOLS_STATE_RELATIVE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "schema_version": 1,
        "release": SKILLS_REF,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "skills": versions,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


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
# outlives this container. It does not outlive the workshop: teardown deletes the
# workspace too, so keeping the work means pushing it to a remote the attendee
# owns. Only syncs repos inside ~/projects/.
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
