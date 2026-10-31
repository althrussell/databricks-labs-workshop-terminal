"""The agent configuration that makes each agent a workshop teammate (26 Oct 2026).

The owner tested a seat with the wizard skipped: the terminal sat silent, and
then every agent built for half an hour without asking how they wanted to work.
These tests pin the fix: the agent speaks first, meets the attendee, works in a
visible rhythm, and loads one lean set of instructions and skills.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading

import pytest

import uuid

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = ROOT / "assets" / "instructions"


@pytest.fixture
def person():
    """A fresh attendee per test: the opener is once per attendee, and other
    tests' sessions must not have used it up."""
    email = f"opener-{uuid.uuid4().hex[:8]}@example.com"
    return {"X-Forwarded-Email": email}


def _ready(monkeypatch):
    from server import credentials
    import server.main as main

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    monkeypatch.setattr(credentials.credential_manager, "token", lambda: "dapi-test-token")
    monkeypatch.setattr(main.install, "ready", lambda: {"claude": True, "codex": True, "omnigent": True})
    launched: list[str] = []
    monkeypatch.setattr(main.agents, "launch_command",
                        lambda agent: launched.append(agent["command"]) or ["/bin/bash"])
    return launched


def _open(client, person, agent="claude", **body):
    from server.sessions import session_manager

    response = client.post("/api/sessions", json={"agent_id": agent, **body}, headers=person)
    assert response.status_code == 200, response.text
    session_manager.terminate(session_manager.get(response.json()["session"]["id"], person["X-Forwarded-Email"]))
    return response


def _home(person):
    from server.users import user_manager

    return user_manager.get(person["X-Forwarded-Email"]).home


# --- the agent speaks first ------------------------------------------------------


def test_the_first_agent_opens_with_the_workshop_opener_and_only_once(client, monkeypatch, person):
    from server import agents

    launched = _ready(monkeypatch)
    _open(client, person, "claude")
    _open(client, person, "claude")

    assert launched[0] == f"claude {agents.shlex.quote(agents.WORKSHOP_OPENER)}"
    assert launched[1] == f"claude {agents.shlex.quote(agents.RETURN_OPENER)}"


@pytest.mark.parametrize("agent, expected", [
    ("claude", "claude 'Hi! I'\"'\"'ve just sat down for the workshop. Let'\"'\"'s get started.'"),
    ("codex", "codex --no-daemon 'Hi! I'\"'\"'ve just sat down for the workshop. Let'\"'\"'s get started.'"),
    ("omnigent", "workshop-omnigent"),
])
def test_each_cli_receives_the_opener_its_own_way(agent, expected):
    from server import agents

    catalog = {item["id"]: item for item in agents.load_catalog()}
    assert agents.with_first_message(catalog[agent], agents.WORKSHOP_OPENER)["command"] == expected


def test_no_opener_on_top_of_the_wizard_prompt_or_an_agent_switch(client, monkeypatch, person):
    from server import agents

    launched = _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    _open(client, person, "codex", replaces_agent_id="claude")
    _open(client, person, "claude", replaces_agent_id="codex", has_starter_prompt=True)

    assert launched == [
        "claude",
        f"codex --no-daemon {agents.shlex.quote(agents.switch_opener('Codex'))}",
        "claude",
    ]


def test_no_opener_once_they_have_said_how_they_want_to_work(client, monkeypatch, person):
    from server import agents

    launched = _ready(monkeypatch)
    response = client.post("/api/profile", json={"working_approach": "learn", "expected_revision": 0}, headers=person)
    assert response.status_code == 200, response.text
    _open(client, person, "claude")

    assert launched == [f"claude {agents.shlex.quote(agents.RETURN_OPENER)}"]


def test_two_tabs_opening_at_once_claim_the_opener_once(client, monkeypatch, person):
    from server import agents
    from server.users import user_manager

    from types import SimpleNamespace

    from server import attendee_profile

    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)  # provision the attendee's home
    user = user_manager.get(person["X-Forwarded-Email"])
    # Only the claim's own lock is under test here; the profile read is the request's.
    monkeypatch.setattr(attendee_profile, "read", lambda _user: SimpleNamespace(
        effective=lambda: {"working_approach_stated": False}))
    claims: list[str | None] = []
    threads = [threading.Thread(target=lambda: claims.append(agents.claim_opener(user))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert claims.count(agents.WORKSHOP_OPENER) == 1


def test_a_launch_that_fails_gives_the_opener_back(client, monkeypatch, person):
    import server.main as main

    launched = _ready(monkeypatch)
    calls = {"n": 0}
    real_create = main.session_manager.create

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("pty unavailable")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(main.session_manager, "create", fail_once)
    with pytest.raises(OSError):
        client.post("/api/sessions", json={"agent_id": "claude"}, headers=person)
    _open(client, person, "claude")

    assert launched[-1].startswith("claude 'Hi!")


# --- one lean set of instructions ---------------------------------------------------


def _channels(home: str) -> dict[str, str]:
    return {name: Path(home, *parts).read_text() for name, parts in {
        "claude": (".claude", "CLAUDE.md"), "codex": (".codex", "AGENTS.md"),
        "project": (".config", "workshop", "project-memory.md")}.items()}


def _words(text: str) -> int:
    return len(text.split())


def test_the_home_file_puts_who_they_are_before_the_mechanics(client, monkeypatch, person):
    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    for name in ("claude", "codex"):
        text = _channels(_home(person))[name]
        order = [text.index(marker) for marker in (
            "<!-- workshop-terminal-safety:v1 -->", "## Who you are", "### Meeting them",
            "### How you work together", "## How to build", "## Databricks essentials")]
        assert order == sorted(order), name


def test_only_the_main_session_meets_the_attendee(client, monkeypatch, person):
    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    channels = _channels(_home(person))

    assert "### Meeting them" in channels["claude"] and "### Meeting them" in channels["codex"]
    assert "Meeting them" not in channels["project"]
    assert ("If another agent handed you a task, do that task and report back to it"
            in " ".join(channels["project"].split()))


def test_standing_instructions_stay_lean(client, monkeypatch, person):
    """test1025 and run 2 loaded about 4,300 words per agent inside a project."""
    monkeypatch.setenv("LAB_COACH", "false")
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    from server import user_content

    home = _words(user_content._base_instructions())
    project = _words(user_content._project_memory())
    # Owner, 30 Oct: raise the limits as far as the desired voice needs
    # (instruction review §5 and §7: home 1,700; Claude and Codex 2,000; project 430, template 360).
    assert home <= 1700, home
    assert project <= 360, project

    # Real event configuration: capture on, discovery on, coach on, demo data on.
    monkeypatch.setenv("LAB_COACH", "true")
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("DISCOVERY_ENABLED", "true")
    monkeypatch.setattr(user_content, "_demo_data_overlay", lambda _: (
        "<!-- workshop-demo-data -->\n## Demo data in `$WORKSHOP_DEMO_CATALOG` (read-only)\n"
        "- `workshop_demo.energy_utilities.outages` (200 rows)\n"
    ))
    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    channels = _channels(_home(person))
    assert _words(channels["claude"]) <= 2000, _words(channels["claude"])
    assert _words(channels["codex"]) <= 2000, _words(channels["codex"])
    assert _words(channels["project"]) <= 430, _words(channels["project"])
    assert "<!-- workshop-workflow-contract:v3 -->" not in channels["project"]


def test_codex_is_told_its_own_skills_folder(client, monkeypatch, person):
    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    channels = _channels(_home(person))

    assert "`~/.claude/skills`" in channels["claude"] and "`~/.agents/skills`" not in channels["claude"]
    assert "`~/.agents/skills`" in channels["codex"] and "`~/.claude/skills`" not in channels["codex"]


def test_domain_expertise_and_the_working_rhythm_reach_every_main_session(client, monkeypatch, person):
    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    for name in ("claude", "codex"):
        text = " ".join(_channels(_home(person))[name].split())
        assert "**Know their field.**" in text
        assert "Never let five minutes pass in silence." in text
        assert "**Plan, then wait**" in text


def test_the_coach_banner_is_gone():
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text()

    assert "coach-hint" not in app and "STARTER_PROMPT" not in app


# --- one set of skills ------------------------------------------------------------------


def test_the_workshop_installs_one_set_of_databricks_skills_without_the_unused_ones():
    from server.bootstrap.install import FORK_SKILLS
    from server.bootstrap.skill_projection import PROJECTED_OUT, WORKSHOP_UNUSED_SKILLS

    vendored = {path.name for path in (ROOT / "assets" / "skills").iterdir() if (path / "SKILL.md").is_file()}

    assert not vendored & PROJECTED_OUT
    assert WORKSHOP_UNUSED_SKILLS <= PROJECTED_OUT
    assert "workshop" in FORK_SKILLS and "workshop" in vendored
    databricks = {name for name in vendored if name.startswith("databricks-")}
    assert len(databricks) == 22, sorted(databricks)
    assert {"databricks-core", "databricks-apps", "databricks-lakebase", "databricks-data-discovery"} <= databricks


def test_each_agent_discovers_every_skill_exactly_once(client, monkeypatch, person):
    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    home = _home(person)

    for relative in (".claude/skills", ".agents/skills"):
        names = sorted(os.listdir(os.path.join(home, relative)))
        assert len(names) == len(set(names)), relative
        assert "workshop" in names and "databricks-core" in names, relative
        assert "refresh-databricks-skills" not in names, relative
        assert not {"databricks-iceberg", "databricks-app-design"} & set(names), relative
    legacy = os.path.join(home, ".codex", "skills")
    assert not os.path.isdir(legacy) or not os.listdir(legacy)
    for rc in (".profile", ".bashrc"):
        assert ".local/bin:" in Path(home, rc).read_text(), rc


def test_export_strips_workshop_managed_project_instructions(tmp_path):
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader
    import zipfile

    loader = SourceFileLoader("workshop_export", str(ROOT / "assets/bin/workshop-export"))
    spec = spec_from_loader(loader.name, loader)
    export_mod = module_from_spec(spec)
    loader.exec_module(export_mod)

    project = tmp_path / "my-app"
    project.mkdir()
    (project / "app.py").write_text("print('ok')\n")
    (project / "AGENTS.md").write_text(
        "<!-- workshop-managed:begin -->\nWorkshop rules\n<!-- workshop-managed:end -->\n"
    )
    (project / "CLAUDE.md").write_text(
        "<!-- workshop-managed:begin -->\nWorkshop rules\n<!-- workshop-managed:end -->\n"
        "\n## Notes that were already in this project\n\nKeep this note.\n"
    )
    archive_path = tmp_path / "out.zip"
    export_mod.build_archive(project, archive_path, single_project=True)
    with zipfile.ZipFile(archive_path) as zf:
        names = set(zf.namelist())
        assert "workshop-code/my-app/AGENTS.md" not in names
        assert "workshop-code/my-app/CLAUDE.md" in names
        claude_text = zf.read("workshop-code/my-app/CLAUDE.md").decode()
        assert "Workshop rules" not in claude_text
        assert "Keep this note." in claude_text


def test_every_home_channel_overrides_the_skills_the_audit_flagged(client, monkeypatch, person):
    """Agent-load audit, 29 Oct. databricks-core tells the agent to make the user
    pick a CLI profile (A1); databricks-apps and apx ask for validate, Playwright
    and a technical choice the attendee shouldn't have to make (A3, A8). Claude,
    Codex and Omnigent's brain all read these two files."""
    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    channels = _channels(_home(person))

    for name in ("claude", "codex"):
        text = " ".join(channels[name].split())
        assert "Profiles are preset: never ask the attendee to choose one, whatever a skill says." in text
        assert "`databricks …` (`DEFAULT`) builds" in text and "`databricks-me …` (`me`) reads as the attendee" in text
        assert ("Do not install Playwright browsers or run `databricks apps validate`, whatever a skill says; "
                "fold a skill's questions for the attendee (e.g. Analytics vs Lakebase synced tables) "
                "into your recommendation.") in text


def test_the_meeting_asks_only_whats_missing_in_each_agents_own_way(client, monkeypatch, person):
    """A4: a goal-first turn or a partial save re-asked everything or ran commands
    first. A5: Omnigent's Codex was told to use a tool it doesn't have, and 2–4
    options can't list 14 seeded industries."""
    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    text = " ".join(_channels(_home(person))["claude"].split())

    assert ("On a first turn with meeting answers missing (the opener, a bare \"hi\", or nothing saved yet), "
            "**reply straight away, without running commands or reading files**") in text
    assert "If a form closes unanswered, ask the same question in text" in text
    # F-043: one clickable question set, never the scripted text block.
    assert "one clickable question set, never a text list" in text
    assert "`AskUserQuestion` (Claude Code, Omnigent's Claude) or `request_user_input` (Codex, Omnigent's Codex)" in text
    assert "every seeded industry there, plus Something else" in text
    assert "Four quick things" not in text and "Hi, I'm your build partner" not in text


def test_codex_asks_meeting_choices_with_request_user_input(client, monkeypatch, person):
    # Codex (CLI and Omnigent's Codex worker) should put the meeting choices to
    # the attendee as a clickable form, like AskUserQuestion for Claude.
    _ready(monkeypatch)
    _open(client, person, "claude", has_starter_prompt=True)
    channels = _channels(_home(person))
    assert "request_user_input" in channels["codex"]
    assert "request_user_input" in channels["project"]


def test_attach_image_shows_what_was_attached_and_why_it_is_disabled():
    """F-046: the owner couldn't tell what "Attach image" did; the only sign was a path in the terminal."""
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text()

    assert "Attached. Now tell the agent what to do with it." in app
    assert 'className="attach-thumb"' in app
    assert "Attaching images works while the workshop is open" in app
