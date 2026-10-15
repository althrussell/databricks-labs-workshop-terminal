"""Existing identity outputs remain atomic and recover after acknowledgement."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from server import attendee_names, identity, operational
from server.capture_journal import (
    CaptureCapacityError,
    CaptureHeldError,
    CaptureJournal,
    CaptureStorageError,
    LocalCaptureStorage,
    MemoryCaptureStorage,
)
from server.event_emitter import EventEmitter

BINDING = {
    "run_id": "run-identity",
    "unit_id": "unit-identity",
    "workspace_id": "123",
    "owner": "alice@example.com",
}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    identity.reset_for_tests()
    attendee_names.reset_for_tests()
    yield
    identity.reset_for_tests()
    attendee_names.reset_for_tests()


def emitter(storage=None, **kwargs):
    retained = CaptureJournal(
        storage if storage is not None else MemoryCaptureStorage(), BINDING, {}
    )
    return EventEmitter(
        run_id=BINDING["run_id"],
        workspace_id=BINDING["workspace_id"],
        ingest_url="",
        ingest_token="",
        journal=retained,
        **kwargs,
    )


def test_names_and_exact_event_recover_headlessly_after_ack_in_new_process(tmp_path):
    retained = emitter(LocalCaptureStorage(tmp_path / "workspace"))
    attendee_names.configure_capture(retained)
    first_home = str(tmp_path / "discarded-container")
    assert (
        attendee_names.capture(BINDING["owner"], first_home, "Priya Raman", "wizard")
        is True
    )
    original = retained.collect()["events"]
    retained.acknowledge(schema_version=2, stream_id=retained.stream_id, through_seq=1)
    code = """
import json,sys
from server import attendee_names
from server.capture_journal import CaptureJournal,LocalCaptureStorage
from server.event_emitter import EventEmitter
j=CaptureJournal(LocalCaptureStorage(sys.argv[1]),json.loads(sys.argv[2]),{})
e=EventEmitter(run_id=j.binding['run_id'],workspace_id=j.binding['workspace_id'],ingest_url='',ingest_token='',journal=j)
attendee_names.configure_capture(e)
print(json.dumps({'names':[x.to_json() for x in attendee_names.read('/fresh-container-no-local-files')], 'state':j.read()}))
"""
    env = {**os.environ, "WORKSHOP_INSIGHT_CAPTURE": "true"}
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "workspace"), json.dumps(BINDING)],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    recovered = json.loads(result.stdout)
    assert recovered["names"] == original[0]["event"]["payload"]["names"]
    assert recovered["state"]["insight"]["events"]["1"] == original[0]["event"]
    replacement = emitter(LocalCaptureStorage(tmp_path / "workspace"))
    attendee_names.configure_capture(replacement)
    assert (
        attendee_names.capture(
            BINDING["owner"], str(tmp_path / "fresh"), "Priya Raman", "wizard"
        )
        is False
    )
    assert (
        attendee_names.capture(
            BINDING["owner"], str(tmp_path / "fresh"), "P. Raman", "certificate"
        )
        is True
    )
    assert replacement.collect(after=1, stream=replacement.stream_id)["events"][0][
        "event"
    ]["idempotency_key"].endswith(":2")


def test_failed_name_append_changes_neither_retained_profile_nor_local_mirror(
    tmp_path, monkeypatch
):
    store = MemoryCaptureStorage()
    retained = emitter(store)
    attendee_names.configure_capture(retained)
    home = str(tmp_path)
    attendee_names.capture(BINDING["owner"], home, "Priya", "wizard")
    before = retained.journal.read()["attendee_names"]
    local_before = (tmp_path / ".workshop" / "names.json").read_bytes()
    original = store.create
    monkeypatch.setattr(
        store, "create", lambda *_args: (_ for _ in ()).throw(OSError("offline"))
    )
    with pytest.raises(CaptureStorageError):
        attendee_names.capture(BINDING["owner"], home, "P. Raman", "certificate")
    assert (tmp_path / ".workshop" / "names.json").read_bytes() == local_before
    monkeypatch.setattr(store, "create", original)
    assert retained.journal.read()["attendee_names"] == before
    assert (
        attendee_names.capture(BINDING["owner"], home, "P. Raman", "certificate")
        is True
    )
    assert retained.journal.read()["attendee_names"][BINDING["owner"]]["revision"] == 2


def test_full_queue_does_not_change_current_name_profile(tmp_path):
    retained = emitter(max_buffer=1)
    attendee_names.configure_capture(retained)
    attendee_names.capture(BINDING["owner"], str(tmp_path), "Priya", "wizard")
    before = retained.journal.read()["attendee_names"]
    with pytest.raises(CaptureCapacityError):
        attendee_names.capture(
            BINDING["owner"], str(tmp_path), "P. Raman", "certificate"
        )
    assert retained.journal.read()["attendee_names"] == before
    assert [name.name for name in attendee_names.read(str(tmp_path))] == ["Priya"]


def test_consent_off_name_never_enters_retained_journal(tmp_path, monkeypatch):
    retained = emitter()
    attendee_names.configure_capture(retained)
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "false")
    assert (
        attendee_names.capture(BINDING["owner"], str(tmp_path), "Priya", "wizard")
        is True
    )
    assert "attendee_names" not in retained.journal.read()
    assert retained.pending() == 0


def test_semantically_invalid_names_hold_journal():
    retained = emitter()
    retained.journal.transaction(
        lambda state: state.update(
            {"attendee_names": {BINDING["owner"]: {"revision": 2, "observations": []}}}
        )
    )
    with pytest.raises(CaptureHeldError, match="revision"):
        attendee_names.configure_capture(retained)
    assert retained.journal.health()["held"] is True


def test_identity_snapshot_and_revision_continue_after_ack_and_replacement(tmp_path):
    store = LocalCaptureStorage(tmp_path)
    retained = emitter(store)
    identity.configure_capture(retained)
    first = {
        "attendee": BINDING["owner"],
        "planes": {"workshop_terminal": {"databricks": "app-principal"}},
        "resolved_at": 10.0,
    }
    identity._emit(first)
    original = retained.collect()["events"]
    retained.acknowledge(schema_version=2, stream_id=retained.stream_id, through_seq=1)
    identity.reset_for_tests()
    replacement = emitter(LocalCaptureStorage(tmp_path))
    identity.configure_capture(replacement)
    assert identity.snapshot(BINDING["owner"]) == first
    assert identity.all_snapshots() == [first]
    second = {**first, "resolved_at": 20.0}
    identity._emit(second)
    assert (
        replacement.journal.read()["identity_snapshots"][BINDING["owner"]]["revision"]
        == 2
    )
    assert replacement.journal.read()["insight"]["events"]["1"] == original[0]["event"]
    assert replacement.collect(after=1, stream=replacement.stream_id)["events"][0][
        "event"
    ]["idempotency_key"].endswith(":2")


def test_failed_cli_identity_event_does_not_cache_snapshot(monkeypatch):
    store = MemoryCaptureStorage()
    retained = emitter(store)
    identity.configure_capture(retained)
    first = {"attendee": BINDING["owner"], "planes": {}, "resolved_at": 10.0}
    identity._emit(first)
    original = store.create
    monkeypatch.setattr(
        store, "create", lambda *_args: (_ for _ in ()).throw(OSError("offline"))
    )
    with pytest.raises(CaptureStorageError):
        identity._emit({**first, "resolved_at": 20.0})
    assert identity._snapshots[BINDING["owner"]] == first
    monkeypatch.setattr(store, "create", original)
    assert identity.snapshot(BINDING["owner"]) == first


def test_invalid_cli_identity_revision_holds_journal():
    retained = emitter()
    retained.journal.transaction(
        lambda state: state.update(
            {"identity_snapshots": {BINDING["owner"]: {"revision": 0, "snapshot": {}}}}
        )
    )
    with pytest.raises(CaptureHeldError, match="revision"):
        identity.configure_capture(retained)
    assert retained.journal.health()["held"] is True


def test_operational_retries_original_failed_sample_and_reports_failure(caplog):
    calls = []

    class Sink:
        available = False

        def emit(self, kind, attendee, payload, *, idempotency_key):
            calls.append((kind, attendee, payload, idempotency_key))
            if not self.available:
                raise CaptureStorageError("offline")

    sink = Sink()
    snapshots = []

    def sample():
        snapshots.append(len(snapshots) + 1)
        return {"generation": snapshots[-1]}

    reporter = operational.OperationalHealthReporter(sink, snapshot_fn=sample)
    assert reporter.emit_once() is False
    assert reporter.status() == {
        "pending": True,
        "last_error": "CaptureStorageError",
        "failed_attempts": 1,
        "last_successful_at": None,
    }
    assert "was not accepted; retry required" in caplog.text
    sink.available = True
    assert reporter.emit_once() is True
    assert calls[0] == calls[1]
    assert snapshots == [1]
    assert reporter.status()["pending"] is False
    assert reporter.status()["last_successful_at"] is not None


def test_startup_imports_legacy_current_names_headlessly_and_keeps_gap_visible(
    tmp_path, monkeypatch
):
    from server.users import email_slug

    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    home = os.path.join(
        attendee_names.config.users_root(), email_slug(BINDING["owner"])
    )
    attendee_names.observe(home, "Priya", "wizard")
    retained = emitter()
    attendee_names.configure_capture(retained)
    state = retained.journal.read()
    assert (
        state["attendee_names"][BINDING["owner"]]["observations"][0]["name"] == "Priya"
    )
    assert state["producer_issues"] == ["legacy_attendee_names_delivery_unverified"]
    assert state["insight"]["events"]["1"]["type"] == "attendee.identity"


def test_corrupt_legacy_names_cannot_initialize_empty_profile(tmp_path, monkeypatch):
    from server.users import email_slug

    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    home = os.path.join(
        attendee_names.config.users_root(), email_slug(BINDING["owner"])
    )
    path = attendee_names.names_path(home)
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{broken legacy names")
    retained = emitter()
    with pytest.raises(CaptureHeldError, match="legacy attendee names"):
        attendee_names.configure_capture(retained)
    assert retained.journal.health()["held"] is True
    assert "attendee_names" not in emitter(retained.journal.storage).journal.read()
