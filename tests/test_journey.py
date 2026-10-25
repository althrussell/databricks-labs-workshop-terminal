"""Visible progress (W4): "Your build", coarse facilitator signals, real control, export."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from server import design_workflow, journey, projects
from server.users import User

from .conftest import ALICE

ADMIN = {"X-Forwarded-Email": "op@example.com"}


@pytest.fixture(autouse=True)
def _reset():
    journey.reset_for_tests()
    yield
    journey.reset_for_tests()


@pytest.fixture
def attendee(tmp_path, monkeypatch):
    user = User("alice@example.com")
    user.home = str(tmp_path / "home")
    root = projects.root(user)
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": user.home, "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": user.email,
           "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": user.email}
    monkeypatch.setattr(user, "shell_env", lambda: env.copy())
    projects.select(user, "outages", goal="Track substation outages", new=True)
    project = projects.project_path(user, "outages")
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True, env=env)
    (project / "app.py").write_text("print('outages')\n")
    subprocess.run(["git", "-C", str(project), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(project), "-c", "core.hooksPath=/dev/null", "commit", "-qm",
                    "Outage list with severity filter"], check=True, env=env)
    return user


def _write(project: Path, mutate) -> None:
    workflow = design_workflow.workflow
    record = workflow.load(project)
    base = record["revision"]
    mutate(record)
    workflow.save(project, record, expected_revision=None, base_revision=base)


def test_your_build_shows_goal_stage_next_decision_and_what_changed(attendee):
    view = journey.view(attendee)
    assert view["project"] == "outages" and view["goal"] == "Track substation outages"
    assert view["phase"] == "understand" and view["path"] is None
    assert view["status"] == "needs_decision" and "rapid prototype or guided ideation" in view["next_decision"]
    assert view["what_changed"] == ["Outage list with severity filter"]
    assert view["deploy"] == {"state": "not_deployed", "preview_url": None} and view["review"]["state"] == "none"


def test_signals_follow_the_record_never_terminal_text(attendee, monkeypatch):
    project = projects.project_path(attendee, "outages")
    _write(project, lambda r: design_workflow.workflow.choose_path(r, "rapid", by="attendee", source="ui"))
    assert journey.journey(attendee)["status"] == "working"
    _write(project, lambda r: r.update(phase="inspect", evidence={"build": {"preview": "https://outages.aws.databricksapps.com"}}))
    view = journey.view(attendee)
    # Deployed, but not reviewed: deployment and review are tracked apart.
    assert view["deploy"]["state"] == "deployed" and view["review"]["state"] == "none"
    assert view["status"] == "working"
    shot = project / ".workshop/evidence/x.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG fake")
    import hashlib

    head = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    _write(project, lambda r: r["evidence"].setdefault("inspect", {}).setdefault("artifacts", []).append(
        {"kind": "screenshot", "path": ".workshop/evidence/x.png", "sha256": hashlib.sha256(shot.read_bytes()).hexdigest(),
         "revision": {"commit": head}}))
    assert journey.journey(attendee)["status"] == "ready_to_try"
    _write(project, lambda r: r["evidence"]["inspect"].setdefault("gaps", []).append(
        {"kind": "gap", "code": "sign_in_blocked", "detail": "sign-in page"}))
    assert journey.journey(attendee)["status"] == "ready_to_try"  # a verified review outranks a later gap
    monkeypatch.setattr(journey, "_help_raised", lambda: True)
    assert journey.journey(attendee)["status"] == "help_requested"


def test_a_guided_direction_waiting_for_the_attendee_needs_a_decision(attendee):
    project = projects.project_path(attendee, "outages")

    def guided(record):
        design_workflow.workflow.choose_path(record, "guided", by="attendee", source="ui")
        record.update(phase="agree", direction={"text": "Shared outage queue", "confirmed_by": None})

    _write(project, guided)
    assert journey.journey(attendee)["status"] == "needs_decision"
    record = journey.decide(project, question_id=None, answer=None, confirm_direction=True, path=None)
    assert record["direction"]["confirmed_by"] == "attendee"
    assert journey.journey(attendee)["status"] == "working"


def test_ui_answers_land_once_in_the_same_record_chat_uses(attendee):
    project = projects.project_path(attendee, "outages")
    _write(project, lambda r: r["questions"].append({"id": "q1", "text": "Who triages?", "answer": None,
                                                     "answered_by": None}))
    assert journey.view(attendee)["open_questions"] == [{"id": "q1", "text": "Who triages?"}]
    journey.decide(project, question_id="q1", answer="The control room lead", confirm_direction=False, path=None)
    saved = design_workflow.read(project)
    assert saved["questions"][0]["answer"] == "The control room lead" and saved["questions"][0]["answered_by"] == "attendee"
    with pytest.raises(ValueError, match="already answered"):
        journey.decide(project, question_id="q1", answer="again", confirm_direction=False, path=None)


def test_a_change_request_waits_for_the_agents_next_step_and_is_resolved_there(attendee):
    project = projects.project_path(attendee, "outages")
    entry = journey.add_request(project, "change", "Make severity a colour, not a number")
    assert entry["id"] == "r1" and journey.view(attendee)["requests"][0]["text"].startswith("Make severity")
    helper = Path(__file__).resolve().parents[1] / "assets/bin/workshop-workflow"
    shown = subprocess.run([str(helper), "--project", str(project), "show"], capture_output=True, text=True)
    assert "handle the attendee's change request [r1]" in shown.stdout
    done = subprocess.run([str(helper), "--project", str(project), "done", "r1", "Severity is now a coloured chip"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert journey.view(attendee)["requests"] == []


def test_stop_sends_a_real_interrupt_and_says_what_it_does():
    class Session:
        exited, agent_id, label = False, "claude", "Claude Code"
        sent: list[str] = []

        def write_keystrokes(self, data):
            self.sent.append(data)

    session = Session()
    result = journey.stop(session)
    assert session.sent == ["\x1b"] and result["stopped"] is True and "may still finish" in result["detail"]
    assert journey.stop(None) == {"stopped": False, "detail": "No agent is running."}


def test_export_reuses_a_recent_export_and_keeps_the_volume_contract(attendee, monkeypatch, tmp_path):
    local = tmp_path / "workshop-code-20261025T040000Z-abcd1234.zip"
    local.write_bytes(b"PK" * 50)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        receipt = {"files": 3, "local_zip": str(local), "filename": local.name,
                   "volume_path": f"/Volumes/cat/sch/workshop_exports/code/{local.name}", "verified": True}
        return subprocess.CompletedProcess(command, 0, json.dumps(receipt) + "\n", "")

    monkeypatch.setattr(journey.subprocess, "run", run)
    first = journey.export(attendee, reason="wrap_up", reuse=True)
    assert first["path"] == f"/Volumes/cat/sch/workshop_exports/code/{local.name}" and first["bytes"] == 100
    assert first["reused"] is False and calls[0][-1] == "--json"
    again = journey.export(attendee, reason="wrap_up", reuse=True)
    assert again["reused"] is True and len(calls) == 1
    journey.export(attendee, reason="attendee")  # an attendee's own click always makes a fresh one
    assert len(calls) == 2


def test_admin_export_and_presence_carry_the_seat_contract(client, as_admin, monkeypatch, tmp_path):
    from server import config
    from server.users import user_manager

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    assert client.post("/api/projects/select", json={"name": "outages", "goal": "Track outages", "new": True},
                       headers=ALICE).status_code == 200
    user = user_manager.get("alice@example.com")
    projects.project_path(user, "outages").mkdir(parents=True)
    monkeypatch.setattr(journey, "export", lambda u, reason="", reuse=False: {
        "path": "/Volumes/c/s/workshop_exports/code/workshop-code-1.zip", "bytes": 42, "files": 2, "reused": reuse})
    exported = client.post("/api/admin/export", json={"reason": "wrap_up"}, headers=ADMIN)
    assert exported.status_code == 200, exported.text
    body = exported.json()
    assert body["path"].endswith("workshop-code-1.zip") and body["bytes"] == 42 and body["failed"] == []
    # Admin-only like every /api/admin route (the shared router dependency, tested with the router).
    from server.admin import router

    assert any(route.path == "/api/admin/export" for route in router.routes)

    emitted = []
    monkeypatch.setattr("server.telemetry.emit", lambda kind, who, payload=None: emitted.append((kind, payload)))
    presence = client.get("/api/admin/presence", headers=ADMIN).json()
    assert presence["journey"]["status"] == "needs_decision" and presence["journey"]["phase"] == "understand"
    assert set(presence["journey"]) == {"phase", "path", "status", "next_decision", "updated_at"}
    client.get("/api/admin/presence", headers=ADMIN)
    assert [kind for kind, _ in emitted] == ["journey.status"]  # only when the signal changes

    build = client.get("/api/build", headers=ALICE).json()
    assert build["project"] == "outages" and build["status"] == "needs_decision"
    queued = client.post("/api/build/request", json={"kind": "explain", "text": "Why a queue?"}, headers=ALICE)
    assert queued.status_code == 200 and queued.json()["delivery"] == "next_safe_boundary"
    assert client.post("/api/build/stop", headers=ALICE).json()["stopped"] is False
