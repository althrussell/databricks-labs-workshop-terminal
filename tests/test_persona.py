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
CONTRACT = Path(__file__).resolve().parents[1] / "assets/instructions/workshop_contract.md"
LAB_COACH = ROOT / "assets" / "instructions" / "lab_coach.md"
TEAMMATE = ROOT / "assets" / "instructions" / "teammate.md"

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
        Path(home, ".workshop", "profile.json").unlink(missing_ok=True)
    except OSError:
        pass
    yield


def _provisioned_home(client, monkeypatch) -> str:
    from server import credentials, user_content
    import server.main as main
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    monkeypatch.setattr(
        credentials.credential_manager, "token", lambda: "dapi-test-token"
    )
    monkeypatch.setattr(main.install, "ready", lambda: {"claude": True})
    monkeypatch.setattr(main.agents, "launch_command", lambda _agent: ["/bin/bash"])
    resp = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)
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
    # A stated technical style is kept as vocabulary (W2), not an expertise score.
    assert "technical discussion" in _persona_block(home)


def test_changing_it_after_launching_rewrites_the_instructions(client, monkeypatch):
    """The persona is inlined into the instructions, so a later change has to
    rewrite them — otherwise the agent keeps reading a stale line."""
    home = _provisioned_home(client, monkeypatch)
    assert "technical discussion" not in _persona_block(home)
    assert "(default, not chosen yet: offer the three ways when you meet them)" in _persona_block(home)

    resp = client.post("/api/persona", json={"persona": "technical"}, headers=ALICE)

    assert resp.status_code == 200
    assert "technical discussion" in _persona_block(home)


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

    text = " ".join(text.split())
    assert "How they want to work with you" in text
    assert "never guess them from their role, jargon or message length" in text


def test_the_agent_asks_how_they_want_to_work_once_and_never_infers(client, monkeypatch):
    """The review requires direct conversation to collect the same choices as the
    wizard when it is skipped: ask once, save, never infer, never ask again."""
    text = " ".join(_instructions(_provisioned_home(client, monkeypatch)).split())

    for choice in ("Build with me (I explain as I go)", "Teach me as we go (you try bits yourself)",
                   "Let me drive (you decide, I build)",
                   "Business or domain · Data and SQL · Software developer · Just exploring",
                   "**Industry:**"):
        assert choice in text
    assert "workshop-workflow prefs --approach co_build|learn|lead --background" in text
    assert "--industry <industry>" in text
    assert "Never ask them again on resume, follow-up or agent switch" in text


def test_the_coach_no_longer_runs_a_persona_question(client, monkeypatch):
    """Meeting them is three short questions in the contract, not a coach questionnaire."""
    coach = " ".join(LAB_COACH.read_text(encoding="utf-8").split())
    teammate = " ".join(TEAMMATE.read_text(encoding="utf-8").split())

    assert "~/.workshop/persona" not in coach
    assert "AskUserQuestion" not in coach
    assert "AskUserQuestion" in teammate
    assert "Check for a saved persona" not in coach
    assert "These add to being their build partner" in coach


def test_how_they_are_helped_follows_where_they_start_and_what_they_ask():
    teammate = " ".join(TEAMMATE.read_text(encoding="utf-8").split())

    assert "Fit where they're starting from." in teammate
    assert "Presets, never limits:" in teammate and "in-the-moment requests win" in teammate


def test_a_change_in_how_they_talk_is_followed_silently():
    teammate = " ".join(TEAMMATE.read_text(encoding="utf-8").split())

    assert "just change how you explain; don't make them confirm it" in teammate


# --- the first turn points somewhere useful ------------------------------------


def test_the_attendee_is_no_longer_told_to_say_hi():
    """"Say hi" spends the first turn on a greeting the attendee has to type; the
    workshop opener starts the conversation instead."""
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    pack = (ROOT / "content" / "default_pack.json").read_text(encoding="utf-8")

    assert "just say <strong>hi</strong>" not in app
    assert "Say hi to begin" not in pack


def test_no_banner_types_a_canned_idea_prompt():
    """The coach banner and its "two small demo ideas" prompt competed with the
    instructions. The agent meets the attendee and offers ideas itself."""
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "STARTER_PROMPT" not in app
    assert "coach-hint" not in app
    assert "When the prompt appears" not in app


def test_preferences_work_when_onboarding_is_disabled(client, monkeypatch):
    from server import config
    monkeypatch.setattr(config, "onboarding_wizard_enabled", lambda: False)
    initial = client.get("/api/profile", headers=ALICE).json()
    assert initial["help_preference"] == ""
    saved = client.post("/api/profile", headers=ALICE, json={"expected_revision": initial["revision"], "help_preference": "concise"})
    assert saved.status_code == 200
    assert saved.json()["source"] == "attendee"
    assert client.get("/api/wizard", headers=ALICE).json()["should_show"] is False
