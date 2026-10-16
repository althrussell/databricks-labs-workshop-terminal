"""Current-policy facts are fresh, retained once, and model independent."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from server import artifacts, closeout_outputs, discovery, insight_summary, stats
from server import event_emitter as emitter_module
from server.artifacts import Harvest
from server.capture_journal import CaptureJournal, CaptureStorageError, MemoryCaptureStorage, canonical_json
from server.closeout_guard import control_transaction
from server.event_emitter import EventEmitter
from server.users import user_manager

BINDING = {"run_id": "final-run", "unit_id": "final-unit", "workspace_id": "123",
           "owner": "alice@example.com"}
RESOURCE_COUNTS = {"jobs": 0, "pipelines": 0, "apps": 0, "dashboards": 0}
REAL_HARVEST = artifacts.harvest_user
REAL_CENSUS = stats._workspace_resources


@pytest.fixture
def source(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    journal = CaptureJournal(MemoryCaptureStorage(), BINDING, {})
    emitter = EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                           ingest_url="", ingest_token="", journal=journal)
    monkeypatch.setattr(emitter_module, "event_emitter", emitter)
    user_manager.configure_capture(journal, BINDING["owner"])
    discovery.discovery_store.configure_capture(emitter)
    stats.configure_capture(journal)
    insight_summary.stamps.configure_capture(journal)
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census(
        RESOURCE_COUNTS, confirmed=RESOURCE_COUNTS))
    monkeypatch.setattr(insight_summary, "_background_workers", set())
    monkeypatch.setattr(insight_summary, "_background_outcomes", [])
    owner = user_manager.peek(BINDING["owner"])
    Path(owner.home, "projects").mkdir(parents=True)
    monkeypatch.setattr(artifacts, "harvest_user", lambda *_args, **_kwargs: Harvest(
        prompts=["Build our streaming orders pipeline"], prompt_count=1))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("sealed final facts must never call a model")

    monkeypatch.setattr(insight_summary, "_ask_model", forbidden)
    return emitter, owner


def events(emitter):
    return list(emitter.journal.read()["insight"]["events"].values())


def test_final_outputs_bypass_code_cache_and_never_call_model(source, monkeypatch):
    emitter, owner = source
    owner.first_seen = time.time() - 120
    stats._code_cache[owner.email] = (time.time(), {"projects": 99, "commits": 99,
                                                 "files": 99, "lines": 99})
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is True and result["issues"] == []
    [row] = result["outputs"]["stats"]["users"]
    assert row["code"] == {"projects": 0, "commits": 0, "files": 0, "lines": 0}
    assert row["capture_observation"]["code"]["source"] == "local"
    facts = result["outputs"]["summary_facts"][owner.email]
    assert facts["payload"]["generator"] == "extraction"
    assert facts["payload"]["model"] is None
    assert facts["payload"]["final_capture_id"] == "capture-one"
    assert {event["type"] for event in events(emitter)} == {"insight.summary", "workshop.signal"}
    stats.configure_capture(emitter.journal)  # Retained signal index remains valid.


def test_missing_expected_owner_cannot_finalize_operator_only_snapshot(source, monkeypatch):
    emitter, _owner = source
    user_manager._users.clear()
    # A visitor is deliberately registered without attendee bootstrap/credentials.
    from server.users import User

    operator = User("operator@example.com")
    user_manager._users[operator.email] = operator
    monkeypatch.setattr(stats, "gather_all", lambda *_args, **_kwargs: pytest.fail(
        "must not harvest an operator in place of the assigned attendee"))
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is False
    assert result["issues"] == ["final_expected_owner_missing"]
    assert result["outputs"]["stats"]["users"] == []
    assert not events(emitter)


def test_operator_visitors_do_not_contaminate_or_hold_owner_finalization(source, monkeypatch):
    emitter, owner = source
    from server.users import User

    operator = User("operator@example.com")
    operator.first_seen = time.time() - 60
    operator.home = "/missing/operator-home"
    user_manager._users[operator.email] = operator
    harvested = []

    def harvest(user, *, single_attendee):
        harvested.append((user.email, single_attendee))
        assert user.email == owner.email
        return Harvest(prompts=["Assigned attendee use case"], prompt_count=1)

    monkeypatch.setattr(artifacts, "harvest_user", harvest)
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is True
    assert [row["email"] for row in result["outputs"]["stats"]["users"]] == [owner.email]
    assert harvested == [(owner.email, True)]
    assert {event["attendee"] for event in events(emitter)} == {owner.email}
    assert set(result["outputs"]["summary_facts"]) == {owner.email}


def _init_repo(owner, monkeypatch):
    # Isolate developer git settings and signing; this test needs repository
    # semantics, never the operator's global configuration or hooks.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    repo = Path(owner.home, "projects", "fresh-app")
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)
    return repo


def test_valid_unborn_head_is_confirmed_zero_and_can_finalize(source, monkeypatch):
    emitter, owner = source
    repo = _init_repo(owner, monkeypatch)
    (repo / "app.py").write_text("print('first draft')\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    assert stats._unborn_head(str(repo)) is True
    counts = stats._code_stats_uncached(owner)
    assert counts == {"projects": 1, "commits": 0, "files": 1, "lines": 1}
    assert counts.confirmed is True
    harvest = REAL_HARVEST(owner)
    assert "commit_read_failed" not in harvest.issues
    monkeypatch.setattr(artifacts, "harvest_user", REAL_HARVEST)
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is True
    assert result["outputs"]["stats"]["users"][0]["code"]["commits"] == 0


@pytest.mark.parametrize("corruption", ["invalid_ref", "missing_commit_object", "broken_head"])
def test_broken_git_history_is_not_treated_as_valid_unborn_head(source, monkeypatch, corruption):
    emitter, owner = source
    repo = _init_repo(owner, monkeypatch)
    if corruption == "broken_head":
        (repo / ".git" / "HEAD").write_text("broken HEAD contents\n")
    else:
        branch = repo / ".git" / "refs" / "heads" / "main"
        branch.write_text("invalid-ref\n" if corruption == "invalid_ref" else "a" * 40 + "\n")
    assert stats._unborn_head(str(repo)) is False
    assert stats._code_stats_uncached(owner).confirmed is False
    harvest = REAL_HARVEST(owner)
    assert "commit_read_failed" in harvest.issues
    owner.first_seen = time.time() - 120
    monkeypatch.setattr(artifacts, "harvest_user", REAL_HARVEST)
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is False
    assert "commit_read_failed" in result["issues"]
    assert "final_code_observation_unavailable" in result["issues"]


def test_changed_final_facts_keep_prior_llm_envelope_and_provenance(source, monkeypatch):
    emitter, owner = source
    monkeypatch.setattr(insight_summary, "_ask_model", lambda *_args: (
        {"headline": "Earlier narrative"}, "system.ai.claude-haiku-4-5"))
    narrative = insight_summary.summarise_user(owner, phase="build", emitter=emitter)
    original = copy.deepcopy(events(emitter)[0])
    monkeypatch.setattr(artifacts, "harvest_user", lambda *_args, **_kwargs: Harvest(
        prompts=["The final goal changed to a customer analytics app"], prompt_count=3))
    monkeypatch.setattr(insight_summary, "_ask_model", lambda *_args: pytest.fail("model called"))
    result = closeout_outputs.finalize(emitter, "capture-one")
    facts = result["outputs"]["summary_facts"][owner.email]["payload"]
    assert facts["revision"] == narrative["revision"] + 1
    assert facts["prompt_count"] == 3 and facts["generator"] == "extraction"
    assert facts["prior_narrative"] == {
        "sequence": 1, "revision": narrative["revision"], "generator": "llm",
        "model": "system.ai.claude-haiku-4-5",
        "raw_entry_sha256": hashlib.sha256(canonical_json({"seq": 1, "event": original})).hexdigest(),
    }
    assert events(emitter)[0] == original


def test_final_retry_and_replacement_reuse_exact_observations_and_keys(source, monkeypatch):
    emitter, _owner = source
    first = closeout_outputs.finalize(emitter, "capture-one")
    retained = copy.deepcopy(emitter.journal.read())
    monkeypatch.setattr(stats, "gather_all", lambda *_args, **_kwargs: pytest.fail("reharvested"))
    monkeypatch.setattr(artifacts, "harvest_user", lambda *_args, **_kwargs: pytest.fail("reharvested"))
    assert closeout_outputs.finalize(emitter, "capture-one") == first
    replacement = EventEmitter(
        run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"], ingest_url="", ingest_token="",
        journal=CaptureJournal(emitter.journal.storage, BINDING, {}))
    assert closeout_outputs.finalize(replacement, "capture-one") == first
    assert replacement.journal.read() == retained


def test_failure_between_final_events_resumes_without_new_revision_or_snapshot(source, monkeypatch):
    emitter, _owner = source
    emit = emitter.emit_transaction
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CaptureStorageError("private backend failure")
        return emit(*args, **kwargs)

    monkeypatch.setattr(emitter, "emit_transaction", fail_second)
    with pytest.raises(CaptureStorageError):
        closeout_outputs.finalize(emitter, "capture-one")
    assert len(events(emitter)) == 1
    before = copy.deepcopy(events(emitter)[0])
    monkeypatch.setattr(stats, "gather_all", lambda *_args, **_kwargs: pytest.fail("reharvested"))
    monkeypatch.setattr(emitter, "emit_transaction", emit)
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is True and len(events(emitter)) == 2
    assert events(emitter)[0] == before
    assert closeout_outputs.finalize(emitter, "capture-one") == result


@pytest.mark.parametrize("resources", [
    stats._Census(), stats._Census(RESOURCE_COUNTS, confirmed={"jobs", "apps", "dashboards"}),
])
def test_unconfirmed_final_resource_observation_cannot_complete(source, monkeypatch, resources):
    emitter, _owner = source
    monkeypatch.setattr(stats, "_workspace_resources", lambda: resources)
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is False
    assert "final_resource_observation_unavailable" in result["issues"]
    assert not events(emitter)
    assert "capture-one" not in emitter.journal.read().get("closeout_outputs", {})


@pytest.mark.parametrize("continuation", [{"next_page_token": "page-two"}, {"has_more": True}])
@pytest.mark.parametrize("path,key,kind", [
    ("/api/2.2/jobs/list", "jobs", "jobs"),
    ("/api/2.0/pipelines", "statuses", "pipelines"),
    ("/api/2.0/apps", "apps", "apps"),
    ("/api/2.0/lakeview/dashboards", "dashboards", "dashboards"),
])
def test_truncated_live_census_cannot_complete_final_outputs(
    source, monkeypatch, continuation, path, key, kind,
):
    emitter, _owner = source
    from server.credentials import credential_manager

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    monkeypatch.setattr(stats, "_workspace_resources", REAL_CENSUS)
    monkeypatch.setattr(credential_manager, "token", lambda: "test-census-token")
    monkeypatch.setattr(stats.requests, "get", lambda url, **_kwargs: Response(
        {key: [{"name": "first-page-resource"}], **continuation}
        if url.endswith(path) else {}))
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is False
    assert result["issues"] == ["final_resource_observation_unavailable"]
    assert kind not in result["outputs"]["stats"]["resource_observation"]
    assert not events(emitter)


def test_explicit_last_census_page_is_confirmed(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"apps": [{"name": "only-app"}], "has_more": False, "next_page_token": ""}

    monkeypatch.setattr(stats.requests, "get", lambda *_args, **_kwargs: Response())
    assert stats._list_checked("https://workspace/api/2.0/apps", "token", {}, "apps") == (
        [{"name": "only-app"}], True)


def test_unreadable_artifact_is_explicit_incomplete_and_recoverable(source, monkeypatch):
    emitter, _owner = source
    monkeypatch.setattr(artifacts, "harvest_user", lambda *_args, **_kwargs: Harvest(
        issues=["document_read_failed"]))
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is False and result["issues"] == ["document_read_failed"]
    assert not events(emitter)
    monkeypatch.setattr(artifacts, "harvest_user", lambda *_args, **_kwargs: Harvest())
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is True
    assert result["outputs"]["summary_facts"][BINDING["owner"]] == {"status": "no_material"}


def test_capture_disabled_is_incomplete(source, monkeypatch):
    emitter, _owner = source
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "false")
    assert closeout_outputs.finalize(emitter, "capture-one") == {
        "complete": False, "issues": ["capture_disabled"], "outputs": {},
    }
    assert not events(emitter)


def test_lost_active_home_and_retained_code_are_not_fresh_facts(source, monkeypatch):
    emitter, owner = source
    owner.first_seen = time.time() - 120
    owner._capture_recovered = True
    monkeypatch.setattr(owner, "home", "/unavailable/previous-container-home")
    result = closeout_outputs.finalize(emitter, "capture-one")
    assert result["complete"] is False
    assert set(result["issues"]) >= {"final_artifact_home_unavailable", "final_code_observation_unavailable"}


def test_background_summary_drain_waits_for_accepted_model_work(source, monkeypatch):
    _emitter, owner = source
    inside, release = threading.Event(), threading.Event()

    def model(*_args):
        inside.set()
        assert release.wait(timeout=5)
        return {"headline": "Completed earlier accepted work"}, "system.ai.claude-haiku-4-5"

    monkeypatch.setattr(insight_summary, "_ask_model", model)
    thread = insight_summary.summarise_in_background([owner], phase="build", force=True)
    try:
        assert inside.wait(timeout=5)
        held = insight_summary.drain_for_closeout(0)
        assert held["complete"] is False and held["issues"] == ["summary_drain_timeout"]
    finally:
        release.set()
        thread.join(timeout=5)
    result = insight_summary.drain_for_closeout(1)
    assert result["complete"] is True
    assert result["outcomes"][-1] == {"writer": "insight_summary", "outcome": "completed"}


def test_rolling_summary_does_not_start_after_barrier(source):
    emitter, owner = source

    def closing(state):
        value = state.setdefault("closeout", {"schema_version": 1, "phase": "OPEN",
                                              "writers": {}, "writer_outcomes": []})
        value.update(phase="CLOSING", capture_id="capture-one")

    control_transaction(emitter.journal, closing)
    assert insight_summary.summarise_in_background([owner], phase="build", force=True) is None
    assert insight_summary.drain_for_closeout(0)["complete"] is True


def test_thread_start_failure_releases_durable_reservation_as_failure(source, monkeypatch):
    emitter, owner = source
    monkeypatch.setattr(threading.Thread, "start", lambda _thread: (_ for _ in ()).throw(
        RuntimeError("cannot start")))
    with pytest.raises(RuntimeError, match="cannot start"):
        insight_summary.summarise_in_background([owner], phase="build", force=True)
    closeout = emitter.journal.read()["closeout"]
    assert closeout["writers"] == {}
    assert closeout["writer_outcomes"][-1]["status"] == "failed"


@pytest.mark.parametrize("timeout", [-1, True, "1", float("nan"), float("inf")])
def test_drain_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        insight_summary.drain_for_closeout(timeout)


def test_real_harvest_reports_read_errors_without_paths_or_exception_text(source, monkeypatch):
    _emitter, owner = source
    # Restore the real scanner: a present transcript that cannot be opened must
    # not look like the attendee supplied no prompts.
    path = Path(owner.home, ".claude", "session.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"role": "user", "content": "Our customer use case"}) + "\n")
    import builtins
    original_open = builtins.open

    def unreadable(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError("sensitive path and token")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", unreadable)
    result = REAL_HARVEST(owner)
    assert result.issues == ["transcript_read_failed"]
    assert "sensitive" not in str(result.issues)
