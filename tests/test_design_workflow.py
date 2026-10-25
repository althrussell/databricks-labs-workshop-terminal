"""W2: one workflow record per attendee project, resumable by any agent.

The record (``.workshop/design-workflow.json``) holds the path, stage,
confirmed facts, labelled assumptions, open questions, the agreed direction
and evidence. Only the attendee (or the wizard, or a saved preference) chooses
a path; only the attendee confirms a direction. These tests drive the helper
the agents use, the server routes and the wizard-on/off parity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from server import design_workflow, user_content

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "assets/bin/workshop-workflow"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "projects/care-console"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def run(project: Path, *args: str, email: str = "alice@example.com") -> subprocess.CompletedProcess:
    env = {**os.environ, "WORKSHOP_USER_EMAIL": email}
    return subprocess.run(["python3", str(HELPER), "--project", str(project), *args],
                          capture_output=True, text=True, env=env, timeout=30)


def record(project: Path) -> dict:
    return design_workflow.read(project)


def test_a_new_project_asks_for_a_path_and_an_agent_cannot_choose_it(project):
    shown = run(project, "show")
    assert shown.returncode == 0 and "path not chosen" in shown.stdout
    assert "rapid prototype or guided ideation" in shown.stdout
    refused = run(project, "path", "rapid", "--by", "agent")
    assert refused.returncode != 0
    assert not (project / design_workflow.RECORD).exists()


def test_rapid_builds_without_an_interview_and_looking_at_the_page_is_optional(project):
    assert run(project, "path", "rapid", "--by", "attendee", "--source", "chat").returncode == 0
    assert run(project, "phase", "build").returncode == 0  # no shaping round for rapid
    blocked = run(project, "phase", "inspect")
    assert blocked.returncode == 2 and "build evidence" in blocked.stderr
    assert run(project, "phase", "inspect", "--evidence", "preview=https://app.example").returncode == 0
    # No screenshot ritual: the attendee can always check the app themselves.
    assert run(project, "phase", "feedback", "--evidence", "checked=primary action and reload").returncode == 0
    assert run(project, "check").returncode == 0
    saved = record(project)
    assert saved["path"] == {**saved["path"], "value": "rapid", "chosen_by": "attendee", "source": "chat"}
    assert saved["evidence"]["build"]["preview"] == "https://app.example"
    assert saved["evidence"]["inspect"]["checked"].startswith("primary action")
    assert '"state": "none"' in run(project, "verification", "--json").stdout


def test_guided_ideation_waits_for_a_confirmed_direction(project):
    assert run(project, "path", "guided", "--by", "attendee").returncode == 0
    straight = run(project, "phase", "build")
    assert straight.returncode == 2 and "confirm a direction" in straight.stderr
    assert run(project, "phase", "shape").returncode == 0
    question = run(project, "ask", "Who decides priority each morning?").stdout.splitlines()[0]
    assert run(project, "phase", "agree").returncode == 0
    # The agent's own summary is not approval.
    assert run(project, "direction", "Shared call queue with ownership").returncode == 0
    unconfirmed = run(project, "phase", "build")
    assert unconfirmed.returncode == 2 and "confirm a direction" in unconfirmed.stderr
    assert "NOT yet confirmed" in run(project, "show").stdout
    refused = run(project, "direction", "x", "--confirmed-by", "agent")
    assert refused.returncode != 0
    assert run(project, "answer", question, "The regional supervisor").returncode == 0
    assert run(project, "direction", "Shared call queue with ownership", "--confirmed-by", "attendee").returncode == 0
    assert run(project, "phase", "build").returncode == 0
    saved = record(project)
    assert saved["questions"][0]["answered_by"] == "attendee"
    assert saved["brief_revision"] >= 2


def test_an_inferred_fact_is_an_assumption_until_the_attendee_says_it(project):
    run(project, "fact", "Backup supply means a generator on site")
    run(project, "fact", "Calls are logged in the CRM", "--by", "attendee")
    saved = record(project)
    assert [f["text"] for f in saved["facts"]] == ["Calls are logged in the CRM"]
    assert [a["text"] for a in saved["assumptions"]] == ["Backup supply means a generator on site"]


def test_switching_paths_keeps_every_confirmed_fact(project):
    run(project, "path", "rapid", "--by", "attendee")
    run(project, "fact", "Life-support customers are called first", "--by", "attendee")
    run(project, "path", "guided", "--by", "attendee")
    saved = record(project)
    assert saved["path"]["value"] == "guided"
    assert saved["facts"][0]["text"] == "Life-support customers are called first"
    assert any(h.get("event") == "path_switched" for h in saved["history"])


def test_a_stale_write_and_another_attendees_project_are_refused(project):
    run(project, "path", "rapid", "--by", "attendee")
    stale = run(project, "--expect-revision", "0", "fact", "late", "--by", "attendee")
    assert stale.returncode == 3 and "revision 1" in stale.stderr
    other = run(project, "fact", "not mine", email="bob@example.com")
    assert other.returncode == 2 and "another attendee" in other.stderr
    assert record(project)["revision"] == 1


def test_a_worktree_or_another_agent_resumes_from_the_committed_record(project, tmp_path):
    run(project, "path", "guided", "--by", "wizard", "--source", "wizard")
    run(project, "fact", "Regions split the work", "--by", "attendee")
    env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
           "GIT_COMMITTER_EMAIL": "a@x"}
    subprocess.run(["git", "-C", str(project), "add", design_workflow.RECORD], check=True, env=env)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "record"], check=True, env=env)
    worker = tmp_path / "worker"
    subprocess.run(["git", "-C", str(project), "worktree", "add", "-q", "--detach", str(worker)], check=True, env=env)
    shown = run(worker, "show")
    assert "path guided (chosen by wizard)" in shown.stdout and "Regions split the work" in shown.stdout
    assert "Next: ask the open design questions" not in shown.stdout  # no open questions


def test_the_validator_flags_untrustworthy_claims():
    record = design_workflow.workflow.new_record("a@x", "p")
    record.update(phase="feedback", path={"value": "guided", "chosen_by": "agent", "source": None, "at": None},
                  direction={"text": "x", "confirmed_by": "agent"},
                  questions=[{"id": "q1", "text": "?", "answer": "yes", "answered_by": "agent"}])
    problems = " | ".join(design_workflow.check(record))
    for expected in ("not chosen by the attendee", "guided build without an attendee-confirmed direction",
                     "only be confirmed by the attendee", "marked answered without the attendee",
                     "no recorded build or preview"):
        assert expected in problems
    assert "inspection" not in problems  # looking at the page is optional


def test_the_helper_is_installed_and_named_in_every_channel(client, monkeypatch):
    from tests.test_user_content import _MEMORY_CHANNELS, _provisioned_home

    home = _provisioned_home(client, monkeypatch)
    assert os.access(os.path.join(home, ".local/bin/workshop-workflow"), os.X_OK)
    for parts in _MEMORY_CHANNELS:
        text = " ".join(Path(home, *parts).read_text().split())  # wrapping is not meaning
        assert "workshop-workflow show" in text and "safe point to compact" in text
        assert "Would you like a rapid prototype to react to, or guided ideation" in text
        assert "workshop-app-deploy" in text and "never `databricks bundle deploy`" in text
        # The blanket bypass is gone: guided ideation may ask and wait.
        assert "This contract overrides upstream workflow ceremony" not in text
        assert "Shall I build this direction, or refine it further?" in text


def _capability(user) -> str:
    user_content._write_callback_capability(user)
    return Path(user_content.callback_capability_path(user)).read_text().strip()


def test_a_preference_stated_in_chat_saves_through_the_revision_checked_service(client):
    from server.users import user_manager

    user = user_manager.get("chat-prefs@example.com")
    user.bootstrap_home()
    headers = {"X-Workshop-Capability": _capability(user)}
    current = client.post("/api/agent/profile", headers=headers, json={"email": "chat-prefs@example.com"})
    assert current.status_code == 200 and current.json()["revision"] == 0
    saved = client.post("/api/agent/profile", headers=headers, json={
        "email": "chat-prefs@example.com", "expected_revision": 0,
        "changes": {"working_approach": "learn", "explanation_detail": "brief"}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["choice_sources"] == {"working_approach": "chat", "explanation_detail": "chat"}
    stale = client.post("/api/agent/profile", headers=headers, json={
        "email": "chat-prefs@example.com", "expected_revision": 0, "changes": {"working_approach": "lead"}})
    assert stale.status_code == 409
    missing = client.post("/api/agent/profile", headers=headers, json={
        "email": "chat-prefs@example.com", "changes": {"working_approach": "lead"}})
    assert missing.status_code == 422
    forged = client.post("/api/agent/profile", headers={"X-Workshop-Capability": "nope"},
                         json={"email": "chat-prefs@example.com"})
    assert forged.status_code == 403
    # The browser sees the same state the conversation saved.
    browser = client.get("/api/profile", headers={"X-Forwarded-Email": "chat-prefs@example.com"}).json()
    assert browser["working_approach"] == "learn" and browser["revision"] == 1
    claude_md = Path(user.home, ".claude/CLAUDE.md").read_text()
    assert "Teach me while we build" in claude_md and "keep explanations brief" in claude_md


def test_the_browser_saves_the_same_dimensions(client):
    owner = {"X-Forwarded-Email": "ui-prefs@example.com"}
    profile = client.get("/api/profile", headers=owner).json()
    saved = client.post("/api/profile", headers=owner, json={
        "expected_revision": profile["revision"], "working_approach": "lead", "engagement_path": "guided"})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["working_approach"] == "lead" and body["engagement_path"] == "guided"
    assert body["choice_sources"]["working_approach"] == "ui" and body["help_preference"] == ""


@pytest.mark.parametrize("wizard_on", [True, False])
def test_wizard_on_and_off_reach_the_same_project_choice(client, project, monkeypatch, wizard_on):
    """With the wizard the choice comes from it; without, from the conversation.
    Both end in the same recorded path, and neither asks twice."""
    from server import wizard
    from server.users import user_manager

    user = user_manager.get("alice@example.com")
    user.bootstrap_home()
    if wizard_on:
        wizard.save(user, {"what_building": "Care console", "engagement_path": "rapid"})
        monkeypatch.setattr(user_content, "_write_instructions", lambda _: None)
        overlay = user_content._wizard_overlay(user)
        assert "Do not ask again" in overlay and "workshop-workflow path rapid --by wizard" in overlay
        assert run(project, "path", "rapid", "--by", "wizard", "--source", "wizard").returncode == 0
    else:
        assert run(project, "path", "rapid", "--by", "attendee", "--source", "chat").returncode == 0
    saved = record(project)
    assert saved["path"]["value"] == "rapid" and saved["path"]["chosen_by"] in {"wizard", "attendee"}
    assert "path rapid" in run(project, "show").stdout
    with pytest.raises(ValueError):
        wizard.save(user, {"what_building": "Care console", "engagement_path": "something-else"})
