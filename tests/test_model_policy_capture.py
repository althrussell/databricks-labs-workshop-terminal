"""CT policy survives a replacement container without trusting its local cache."""

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import capture, config, model_policy
from server.capture_journal import CaptureHeldError, CaptureJournal, CaptureStorageError, WorkspaceCaptureStorage
from server.closeout_guard import CaptureClosedError, control_transaction, initial_state
from .test_capture_startup import BINDING, WorkspaceFiles, capture_modules, emitter  # noqa: F401
from .test_governed_model_policy import _body


def request(revision=1, *, denied=None):
    body = _body(revision)
    if denied is not None:
        body["denied_models"] = denied
    return model_policy.ModelPolicyRequest.model_validate(body)


def attach(files):
    retained = emitter()
    capture.configure(retained, client=SimpleNamespace(workspace=files))
    assert retained.delivery_status()["retention_ready"] is True
    return retained


def another_store(files):
    journal = CaptureJournal(WorkspaceCaptureStorage(SimpleNamespace(workspace=files),
        capture.workspace_path(capture.source_binding())), capture.source_binding(), {})
    store = model_policy.PolicyStore()
    store.configure_capture(journal)
    return store, journal


def test_full_policy_survives_empty_replacement_home_and_process(tmp_path, monkeypatch):
    files = WorkspaceFiles()
    original = attach(files)
    accepted, changed = model_policy.store.apply(request(7))
    assert changed is True
    before = copy.deepcopy(original.journal.read()["model_policy"])
    epoch = original.stream_id
    assert Path(config.data_root(), "model-policy-v1.json").exists()

    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "empty-replacement"))
    model_policy.store.reset_for_tests()
    replacement = attach(files)
    assert not Path(config.data_root()).exists()
    assert model_policy.store.snapshot() == accepted
    assert replacement.stream_id == epoch
    assert replacement.journal.read()["model_policy"] == before
    assert set(before) == {"schema_version", "revision", "pool", "denied_models"}
    assert before == model_policy._canonical_payload(request(7))


def test_new_source_does_not_import_the_previous_unbound_cache(monkeypatch):
    files = WorkspaceFiles()
    attach(files)
    model_policy.store.apply(request(7))
    cached = Path(config.data_root(), "model-policy-v1.json").read_bytes()
    monkeypatch.setenv("WORKSHOP_RUN_ID", BINDING["run_id"] + "-next")
    next_emitter = emitter()
    next_emitter.run_id = capture.source_binding()["run_id"]
    capture.configure(next_emitter, client=SimpleNamespace(workspace=files))
    assert next_emitter.delivery_status()["retention_ready"] is True
    assert model_policy.store.snapshot().revision == 0
    assert "model_policy" not in next_emitter.journal.read()
    assert Path(config.data_root(), "model-policy-v1.json").read_bytes() == cached


def test_unattached_production_store_refuses_an_old_local_cache(monkeypatch):
    attach(WorkspaceFiles())
    model_policy.store.apply(request(7))
    model_policy.store.reset_for_tests()
    monkeypatch.setattr(config, "local_dev", lambda: False)
    with pytest.raises(CaptureHeldError, match="not initialized"):
        model_policy.store.snapshot()
    with pytest.raises(CaptureHeldError, match="not initialized"):
        model_policy.store.apply(request(8))


def test_shared_source_reads_newer_policy_and_rejects_stale_or_changed_replays():
    files = WorkspaceFiles()
    attach(files)
    first, _ = another_store(files)
    second, _ = another_store(files)
    first.apply(request(7))
    assert second.snapshot().revision == 7
    second.apply(request(8))
    assert first.snapshot().revision == 8
    assert first.apply(request(8))[1] is False
    with pytest.raises(model_policy.StalePolicy):
        first.apply(request(7))
    with pytest.raises(model_policy.RevisionConflict):
        first.apply(request(8, denied=["system.ai.gpt-oss-20b"]))
    assert second.snapshot().revision == 8


def test_competing_containers_cas_the_authoritative_revision():
    class ContendedFiles(WorkspaceFiles):
        def __init__(self):
            super().__init__()
            self.barrier = threading.Barrier(2)
            self.calls = 0
            self.lock = threading.Lock()

        def upload(self, path, content, **kwargs):
            changes_policy = any(row["path"] and row["path"][0] == "model_policy"
                                 for row in json.loads(content).get("patch", []))
            with self.lock:
                if changes_policy:
                    self.calls += 1
                wait = changes_policy and self.calls <= 2
            if wait:
                self.barrier.wait(timeout=5)
            return super().upload(path, content, **kwargs)

    files = ContendedFiles()
    attach(files)
    first, _ = another_store(files)
    second, _ = another_store(files)

    def apply(store, desired):
        try:
            return store.apply(desired)[0].fingerprint
        except model_policy.RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(apply, first, request(7)),
                   workers.submit(apply, second, request(7, denied=["system.ai.gpt-oss-20b"]))]
        results = [future.result(timeout=10) for future in futures]
    assert results.count("conflict") == 1
    assert first.snapshot() == second.snapshot()


class FailingFiles(WorkspaceFiles):
    fail_policy = False
    fail_reads = False

    def list(self, path):
        if self.fail_reads:
            raise PermissionError("primary unavailable")
        return super().list(path)

    def upload(self, path, content, **kwargs):
        if self.fail_policy and any(row["path"] and row["path"][0] == "model_policy"
                                    for row in json.loads(content).get("patch", [])):
            raise PermissionError("policy commit unavailable")
        return super().upload(path, content, **kwargs)


def test_primary_failure_never_accepts_or_falls_back_to_cached_policy():
    files = FailingFiles()
    attach(files)
    accepted, _ = model_policy.store.apply(request(7))
    cached = Path(config.data_root(), "model-policy-v1.json").read_bytes()
    files.fail_policy = True
    with pytest.raises(CaptureStorageError):
        model_policy.store.apply(request(8))
    assert Path(config.data_root(), "model-policy-v1.json").read_bytes() == cached
    files.fail_policy = False
    assert model_policy.store.snapshot() == accepted
    files.fail_reads = True
    with pytest.raises(CaptureStorageError):
        model_policy.store.snapshot()


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(revision=True),
    lambda raw: raw.update(schema_version=True),
    lambda raw: raw.update(unexpected="field"),
    lambda raw: raw["pool"][0].update(service_name="wrong-name"),
])
def test_semantically_corrupt_primary_policy_is_a_sticky_hold(mutation):
    retained = attach(WorkspaceFiles())
    model_policy.store.apply(request(7))

    def corrupt(state):
        mutation(state["model_policy"])

    retained.journal.transaction(corrupt)
    with pytest.raises(CaptureHeldError, match="retained model policy"):
        model_policy.store.snapshot()
    assert retained.journal.health()["held"] is True
    with pytest.raises(CaptureHeldError):
        model_policy.store.apply(request(8))


def test_cache_failure_cannot_reject_a_durable_acceptance(monkeypatch):
    files = WorkspaceFiles()
    retained = attach(files)

    def unavailable(_payload):
        raise OSError("disposable container is unwritable")

    monkeypatch.setattr(model_policy.store, "_write_locked", unavailable)
    accepted, changed = model_policy.store.apply(request(7))
    assert changed is True
    assert retained.journal.read()["model_policy"] == model_policy._canonical_payload(request(7))
    fresh, _ = another_store(files)
    assert fresh.snapshot() == accepted


@pytest.mark.parametrize("phase", ["CLOSING", "SEALED"])
def test_closed_source_restores_without_journal_writes_and_refuses_direct_apply(phase):
    files = WorkspaceFiles()
    retained = attach(files)
    accepted, _ = model_policy.store.apply(request(7))
    control_transaction(retained.journal, lambda state: state.update(closeout={**initial_state(), "phase": phase}))
    previous_objects = dict(files.objects)
    fresh, journal = another_store(files)
    assert fresh.snapshot() == accepted
    assert files.objects == previous_objects
    for desired in (request(7), request(8)):
        with pytest.raises(CaptureClosedError):
            fresh.apply(desired)
    assert journal.read()["model_policy"] == model_policy._canonical_payload(request(7))
    assert files.objects == previous_objects


def test_bound_policy_refuses_a_changed_source_without_reconfiguration(monkeypatch):
    attach(WorkspaceFiles())
    model_policy.store.apply(request(7))
    monkeypatch.setenv("WORKSHOP_UNIT_ID", BINDING["unit_id"] + "-changed")
    with pytest.raises(CaptureHeldError, match="does not match"):
        model_policy.store.snapshot()
