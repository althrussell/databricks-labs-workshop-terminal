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


def test_the_helper_drops_a_warning_whose_premise_is_false():
    """The scaffolder warns that Databricks skills are not installed and tells
    you to run `databricks aitools install`. In this terminal that is wrong —
    a curated, trimmed skill set is already linked into ~/.claude and was used
    successfully throughout the session that reported this. A warning that
    fires against its own false premise teaches people to skim past warnings,
    which is worse than not printing it.
    """
    helper = (ROOT / "assets" / "bin" / "workshop-init-project").read_text()
    assert "Databricks skills are not installed" in helper
    assert "coding agents detected without Databricks skills" in helper
    assert "grep -vF" in helper, "filtered by exact text, not a broad pattern"
    assert "PIPESTATUS" in helper, (
        "and the scaffold's own exit status must survive the pipe, or a real "
        "failure reads as success"
    )


def test_npm_is_configured_not_to_alarm_the_attendee(tmp_path):
    """`npm install <anything>` in a scaffolded app ends with a count of
    vulnerabilities including criticals, all transitive to the template's dev
    toolchain. It is the first thing a non-technical attendee sees after their
    first npm command."""
    import inspect
    from types import SimpleNamespace

    from server import user_content

    user = SimpleNamespace(home=str(tmp_path), email="alice@example.com")
    user_content._write_npm_setup(user)
    npmrc = (tmp_path / ".npmrc").read_text()
    assert "audit=false" in npmrc
    assert "fund=false" in npmrc
    assert "_write_npm_setup" in inspect.getsource(user_content.provision), (
        "the step has to actually run during provisioning"
    )


def test_an_npmrc_the_attendee_already_has_is_not_overwritten(tmp_path):
    """Provisioning is idempotent and runs on every session start. Their own
    registry or auth settings outrank our noise suppression."""
    from types import SimpleNamespace

    from server import user_content

    (tmp_path / ".npmrc").write_text("registry=https://npm.internal.example\n")
    user_content._write_npm_setup(SimpleNamespace(home=str(tmp_path), email="a@b.c"))
    assert (tmp_path / ".npmrc").read_text() == "registry=https://npm.internal.example\n"


def test_no_surface_tells_an_agent_to_run_jq():
    """`jq` is not installed in the terminal.

    A live session lost a deploy cycle to it: the agent built a
    `--source-code-path` out of `$(... | jq -r .userName)`, got
    `jq: command not found` bundled with an unrelated "app does not exist"
    error, and had to abandon the whole command. The poll command shipped in
    these instructions had the same dependency, so we were the ones teaching
    the habit.
    """
    for path in AGENT_FACING:
        for line in _read(path).splitlines():
            stripped = line.strip().lstrip("$#- ").strip()
            if stripped.startswith(("databricks ", "npx ", "npm ")) and "jq" in stripped:
                pytest.fail(
                    f"{path.name} pipes a command through jq, which is not "
                    f"installed: {stripped!r} — parse JSON with python3 -c"
                )
    body = _read(INSTRUCTIONS).lower()
    assert "not installed" in body and "jq" in body, (
        "and say so once, or an agent reaches for it out of habit"
    )


def test_the_deploy_command_is_named():
    """Naming only the *step* left the agent to invent the command.

    It guessed a hand-built `--source-code-path` form, which failed, and it
    rated that the single thing in the session a non-technical attendee could
    not have recovered from — the fix being a different command rather than a
    patch to the one they had. The bare `bundle deploy` path is worth warning
    about too: it uploads the code and leaves the app stopped with no URL,
    which looks like success until someone opens the link.
    """
    body = _read(INSTRUCTIONS)
    assert "databricks apps deploy" in body, "the ship gate must name the command"
    lowered = " ".join(body.split()).lower()
    assert "bundle deploy" in lowered, "and warn off the one that silently no-ops"
    assert "source-code-path" in lowered, "and off the form the agent invented"
