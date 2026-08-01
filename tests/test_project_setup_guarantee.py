"""Project setup can't be allowed to eat the project's own memory.

`databricks apps init` always creates a subdirectory named after the app, and it
refuses to write into a directory that already exists. Those two behaviours
together mean an agent that creates the project first can only scaffold by
nesting and then flattening with `mv` — and that `mv` replaces the committed
CLAUDE.md, which is where the workshop's rules live. It happened in a live run:
the project memory was overwritten by the scaffold's generic file and recovered
by hand from git history.

`workshop-init-project --appkit` removes the trap by doing both steps itself.
That only helps if every surface an agent might read says so, and none of them
still shows the old order — hence these assertions. The behaviour of the helper
is covered in test_user_content.py; this file pins the instructions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = ROOT / "assets" / "instructions" / "CLAUDE.md"
PROJECT_MEMORY = ROOT / "assets" / "instructions" / "project_memory.md"
BUILD_PROMPT = ROOT / "assets" / "skills" / "promote" / "build-prompt.md"

AGENT_FACING = (INSTRUCTIONS, PROJECT_MEMORY, BUILD_PROMPT)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing agent-facing file: {path}"
    return path.read_text()


@pytest.mark.parametrize("path", AGENT_FACING, ids=lambda p: p.name)
def test_no_surface_still_tells_an_agent_to_scaffold_by_hand(path):
    """The specific instruction that produced the clobber.

    A bare `databricks apps init` is the failure: it is the only step that can
    nest, and nesting is the only reason to run the `mv` that overwrites
    CLAUDE.md. Mentioning the command while telling the agent *not* to call it
    is fine; issuing it as a step is not.
    """
    body = _read(path)

    for line in body.splitlines():
        stripped = line.strip().lstrip("$#- ").strip()
        if stripped.startswith("databricks apps init"):
            pytest.fail(
                f"{path.name} runs `databricks apps init` in a command block: "
                f"{stripped!r} — this nests into <name>/<name> and the manual "
                "flatten overwrites the project's CLAUDE.md"
            )

    # Prose is checked separately because the instruction that caused this was
    # inline code ("scaffold with `databricks apps init --features ...`"), which
    # no code-fence guard would ever see. Wrapping means the negation often sits
    # on the line above the mention, so match against unwrapped text.
    flat = " ".join(body.split())
    negations = ("never run", "do not run", "not call", "instead of", "does it for you")
    start = 0
    while (hit := flat.find("databricks apps init", start)) != -1:
        preceding = flat[max(0, hit - 90):hit].lower()
        assert any(n in preceding for n in negations), (
            f"{path.name} mentions `databricks apps init` without telling the "
            f"agent not to call it: ...{flat[max(0, hit - 90):hit + 60]!r}"
        )
        start = hit + 1


@pytest.mark.parametrize("path", AGENT_FACING, ids=lambda p: p.name)
def test_every_surface_points_at_the_helper_instead(path):
    body = _read(path)
    assert "workshop-init-project" in body
    assert "--appkit" in body, (
        f"{path.name} names the helper but not the flag that does the scaffold, "
        "which leaves the agent to reach for `databricks apps init` itself"
    )


def test_the_instructions_say_why_not_just_what():
    """An agent that only knows the rule will drop it the moment the rule is
    inconvenient. The reason is what survives a context squeeze."""
    body = _read(INSTRUCTIONS).lower()
    assert "never run `databricks apps init` yourself" in body
    assert "subdirectory" in body, "the nesting has to be named as the cause"
    assert "claude.md" in body, "and the cost — losing the project memory"


def test_the_deploy_step_does_not_expect_to_block():
    """A first deploy starts cold app compute and routinely outlives a
    foreground timeout. Read as a failure, that costs a pointless retry — or
    worse, an agent 'fixing' code that was already fine."""
    body = _read(INSTRUCTIONS)
    assert "databricks apps get" in body, "there must be a way to poll for state"
    assert "ACTIVE" in body, "and a stated condition to wait for"
    lowered = body.lower()
    assert "timeout on the deploy command is not a failed deploy" in lowered
