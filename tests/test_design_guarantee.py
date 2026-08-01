"""Every attendee leaves with something that looks designed, and never knew it.

Two promises hold this together, and both are enforced only by text an agent
reads — nothing at runtime checks either one:

1. **Every UI gets the design treatment.** Not a suggestion an agent can skip
   when it is in a hurry to make a test pass.
2. **The attendee is never asked to participate in it.** They came to build
   something, not to choose a colour palette. The skill generates three creative
   directions and presents none of them; an agent that asks "which of these do
   you prefer?" has handed a non-designer homework and stalled the session.

The second promise is the fragile one. The upstream skill this was adapted from
asks the user to select a direction, so the natural pull of the source material
is back towards a question. These tests pin the difference across every surface
that has to agree, because the surfaces are read by different agents: Claude
reads the home instructions, Codex and Omnigent's worktree worker read the
project memory, subagents read their own policy files, and only Claude Code ever
loads SKILL.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "assets" / "skills" / "workshop-design-studio" / "SKILL.md"
INSTRUCTIONS = ROOT / "assets" / "instructions" / "CLAUDE.md"
PROJECT_MEMORY = ROOT / "assets" / "instructions" / "project_memory.md"
LAB_COACH = ROOT / "assets" / "instructions" / "lab_coach.md"
PROMOTE = ROOT / "assets" / "skills" / "promote" / "SKILL.md"
GATE_HELPER = ROOT / "assets" / "bin" / "workshop-design-gate"


def _read(path: Path) -> str:
    """Read with whitespace collapsed.

    Every file here is wrapped prose, so a phrase this suite pins can land
    across a line break at any time. Matching the wrapped form instead would
    make these tests fail on a reflow and pass on a deletion — exactly backwards
    for a guard test.
    """
    return " ".join(path.read_text(encoding="utf-8").split())


@pytest.fixture(scope="module")
def skill() -> str:
    return _read(SKILL)


@pytest.fixture(scope="module")
def instructions() -> str:
    return _read(INSTRUCTIONS)


@pytest.fixture(scope="module")
def project_memory() -> str:
    return _read(PROJECT_MEMORY)


@pytest.fixture(scope="module")
def lab_coach() -> str:
    return _read(LAB_COACH)


# --- the skill is reachable at all --------------------------------------------


def test_the_skill_ships_with_everything_it_needs():
    directory = SKILL.parent
    for relative in (
        "SKILL.md",
        "NOTICE.md",
        "scripts/audit_project.py",
        "scripts/quality_gate.py",
        "scripts/generate_design_system.py",
        "templates/quality-gate.json",
        "references/workshop-fast-path.md",
    ):
        assert (directory / relative).is_file(), relative


def test_the_description_fires_on_a_plain_build_request(skill: str):
    """Skills are selected from the description alone. An attendee says "build me
    a page to track orders" and never says "design", so a description written in
    designer vocabulary would leave this skill unloaded for the exact request it
    exists to serve."""
    description = skill.split("description:", 1)[1].split("\n", 1)[0].lower()

    assert "web app" in description
    assert "dashboard" in description
    for phrase in ("mandatory", "any interface"):
        assert phrase in description


def test_the_skill_stays_out_of_backend_only_work(skill: str):
    """Mandatory for every UI, but a pipeline or a Lakebase migration has no
    interface to design — firing there would burn workshop time."""
    description = skill.split("description:", 1)[1].split("\n", 1)[0].lower()

    assert "skip only for" in description
    assert "non-visual" in description


# --- it runs without involving the attendee -----------------------------------


def test_the_skill_forbids_asking_a_design_question(skill: str):
    assert "Never ask the attendee a design question" in skill
    assert "Generate three directions internally, present none" in skill


def test_the_skill_forbids_narrating_its_own_process(skill: str):
    """Naming the machinery is its own failure: an attendee who hears "creative
    direction" and "critique pass" now believes design was the hard part, and
    starts supervising it."""
    assert "Never narrate the process" in skill
    assert "never how it was made" in skill


def test_design_never_becomes_a_visible_checkpoint(skill: str):
    assert "Never let design become a blocker they can see" in skill
    assert "If you need a decision, make it." in skill


def test_an_attendee_who_raises_design_is_answered_properly(skill: str):
    """Silence is the default, not a gag. Someone who brings a brand kit is
    asking for a design conversation and should get one."""
    assert "The one exception" in skill
    assert "supplies a brand kit" in skill


def test_the_lab_coach_carries_the_same_silence_rule(lab_coach: str):
    """The coach overlay is what shapes the attendee-facing voice — the rule has
    to exist here too or it only applies to code, not conversation."""
    assert "Never ask a design question" in lab_coach
    assert "Never narrate the design process" in lab_coach
    assert "quietly amazed" in lab_coach


def test_the_coach_translates_gate_failures_out_of_jargon(lab_coach: str):
    assert "WCAG AA contrast finding" in lab_coach, (
        "the rule needs the jargon example it is banning, or it reads as vague"
    )


def test_the_project_memory_carries_the_silence_rule(project_memory: str):
    """Omnigent's Codex worker runs in an isolated worktree and reads only the
    committed project-level file — this is its single copy of the rule."""
    assert "Never ask the user a design question" in project_memory
    assert "never narrate the process" in project_memory


# --- the attendee's app is theirs, not the platform's --------------------------


@pytest.mark.parametrize(
    "text_fixture, phrase",
    [
        ("skill", "the deployment platform is not the brand"),
        ("lab_coach", "their app is not a databricks app"),
        ("project_memory", "do not impose databricks styling on it"),
        ("instructions", "the platform is not the brand"),
    ],
)
def test_databricks_styling_is_not_imposed_on_an_attendee_app(
    text_fixture: str, phrase: str, request: pytest.FixtureRequest
):
    """A workshop full of identical console-grey apps is the obvious failure of
    a design skill that ships inside a Databricks product."""
    assert phrase in request.getfixturevalue(text_fixture).lower()


def test_the_skill_says_the_platform_is_a_deployment_fact(skill: str):
    assert "The deployment platform is not the brand" in skill
    assert "deployment fact, not an art direction" in skill


# --- every agent that writes UI is covered ------------------------------------


def test_the_home_instructions_require_the_skill_for_any_interface(
    instructions: str,
):
    assert "workshop-design-studio" in instructions
    assert "anything with a visible interface" in instructions


def test_the_skills_table_lists_it_under_apps():
    """The table is the index an agent scans first; a skill missing from it gets
    reached for late or not at all. Read raw here — the row is one line, and the
    point is that it is on the Apps row specifically."""
    raw = INSTRUCTIONS.read_text(encoding="utf-8")

    row = next(line for line in raw.splitlines() if line.startswith("| Apps"))
    assert "workshop-design-studio" in row


def test_the_project_memory_requires_it_too(project_memory: str):
    assert "workshop-design-studio" in project_memory
    assert "visible interface, every time" in project_memory


@pytest.mark.parametrize("agent", ["implementer.md", "build-feature.md"])
def test_subagents_that_write_ui_inherit_the_mandate(agent: str):
    """Subagents do most of the actual UI writing and never read the home
    instructions. An implementer optimising purely for green tests will happily
    ship an unstyled component."""
    text = _read(ROOT / "assets" / "agents" / agent)

    assert "workshop-design-studio" in text
    assert "workshop-design-gate" in text
    assert "design question" in text


def test_the_division_of_labour_with_the_other_app_skills_is_stated(
    instructions: str,
):
    """Three skills touch the same screen. Without a stated split, the agent
    either applies none of them or lets component choice dictate composition."""
    assert "composition the design studio wins" in instructions
    assert "databricks-app-design" in instructions


# --- the gate actually blocks --------------------------------------------------


def test_the_gate_helper_is_installed_onto_the_attendee_path():
    """The instructions call `workshop-design-gate` as a bare command, so it has
    to be copied into ~/.local/bin like the other helpers — and made executable
    there, since repo file modes do not survive a checkout reliably."""
    source = (ROOT / "server" / "user_content.py").read_text(encoding="utf-8")

    assert GATE_HELPER.is_file()
    assert '"workshop-design-gate"' in source
    assert "os.chmod(dst, 0o755)" in source


def test_the_gate_helper_is_harness_neutral():
    """assets/instructions/CLAUDE.md is written verbatim to ~/.codex/AGENTS.md,
    so a Claude-specific skills path in the instructions would be a broken
    command for every Codex attendee. The helper resolves it instead."""
    text = _read(GATE_HELPER)

    assert ".claude/skills/workshop-design-studio" in text
    assert ".codex/skills/workshop-design-studio" in text


def test_the_gate_helper_fails_loudly_rather_than_silently_passing():
    text = _read(GATE_HELPER)

    assert 'exit "$status"' in text
    assert "not finished" in text


def test_a_missing_skill_does_not_wedge_the_build():
    """If the skill is somehow absent, the gate must not become an impassable
    wall between an attendee and their finished app."""
    text = _read(GATE_HELPER)

    assert "skipping the visual gate" in text
    assert "exit 0" in text


@pytest.mark.parametrize(
    "path", [INSTRUCTIONS, PROJECT_MEMORY, LAB_COACH], ids=["claude", "project", "coach"]
)
def test_every_memory_channel_runs_the_gate_before_declaring_done(path: Path):
    assert "workshop-design-gate" in _read(path)


@pytest.mark.parametrize(
    "path", [INSTRUCTIONS, PROJECT_MEMORY], ids=["claude", "project"]
)
def test_a_red_gate_blocks_success_and_the_promote_offer(path: Path):
    text = _read(path)

    assert "either gate is red" in text
    assert "Promote" in text


def test_wrap_up_promote_survives_a_red_gate(instructions: str):
    """The gate blocks *offering* promote after a build. It must never block the
    wrap-up run, or a session that ended on a failing gate — the one whose notes
    are most useful to whoever picks the work up — leaves with nothing."""
    assert "never blocks promote at wrap-up" in instructions
    assert "Wrap-up promote is unconditional" in instructions


def test_the_visual_checks_extend_the_existing_smoke_spec(instructions: str):
    """A second Playwright config is a second thing to keep green in a 60-minute
    workshop, and `databricks apps validate` would not run it anyway."""
    assert "playwright.visual.spec.ts" in instructions
    assert "one spec, one gate" in instructions


def test_the_visual_spec_template_needs_no_committed_baseline():
    """Playwright fails a screenshot assertion the first time it runs, because
    no baseline exists yet. Shipping one here would turn the gate red for every
    attendee on their first validate."""
    text = _read(SKILL.parent / "templates" / "playwright.visual.spec.ts")

    assert "expect(page).toHaveScreenshot" not in text
    assert "no baseline exists" in text


# --- the design work survives the teardown ------------------------------------


def test_promote_carries_the_design_record_into_the_handoff():
    """The reasoning was deliberately kept out of the conversation, so these
    files are the only record of why the app looks the way it does — and the
    environment is deleted at the end of the workshop."""
    text = _read(PROMOTE)

    assert ".design-studio" in text
    assert "creative-brief.md" in text
    assert "design-system.json" in text


def test_the_rebuild_prompt_can_reproduce_the_look():
    """A build prompt listing only components rebuilds a framework starter."""
    text = _read(ROOT / "assets" / "skills" / "promote" / "build-prompt.md")

    assert ".design-studio" in text
    assert "requirements, not suggestions" in text


def test_attribution_for_the_upstream_work_is_preserved():
    notice = _read(SKILL.parent / "NOTICE.md")

    assert "MIT License" in notice
    assert "ui-ux-pro-max-skill" in notice
