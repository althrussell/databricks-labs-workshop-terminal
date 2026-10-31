"""After a container restart, an attendee's saved builds come back on their own.

Live run 1 (25 Oct), seat 3: after CT restarted the terminal, My builds said
"No builds yet" until the attendee found Check saved builds. These drive the
restorer against a simulated Workspace copy and the real recovery path.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest

from server import build_restore, projects
from server.users import User

from .test_closeout_api import retained, seal  # noqa: F401 — closeout fixtures

ALICE = {"X-Forwarded-Email": "alice@example.com"}


@pytest.fixture
def attendee(tmp_path, monkeypatch):
    user = User("alice@example.com")
    user.home = str(tmp_path / "home")
    projects.root(user)
    template = Path(user.home) / ".config/workshop/project-memory.md"
    template.parent.mkdir(parents=True)
    template.write_text("Workshop policy: preserve other builds and read project context.\n")
    env = {**os.environ, "HOME": user.home, "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Attendee", "GIT_AUTHOR_EMAIL": user.email,
           "GIT_COMMITTER_NAME": "Attendee", "GIT_COMMITTER_EMAIL": user.email}
    monkeypatch.setattr(user, "shell_env", lambda: env.copy())
    return user


@pytest.fixture
def workspace(attendee, monkeypatch):
    """Saved copies by build name: (files, newest modified_at in ms). Edit before use."""
    parent = f"/Workspace/Users/{attendee.email}/projects"
    saved: dict[str, tuple[dict[str, str], int]] = {}
    failing: set[str] = set()
    calls: list[list[str]] = []

    def cli(user, arguments, **kwargs):
        calls.append(arguments)
        if arguments[:2] == ["workspace", "list"] and arguments[2] == parent:
            payload = [{"path": f"{parent}/{name}", "object_type": "DIRECTORY"} for name in saved]
            payload.append({"path": f"{parent}/.workshop-capture", "object_type": "DIRECTORY"})
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        if arguments[:2] == ["workspace", "list"]:
            files, modified = saved[arguments[2].rsplit("/", 1)[-1]]
            payload = [{"path": f"{arguments[2]}/{file}", "object_type": "FILE", "modified_at": modified - index}
                       for index, file in enumerate(files)]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        assert arguments[:2] == ["workspace", "export-dir"]
        name = arguments[2].rsplit("/", 1)[-1]
        if name in failing:
            raise projects.ProjectError("Workspace project access failed.")
        staged = Path(arguments[3])
        staged.mkdir()
        for file, text in saved[name][0].items():
            (staged / file).write_text(text)
        # The helper commits .workshop/project.json with the build, so a saved
        # copy carries it; the plan gate reads it as "already scaffolded".
        (staged / ".workshop").mkdir(exist_ok=True)
        (staged / ".workshop" / "project.json").write_text(json.dumps({"version": 1, "name": name, "goal": ""}))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(projects, "_cli", cli)
    return saved, failing, calls


@pytest.fixture
def events(monkeypatch):
    from server import telemetry

    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(telemetry, "emit", lambda kind, attendee, payload=None: seen.append((kind, attendee, payload)) or True)
    return seen


def _restorer(**overrides) -> build_restore.BuildRestorer:
    clock = overrides.pop("clock", None) or iter(float(n) for n in range(1_000_000)).__next__
    return build_restore.BuildRestorer(spawn=lambda target, *args: target(*args), clock=clock,
                                       sleep=lambda seconds: None, **overrides)


def _empty(user):
    return projects.inventory(user)


def test_a_restarted_container_restores_saved_builds_and_selects_the_newest(attendee, workspace, events):
    saved, _, _ = workspace
    saved["repair-desk"] = ({"app.py": "older build"}, 1_790_000_000_000)
    saved["substation-outages"] = ({"app.py": "newest build", "README.md": "# Outages"}, 1_790_000_600_000)

    status = _restorer().observe(attendee, _empty(attendee), prepare=lambda: None, busy=lambda: False)

    assert status["state"] == "restored"
    assert status["restored"] == ["substation-outages", "repair-desk"]
    assert status["active"] == "substation-outages" and status["saved_at"] == 1_790_000_600
    assert (projects.project_path(attendee, "substation-outages") / "app.py").read_text() == "newest build"
    assert (projects.project_path(attendee, "repair-desk") / "app.py").read_text() == "older build"
    value = projects.inventory(attendee)
    assert value["active"] == "substation-outages"
    assert {entry["name"]: entry["available"] for entry in value["projects"]} == {
        "repair-desk": True, "substation-outages": True}
    [(kind, who, payload)] = events
    assert kind == "selfheal.attempted" and who == attendee.email
    assert payload["outcome"] == "restored" and payload["restored"] == 2 and payload["failed"] == 0


def test_an_attendee_with_local_builds_is_left_alone(attendee, workspace, events):
    saved, _, calls = workspace
    saved["repair-desk"] = ({"app.py": "saved copy"}, 1_790_000_000_000)
    target = projects.project_path(attendee, "garden-plan")
    target.mkdir()
    projects.select(attendee, "garden-plan", goal="Plan a garden")
    spawned = []
    restorer = build_restore.BuildRestorer(spawn=lambda *args: spawned.append(args))

    assert restorer.observe(attendee, projects.inventory(attendee), prepare=lambda: None, busy=lambda: False) is None
    # Remembered: emptying the list later is the attendee's choice, not a restart.
    assert restorer.observe(attendee, {"active": "", "projects": []}, prepare=lambda: None, busy=lambda: False) is None
    assert spawned == [] and calls == [] and events == []


def test_a_failed_restore_falls_back_to_manual_recovery_and_overwrites_nothing(attendee, workspace, events):
    saved, failing, _ = workspace
    saved["repair-desk"] = ({"app.py": "saved copy"}, 1_790_000_000_000)
    saved["garden-plan"] = ({"app.py": "saved garden"}, 1_790_000_300_000)
    failing.add("garden-plan")

    status = _restorer().observe(attendee, _empty(attendee), prepare=lambda: None, busy=lambda: False)

    assert status["state"] == "partial"
    assert status["restored"] == ["repair-desk"] and status["failed"] == ["garden-plan"]
    assert not projects.project_path(attendee, "garden-plan").exists()
    assert list(projects.root(attendee).iterdir()) == [projects.project_path(attendee, "repair-desk")]
    # The failed one is still offered for manual recovery.
    later = projects.saved_inventory(attendee)
    assert {entry["name"]: entry["saved"] for entry in later["projects"]} == {"garden-plan": True, "repair-desk": True}
    assert events[0][2]["outcome"] == "partial"


def test_a_build_whose_setup_fails_never_stays_current(attendee, workspace, monkeypatch):
    """Found in the local harness (26 Oct): workshop-init-project marks the build
    current before its last steps, then failed on an old saved build. The
    half-prepared build stayed current behind the page, and every agent launch
    was refused as "The active build changed in another tab"."""
    saved, _, _ = workspace
    saved["hello-world"] = ({"notes.md": "an old build"}, 1_790_000_000_000)

    def helper_marks_current_then_fails(arguments, **kwargs):
        state = projects.read(attendee)
        state["active"] = arguments[2]
        projects._write(projects.selection_path(attendee), state, within=Path(attendee.home))
        return subprocess.CompletedProcess(arguments, 1, "", "commit failed")

    monkeypatch.setattr(projects.subprocess, "run", helper_marks_current_then_fails)
    with pytest.raises(projects.ProjectError, match="project setup is incomplete"):
        projects.recover(attendee, "hello-world", activate=False)

    assert projects.read(attendee)["active"] == ""
    assert projects.project_path(attendee, "hello-world").exists()  # local files retained


def test_local_files_that_appear_during_the_download_are_never_replaced(attendee, workspace, monkeypatch):
    saved, _, _ = workspace
    saved["repair-desk"] = ({"app.py": "saved copy"}, 1_790_000_000_000)
    real_cli = projects._cli

    def racing(user, arguments, **kwargs):
        result = real_cli(user, arguments, **kwargs)
        if arguments[:2] == ["workspace", "export-dir"]:
            target = projects.project_path(attendee, "repair-desk")
            target.mkdir()
            (target / "app.py").write_text("new local work")
        return result

    monkeypatch.setattr(projects, "_cli", racing)
    status = _restorer().observe(attendee, _empty(attendee), prepare=lambda: None, busy=lambda: False)

    assert status["state"] == "failed" and status["failed"] == ["repair-desk"]
    assert (projects.project_path(attendee, "repair-desk") / "app.py").read_text() == "new local work"


def test_one_restore_per_attendee_however_many_requests_ask(attendee, workspace):
    spawned = []
    restorer = build_restore.BuildRestorer(spawn=lambda *args: spawned.append(args))
    first = restorer.observe(attendee, _empty(attendee), prepare=lambda: None, busy=lambda: False)
    second = restorer.observe(attendee, _empty(attendee), prepare=lambda: None, busy=lambda: False)
    assert len(spawned) == 1
    assert first["state"] == second["state"] == "checking" and first["id"] == second["id"]


def test_it_waits_for_credentials_then_stays_silent_if_they_never_come(attendee, workspace, events):
    saved, _, calls = workspace
    saved["repair-desk"] = ({"app.py": "saved copy"}, 1_790_000_000_000)
    attempts = []

    def not_yet():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("No current credential")

    late = _restorer(wait=60.0, poll=5.0)
    assert late.observe(attendee, _empty(attendee), prepare=not_yet, busy=lambda: False)["state"] == "restored"
    assert len(attempts) >= 3

    shutil_target = projects.project_path(attendee, "repair-desk")
    import shutil

    shutil.rmtree(shutil_target)
    projects.selection_path(attendee).unlink()
    del events[:]
    calls.clear()

    def never():
        raise RuntimeError("No current credential")

    # Without credentials nothing proves a restart, so the attendee hears nothing
    # and My builds keeps its manual Check saved builds.
    assert _restorer(wait=10.0, poll=5.0).observe(attendee, _empty(attendee), prepare=never, busy=lambda: False) is None
    assert calls == [] and events == [] and not shutil_target.exists()


def test_the_attendee_is_never_switched_away_from_their_own_choice_or_a_running_agent(attendee, workspace):
    saved, _, _ = workspace
    saved["repair-desk"] = ({"app.py": "saved copy"}, 1_790_000_000_000)

    status = _restorer().observe(attendee, _empty(attendee), prepare=lambda: None, busy=lambda: True)
    assert status["state"] == "restored" and status["active"] == ""
    assert projects.read(attendee)["active"] == ""
    assert projects.project_path(attendee, "repair-desk").is_dir()


def test_the_build_list_carries_restore_status_without_waiting_for_it(client, monkeypatch):
    import server.main as main

    seen = []

    def observe(user, inventory, *, prepare, busy, accepting):
        assert accepting() is True
        seen.append((user.email, inventory["projects"]))
        return {"id": "1.000", "state": "restoring", "restored": [], "failed": [], "active": "", "saved_at": None}

    monkeypatch.setattr(build_restore, "restorer", type("Stub", (), {"observe": staticmethod(observe)})())
    monkeypatch.setattr(main, "ensure_user_credentials", lambda user: None)
    response = client.get("/api/projects", headers=ALICE)
    assert response.status_code == 200
    assert response.json()["restore"]["state"] == "restoring"
    assert seen == [("alice@example.com", [])]


def test_closeout_starts_no_restore_and_stops_one_in_progress(attendee, workspace, events):
    saved, _, calls = workspace
    saved["repair-desk"] = ({"app.py": "saved copy"}, 1_790_000_000_000)
    saved["garden-plan"] = ({"app.py": "saved garden"}, 1_790_000_300_000)
    spawned = []
    closed = build_restore.BuildRestorer(spawn=lambda *args: spawned.append(args))
    assert closed.observe(attendee, _empty(attendee), prepare=lambda: None, busy=lambda: False,
                          accepting=lambda: False) is None
    assert spawned == [] and calls == []

    # Closeout begins after the first build: the rest is left for manual recovery.
    answers = iter([True, True, True])
    status = _restorer().observe(attendee, _empty(attendee), prepare=lambda: None, busy=lambda: False,
                                 accepting=lambda: next(answers, False))
    assert status["state"] == "partial"
    assert status["restored"] == ["garden-plan"] and status["failed"] == ["repair-desk"]
    assert not projects.project_path(attendee, "repair-desk").exists()


@pytest.mark.parametrize("method", ["GET", "POST"])  # POST: tabs loaded before the read existed
def test_a_failing_saved_builds_check_never_blocks_the_seal(client, as_admin, retained, monkeypatch, method):
    """The automatic check runs on a fresh list, possibly while a capture closes."""
    import server.main as main
    from server.credentials import CredentialError

    def unavailable(user):
        raise CredentialError("No current credential")

    monkeypatch.setattr(main, "ensure_user_credentials", unavailable)
    response = client.request(method, "/api/projects/saved", headers=ALICE)
    assert response.status_code == 503
    assert retained.journal.read().get("closeout", {}).get("writer_outcomes", []) == []
    sealed = seal(client, retained)
    assert sealed["phase"] == "SEALED"
