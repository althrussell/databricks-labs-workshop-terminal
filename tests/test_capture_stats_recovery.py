"""Derived workshop counters survive fresh app processes without attendee login."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest

from server import stats, users
from server.capture_journal import (
    CaptureHeldError, CaptureJournal, CaptureStorageError,
    LocalCaptureStorage, MemoryCaptureStorage,
)
from server.event_emitter import EventEmitter

BINDING = {"run_id": "run-one", "unit_id": "unit-one", "workspace_id": "123",
           "owner": "alice@example.com"}


def _setup(monkeypatch, storage=None):
    storage = storage if storage is not None else MemoryCaptureStorage()
    journal = CaptureJournal(storage, BINDING, {})
    manager = users.UserManager()
    monkeypatch.setattr(users, "user_manager", manager)
    manager.configure_capture(journal, BINDING["owner"])
    stats.configure_capture(journal)
    emitter = EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                           ingest_url="", ingest_token="", journal=journal)
    return manager, manager.peek(BINDING["owner"]), emitter


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setattr(stats.config, "discovery_enabled", lambda: False)
    stats._code_cache.clear()
    yield
    stats._code_cache.clear()


def _output(monkeypatch, user, tmp_path):
    user.home = str(tmp_path / "home")
    project = tmp_path / "home" / "projects" / "demo"
    (project / ".git").mkdir(parents=True)
    (project / "app.py").write_text("print('hello')\n")
    monkeypatch.setattr(stats, "_git", lambda _repo, *args: "3" if args[0] == "rev-list" else "app.py\n")
    monkeypatch.setattr(stats, "_workspace_resources", lambda: {"jobs": 2, "apps": 1})
    user.first_seen, user.last_seen = 100.0, 200.0
    user.sessions_launched = {"bash": 2, "claude": 3}
    user.topics, user.topic_hits = {"lakebase": 150.0}, {"lakebase": 7}
    user.errors = 2
    harvest = stats.gather_all([user])
    return harvest


def test_headless_owner_registration_does_not_bootstrap_or_invent_activity(monkeypatch):
    monkeypatch.setattr(users.User, "bootstrap_home", lambda _self: pytest.fail("must not bootstrap"))
    manager, user, _emitter = _setup(monkeypatch)
    assert manager.all() == [user]
    assert user.first_seen == user.last_seen == 0
    assert user.sessions_launched == {}
    assert stats.gather_user(user)["minutes_building"] == 0


def test_users_recover_all_counters_in_fresh_process_without_home(monkeypatch, tmp_path):
    storage = LocalCaptureStorage(tmp_path / "retained")
    manager, user, emitter = _setup(monkeypatch, storage)
    harvest = _output(monkeypatch, user, tmp_path)
    stats.emit_signals(harvest, emitter)
    manager.checkpoint(user)
    code = """
import json, sys
from server import users, stats
from server.capture_journal import CaptureJournal, LocalCaptureStorage
j = CaptureJournal(LocalCaptureStorage(sys.argv[1]), json.loads(sys.argv[2]), {})
m = users.UserManager()
m.configure_capture(j, j.binding['owner'])
u = m.all()[0]
u.home = sys.argv[3]
stats._workspace_resources = lambda: stats._Census()
print(json.dumps({'user': users.user_snapshot(u), 'stats': stats.gather_all([u])}))
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "retained"),
                             json.dumps(BINDING), str(tmp_path / "new-container")],
                            cwd=os.getcwd(), capture_output=True, text=True, check=True)
    recovered = json.loads(result.stdout)
    assert recovered["user"] == users.user_snapshot(user)
    row = recovered["stats"]["users"][0]
    assert row["code"] == harvest["users"][0]["code"] == {"projects": 1, "commits": 3, "files": 1, "lines": 1}
    assert row["signal"]["engagement"] == "builder"
    assert row["capture_observation"]["code"]["source"] == "retained"
    assert recovered["stats"]["resources"] == {"jobs": 2, "apps": 1}
    assert all(value["source"] == "retained" for value in recovered["stats"]["resource_observation"].values())


def test_empty_recreated_home_and_failed_census_preserve_confirmed_output(monkeypatch, tmp_path):
    _manager, user, emitter = _setup(monkeypatch)
    harvest = _output(monkeypatch, user, tmp_path)
    stats.emit_signals(harvest, emitter)
    _manager, recovered, _emitter = _setup(monkeypatch, emitter.journal.storage)
    recovered.home = str(tmp_path / "new-home")
    (tmp_path / "new-home" / "projects").mkdir(parents=True)
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census())
    replay = stats.gather_all([recovered])
    assert replay["users"][0]["code"] == harvest["users"][0]["code"]
    assert replay["resources"] == harvest["resources"]
    assert replay["users"][0]["capture_observation"]["code"] == {
        "source": "retained", "observed_at": harvest["users"][0]["capture_observation"]["code"]["observed_at"]}


def test_successful_empty_census_replaces_prior_counts_but_failure_does_not(monkeypatch, tmp_path):
    _manager, user, emitter = _setup(monkeypatch)
    harvest = _output(monkeypatch, user, tmp_path)
    stats.emit_signals(harvest, emitter)
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census({"jobs": 0}, confirmed={"jobs"}))
    out = stats.gather_all([user])
    assert out["resources"] == {"jobs": 0, "apps": 1}
    assert out["resource_observation"]["jobs"]["source"] == "live"
    assert out["resource_observation"]["apps"]["source"] == "retained"
    stats.emit_signals(out, emitter)
    assert emitter.journal.read()["stats"]["resources"] == {"jobs": 0, "apps": 1}


def test_signal_revisions_change_inside_one_bucket_and_retry_after_ack_restart(monkeypatch, tmp_path):
    _manager, user, emitter = _setup(monkeypatch)
    harvest = _output(monkeypatch, user, tmp_path)
    monkeypatch.setattr(stats.time, "time", lambda: 1000.0)
    stats.emit_signals(harvest, emitter)
    first = emitter.collect()["events"][0]
    stats.emit_signals(copy.deepcopy(harvest), emitter)
    assert emitter.collect()["events"] == [first]
    changed = copy.deepcopy(harvest)
    changed["users"][0]["errors"] += 1
    stats.emit_signals(changed, emitter)
    second = emitter.collect()["events"][1]
    assert second["seq"] == first["seq"] + 1
    assert second["event"]["idempotency_key"].endswith(":r2")
    emitter.acknowledge(schema_version=2, stream_id=emitter.stream_id, through_seq=2)
    _manager, _user, replay = _setup(monkeypatch, emitter.journal.storage)
    stats.emit_signals(changed, replay)
    assert replay.pending() == 0
    assert replay.journal.read()["stats"]["signals"][user.email]["revision"] == 2
    assert replay.journal.read()["insight"]["events"]["2"] == second["event"]


def test_failed_signal_acceptance_does_not_advance_counters_or_revision(monkeypatch, tmp_path):
    _manager, user, emitter = _setup(monkeypatch)
    harvest = _output(monkeypatch, user, tmp_path)
    before = emitter.journal.read()
    baseline = copy.deepcopy(user._capture_baseline)
    create = emitter.journal.storage.create
    monkeypatch.setattr(emitter.journal.storage, "create", lambda *_args: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(CaptureStorageError):
        stats.emit_signals(harvest, emitter)
    assert user._capture_baseline == baseline
    assert user.sessions_launched == {"bash": 2, "claude": 3}
    monkeypatch.setattr(emitter.journal.storage, "create", create)
    assert emitter.journal.read() == before
    stats.emit_signals(harvest, emitter)
    assert emitter.journal.read()["users"][user.email]["sessions_launched"] == user.sessions_launched


def test_redundant_signal_still_checkpoints_activity_since_snapshot(monkeypatch, tmp_path):
    _manager, user, emitter = _setup(monkeypatch)
    harvest = _output(monkeypatch, user, tmp_path)
    stats.emit_signals(harvest, emitter)
    user.topic_hits["lakebase"] += 4
    stats.emit_signals(harvest, emitter)
    assert emitter.pending() == 1
    assert emitter.journal.read()["users"][user.email]["topic_hits"] == {"lakebase": 11}
    assert user.topic_hits == {"lakebase": 11}


def test_stale_containers_add_each_process_counter_delta_once(monkeypatch):
    manager, first, emitter = _setup(monkeypatch)
    second_manager = users.UserManager()
    second_manager.configure_capture(CaptureJournal(emitter.journal.storage, BINDING, {}), BINDING["owner"])
    second = second_manager.peek(BINDING["owner"])
    first.topic_hits["sql"] = 2
    second.topic_hits["sql"] = 3
    manager.checkpoint(first)
    second_manager.checkpoint(second)
    manager.checkpoint(first)
    assert emitter.journal.read()["users"][first.email]["topic_hits"] == {"sql": 5}
    assert first.topic_hits == {"sql": 5}


def test_transient_retained_read_failure_keeps_diagnostic_harvest_available(monkeypatch, tmp_path):
    _manager, user, emitter = _setup(monkeypatch)
    harvest = _output(monkeypatch, user, tmp_path)
    stats.emit_signals(harvest, emitter)
    user.errors += 1
    user.home = str(tmp_path / "missing-home")
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census())
    list_names = emitter.journal.storage.list_names
    monkeypatch.setattr(emitter.journal.storage, "list_names", lambda *_args: (_ for _ in ()).throw(OSError("offline")))
    diagnostic = stats.gather_all([user])
    row = diagnostic["users"][0]
    assert row["errors"] == 3
    assert row["terminal_sessions"] == 5
    assert row["capture_observation"]["journal"] == "unavailable"
    assert row["capture_observation"]["code"] == {"source": "unavailable", "observed_at": 0}
    assert diagnostic["resources"] == {}
    with pytest.raises(CaptureStorageError):
        stats.emit_signals(diagnostic, emitter)
    monkeypatch.setattr(emitter.journal.storage, "list_names", list_names)
    with pytest.raises(CaptureStorageError, match="retry the harvest"):
        stats.emit_signals(diagnostic, emitter)
    restored = stats.gather_all([user])
    assert restored["users"][0]["code"] == harvest["users"][0]["code"]
    assert restored["resources"] == harvest["resources"]


def test_failed_git_observation_does_not_replace_confirmed_code(monkeypatch, tmp_path):
    _manager, user, emitter = _setup(monkeypatch)
    harvest = _output(monkeypatch, user, tmp_path)
    stats.emit_signals(harvest, emitter)
    monkeypatch.setattr(stats, "_git", lambda *_args: None)
    row = stats.gather_user(user)
    assert row["code"] == harvest["users"][0]["code"]
    assert row["capture_observation"]["code"]["source"] == "retained"


@pytest.mark.parametrize("section", ["users", "stats"])
def test_corrupt_derived_state_is_held_instead_of_reset(monkeypatch, section):
    manager, user, emitter = _setup(monkeypatch)
    emitter.journal.transaction(lambda state: state.update({section: []}))
    with pytest.raises(CaptureHeldError):
        manager.checkpoint(user) if section == "users" else stats.gather_user(user)
    assert emitter.journal.health()["held"] is True


def test_user_guard_blocks_bootstrap_and_memory_only_checkpoint(monkeypatch):
    manager = users.UserManager()
    def held():
        raise CaptureHeldError("recovery unavailable")
    manager.require_capture_ready(held)
    monkeypatch.setattr(users.User, "bootstrap_home", lambda _self: pytest.fail("must not bootstrap"))
    with pytest.raises(CaptureHeldError, match="recovery unavailable"):
        manager.get(BINDING["owner"])
    with pytest.raises(CaptureHeldError, match="recovery unavailable"):
        manager.checkpoint(users.User(BINDING["owner"]))
    assert manager.all() == []
