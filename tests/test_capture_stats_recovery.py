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


def test_recovered_confirmed_zero_code_is_reobserved_in_readable_empty_home(monkeypatch, tmp_path):
    manager, user, emitter = _setup(monkeypatch)
    user.home = str(tmp_path / "original")
    (tmp_path / "original" / "projects").mkdir(parents=True)
    user.first_seen = user.last_seen = 100.0
    monkeypatch.setattr(stats, "_workspace_resources", lambda: {})
    harvest = stats.gather_all([user], fresh=True)
    stats.emit_signals(harvest, emitter)
    manager.checkpoint(user)
    original = copy.deepcopy(emitter.journal.read()["stats"]["users"][user.email])

    _manager, recovered, _emitter = _setup(monkeypatch, emitter.journal.storage)
    recovered.home = str(tmp_path / "replacement")
    (tmp_path / "replacement" / "projects").mkdir(parents=True)
    assert recovered._capture_recovered is True
    calls = []
    scan = stats._code_stats_uncached

    def observe(owner):
        calls.append(owner.email)
        return scan(owner)

    monkeypatch.setattr(stats, "_code_stats_uncached", observe)
    row = stats.gather_all([recovered], fresh=True)["users"][0]
    assert calls == [recovered.email]
    assert row["code"] == original["row"]["code"] == {
        "projects": 0, "commits": 0, "files": 0, "lines": 0,
    }
    assert row["capture_observation"]["code"]["source"] == "local"
    assert row["capture_observation"]["code"]["observed_at"] >= original["code_observed_at"]


def test_recovered_zero_code_stays_retained_when_empty_home_cannot_be_scanned(monkeypatch, tmp_path):
    _manager, user, emitter = _setup(monkeypatch)
    user.home = str(tmp_path / "home")
    (tmp_path / "home" / "projects").mkdir(parents=True)
    user.first_seen = user.last_seen = 100.0
    monkeypatch.setattr(stats, "_workspace_resources", lambda: {})
    harvest = stats.gather_all([user], fresh=True)
    stats.emit_signals(harvest, emitter)
    original = harvest["users"][0]
    user._capture_recovered = True
    monkeypatch.setattr(stats, "_code_stats_uncached", lambda _user: stats._CodeCounts(
        {"projects": 0, "commits": 0, "files": 0, "lines": 0}, confirmed=False))
    row = stats.gather_all([user], fresh=True)["users"][0]
    assert row["code"] == original["code"]
    assert row["capture_observation"]["code"] == {
        "source": "retained", "observed_at": original["capture_observation"]["code"]["observed_at"],
    }


@pytest.mark.parametrize("uncertainty", ["root_unreadable", "nested_unreadable", "nonempty_unrestored"])
def test_recovered_zero_code_requires_proven_empty_projects(monkeypatch, tmp_path, uncertainty):
    _manager, user, emitter = _setup(monkeypatch)
    user.home = str(tmp_path / "home")
    projects = tmp_path / "home" / "projects"
    projects.mkdir(parents=True)
    user.first_seen = user.last_seen = 100.0
    monkeypatch.setattr(stats, "_workspace_resources", lambda: {})
    harvest = stats.gather_all([user], fresh=True)
    stats.emit_signals(harvest, emitter)
    original = harvest["users"][0]
    user._capture_recovered = True
    listdir, isdir = stats.os.listdir, stats.os.path.isdir
    if uncertainty == "root_unreadable":
        def list_projects(path):
            if str(path) == str(projects):
                raise PermissionError("private projects path")
            return listdir(path)
        monkeypatch.setattr(stats.os, "listdir", list_projects)
    else:
        child = projects / "unresolved-project"
        child.mkdir()
        if uncertainty == "nested_unreadable":
            # isdir swallows stat errors, so an unreadable child can look as
            # though it lacks .git. Do not turn that uncertainty into zero.
            monkeypatch.setattr(stats.os.path, "isdir", lambda path: (
                False if str(path) == str(child / ".git") else isdir(path)))
    monkeypatch.setattr(stats, "_code_stats_uncached", lambda _user: pytest.fail(
        "uncertain recovered source must not be reclassified as a fresh zero"))
    row = stats.gather_all([user], fresh=True)["users"][0]
    assert row["code"] == original["code"]
    assert row["capture_observation"]["code"] == {
        "source": "retained", "observed_at": original["capture_observation"]["code"]["observed_at"],
    }


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


@pytest.mark.parametrize("extra_activity", [False, True])
def test_checkpoint_verification_failure_does_not_reapply_committed_counter_delta(monkeypatch, extra_activity):
    manager, user, emitter = _setup(monkeypatch)
    user.sessions_launched["claude"] = 1
    user.topic_hits["sql"] = 2
    user.errors = 1
    store = emitter.journal.storage
    create, read = store.create, store.read
    fail_next_read = False

    def committed_create(name, content):
        nonlocal fail_next_read
        result = create(name, content)
        fail_next_read = True
        return result

    def unavailable_verification(name):
        nonlocal fail_next_read
        if fail_next_read:
            fail_next_read = False
            raise OSError("committed operation verification unavailable")
        return read(name)

    monkeypatch.setattr(store, "create", committed_create)
    monkeypatch.setattr(store, "read", unavailable_verification)
    with pytest.raises(CaptureStorageError):
        manager.checkpoint(user)
    assert user._capture_baseline["sessions_launched"] == {}
    monkeypatch.setattr(store, "create", create)
    monkeypatch.setattr(store, "read", read)
    assert emitter.journal.read()["users"][user.email]["sessions_launched"] == {"claude": 1}
    if extra_activity:
        user.sessions_launched["claude"] += 1
        user.topic_hits["sql"] += 3
        user.errors += 1
    manager.checkpoint(user)
    saved = emitter.journal.read()["users"][user.email]
    assert saved["sessions_launched"] == {"claude": 2 if extra_activity else 1}
    assert saved["topic_hits"] == {"sql": 5 if extra_activity else 2}
    assert saved["errors"] == (2 if extra_activity else 1)
    # A successful checkpoint advances its baseline; a later no-op cannot add
    # this generation again, including after another process contributed.
    manager.checkpoint(user)
    assert emitter.journal.read()["users"][user.email] == saved


def test_omnigent_conversations_survive_a_restart_and_count_as_agent_sessions(monkeypatch):
    """F-006 (31 Oct): an Omnigent seat showed 0 agent sessions while it built."""
    manager, first, emitter = _setup(monkeypatch)
    first.omnigent_sessions["conv_a"] = 1
    first.omnigent_sessions["conv_b"] = 1
    manager.checkpoint(first)
    restarted = users.UserManager()
    restarted.configure_capture(CaptureJournal(emitter.journal.storage, BINDING, {}), BINDING["owner"])
    second = restarted.peek(BINDING["owner"])
    assert second.omnigent_sessions == {"conv_a": 1, "conv_b": 1}
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census())
    row = stats.gather_all([second])["users"][0]
    assert row["agent_sessions"] == 2
    assert row["terminal_sessions"] == 0


def test_a_restart_that_recounts_a_conversation_does_not_double_it(monkeypatch):
    manager, first, emitter = _setup(monkeypatch)
    first.omnigent_sessions["conv_a"] = 1
    manager.checkpoint(first)
    first.omnigent_sessions["conv_a"] = 1  # the log sweep sees the same conversation again
    manager.checkpoint(first)
    assert emitter.journal.read()["users"][first.email]["omnigent_sessions"] == {"conv_a": 1}


def test_a_journal_from_before_omnigent_was_counted_still_reads(monkeypatch):
    # The row as an earlier release wrote it: no omnigent_sessions key at all.
    old_row = {"topics": {}, "topic_hits": {}, "sessions_launched": {"claude": 1},
               "errors": 0, "first_seen": 1.0, "last_seen": 2.0}
    assert users.validate_user_snapshot(dict(old_row))["omnigent_sessions"] == {}
    current = users.validate_user_snapshot({**old_row, "omnigent_sessions": {"conv_a": 1}})
    merged = users.merge_user_snapshot(old_row, current, users.validate_user_snapshot(dict(old_row)))
    assert merged["omnigent_sessions"] == {"conv_a": 1}


def test_stale_container_harvest_reconciles_before_deriving_signal(monkeypatch):
    manager, first, emitter = _setup(monkeypatch)
    second_manager = users.UserManager()
    second_manager.configure_capture(CaptureJournal(emitter.journal.storage, BINDING, {}), BINDING["owner"])
    second = second_manager.peek(BINDING["owner"])
    first.sessions_launched["claude"] = 1
    first.topic_hits["sql"] = 2
    first.errors = 1
    manager.checkpoint(first)
    second.sessions_launched["claude"] = 1
    second.topic_hits["sql"] = 3
    second.errors = 2
    second_manager.checkpoint(second)
    monkeypatch.setattr(stats, "_workspace_resources", lambda: stats._Census())
    snapshot = stats.gather_all([first])
    assert snapshot["users"][0]["agent_sessions"] == 2
    assert snapshot["users"][0]["signal"]["topic_hits"] == {"sql": 5}
    assert snapshot["users"][0]["errors"] == 3
    stats.emit_signals(snapshot, emitter)
    signal = emitter.collect()["events"][0]["event"]["payload"]
    assert signal["agent_sessions"] == 2
    assert signal["signal"]["topic_hits"] == {"sql": 5}
    assert signal["errors"] == 3


def test_signal_checkpoint_retry_after_committed_but_unverified_append(monkeypatch, tmp_path):
    _manager, user, emitter = _setup(monkeypatch)
    harvest = _output(monkeypatch, user, tmp_path)
    # New output after the gathered snapshot is checkpointed in the signal's
    # atomic append, independently of the snapshot's unchanged content key.
    user.topic_hits["lakebase"] += 4
    user.errors += 1
    store = emitter.journal.storage
    create, read = store.create, store.read
    verification_pending = False
    failures = 1

    def committed_create(name, content):
        nonlocal verification_pending
        result = create(name, content)
        verification_pending = True
        return result

    def unavailable_verification(name):
        nonlocal verification_pending, failures
        if verification_pending and failures:
            verification_pending = False
            failures -= 1
            raise OSError("accepted signal could not be verified")
        return read(name)

    monkeypatch.setattr(store, "create", committed_create)
    monkeypatch.setattr(store, "read", unavailable_verification)
    with pytest.raises(CaptureStorageError):
        stats.emit_signals(harvest, emitter)
    monkeypatch.setattr(store, "create", create)
    monkeypatch.setattr(store, "read", read)
    stats.emit_signals(harvest, emitter)
    committed = emitter.journal.read()
    assert committed["insight"]["next_seq"] == 1
    assert committed["users"][user.email]["sessions_launched"] == {"bash": 2, "claude": 3}
    assert committed["users"][user.email]["topic_hits"] == {"lakebase": 11}
    assert committed["users"][user.email]["errors"] == 3


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
