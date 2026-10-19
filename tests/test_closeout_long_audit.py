"""Long immutable replay renews ownership without blocking the live cache."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from server.capture_journal import (
    CaptureHeldError, CaptureJournal, CaptureStorageError, MemoryCaptureStorage,
    _bounded_capture_client,
)
from server.closeout import CloseoutConflict, CloseoutManager
from server.closeout_guard import CaptureClosedError, control_transaction, finalizer_scope
from server.event_emitter import EventEmitter


BINDING = {"run_id": "long-run", "unit_id": "long-unit", "workspace_id": "123",
           "owner": "alice@example.com"}


class ControlledStorage(MemoryCaptureStorage):
    def __init__(self, now):
        super().__init__()
        self.now, self.delay, self.reads, self.before_read = now, 0, [], None

    def read(self, name):
        self.reads.append(name)
        self.now[0] += self.delay
        if self.before_read:
            self.before_read(name)
        return super().read(name)


def source(storage):
    journal = CaptureJournal(storage, BINDING, {}, checkpoint_interval=16)
    return EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                        ingest_url="", ingest_token="", journal=journal)


def final_outputs(emitter, capture_id):
    sequence = emitter.emit("workshop.signal", BINDING["owner"], {"final": capture_id},
                            idempotency_key=f"final:{capture_id}")
    return {"complete": True, "issues": [], "outputs": {"final_signal_sequence": sequence}}


def manager_for(emitter, now, **kwargs):
    return CloseoutManager(emitter, clock=lambda: now[0],
        drainer=lambda _timeout: {"complete": True, "issues": [], "outcomes": []},
        finalizer=kwargs.get("finalizer", final_outputs))


def begin(manager):
    return manager.begin(capture_id="long-capture", stream_id=manager.emitter.stream_id,
                         source=BINDING, drain_timeout_seconds=1)


@pytest.fixture(autouse=True)
def capture_enabled(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")


def test_full_replay_longer_than_original_lease_renews_and_audits_every_prefix():
    now = [100.0]
    storage = ControlledStorage(now)
    emitter = source(storage)
    for counter in range(850):
        emitter.journal.transaction(lambda state: state.update(counter=counter))
    original_names = {name for name in storage.objects if not name.startswith("checkpoint-")}
    assert any(name.startswith("checkpoint-") for name in storage.objects)
    manager = manager_for(emitter, now)
    started = begin(manager)
    storage.reads.clear()
    storage.delay = 0.2
    sealed = manager.advance()
    assert now[0] > 220  # replay crossed the original claimant's 120-second expiry
    assert sealed["phase"] == "SEALED" and sealed["complete"] is True
    assert sealed["generation"] == started["generation"]
    assert original_names <= set(storage.reads)
    assert sealed["manifest"]["streams"]["insight"]["count"] == 1
    renewals = [json.loads(raw) for raw in storage.objects.values()
                if b'"expires_at"' in raw and b'"claim_id"' not in raw]
    assert len(renewals) >= 5


def test_status_and_ack_proceed_while_full_audit_is_paused_and_cache_is_never_rewound():
    now = [100.0]
    storage = ControlledStorage(now)
    emitter = source(storage)
    emitter.emit("workshop.signal", BINDING["owner"], {"accepted": True})
    manager = manager_for(emitter, now)
    begin(manager)
    entered, release = threading.Event(), threading.Event()

    def pause_old_prefix(name):
        if name == f"{0:020d}.json":
            entered.set()
            assert release.wait(timeout=5)

    storage.before_read = pause_old_prefix
    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(manager.advance)
        try:
            assert entered.wait(timeout=5)
            status = executor.submit(manager.status).result(timeout=1)
            assert status["phase"] == "CLOSING"
            emitter.acknowledge(schema_version=2, stream_id=emitter.stream_id, through_seq=1)
            assert emitter.journal.read()["insight"]["acked_through"] == 1
        finally:
            release.set()
        sealed = future.result(timeout=5)
    assert sealed["complete"] is True
    assert sealed["manifest"]["streams"]["insight"]["count"] == 2
    assert emitter.journal.read()["insight"]["acked_through"] == 1


@pytest.mark.parametrize("takeover", [False, True])
def test_expired_claim_cannot_renew_or_seal_and_cannot_release_another_owner(takeover):
    now = [100.0]
    storage = ControlledStorage(now)
    emitter = source(storage)
    manager = manager_for(emitter, now)
    started = begin(manager)
    changed = [False]

    def expire_during_prefix(name):
        if name != f"{0:020d}.json" or changed[0]:
            return
        changed[0] = True
        now[0] += 121
        if takeover:
            other = CaptureJournal(storage, BINDING, {})
            control_transaction(other, lambda state: state["closeout"].update(
                finalizer={"claim_id": "new-owner", "expires_at": now[0] + 120}))

    storage.before_read = expire_during_prefix
    with pytest.raises((CaptureClosedError, CloseoutConflict), match="ownership changed"):
        manager.advance()
    current = emitter.journal.read()["closeout"]
    assert current["phase"] == "CLOSING" and "manifest" not in current
    if takeover:
        assert current["finalizer"]["claim_id"] == "new-owner"
        now[0] += 121
    else:
        assert "finalizer" not in current
    storage.before_read = None
    sealed = manager.advance()
    assert sealed["complete"] is True and sealed["generation"] == started["generation"]
    assert sealed["manifest"]["streams"]["insight"]["count"] == 1


def test_audit_storage_error_is_retryable_after_restart_without_duplicate_final_output():
    now = [100.0]
    storage = ControlledStorage(now)
    emitter = source(storage)
    manager = manager_for(emitter, now)
    started = begin(manager)

    def unavailable(name):
        if name == f"{0:020d}.json":
            raise TimeoutError("synthetic unavailable storage")

    storage.before_read = unavailable
    with pytest.raises(CaptureStorageError):
        manager.advance()
    assert manager.status()["phase"] == "CLOSING"
    assert manager.status()["manifest"] is None
    storage.before_read = None
    restarted = manager_for(source(storage), now)
    sealed = restarted.advance()
    assert sealed["complete"] is True and sealed["generation"] == started["generation"]
    assert sealed["manifest"]["streams"]["insight"]["count"] == 1


@pytest.mark.parametrize("missing", [False, True])
def test_old_prefix_corruption_or_loss_is_held_despite_valid_recent_checkpoint(missing):
    now = [100.0]
    storage = ControlledStorage(now)
    emitter = source(storage)
    for counter in range(20):
        emitter.journal.transaction(lambda state: state.update(counter=counter))
    manager = manager_for(emitter, now)
    begin(manager)
    name = f"{0:020d}.json"
    if missing:
        del storage.objects[name]
    else:
        storage.objects[name] = b'{"corrupt":true}'
    with pytest.raises(CaptureHeldError):
        manager.advance()
    assert emitter.journal.health()["held"] is True


def test_unexpected_producer_tail_after_audit_cannot_enter_sealed_inventory(monkeypatch):
    now = [100.0]
    storage = ControlledStorage(now)
    emitter = source(storage)
    manager = manager_for(emitter, now)
    begin(manager)
    original = emitter.journal.audit_snapshot

    def audit_with_racing_tail(**kwargs):
        snapshot = original(**kwargs)
        read = snapshot.read

        def tail_after_verified_read():
            audited = read()
            operation = emitter.journal.read()["closeout"]
            with finalizer_scope(emitter.journal, operation["capture_id"],
                                 operation["finalizer"]["claim_id"], clock=lambda: now[0]):
                emitter.emit("workshop.signal", BINDING["owner"], {"unexpected": True})
            return audited

        snapshot.read = tail_after_verified_read
        return snapshot

    monkeypatch.setattr(emitter.journal, "audit_snapshot", audit_with_racing_tail)
    with pytest.raises(CaptureHeldError, match="changed after its full audit"):
        manager.advance()
    assert manager.status()["manifest"] is None


def test_unpersisted_rejected_output_still_prevents_seal(monkeypatch):
    now = [100.0]
    storage = ControlledStorage(now)
    emitter = source(storage)

    def finalize_with_rejected_output(emitter, capture_id):
        result = final_outputs(emitter, capture_id)
        create = storage.create

        def unavailable(*_args):
            raise OSError("synthetic unavailable storage")

        monkeypatch.setattr(storage, "create", unavailable)
        emitter._rejected(CaptureStorageError("not retained"), "required-final-output")
        monkeypatch.setattr(storage, "create", create)
        return result

    manager = manager_for(emitter, now, finalizer=finalize_with_rejected_output)
    begin(manager)
    with pytest.raises(CaptureClosedError):
        manager.advance()
    assert manager.status()["phase"] == "CLOSING"
    assert manager.status()["manifest"] is None
    assert len(emitter._pending_issues) == 1


def test_installed_sdk_capture_client_reuses_exact_identity_and_sets_best_effort_timeouts(
    tmp_path, monkeypatch,
):
    import databricks.sdk.config as sdk_config
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.config import Config

    for name in list(os.environ):
        if name.startswith("DATABRICKS_"):
            monkeypatch.delenv(name)
    config_file = tmp_path / "synthetic-databrickscfg"
    config_file.write_text("")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(sdk_config, "get_host_metadata", lambda _host: SimpleNamespace(
        account_id=None, workspace_id=None, oidc_endpoint=None, cloud=None,
        token_federation_default_oidc_audiences=[]))

    def forbidden_http(*_args, **_kwargs):
        raise AssertionError("capture constructor must not contact a workspace")

    monkeypatch.setattr("requests.sessions.Session.request", forbidden_http)
    original = WorkspaceClient(config=Config(host="https://offline.example.invalid",
        token="synthetic-offline-token", auth_type="pat",
        http_timeout_seconds=40, retry_timeout_seconds=60))
    bounded = _bounded_capture_client(original)
    assert bounded is not original
    assert bounded.config.host == original.config.host
    assert bounded.config.authenticate() == original.config.authenticate()
    assert bounded.config.http_timeout_seconds == 10
    assert bounded.config.retry_timeout_seconds == 15
    assert original.config.http_timeout_seconds == 40
    assert original.config.retry_timeout_seconds == 60

