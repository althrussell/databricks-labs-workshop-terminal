"""Per-user workshop content: instructions, subagents, skills, git, MCP.

Refreshes before local/remote launches after the HOME is bootstrapped:

- ~/.claude/CLAUDE.md       — workshop instructions (+ lab coach when enabled)
- ~/.codex/AGENTS.md        — the same instructions adapted for Codex
- ~/.local/bin/workshop-init-project — project bootstrap that commits the AppKit
                              mandate as project-level CLAUDE.md + AGENTS.md (the
                              only channel Omnigent's worktree-bound Codex worker
                              reads), backed by ~/.config/workshop/project-memory.md
- ~/.claude/agents/         — retired workshop chain removed; custom agents kept
- ~/.claude/skills          — per-skill symlinks into the shared skills library
                              (reviewed databricks-agent-skills, fetched at boot);
                              ~/.agents/skills gets the same set, which is where
                              Codex and Omnigent's Codex worker discover them
- ~/.claude.json            — onboarding skipped + the local deploy MCP tool;
                              optional public MCP servers remain opt-in
- ~/.gitconfig + hooks      — attendee git identity and a post-commit hook that
                              syncs ~/projects repos to the attendee's
                              Workspace home (survives an app restart, not the
                              workshop — the workspace is deleted at teardown)
"""

from __future__ import annotations

import json
import hmac
import logging
import math
import os
import re
from datetime import datetime, timezone
import secrets
import subprocess
import time
from pathlib import Path

from . import config, projects
from .users import User, _atomic_write, email_slug

logger = logging.getLogger(__name__)

_ASSETS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets"))
_COACH_MARKER = "<!-- workshop-lab-coach -->"
_DISCOVERY_MARKER = "<!-- workshop-discovery -->"
# Placeholder inside CLAUDE.md's ship gate, swapped for the anchor when the
# discovery tier is on and removed entirely when it is off.
_DISCOVERY_ANCHOR_SLOT = "<!-- discovery-anchor -->"
_WORKSHOP_CONTRACT_SLOT = "<!-- workshop-contract-slot -->"
_TERMINAL_SAFETY_SLOT = "<!-- terminal-safety-slot -->"
# Who the agent is and how it works with the attendee. Home channels only: a
# project worker is handed tasks by another agent and never meets the attendee.
_TEAMMATE_SLOT = "<!-- workshop-teammate-slot -->"
_CALLBACK_CAPABILITY = os.path.join(".config", "workshop", "callback-capability")

DEFAULT_DEEPWIKI_MCP = "https://mcp.deepwiki.com/mcp"
DEFAULT_EXA_MCP = "https://mcp.exa.ai/mcp"

class PreparationError(RuntimeError):
    """Required workshop content could not be prepared; a retry repairs it."""

    def __init__(self, steps: list[str]):
        self.steps = steps
        super().__init__("Workshop preparation failed: " + ", ".join(steps))


def provision(user: User) -> None:
    """Refresh launch content, retrying failed/missing writes on every launch.

    Only git/npm conveniences are optional. Never cache an email as ready.
    """
    failures = []
    with user.content_lock:
        steps = (
            user.refresh_launchers,
            _write_callback_capability,
            _write_persona,
            _write_instructions,
            _install_project_helper,
            _install_cli_helpers,
            _install_subagents,
            _link_skills,
            _write_claude_json,
        )
        for step in (*steps, _write_git_setup, _write_npm_setup, _write_shell_setup):
            try:
                if step == user.refresh_launchers:
                    step()
                else:
                    step(user)
            except Exception as e:  # noqa: BLE001 — report all failed steps
                logger.warning("user content step %s failed for %s: %s",
                               step.__name__, user.email, e)
                if step in steps:
                    failures.append(step.__name__.lstrip("_"))
    if failures:
        raise PreparationError(failures)


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


def callback_capability_owner(supplied: str) -> str | None:
    """The attendee whose capability this is, for a helper that lost its email.

    A harness that filters its environment (Omnigent's runner did, test1025)
    leaves a helper with the capability file but no WORKSHOP_USER_EMAIL. The
    capability is a per-attendee secret, so it identifies its owner by itself;
    every attendee is compared, so the answer takes the same time either way.
    """
    from .users import user_manager

    if not supplied:
        return None
    owner = None
    for user in user_manager.all():
        try:
            with open(callback_capability_path(user)) as f:
                expected = f.read().strip()
        except OSError:
            continue
        if expected and hmac.compare_digest(expected, supplied):
            owner = user.email
    return owner


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
_WIZARD_MARKER = "<!-- workshop-brief -->"


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
    from . import attendee_profile

    attendee_profile.save(user, "technical" if persona == "technical" else "guided", expected_revision=None)
    _store_persona(user, persona)
    _write_instructions(user)
    return persona


def set_wizard_brief(user: User, brief) -> None:
    """Fold a completed wizard brief into the agent's instructions.

    Same shape as ``set_persona`` and for the same reason: the brief is inlined
    into the instruction files, and the attendee can finish the wizard either
    before their first session or after one is already running with instructions
    that predate it.

    Preferences belong to the separately revisioned profile. An older product
    brief cannot overwrite a newer session preference.
    """
    _write_instructions(user)


# -- instructions (CLAUDE.md / AGENTS.md + lab coach) --

def _overlay(text: str, name: str, marker: str) -> str:
    """Append an instruction overlay once, keyed by its marker comment."""
    if marker in text:
        return text
    with open(os.path.join(_ASSETS, "instructions", name)) as f:
        return f"{text}\n\n{f.read()}"


def _base_instructions() -> str:
    with open(os.path.join(_ASSETS, "instructions", "CLAUDE.md")) as f:
        text = _compose_workshop_contract(f.read())
    if text.count(_TEAMMATE_SLOT) != 1:
        raise ValueError("Home instructions require exactly one teammate slot")
    with open(os.path.join(_ASSETS, "instructions", "teammate.md")) as f:
        text = text.replace(_TEAMMATE_SLOT, f.read().strip())
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


def _compose_workshop_contract(text: str) -> str:
    """Inline one fork-owned contract in every always-read memory channel.

    It lives outside the upstream skills tree, so refreshing that tree cannot
    overwrite it. A missing/duplicate slot is a packaging defect, not a reason
    to silently ship an instruction channel with different behavior.
    """
    if text.count(_WORKSHOP_CONTRACT_SLOT) != 1:
        raise ValueError("Workshop instructions require exactly one contract slot")
    # The one rule that protects the terminal itself sits at the top of every
    # channel rather than inside the contract, so it is read first.
    if text.count(_TERMINAL_SAFETY_SLOT) != 1:
        raise ValueError("Workshop instructions require exactly one terminal safety slot")
    with open(os.path.join(_ASSETS, "instructions", "terminal_safety.md")) as f:
        text = text.replace(_TERMINAL_SAFETY_SLOT, f.read().strip())
    with open(os.path.join(_ASSETS, "instructions", "workshop_contract.md")) as f:
        return text.replace(_WORKSHOP_CONTRACT_SLOT, f.read().strip())


_APPROACH = {
    "co_build": "**Build with me**: co-design the MVP and plan first, waiting for their go-ahead; explain key decisions as you go.",
    "learn": "**Teach me while we build**: co-design and confirm the plan first, then offer small, relevant hands-on steps and explain why.",
    "lead": "**Let me drive**: they set constraints and choose what you implement at every step; offer options and wait before acting.",
}
_BACKGROUND = {
    "domain": "**business or domain side**: outcomes first in plain language; name the building blocks and why",
    "data": "**data and SQL**: keep their queries and data choices visible; explain query to API to app",
    "developer": "**software developer**: real component names, architecture and diffs; they set constraints",
    "exploring": "**just exploring**: concrete examples and prepared data first, then their own idea",
}
_DETAIL = {
    "adaptive": "adapt explanation detail to each request",
    "brief": "keep explanations brief: lead with the result",
    "detailed": "explain in detail when it helps them decide or learn",
}


def _industry_line(user: User, stated: str) -> str:
    """What the meeting does about the industry, for this attendee.

    The attendee's own answer wins. Otherwise a confident roster suggestion is
    confirmed first; with none, every seeded industry is offered, so the menu
    never omits one (30 Oct: Telco, food and healthcare were missing).
    """
    if stated:
        return f"**{stated.replace('_', ' ')}**"
    from . import attendee_roster, demo_data

    suggested = attendee_roster.suggestion(user.email)
    if suggested:
        label = suggested.get("industry_label") or suggested["suggested_industry"].replace("_", " ")
        company = suggested.get("company") or "their company"
        return (f"not confirmed yet. Suggested **{label}** (roster: {company}, confidence "
                f"{float(suggested['industry_confidence']):.2f}); the first meeting question confirms it")
    try:
        names = [slug.replace("_", " ") for slug in demo_data.offered_industries()]
    except Exception:  # noqa: BLE001 — a catalog hiccup must not drop the menu
        names = [slug.replace("_", " ") for slug in demo_data.KNOWN_INDUSTRIES]
    return "not known yet; offer all of these, plus Something else: " + ", ".join(names)


def _persona_overlay(user: User) -> str:
    """Inline the attendee's working preferences; they are presets, not an assessment."""
    from . import attendee_profile

    try:
        profile = attendee_profile.read(user)
    except attendee_profile.ProfileReadError:
        logger.warning("Using adaptive help while attendee profile is unreadable")
        profile = attendee_profile.Profile(source="unavailable")
    effective = profile.effective()
    approach = _APPROACH[effective["working_approach"]]
    if not effective["working_approach_stated"]:
        approach += " (default, not chosen yet: offer the three ways when you meet them)."
    background = effective.get("background") or ""
    starting = (_BACKGROUND[background] if background in _BACKGROUND and effective.get("background_stated")
                else "not known yet (ask when you meet them)")
    stated_industry = effective.get("industry") if effective.get("industry_stated") else ""
    if not stated_industry:
        try:
            from . import wizard

            stated_industry = wizard.read_brief(user).stated_industry
        except Exception:  # noqa: BLE001
            stated_industry = ""
    industry = _industry_line(user, stated_industry)
    lines = [
        _PERSONA_MARKER,
        "## How they want to work with you",
        "",
        f"- Working approach: {approach}",
        f"- Starting point: {starting}.",
        f"- Industry: {industry}.",
        f"- Explanation detail: {_DETAIL[effective['explanation_detail']]}.",
    ]
    if effective["technical_vocabulary"]:
        lines.append("- They asked for technical discussion: use real component names and implementation detail.")
    if effective["plain_language"]:
        lines.append("- They asked for plain-language help: lead with business outcomes and explain each "
                     "Databricks building block and why it fits in plain English.")
    if effective["engagement_path"]:
        lines.append(f"- Default path for a new idea: {effective['engagement_path']} (a project's recorded "
                     "path wins; still ask if it does not fit this idea).")
    lines += [
        "",
        f"Profile revision {profile.revision}. Presets, changeable any time; in-the-moment requests win.",
    ]
    return "\n".join(lines) + "\n"


def _wizard_overlay(user: User) -> str:
    """What the attendee told the wizard, stated in the instructions.

    Two jobs. The obvious one is that the agent knows what they came to build
    before the first token, so it can reuse the facts without asking again.

    The less obvious one is ``record_id``. The wizard has already filed a
    discovery record; the agent must refine *that* record rather than open a
    second one. Without this line the same attendee arrives at Control Tower as
    two unrelated use cases and the account brief reads as though they wanted two
    different things.

    Empty when the attendee skipped: the agent then meets them itself and asks
    what the wizard would have (teammate.md, "Meeting them").
    """
    from . import content, wizard

    brief = wizard.read_brief(user)
    if brief.skipped or brief.stage != "complete" or not brief.has_content:
        return ""

    lines = [_WIZARD_MARKER, "## What this attendee came to build", ""]
    what = brief.what_building.strip()
    idea = content.WizardIdea.model_validate(brief.selected_idea) if brief.selected_idea else None
    if what:
        authored = {"legacy_unverified": "Saved goal (legacy authorship unverified)",
                    "confirmed_goal": "They confirmed this saved goal"}.get(brief.words_source, "In their own words")
        lines.append(f"{authored}: **{what}**")
    elif idea:
        lines.append(f"They picked this from a list of ideas: **{idea.label}** — {idea.outcome}.")
    if idea and what:
        lines.append("")
        lines.append(f"They also picked the idea **{idea.label}** — {idea.outcome}.")
    if brief.stated_industry:
        lines += ["", f"Industry: **{brief.stated_industry.replace('_', ' ')}**."]
    if brief.intent:
        described = {
            "business_problem": "solving a real problem from work",
            "evaluation": "evaluating whether Databricks can do this",
            "learning": "learning — they want to understand it, not just get output",
            "fun": "here to build something fun",
        }.get(brief.intent, brief.intent)
        lines.append(f"They are here for: **{described}**.")
    if brief.current_stack:
        lines.append(f"They mentioned using: {', '.join(brief.current_stack)}.")
    if brief.engagement_path:
        label = {"rapid": "a rapid prototype to react to", "guided": "guided ideation"}[brief.engagement_path]
        lines.append(f"They chose to start with **{label}**. Do not ask again; record it in the project with "
                     f"`workshop-workflow path {brief.engagement_path} --by wizard --source wizard`.")

    lines += [
        "",
        "They told us these facts on the way in, so **do not ask them again**. "
        "Still follow the teammate co-design steps before changing any files: "
        "confirm the sharpened MVP idea, propose the Databricks building blocks "
        "with a one-line why for each (inviting questions), state the build "
        "plan, and wait for their go-ahead before creating a project, file or table.",
    ]

    lines += ["", "Canonical task context (selected suggestions are not attendee-authored words):",
              "```json", json.dumps(wizard.launch_context(brief), ensure_ascii=False), "```"]

    if config.discovery_enabled() and brief.discovery_record_id:
        lines += [
            "",
            f"A discovery record already exists for this: `{brief.discovery_record_id}`. "
            "When you learn more about what they are trying to do, **update that "
            "record** by passing the same `record_id` — do not open a new one. A "
            "second record for the same attendee reads downstream as a second, "
            "conflicting use case.",
        ]

    return "\n".join(lines) + "\n"


def _demo_data_overlay(user: User) -> str:
    """The demo catalog and how to use it, if this deployment has one.

    Scoped to the attendee's industry when the wizard captured one, because a
    single schema listed in full is something the agent will actually use, while
    every schema in the catalog is a wall of table names it will skim past.

    Empty when the catalog is unset or unreachable, so a deployment without demo
    data never promises the agent tables it cannot query.
    """
    from . import attendee_profile, demo_data, wizard

    if not demo_data.enabled():
        return ""
    brief = wizard.read_brief(user)
    scoped = brief.stated_industry
    if not scoped:
        try:
            scoped = attendee_profile.read(user).industry
        except Exception:  # noqa: BLE001
            scoped = ""
    if not scoped and brief.stage == "complete" and not brief.skipped and brief.selected_idea:
        schemas = {ref.partition(".")[0] for ref in brief.selected_idea.get("demo_tables", [])}
        if len(schemas) == 1:
            scoped = next(iter(schemas))
    manifest = demo_data.manifest(scoped)
    if not manifest:
        # Nothing readable, so nothing to say — including the note below about
        # generating rather than borrowing, which only guards against the agent
        # reaching for a neighbouring industry's tables. Where there are no
        # tables to reach for, generating is what it will do anyway, and an
        # overlay describing a catalog we could not read sends it looking for
        # schemas that may not exist.
        return ""
    with open(os.path.join(_ASSETS, "instructions", "demo_data.md")) as f:
        template = f.read()
    text = template.replace("{manifest}", manifest).replace(
        "{workshop_catalog}", config.workshop_catalog() or "<your catalog>"
    )
    if not scoped:
        text += (
            "\n\nThey have not chosen an industry. Do not assume automotive "
            "or any other schema — ask, or pick the one whose tables match "
            "what they described.\n"
        )
    elif not demo_data.has_industry(scoped):
        # A real industry that nobody seeded here. Saying nothing would leave
        # the agent to pattern-match the attendee's industry onto whichever
        # schema looked closest, and build a logistics demo out of retail.
        # The schemas are listed above, because the manifest of an industry
        # that resolves to nothing is the whole-catalog summary.
        text += (
            f"\n\nTheir industry is **{scoped.replace('_', ' ')}**, and no schema "
            "above is named for it. Use their own data if they upload it; otherwise "
            "generate what you need in their catalog, labelled synthetic. Supporting "
            "tables from another schema (weather, regions, calendars) are fine to "
            "join when they genuinely fit, said plainly; never present another "
            "industry's business records as theirs. If a schema above is their "
            "industry under another name (a bank's is `financial_services`), use it "
            "and save it with `workshop-workflow prefs --industry <schema>`.\n"
        )
    return text


def _write_instructions(user: User) -> None:
    with user.content_lock:
        _write_instructions_locked(user)


def _write_instructions_locked(user: User) -> None:
    from . import projects

    project_context = projects.instructions(user)
    parts = [
        _base_instructions(),
        _persona_overlay(user),
        project_context or _wizard_overlay(user),
        _demo_data_overlay(user),
    ]
    text = "\n\n".join(p for p in parts if p.strip())

    claude_md = os.path.join(user.home, ".claude", "CLAUDE.md")
    os.makedirs(os.path.dirname(claude_md), exist_ok=True)
    _atomic_write(claude_md, text, 0o600)

    # Codex reads AGENTS.md; same content, only the top header is swapped.
    agents_md = os.path.join(user.home, ".codex", "AGENTS.md")
    os.makedirs(os.path.dirname(agents_md), exist_ok=True)
    adapted = re.sub(r"^#\s+.*$", "# Codex Agent Instructions", text, count=1, flags=re.MULTILINE)
    # Codex discovers skills in ~/.agents/skills; ~/.claude/skills holds Claude's payloads.
    adapted = adapted.replace("`~/.claude/skills`", "`~/.agents/skills`")
    _atomic_write(agents_md, adapted, 0o600)


# -- project memory (project-level CLAUDE.md / AGENTS.md via a bootstrap helper) --

def _project_memory() -> str:
    """The worker-only project-memory template (never duplicates the home contract).

    Substituted rather than shipped in the template for the same reason as the
    home instructions: turning discovery off must remove every instruction to
    elicit, which is the consent boundary DISCOVERY_ENABLED exists to draw.
    """
    with open(os.path.join(_ASSETS, "instructions", "project_memory.md")) as f:
        text = f.read()
    if text.count(_TERMINAL_SAFETY_SLOT) != 1:
        raise ValueError("Project instructions require exactly one terminal safety slot")
    with open(os.path.join(_ASSETS, "instructions", "terminal_safety.md")) as f:
        text = text.replace(_TERMINAL_SAFETY_SLOT, f.read().strip())
    if not config.discovery_enabled():
        return text.replace(f"\n{_DISCOVERY_ANCHOR_SLOT}\n", "").replace(_DISCOVERY_ANCHOR_SLOT, "")
    text = text.replace(f"\n{_DISCOVERY_ANCHOR_SLOT}\n", "").replace(_DISCOVERY_ANCHOR_SLOT, "")
    return _overlay(text, "project_discovery.md", _DISCOVERY_MARKER)


def write_project_memory_template(user: User) -> None:
    """Write the project-memory template that `workshop-init-project` commits.

    Also called when the attendee's profile changes, so a project created after
    the industry is saved carries that industry's demo-data section (H2, F-007).
    """
    template_dst = os.path.join(user.home, ".config", "workshop", "project-memory.md")
    os.makedirs(os.path.dirname(template_dst), exist_ok=True)
    # Worktree workers cannot read the home-only demo manifest.
    _atomic_write(template_dst, "\n\n".join(part for part in (
        _project_memory(), _demo_data_overlay(user),
    ) if part.strip()), 0o600)


def _install_project_helper(user: User) -> None:
    """Install the project bootstrap helper + its project-memory template.

    Home-level ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md reach Claude, Codex,
    and Omnigent's polly brain — but polly's Codex sub-agent runs in an isolated
    CODEX_HOME inside a git worktree, so it never sees the global AGENTS.md. The
    only channel that survives that is a *committed, project-level* AGENTS.md,
    which `workshop-init-project` writes (and commits) into every new project so
    it propagates into worktrees. Defense-in-depth for the other agents too.

    That makes this the only instruction channel to the agent that does the work
    in an Omnigent session, which is why the discovery mandate has to be here and
    not only in the home file.
    """
    write_project_memory_template(user)

    helper_src = os.path.join(_ASSETS, "bin", "workshop-init-project")
    local_bin = os.path.join(user.home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)
    helper_dst = os.path.join(local_bin, "workshop-init-project")
    with open(helper_src) as f:
        _atomic_write(helper_dst, f.read(), 0o755)


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
    - ``workshop-app-deploy``: the sole governed app deploy-and-wait command and
      the stdio MCP server registered with Claude and Codex. It uses the rotated
      default profile, persists resumable state, and exposes no generic shell.
    - ``workshop-export``: package source and verify its copy in the assigned
      attendee catalog/schema, with on-screen ZIP download instructions.
    - ``workshop-browser``: real screenshots of the attendee's app for the
      agent to look at, dead-control checks and write proof as the app (W3).

    There is no blocking design-gate helper. Build-time visual defaults and
    practical checks with prepared tooling follow the shared workshop contract.
    """
    local_bin = os.path.join(user.home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)
    helpers = [
        "databricks-me",
        "workshop-grant-me",
        "workshop-discovery",
        "workshop-app-deploy",
        "workshop-export",
        "workshop-workflow",
    ]
    if config.workshop_browser_enabled():
        helpers.append("workshop-browser")
    else:
        browser_dst = os.path.join(local_bin, "workshop-browser")
        if os.path.lexists(browser_dst):
            os.unlink(browser_dst)
    for name in helpers:
        src = os.path.join(_ASSETS, "bin", name)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"Required workshop helper missing: {name}")
        dst = os.path.join(local_bin, name)
        with open(src) as f:
            _atomic_write(dst, f.read(), 0o755)


# -- subagents --

def _install_subagents(user: User) -> None:
    """Install no subagents, and make sure none survive from an earlier build.

    This used to copy a PRD -> failing-tests -> implement -> review chain into
    every attendee's ~/.claude/agents. Claude reaches for those on any "build me
    X", which turns a ten-minute app into an interview plus a test suite. The
    workshop's whole proposition is idea to live URL fast, so the chain is gone.

    The directory is still created because a HOME can outlive a
    deploy: leaving a stale prd-writer.md behind would keep the old behaviour
    running for exactly the attendees who already have a session open.
    """
    target = os.path.join(user.home, ".claude", "agents")
    os.makedirs(target, exist_ok=True)
    for name in ("build-feature.md", "implementer.md", "prd-writer.md", "test-generator.md"):
        path = os.path.join(target, name)
        if os.path.lexists(path):
            os.unlink(path)


# -- skills (shared library, fetched latest at boot) --

# Each harness reads its own directory, and `databricks aitools install` is the
# reference for which: it keeps one canonical copy of the skills and symlinks
# each skill into every detected agent's real skills directory. Claude Code
# loads ~/.claude/skills; current Codex CLI loads ~/.agents/skills, which is also the
# CODEX_HOME configure_codex() writes, so Omnigent's Codex worker inherits it.
HARNESS_SKILL_DIRS = {
    "claude": os.path.join(".claude", "skills"),
    "codex": os.path.join(".agents", "skills"),
}


def shared_skills_dir() -> str:
    return os.path.join(config.shared_prefix(), "skills")


def _link_skills(user: User) -> None:
    from .bootstrap import install
    from .bootstrap.skill_projection import RETIRED_WORKSHOP_SKILLS

    source = shared_skills_dir()
    # A directory surviving the last deployment is not proof of current content.
    # Use this package's assets until this boot verifies/publishes the shared set.
    if not install.skills_ready() or not os.path.isdir(source):
        source = os.path.join(_ASSETS, "skills")
    excluded = set(RETIRED_WORKSHOP_SKILLS) | {"refresh-databricks-skills"}
    if not config.agentbricks_enabled():
        excluded.add("workshop-agent-bricks-cli")
    if not config.discovery_enabled():
        excluded.add("workshop-discovery")
    names = sorted(
        name
        for name in os.listdir(source)
        if name not in excluded and os.path.isfile(os.path.join(source, name, "SKILL.md"))
    )
    # Packaged fallback paths change with PEX versions. Remember ownership so
    # a link into the previous package is refreshed on the next deployment.
    state_path = os.path.join(user.home, ".config", "workshop", "skill-links.json")
    try:
        with open(state_path) as f:
            previous = json.load(f).get("roots", [])
    except (OSError, ValueError, AttributeError):
        previous = []
    if not isinstance(previous, list):
        previous = []
    roots = tuple(sorted({source, shared_skills_dir(), os.path.join(_ASSETS, "skills"),
                          *(root for root in previous if isinstance(root, str) and os.path.isabs(root))}))
    for harness, relative in HARNESS_SKILL_DIRS.items():
        _link_skill_set(source, os.path.join(user.home, relative), names, owned_roots=roots,
                        sources=_provider_sources(source, harness, names))
    _link_claude_companions(source, user, roots)
    # Move managed links out of Codex's legacy discovery path so one skill is
    # discovered once. Preserve attendee-owned files and links there.
    legacy = os.path.join(user.home, ".codex", "skills")
    if os.path.lexists(legacy):
        _link_skill_set(source, legacy, [], owned_roots=roots)
    _atomic_write(state_path, json.dumps({"version": 1, "roots": roots}), 0o600)
    _write_aitools_state(user, source, names)
    from . import skill_routing

    _atomic_write(os.path.join(user.home, ".config", "workshop", "skill-routing.json"),
                  json.dumps(skill_routing.audit(user.home, tree=source), indent=2, sort_keys=True), 0o600)


def _provider_sources(source: str, harness: str, names: list[str]) -> dict[str, str]:
    """Per-skill link targets for one harness: its own native payload where one exists.

    W1: Claude used to receive the Codex Impeccable payload. A tree without a
    verified payload for this harness yields no link for that skill rather than
    the other provider's copy; the routing audit reports the gap.
    """
    from .bootstrap import impeccable

    if harness != "claude" or "impeccable" not in names:
        return {}
    path = impeccable.payload_path(source, "claude")
    return {"impeccable": str(path) if path is not None else ""}


def _link_claude_companions(source: str, user: User, roots: tuple[str, ...]) -> None:
    """Link Impeccable's Claude companion agents into ~/.claude/agents.

    Managed links only: an attendee's own agent file with the same name wins,
    and links into an earlier managed tree are refreshed or retired.
    """
    from .bootstrap import impeccable

    agents = impeccable.payload_path(source, "claude_agents")
    target = os.path.join(user.home, ".claude", "agents")
    os.makedirs(target, exist_ok=True)
    wanted = {name: os.path.join(str(agents), name) for name in os.listdir(agents)} if agents else {}
    managed = (*roots, os.path.abspath(shared_skills_dir()), os.path.join(_ASSETS, "skills"))
    for name in os.listdir(target):
        link = os.path.join(target, name)
        if name.startswith("impeccable-") and os.path.islink(link) and name not in wanted:
            destination = os.path.abspath(os.path.join(target, os.readlink(link)))
            if any(destination.startswith(root + os.sep) for root in managed):
                os.unlink(link)
    for name, destination in wanted.items():
        link = os.path.join(target, name)
        if os.path.islink(link):
            if os.readlink(link) == destination:
                continue
            current = os.path.abspath(os.path.join(target, os.readlink(link)))
            if not any(current.startswith(root + os.sep) for root in managed):
                continue
        elif os.path.exists(link):
            continue  # the attendee's own agent wins
        temporary = link + ".workshop-" + secrets.token_hex(6)
        try:
            os.symlink(destination, temporary)
            os.replace(temporary, link)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)


def refresh_skill_links() -> None:
    """Reconcile early-launch fallback links after background publication."""
    from .users import user_manager

    for user in user_manager.all():
        with user.content_lock:
            try:
                _link_skills(user)
            except OSError:
                logger.warning("skill reconciliation failed for %s; next launch retries", user.email)


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


def _link_skill_set(source: str, target: str, names: list[str], *, owned_roots: tuple[str, ...] = (),
                    sources: dict[str, str] | None = None) -> None:
    """Symlink each skill into one harness directory, per-skill like the CLI.

    Per-skill links rather than one directory link: a harness or the attendee
    may put their own skill next to ours, and a whole-directory symlink would
    either shadow that or (once the directory exists for real) leave the
    Databricks skills unreachable.

    ``sources`` overrides one skill's link target (a provider-native payload).
    An empty override means this harness has no verified copy: a managed link
    is removed rather than pointed at another provider's payload.
    """
    sources = sources or {}
    if os.path.islink(target):
        os.unlink(target)
    os.makedirs(target, exist_ok=True)
    managed_roots = (*owned_roots, os.path.abspath(shared_skills_dir()), os.path.join(_ASSETS, "skills"))
    for name in os.listdir(target):
        link = os.path.join(target, name)
        if name not in names and os.path.islink(link):
            destination = os.path.abspath(os.path.join(target, os.readlink(link)))
            if any(destination.startswith(root + os.sep) for root in managed_roots):
                os.unlink(link)
    for name in names:
        link = os.path.join(target, name)
        wanted = sources.get(name, os.path.join(source, name))
        if os.path.islink(link):
            destination = os.path.abspath(os.path.join(target, os.readlink(link)))
            if not any(destination.startswith(root + os.sep) for root in managed_roots):
                continue  # preserve attendee-owned links too
            if not wanted:
                os.unlink(link)
                continue
            if os.readlink(link) == wanted:
                continue
        elif os.path.exists(link):
            continue  # the attendee's own copy wins
        if not wanted:
            continue
        temporary = link + ".workshop-" + secrets.token_hex(6)
        try:
            os.symlink(wanted, temporary)
            os.replace(temporary, link)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)


# -- ~/.claude.json (onboarding + MCP servers) --

def _write_claude_json(user: User) -> None:
    # P1-21: public MCP servers are an indirect prompt-injection egress path for
    # the autonomous, token-bearing agent. Off by default; operators opt in per
    # event via ENABLE_PUBLIC_MCP. When off, the agent has no external MCP egress.
    mcp_servers = {
        "apx": {
            "type": "stdio",
            "command": os.path.join(config.shared_prefix(), "bin", "apx"),
            "args": ["mcp"],
        },
        "workshop": {
            "type": "stdio",
            "command": os.path.join(
                user.home, ".local", "bin", "workshop-app-deploy"
            ),
            "args": ["--mcp"],
        }
    }
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
    # Claude asks whether to trust a new folder and defaults to exit. WT created
    # the attendee's builds folder; Claude inherits trust into each build in it.
    trusted = existing.get("projects")
    if not isinstance(trusted, dict):
        trusted = {}
    builds = os.path.realpath(projects.root(user))
    entry = trusted.get(builds)
    trusted[builds] = {**(entry if isinstance(entry, dict) else {}), "hasTrustDialogAccepted": True}
    existing["projects"] = trusted
    retained = existing.get("mcpServers", {})
    if not isinstance(retained, dict):
        retained = {}
    for name in ("workshop", "apx", "deepwiki", "exa"):
        retained.pop(name, None)
    existing["mcpServers"] = {**retained, **mcp_servers}
    _atomic_write(path, json.dumps(existing, indent=2), 0o600)


# -- git identity + workspace-sync hook --

_POST_COMMIT = """#!/bin/bash
# Auto-sync committed work to the attendee's Databricks Workspace home, where
# they can open it in the workspace UI, use it from a notebook or job, and still
# reach it once this app is gone. The container's own copy lives on DATA_ROOT
# and is not the thing at risk here.
#
# Export the take-home ZIP before teardown deletes the workshop workspace.
# Only syncs repos inside ~/projects/.
SYNC_LOG="$HOME/.sync.log"
STATUS="{status_path}"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -z "$REPO_ROOT" ] && exit 0
# Codex shortens HOME with a symlink to fit its Unix socket path. Git reports
# the physical repo path, so compare against the physical projects directory.
PROJECTS_ROOT="$(cd "$HOME/projects" 2>/dev/null && pwd -P)"
[ -z "$PROJECTS_ROOT" ] && exit 0
case "$REPO_ROOT" in
  "$PROJECTS_ROOT"/*) ;;
  *) exit 0 ;;
esac

DEST="/Workspace/Users/{email}/projects/$(basename "$REPO_ROOT")"
echo "[post-commit] $(date +%H:%M:%S) syncing $REPO_ROOT -> $DEST" >> "$SYNC_LOG"
mkdir -p "$(dirname "$STATUS")" 2>/dev/null

# Detach the complete worker, including outcome recording, into its own session.
# Native harnesses clean up a shell command's process group when it finishes;
# nohup/disown alone do not keep the background save alive through that cleanup.
# Commits still return immediately. The CLI respects .gitignore and skips .git.
python3 - "$REPO_ROOT" "$DEST" "$STATUS" "$SYNC_LOG" <<'PY'
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

worker = r'''
REPO_ROOT="$1"
DEST="$2"
STATUS="$3"
SYNC_LOG="$4"
databricks sync "$REPO_ROOT" "$DEST" --watch=false >> "$SYNC_LOG" 2>&1
  code=$?
  detail=""
  if [ "$code" -ne 0 ]; then
    # Control characters go first so the record stays one line per field
    # whatever the CLI printed, and the reader never has to unescape anything.
    detail="$(tail -n 20 "$SYNC_LOG" | grep -v '^\\[post-commit\\]' \\
      | tr -d '\\000-\\037' | tail -c 240)"
  fi
  {
    printf 'at=%s\\n' "$(date +%s)"
    printf 'exit=%s\\n' "$code"
    printf 'detail=%s\\n' "$detail"
  } > "$STATUS.$$.tmp" 2>/dev/null && mv -f "$STATUS.$$.tmp" "$STATUS" 2>/dev/null || exit 1
  python3 - "$5" "$code" <<'PY_OUTCOME'
import json
import os
import sys
import time
from pathlib import Path

receipt = Path(sys.argv[1])
outcome = json.loads(receipt.read_text())
outcome.update(completed_at=time.time(), exit=int(sys.argv[2]))
completed = receipt.parent.parent / "workspace-sync-outcomes"
completed.mkdir(exist_ok=True)
temporary = completed / ("." + receipt.name + ".tmp")
with temporary.open("x") as handle:
    json.dump(outcome, handle, allow_nan=False)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, completed / receipt.name)
directory = os.open(completed, os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
receipt.unlink()
PY_OUTCOME
'''
env = os.environ.copy()
for name in ("DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET", "DATABRICKS_HOST", "DATABRICKS_TOKEN"):
    env.pop(name, None)
# WORKSHOP_SYNC_WRITER_PROTOCOL=1. Register before detach under the same local
# lock closeout uses to stop new hook workers. The child retains an individual
# terminal outcome before clearing admission; another repo's success cannot
# erase this worker's failure. An interrupted outcome recording stays held.
state = Path(sys.argv[3]).parent
with (state / "workspace-sync.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if (state / "workspace-sync.closed").exists():
        sys.exit(0)
    writers = state / "workspace-sync-writers"
    writers.mkdir(exist_ok=True)
    receipt = writers / (uuid.uuid4().hex + ".json")
    with receipt.open("x") as handle:
        json.dump({"schema_version": 1, "registered_at": time.time()}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        subprocess.Popen(
            ["bash", "-c", worker, "workshop-sync", *sys.argv[1:], str(receipt)], env=env,
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        # Its accepted sync never started. Keep the receipt, rather than make
        # the required work look successfully drained after a spawn failure.
        raise
PY
"""


def closeout_sync_barrier(users: list[User], *, deadline: float) -> dict:
    """Close detached-hook admission without treating old hooks as tracked."""
    import fcntl

    issues: list[str] = []
    observed = 0
    for user in users:
        home = Path(user.home)
        if not home.is_dir():
            continue  # Headless recovered owners have no local writer to stop.
        state = home / ".workshop"
        try:
            state.mkdir(exist_ok=True)
            with (state / "workspace-sync.lock").open("a") as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("sync hook registration is active")
                        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                (state / "workspace-sync.closed").touch(exist_ok=True)
            hook = home / ".githooks" / "post-commit"
            if hook.exists():
                if "WORKSHOP_SYNC_WRITER_PROTOCOL=1" not in hook.read_text():
                    issues.append("legacy_workspace_sync_untracked")
            elif (home / ".sync.log").exists():
                issues.append("workspace_sync_hook_unverified")
            observed += 1
        except (OSError, TimeoutError, UnicodeError) as error:
            issues.append(f"workspace_sync_barrier_unverified:{type(error).__name__}")
    issues = sorted(set(issues))
    return {"complete": not issues, "issues": issues, "homes_observed": observed}


def drain_sync_for_closeout(users: list[User], *, deadline: float) -> dict:
    """Verify every accepted detached sync's terminal outcome, not just exit.

    Completion records contain only timestamps and exit codes. They preserve
    the existing sync granularity while preventing a later successful repo from
    hiding another required repo's failed save.
    """
    states = [Path(user.home) / ".workshop" for user in users]
    issues: list[str] = []
    pending = 0
    while True:
        pending = 0
        observed_issues: set[str] = set()
        try:
            for state in states:
                root, completed = state / "workspace-sync-writers", state / "workspace-sync-outcomes"
                receipts = list(root.iterdir()) if root.exists() else []
                outcomes = {}
                for path in completed.iterdir() if completed.exists() else []:
                    outcome = _read_sync_completion(path)
                    if outcome is None:
                        observed_issues.add("workspace_sync_outcome_unverified")
                        continue
                    outcomes[path.name] = outcome
                    if outcome["exit"] != 0:
                        observed_issues.add("workspace_sync_failed")
                for receipt in receipts:
                    outcome = outcomes.get(receipt.name)
                    if outcome is None:
                        pending += 1
                        continue
                    try:
                        registration = json.loads(receipt.read_text())
                    except (OSError, UnicodeError, ValueError):
                        registration = None
                    if (not isinstance(registration, dict)
                            or type(registration.get("schema_version")) is not int
                            or registration["schema_version"] != 1
                            or type(registration.get("registered_at")) not in {int, float}
                            or registration.get("registered_at") != outcome["registered_at"]):
                        pending += 1
                        observed_issues.add("workspace_sync_outcome_unverified")
                # Protocol-1 hooks from before terminal receipts wrote the same
                # existing status file. A created writers directory proves a
                # registration was attempted; missing status cannot mean idle.
                status_path = state / "workspace-sync"
                if not outcomes and not receipts and (root.exists() or status_path.exists()):
                    code = _read_legacy_sync_completion(status_path)
                    if code is None:
                        observed_issues.add("workspace_sync_outcome_unverified")
                    elif code != 0:
                        observed_issues.add("workspace_sync_failed")
        except OSError as error:
            issues.append(f"workspace_sync_receipts_unreadable:{type(error).__name__}")
            break
        issues = sorted(observed_issues)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    if pending:
        issues.append("workspace_sync_workers_pending")
    return {"complete": not issues, "issues": issues, "pending": pending}


def _read_sync_completion(path: Path) -> dict | None:
    """Strictly interpret assurance metadata; no paths or CLI detail escape."""
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError):
        return None
    if (not isinstance(value, dict) or set(value) != {
            "schema_version", "registered_at", "completed_at", "exit"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["exit"]) is not int or not 0 <= value["exit"] <= 255
            or any(type(value[name]) not in {int, float} or not math.isfinite(value[name])
                   or value[name] < 0 for name in ("registered_at", "completed_at"))):
        return None
    return value


def _read_legacy_sync_completion(path: Path) -> int | None:
    """The diagnostic reader is forgiving; final assurance rejects ambiguity."""
    try:
        fields = {}
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if not separator or key not in {"at", "exit", "detail"} or key in fields:
                return None
            fields[key] = value
        code, at = int(fields["exit"]), float(fields["at"])
        if not 0 <= code <= 255 or not math.isfinite(at) or at < 0:
            return None
        return code
    except (OSError, UnicodeError, KeyError, ValueError):
        return None


def _write_npm_setup(user: User) -> None:
    """Quieten npm's post-install banners.

    A plain `npm install papaparse` in a scaffolded AppKit app ends with
    "48 vulnerabilities (1 low, 30 moderate, 15 high, 2 critical)". Every one of
    those is transitive to the template's dev toolchain rather than anything the
    attendee chose, and none is reachable from a workshop app that runs for an
    afternoon in a workspace that gets torn down. What it does reliably is alarm
    someone who has just typed their first npm command.

    This is a deliberate trade: real advisories are suppressed along with the
    noise. It holds because of the disposable workspace, and would not hold for
    anything shipped from here. `npm audit` still works on demand.
    """
    npmrc = os.path.join(user.home, ".npmrc")
    if os.path.exists(npmrc):
        return
    with open(npmrc, "w") as f:
        f.write("audit=false\nfund=false\n")


def _write_shell_setup(user: User) -> None:
    """Keep ~/.local/bin and the shared toolchain on PATH across login shells.

    Codex spawns shell commands in a login shell that sources /etc/profile,
    which resets PATH to /usr/local/bin:/usr/bin:/bin and drops the workshop
    helpers and shared CLIs unless ~/.profile and ~/.bashrc restore them.
    """
    shared_bin = os.path.join(config.shared_prefix(), "bin")
    snippet = (
        "# workshop-path:begin\n"
        f'export PATH="$HOME/.local/bin:{shared_bin}:$PATH"\n'
        "# workshop-path:end\n"
    )
    for name in (".profile", ".bashrc"):
        path = os.path.join(user.home, name)
        try:
            existing = open(path).read() if os.path.exists(path) else ""
        except OSError:
            existing = ""
        if "# workshop-path:begin" in existing:
            continue
        _atomic_write(path, (existing.rstrip() + "\n\n" + snippet).lstrip(), 0o644)


def workspace_sync_status_path(user: User) -> str:
    return os.path.join(user.home, ".workshop", "workspace-sync")


def workspace_sync_status_for_email(email: str) -> dict:
    """The sync record for an attendee who may not be in the registry.

    The registry is process-local, so after a restart it is empty until the
    attendee's next request -- while the record itself is on ``DATA_ROOT`` and
    outlived the container. Reading it through the registry would report the
    reassuring "nothing committed yet" over the top of a recorded failure, in
    the one window where an operator is most likely to be looking.

    A home path is a pure function of the email, so this needs no user object
    and creates nothing.
    """
    home = os.path.join(config.users_root(), email_slug(email))
    return _read_workspace_sync(os.path.join(home, ".workshop", "workspace-sync"))


def workspace_sync_status(user: User) -> dict:
    return _read_workspace_sync(workspace_sync_status_path(user))


def _read_workspace_sync(path: str) -> dict:
    """What the last post-commit sync did, for operators rather than attendees.

    ``never`` is not a fault: an attendee who has not committed yet has nothing
    to sync, and reporting that as a failure would train operators to ignore the
    field on exactly the instances where it later matters.

    Parsed defensively on purpose. The record is written by a shell hook running
    on the far side of a network call, so a truncated, empty or half-written file
    is a normal outcome rather than an exceptional one, and none of those should
    be able to take down the readiness endpoint that reports them.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            raw = handle.read(4096)
    except OSError:
        return {"state": "never", "at": None, "exit": None, "detail": ""}

    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields.setdefault(key.strip(), value)

    try:
        code = int(fields.get("exit", ""))
    except ValueError:
        return {"state": "never", "at": None, "exit": None, "detail": ""}
    try:
        at = float(fields.get("at", ""))
    except ValueError:
        at = None

    detail = "".join(
        char for char in fields.get("detail", "") if char.isprintable()
    ).strip()[:240]
    return {
        "state": "ok" if code == 0 else "failed",
        "at": at,
        "exit": code,
        "detail": "" if code == 0 else detail,
    }


def _write_git_setup(user: User) -> None:
    hooks_dir = os.path.join(user.home, ".githooks")
    os.makedirs(hooks_dir, exist_ok=True)

    display = user.email.split("@")[0].replace(".", " ").title()
    gitconfig = os.path.join(user.home, ".gitconfig")
    # Refresh workshop-owned keys while preserving attendee aliases/settings.
    for key, value in (("user.email", user.email), ("user.name", display),
                       ("core.hooksPath", hooks_dir), ("init.defaultBranch", "main")):
        subprocess.run(["git", "config", "--file", gitconfig, "--replace-all", key, value],
                       check=True, capture_output=True, timeout=5)

    post_commit = os.path.join(hooks_dir, "post-commit")
    with open(post_commit, "w") as f:
        f.write(
            _POST_COMMIT.replace("{email}", user.email).replace(
                "{status_path}", workspace_sync_status_path(user)
            )
        )
    os.chmod(post_commit, 0o755)
