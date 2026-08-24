"""The agent is told who it is talking to before it says anything.

Working out whether an attendee is technical or business-oriented used to be
the agent's first job, and it was the slowest possible way to start a workshop:
read a file, ask an interactive question, wait for the answer, write the file —
several round trips before anything the attendee actually came for began. Worse,
the question landed on someone who had just been told to type "hi" and had no
idea why a machine wanted to categorise them.

The answer is now collected on the landing page while they are still reading it,
seeded into their home, and inlined into the instructions the agent loads at
startup. These tests hold that path together: the file, the inlined line, both
orderings of choose-then-launch, and the instructions no longer telling the
agent to ask.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .conftest import ALICE

ROOT = Path(__file__).resolve().parents[1]
LAB_COACH = ROOT / "assets" / "instructions" / "lab_coach.md"

PERSONA_RELATIVE = os.path.join(".workshop", "persona")


@pytest.fixture(autouse=True)
def _no_persona_yet(_test_env):
    """Start every test from an attendee who has not chosen.

    The persona lives in the attendee's home, which outlives a single test —
    without this, a test that sets "technical" silently changes the starting
    conditions of the next one, and the default-seeding tests pass or fail
    depending on file order.

    The home is derived from the email rather than looked up in the registry,
    which is process-wide, reset between tests, and empty before the attendee's
    first request. Asking it where the home is made the reset conditional on
    state this fixture exists to be independent of.
    """
    from server import config
    from server.users import email_slug

    home = os.path.join(config.users_root(), email_slug("alice@example.com"))
    try:
        os.remove(os.path.join(home, PERSONA_RELATIVE))
    except OSError:
        pass
    yield


def _provisioned_home(client, monkeypatch) -> str:
    from server import user_content
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    user_content._provisioned.discard("alice@example.com")
    resp = client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    assert resp.status_code == 200
    return user_manager.get("alice@example.com").home


def _persona_file(home: str) -> str:
    with open(os.path.join(home, PERSONA_RELATIVE)) as f:
        return f.read().strip()


def _instructions(home: str) -> str:
    with open(os.path.join(home, ".claude", "CLAUDE.md")) as f:
        return f.read()


def _persona_block(home: str) -> str:
    """Just the inlined persona section.

    The coach prose discusses both personas by name, so a whole-file search
    would match text that has nothing to do with this attendee's setting.
    """
    return _instructions(home).split("<!-- workshop-persona -->", 1)[1]


# --- the file is always there -------------------------------------------------


def test_provisioning_seeds_a_persona_so_the_agent_never_finds_it_missing(
    client, monkeypatch
):
    home = _provisioned_home(client, monkeypatch)

    assert _persona_file(home) == "business"


def test_the_default_assumes_the_attendee_is_not_an_engineer(client, monkeypatch):
    """Whoever skips the control is likelier to be non-technical — engineers are
    the ones who notice it. It is also the cheaper mistake: jargon loses a
    business attendee, while plain language merely under-serves an engineer."""
    from server import user_content

    assert user_content.DEFAULT_PERSONA == "business"
    home = _provisioned_home(client, monkeypatch)
    assert _persona_file(home) == "business"


# --- the endpoint, in both orderings ------------------------------------------


@pytest.mark.parametrize("persona", ["technical", "business"])
def test_choosing_on_the_landing_page_records_the_choice(
    client, monkeypatch, persona: str
):
    """This is the ordering that matters most: the attendee picks before ever
    launching a session, so nothing has been provisioned yet."""
    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    from server.users import user_manager

    resp = client.post("/api/persona", json={"persona": persona}, headers=ALICE)

    assert resp.status_code == 200
    assert resp.json() == {"persona": persona}
    assert _persona_file(user_manager.get("alice@example.com").home) == persona


def test_a_choice_made_before_launching_survives_provisioning(client, monkeypatch):
    """Provisioning must not stamp the default over a real answer."""
    client.post("/api/persona", json={"persona": "technical"}, headers=ALICE)

    home = _provisioned_home(client, monkeypatch)

    assert _persona_file(home) == "technical"
    assert "**technical**" in _persona_block(home)


def test_changing_it_after_launching_rewrites_the_instructions(client, monkeypatch):
    """The persona is inlined into the instructions, so a later change has to
    rewrite them — otherwise the agent keeps reading a stale line."""
    home = _provisioned_home(client, monkeypatch)
    assert "business-oriented" in _persona_block(home)

    resp = client.post("/api/persona", json={"persona": "technical"}, headers=ALICE)

    assert resp.status_code == 200
    assert "**technical**" in _persona_block(home)
    assert "business-oriented" not in _persona_block(home)


def test_a_nonsense_persona_is_rejected_rather_than_written(client, monkeypatch):
    """A bad value must not reach the file the agent's instructions are built
    from — an unrecognised persona there would render as a nonsense line in the
    system prompt."""
    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    from server.users import user_manager

    client.post("/api/persona", json={"persona": "technical"}, headers=ALICE)

    resp = client.post("/api/persona", json={"persona": "wizard"}, headers=ALICE)

    assert resp.status_code == 400
    assert _persona_file(user_manager.get("alice@example.com").home) == "technical"


# --- the agent is told, not asked to look -------------------------------------


@pytest.mark.parametrize("harness", [".claude/CLAUDE.md", ".codex/AGENTS.md"])
def test_both_harnesses_are_told_who_they_are_working_with(
    client, monkeypatch, harness: str
):
    home = _provisioned_home(client, monkeypatch)

    with open(os.path.join(home, *harness.split("/"))) as f:
        text = f.read()

    assert "Who you are working with" in text
    assert "never ask them whether they are technical or business" in text


def test_the_instructions_forbid_reading_it_from_a_file(client, monkeypatch):
    """A file read is a tool call, and on the first turn that call is the
    difference between an agent that starts building and one doing admin."""
    home = _provisioned_home(client, monkeypatch)

    assert "never read it from a file" in _instructions(home)


def test_the_coach_no_longer_runs_a_persona_question(client, monkeypatch):
    """The whole point. If this text comes back, so does the slow first turn."""
    coach = " ".join(LAB_COACH.read_text(encoding="utf-8").split())

    assert "~/.workshop/persona" not in coach
    assert "AskUserQuestion" not in coach
    assert "Check for a saved persona" not in coach
    assert "Never ask them which they are" in coach


def test_the_coach_still_adapts_its_language_to_the_persona():
    """Removing the question must not remove the reason for having an answer."""
    coach = " ".join(LAB_COACH.read_text(encoding="utf-8").split())

    assert "Business persona" in coach
    assert "Technical persona" in coach


def test_a_wrong_guess_is_corrected_silently():
    coach = " ".join(LAB_COACH.read_text(encoding="utf-8").split())

    assert "just change how you explain things" in coach
    assert "do not confirm it with them" in coach


# --- the first turn points somewhere useful ------------------------------------


def test_the_attendee_is_no_longer_told_to_say_hi():
    """"Say hi" spends the first turn on a greeting, and greetings are exactly
    the input the coach is told NOT to build from."""
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    pack = (ROOT / "content" / "default_pack.json").read_text(encoding="utf-8")

    assert "just say <strong>hi</strong>" not in app
    assert "Say hi to begin" not in pack


def test_the_hint_offers_a_real_build_instead():
    """An attendee facing an empty prompt needs a way through that is not a
    greeting. The starter has to be a concrete build request, because that is
    what triggers the coach's build-immediately path."""
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "STARTER_PROMPT" in app
    assert "Build me something real" in app
    assert "tell it what you'd like to build" in app.lower()


def test_the_ui_asks_before_the_session_starts():
    """Asked in the UI while they read the page, it costs nothing. Asked by the
    agent, it costs the first turn.

    The question moved from a standalone toggle on the landing page into the
    wizard's context step, where it sits beside the other optional questions
    instead of being the only thing on Home that looks like a form. The guarantee
    is unchanged: something other than the agent asks it, before any session.
    """
    wizard = (ROOT / "frontend" / "src" / "components" / "Wizard.tsx").read_text(
        encoding="utf-8"
    )

    assert 'setPersona("business")' in wizard
    assert 'setPersona("technical")' in wizard
    # Framed as how things get explained, never as a profile field about them.
    assert "How should your agent explain things?" in wizard
    assert "Plain language" in wizard


def test_choosing_is_optional():
    """A required choice would be a gate in front of the workshop — the server
    defaults instead, so an attendee can ignore it entirely.

    Two ways out, both of which must leave the persona unset: Skip, and simply
    not touching the chips before Next.
    """
    wizard = (ROOT / "frontend" / "src" / "components" / "Wizard.tsx").read_text(
        encoding="utf-8"
    )

    assert "disabled={!persona}" not in wizard
    # Continuing is gated on having said what they are building and which
    # industry they are in, never on this. Matched as the whole expression
    # rather than one line of it, because a persona term added anywhere in it
    # is the regression this test exists to catch.
    can_continue = wizard.split("const canContinue =", 1)[1].split(";", 1)[0]
    assert 'what.trim().length > 0 || ideaId !== ""' in can_continue
    assert "persona" not in can_continue
    assert 'const [persona, setPersona] = useState("")' in wizard


def test_the_landing_page_no_longer_carries_its_own_picker():
    """One place to answer it, not two that can disagree.

    Home used to own a persona toggle. Leaving it there alongside the wizard's
    would let an attendee set it twice and see the second answer silently
    overwrite the first — with no indication which one the agent got.
    """
    hero = (ROOT / "frontend" / "src" / "components" / "Hero.tsx").read_text(
        encoding="utf-8"
    )

    assert "hero-persona" not in hero
    assert "api.setPersona" not in hero
