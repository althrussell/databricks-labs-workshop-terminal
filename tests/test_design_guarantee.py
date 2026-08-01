"""Every attendee leaves with something that looks designed, and never knew it.

Three promises hold this together, and all of them are enforced only by text an
agent reads — nothing at runtime checks any of them any more:

1. **Every UI gets the design treatment.** Not a suggestion an agent can skip
   when it is in a hurry.
2. **The attendee is never asked to participate in it.** They came to build
   something, not to choose a colour palette.
3. **The quality bar is concrete and always in context.** The scripted design
   gate that used to enforce contrast, focus states, and alt text was deleted
   along with the rest of the six-phase pipeline, because it cost minutes of a
   short workshop before the attendee saw anything. What replaced it is a
   written baseline in the always-loaded instructions plus a library of verified
   AppKit patterns. If the baseline stops being specific, nothing catches it —
   which is exactly why these assertions are worth their weight.

These are pinned across every surface that has to agree, because the surfaces
are read by different agents: Claude reads the home instructions, Codex and
Omnigent's worktree worker read the project memory, and only Claude Code ever
loads SKILL.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "assets" / "skills" / "workshop-design-studio"
SKILL = SKILL_DIR / "SKILL.md"
PATTERNS = SKILL_DIR / "references" / "patterns"
INSTRUCTIONS = ROOT / "assets" / "instructions" / "CLAUDE.md"
PROJECT_MEMORY = ROOT / "assets" / "instructions" / "project_memory.md"
LAB_COACH = ROOT / "assets" / "instructions" / "lab_coach.md"
PROMOTE = ROOT / "assets" / "skills" / "promote" / "SKILL.md"


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
    for relative in (
        "SKILL.md",
        "NOTICE.md",
        "references/patterns/README.md",
        "references/composition-and-layout.md",
        "references/typography.md",
        "references/colour-and-tokens.md",
        "references/motion-and-states.md",
    ):
        assert (SKILL_DIR / relative).is_file(), relative


def test_the_deleted_machinery_stays_deleted():
    """The six-phase pipeline is what made design cost minutes before the
    attendee saw anything. Re-adding a script or a persisted artifact directory
    would quietly restore the wait this workshop exists to remove."""
    for gone in ("scripts", "data", "templates"):
        assert not (SKILL_DIR / gone).exists(), f"{gone}/ came back"

    text = _read(SKILL)
    for phrase in ("generate_design_system", ".design-studio", "workshop-design-gate"):
        assert phrase not in text, phrase


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
    assert "present none" in skill


def test_the_skill_forbids_narrating_its_own_process(skill: str):
    """Naming the machinery is its own failure: an attendee who hears "design
    system" and "critique pass" now believes design was the hard part, and
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


def test_the_coach_translates_visual_fixes_out_of_jargon(lab_coach: str):
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


# --- the boundary between the two design skills -------------------------------


@pytest.mark.parametrize(
    "path", [INSTRUCTIONS, PROJECT_MEMORY], ids=["claude", "project"]
)
def test_the_boundary_between_the_design_skills_is_by_surface(path: Path):
    """Two skills touch the same screen and both used to claim charts. The split
    is now stated by surface, in the files that are always in context, so an
    agent never has to arbitrate between two chart vocabularies mid-build."""
    text = _read(path)

    assert "the split is by surface" in text.lower()
    assert "databricks-app-design" in text
    assert "chart-vocabulary conflict" in text.lower()


@pytest.mark.parametrize(
    "path", [INSTRUCTIONS, PROJECT_MEMORY], ids=["claude", "project"]
)
def test_an_app_with_no_data_surface_uses_the_studio_alone(path: Path):
    """Space Invaders is still a Databricks App, so it still gets the baseline —
    but `databricks-app-design` has nothing to say about a canvas game and must
    not fire for one."""
    text = _read(path).lower()

    assert "no data surface" in text
    assert "design studio only" in text or "the design studio only" in text


def test_the_skill_defers_chart_choice_rather_than_duplicating_it(skill: str):
    """The old `data-visualisation.md` was a second, weaker chart picker. It is
    gone; this asserts the skill hands the decision over instead of re-inventing
    it."""
    assert "wins outright" in skill
    assert not (SKILL_DIR / "references" / "data-visualisation.md").exists()


# --- the quality bar that replaced the gate -----------------------------------


@pytest.mark.parametrize(
    "path", [INSTRUCTIONS, PROJECT_MEMORY], ids=["claude", "project"]
)
def test_the_visual_baseline_is_written_where_it_is_always_in_context(path: Path):
    """"Apply good defaults" is not self-executing. An agent under tempo pressure
    will not open a reference file, so the baseline has to be specific and it has
    to live in the file that is always loaded."""
    text = _read(path).lower()

    for phrase in (
        "type does the hierarchy",
        "one accent colour, used for meaning",
        "focal point",
        "motion on state change",
    ):
        assert phrase in text, phrase


@pytest.mark.parametrize(
    "path", [INSTRUCTIONS, PROJECT_MEMORY], ids=["claude", "project"]
)
def test_accessibility_survived_the_deletion_of_the_gate(path: Path):
    """Contrast, focus, and alt text were enforced only by the scripted gate.
    Deleting it without moving them into the build-time defaults would have
    silently dropped the only accessibility floor the workshop has."""
    text = _read(path).lower()

    assert "4.5:1" in text
    assert "focus state" in text
    assert "alt text" in text


@pytest.mark.parametrize(
    "path", [INSTRUCTIONS, PROJECT_MEMORY, LAB_COACH], ids=["claude", "project", "coach"]
)
def test_no_memory_channel_still_gates_the_url_on_a_design_script(path: Path):
    text = _read(path)

    assert "workshop-design-gate" not in text
    assert ".design-studio" not in text


def test_the_self_critique_replaces_the_scripted_passes(skill: str, instructions: str):
    """One pass, in context, after the URL is already live — the judgement
    without the ceremony. It must be explicitly *after* the deploy, or it
    becomes another thing standing between the attendee and their app."""
    assert "self-critique" in skill.lower()
    assert "after the first deploy" in instructions.lower()
    assert "not a script" in instructions.lower()


# --- the patterns are real ----------------------------------------------------


def test_every_promised_pattern_exists():
    """A pattern library that is missing the pattern an agent reaches for sends
    it back to inventing layout under time pressure, which is the failure this
    library exists to prevent."""
    for name in (
        "app-shell.tsx",
        "hero-first-run.tsx",
        "kpi-row.tsx",
        "chart-card.tsx",
        "data-table.tsx",
        "states.tsx",
        "form.tsx",
    ):
        assert (PATTERNS / name).is_file(), name


def test_the_patterns_only_import_from_the_published_appkit_package():
    """A pattern citing a component AppKit does not ship is worse than no
    pattern: it fails `tsc` in front of the attendee and teaches the agent a
    component name that does not exist. Every import here is verified against
    the published package."""
    import re

    allowed = {"react", "@databricks/appkit-ui/react", "lucide-react"}
    offenders = []
    for pattern in PATTERNS.glob("*.tsx"):
        for module in re.findall(
            r"^import[^'\"]*from\s+['\"]([^'\"]+)['\"]",
            pattern.read_text(encoding="utf-8"),
            re.MULTILINE,
        ):
            if module not in allowed:
                offenders.append(f"{pattern.name}: {module}")
    assert not offenders, "unexpected imports:\n" + "\n".join(offenders)


def test_the_patterns_never_hardcode_raw_colour():
    """Raw hex and raw Tailwind palette utilities bypass the AppKit theme and
    break dark mode. The patterns are the thing agents copy, so a raw colour
    here propagates into every app built from them."""
    import re

    palette = re.compile(
        r"(#[0-9a-fA-F]{3,8}\b"
        r"|\b(?:bg|text|border|fill|stroke|ring)-"
        r"(?:red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|"
        r"indigo|violet|purple|fuchsia|pink|rose|slate|gray|grey|zinc|neutral|stone)-\d{2,3})"
    )
    offenders = [
        f"{pattern.name}: {match}"
        for pattern in PATTERNS.glob("*.tsx")
        for match in palette.findall(pattern.read_text(encoding="utf-8"))
    ]
    assert not offenders, "raw colour in patterns:\n" + "\n".join(
        m if isinstance(m, str) else m[0] for m in offenders
    )


def test_every_pattern_keeps_a_visible_focus_path():
    """The one accessibility rule with no downstream enforcement at all. A
    pattern that strips the focus ring makes every app copied from it unusable
    without a mouse."""
    offenders = [
        pattern.name
        for pattern in PATTERNS.glob("*.tsx")
        if "outline-none" in pattern.read_text(encoding="utf-8")
        and "focus-visible:ring" not in pattern.read_text(encoding="utf-8")
    ]
    assert not offenders, f"focus ring removed without replacement in: {offenders}"


def test_the_patterns_document_the_version_they_were_verified_against():
    """"Verified" ages. The README has to say against what, so a future failure
    reads as a version bump rather than as a broken pattern."""
    text = _read(PATTERNS / "README.md")

    assert "@databricks/appkit-ui" in text
    assert "npx @databricks/appkit docs" in text


# --- the design work survives the teardown ------------------------------------


def test_promote_carries_the_design_record_into_the_handoff():
    """The reasoning was deliberately kept out of the conversation, and there is
    no longer a `.design-studio/` folder holding it — so the build prompt is the
    only place it can survive the teardown."""
    text = _read(PROMOTE)

    assert "visual decisions live in the code" in text
    assert "build-prompt.md" in text


def test_the_rebuild_prompt_can_reproduce_the_look():
    """A build prompt listing only components rebuilds a framework starter."""
    text = _read(ROOT / "assets" / "skills" / "promote" / "build-prompt.md")

    assert "requirements, not suggestions" in text
    assert "type scale" in text


def test_attribution_for_the_upstream_work_is_preserved():
    notice = _read(SKILL_DIR / "NOTICE.md")

    assert "MIT License" in notice
    assert "ui-ux-pro-max-skill" in notice
