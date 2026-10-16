"""Required detached saves retain their own completion status until closeout."""

from __future__ import annotations

import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from server import user_content


def drain(home, timeout=0):
    return user_content.drain_sync_for_closeout(
        [SimpleNamespace(home=str(home))], deadline=time.monotonic() + timeout)


def outcome(home, name="one.json", *, code=0, registered=100.0):
    completed = home / ".workshop" / "workspace-sync-outcomes"
    completed.mkdir(parents=True, exist_ok=True)
    path = completed / name
    path.write_text(json.dumps({"schema_version": 1, "registered_at": registered,
                                "completed_at": 101.0, "exit": code}))
    return path


def test_idle_or_never_configured_hook_has_nothing_required(tmp_path):
    assert drain(tmp_path) == {"complete": True, "issues": [], "pending": 0}
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    (hooks / "post-commit").write_text(user_content._POST_COMMIT)
    assert drain(tmp_path)["complete"] is True


def test_known_failure_is_not_hidden_by_later_success(tmp_path):
    outcome(tmp_path, "failed.json", code=23)
    outcome(tmp_path, "succeeded.json", code=0)
    (tmp_path / ".workshop" / "workspace-sync").write_text("at=102\nexit=0\ndetail=\n")
    result = drain(tmp_path)
    assert result == {"complete": False, "issues": ["workspace_sync_failed"], "pending": 0}


@pytest.mark.parametrize("raw", [
    "", "{", "{}", '{"schema_version":1,"registered_at":100,"completed_at":101,"exit":true}',
    '{"schema_version":1,"registered_at":100,"completed_at":NaN,"exit":0}',
    '{"schema_version":1,"registered_at":100,"completed_at":101,"exit":-1}',
    '{"schema_version":1,"registered_at":100,"completed_at":101,"exit":0,"detail":"private"}',
])
def test_malformed_terminal_outcome_cannot_prove_save_completed(tmp_path, raw):
    path = outcome(tmp_path)
    path.write_text(raw)
    result = drain(tmp_path)
    assert result["complete"] is False
    assert result["issues"] == ["workspace_sync_outcome_unverified"]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("raw", [None, "", "exit=0\n", "at=NaN\nexit=0\n", "at=100\nexit=not-int\n",
                                 "at=100\nexit=0\nexit=17\n", "at=100\nexit=0\ntruncated status"])
def test_registered_legacy_work_without_valid_final_status_is_unverified(tmp_path, raw):
    state = tmp_path / ".workshop"
    (state / "workspace-sync-writers").mkdir(parents=True)
    if raw is not None:
        (state / "workspace-sync").write_text(raw)
    result = drain(tmp_path)
    assert result == {"complete": False, "issues": ["workspace_sync_outcome_unverified"], "pending": 0}


@pytest.mark.parametrize("code,complete,issues", [
    (0, True, []), (17, False, ["workspace_sync_failed"]),
])
def test_legacy_protocol_checks_existing_final_status(tmp_path, code, complete, issues):
    state = tmp_path / ".workshop"
    (state / "workspace-sync-writers").mkdir(parents=True)
    (state / "workspace-sync").write_text(f"at=100\nexit={code}\ndetail=private CLI output\n")
    assert drain(tmp_path) == {"complete": complete, "issues": issues, "pending": 0}


def test_crash_after_persisted_completion_before_registration_cleanup_is_recoverable(tmp_path):
    outcome(tmp_path, "one.json")
    writers = tmp_path / ".workshop" / "workspace-sync-writers"
    writers.mkdir()
    receipt = writers / "one.json"
    receipt.write_text(json.dumps({"schema_version": 1, "registered_at": 100.0}))
    assert drain(tmp_path) == {"complete": True, "issues": [], "pending": 0}
    receipt.write_text(json.dumps({"schema_version": 1, "registered_at": 99.0}))
    result = drain(tmp_path)
    assert result["complete"] is False and result["pending"] == 1
    assert set(result["issues"]) == {"workspace_sync_outcome_unverified", "workspace_sync_workers_pending"}


def _hook_setup(tmp_path, code):
    home = tmp_path / "attendee"
    repo = home / "projects" / "demo"
    repo.mkdir(parents=True)
    state = home / ".workshop"
    state.mkdir()
    hooks = home / ".githooks"
    hooks.mkdir()
    hook = hooks / "post-commit"
    hook.write_text(user_content._POST_COMMIT.replace("{email}", "alice@example.com").replace(
        "{status_path}", str(state / "workspace-sync")))
    hook.chmod(0o755)
    shim = tmp_path / "shim"
    shim.mkdir()
    cli = shim / "databricks"
    cli.write_text(f"#!/bin/sh\necho 'private CLI failure'\nexit {code}\n")
    cli.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{shim}:{os.environ['PATH']}",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    return home, repo, hook, env


@pytest.mark.parametrize("code", [0, 23])
def test_real_detached_worker_retains_success_or_failure_before_clearing_receipt(tmp_path, code):
    home, repo, hook, env = _hook_setup(tmp_path, code)
    subprocess.run([str(hook)], cwd=repo, env=env, check=True)
    result = drain(home, timeout=5)
    assert result == {"complete": code == 0,
                      "issues": [] if code == 0 else ["workspace_sync_failed"], "pending": 0}
    completed = list((home / ".workshop" / "workspace-sync-outcomes").iterdir())
    assert len(completed) == 1
    saved = json.loads(completed[0].read_text())
    assert saved["exit"] == code and set(saved) == {
        "schema_version", "registered_at", "completed_at", "exit"}
    assert list((home / ".workshop" / "workspace-sync-writers").iterdir()) == []
    assert "private" not in json.dumps(result)
