"""Same-operation recovery must neither lose output nor duplicate revisions."""

from __future__ import annotations

import copy
import json

import pytest
import requests

from server.capture_io import storage_failure
from server.capture_journal import (
    CaptureHeldError, CaptureJournal, CaptureStorageError, MemoryCaptureStorage,
)
from server.event_emitter import EventEmitter


BINDING = {"run_id": "run-io", "unit_id": "unit-io", "workspace_id": "123",
           "owner": "attendee@example.com"}


class FaultStore(MemoryCaptureStorage):
    def __init__(self):
        super().__init__()
        self.list_failures = self.read_failures = self.create_failures = 0
        self.invisible_reads = 0
        self.lost_create_reply = False
        self.create_attempts = []
        self.error = TimeoutError("private payload URL token attendee")

    def list_names(self):
        if self.list_failures:
            self.list_failures -= 1
            raise self.error
        return super().list_names()

    def read(self, name):
        if self.read_failures:
            self.read_failures -= 1
            raise self.error
        if self.invisible_reads:
            self.invisible_reads -= 1
            return None
        return super().read(name)

    def create(self, name, content):
        self.create_attempts.append((name, bytes(content)))
        if self.create_failures:
            self.create_failures -= 1
            raise self.error
        result = super().create(name, content)
        if self.lost_create_reply:
            self.lost_create_reply = False
            raise self.error
        return result


def emitter(store):
    return EventEmitter(
        run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
        ingest_url="", ingest_token="", journal=CaptureJournal(store, BINDING, {}),
    )


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr("server.capture_journal.time.sleep", lambda _delay: None)


@pytest.mark.parametrize("operation", ["list", "read", "create"])
def test_one_transient_recovers_exact_operation_without_rerunning_producer(operation):
    store = FaultStore()
    source = emitter(store)
    store.create_attempts.clear()
    setattr(store, f"{operation}_failures", 1)
    calls = 0

    def prepare(state):
        nonlocal calls
        calls += 1
        revision = state.setdefault("producer", {}).get("revision", 0) + 1
        state["producer"]["revision"] = revision
        return ({"revision": revision, "value": "exact accepted content"}, f"revision:{revision}")

    assert source.emit_transaction("workshop.signal", BINDING["owner"], prepare) == 1
    state = source.journal.read()
    assert calls == 1
    assert state["producer"]["revision"] == 1
    assert state["insight"]["next_seq"] == 1
    assert state.get("producer_issues", []) == []
    assert source.delivery_status()["retention_ready"] is True
    # A retry of an uncertain write never manufactures a new operation ID,
    # timestamp, logical key or payload by invoking prepare again.
    assert len({(name, raw) for name, raw in store.create_attempts}) == 1
    assert emitter(store).collect()["events"] == source.collect()["events"]
    failure = source.journal.health()["last_storage_failure"]
    assert failure == {"operation": operation, "http_status": None,
                       "timeout": "timeout", "attempts": 1, "retryable": True}


def test_lost_create_reply_verifies_before_any_resubmission():
    store = FaultStore()
    source = emitter(store)
    store.create_attempts.clear()
    store.lost_create_reply = True
    assert source.emit("heartbeat", "system", {"keep": [1]}, idempotency_key="same") == 1
    assert len(store.create_attempts) == 1
    assert source.delivery_status()["retention_ready"] is True


def test_brief_post_create_invisibility_confirms_exact_bytes_without_new_write():
    store = FaultStore()
    source = emitter(store)
    store.create_attempts.clear()
    store.invisible_reads = 1
    assert source.emit("heartbeat", "system", {"keep": [1]}, idempotency_key="same") == 1
    assert len(store.create_attempts) == 1
    assert source.delivery_status()["retention_ready"] is True
    assert source.journal.health()["last_storage_failure"] == {
        "operation": "create_readback", "http_status": None, "timeout": "none",
        "attempts": 1, "retryable": True,
    }


def test_exhausted_post_create_invisibility_retains_diagnostics_and_refusal():
    store = FaultStore()
    source = emitter(store)
    store.invisible_reads = 3
    with pytest.raises(CaptureStorageError) as failed:
        source.emit("heartbeat", "system", {"keep": [1]}, idempotency_key="same")
    assert failed.value.diagnostics == {
        "operation": "create_readback", "http_status": None, "timeout": "none",
        "attempts": 3, "retryable": True,
    }
    assert source.delivery_status()["retention_ready"] is False
    assert source.journal.read()["insight"]["next_seq"] == 1


def test_skip_after_transient_preflight_does_not_assert_lost_output():
    store = FaultStore()
    source = emitter(store)
    store.list_failures = 1
    assert source.emit_transaction("heartbeat", "system", lambda _state: None) == 0
    assert source.journal.read()["insight"]["next_seq"] == 0
    assert source.delivery_status()["retention_ready"] is True


@pytest.mark.parametrize("operation", ["list", "read", "create"])
def test_exhaustion_retains_failure_and_never_clears_historical_marker(operation):
    store = FaultStore()
    source = emitter(store)
    setattr(store, f"{operation}_failures", 3)
    with pytest.raises(CaptureStorageError) as failed:
        source.emit("heartbeat", "system", {"keep": [1]}, idempotency_key="same")
    assert failed.value.diagnostics == {
        "operation": operation, "http_status": None, "timeout": "timeout",
        "attempts": 3, "retryable": True,
    }
    state = source.journal.read()
    issues = copy.deepcopy(state["producer_issues"])
    assert len(issues) == 1
    assert f":operation={operation}:status=unknown:timeout=timeout:attempts=3" in issues[0]
    assert "private" not in issues[0]
    # A readback failure may occur after commit; an exact logical retry must
    # reuse that event. It must not erase the earlier unresolved assertion.
    assert source.emit("heartbeat", "system", {"keep": [1]}, idempotency_key="same") == 1
    replacement = emitter(store)
    assert replacement.journal.read()["insight"]["next_seq"] == 1
    assert replacement.journal.read()["producer_issues"] == issues
    assert replacement.delivery_status()["retention_ready"] is False


def test_permission_failure_is_not_retried_and_exposes_no_native_message():
    response = requests.Response()
    response.status_code = 403
    response.url = "https://private.invalid/token/secret"
    store = FaultStore()
    source = emitter(store)
    store.error = requests.HTTPError("private attendee and token", response=response)
    store.list_failures = 1
    with pytest.raises(CaptureStorageError) as failed:
        source.emit("heartbeat", "system", {})
    assert failed.value.diagnostics == {
        "operation": "list", "http_status": 403, "timeout": "none",
        "attempts": 1, "retryable": False,
    }
    public = json.dumps(source.delivery_status())
    assert ":operation=list:status=403:timeout=none:attempts=1" in public
    assert "private" not in public and "token" not in public and "attendee and" not in public


def test_sdk_unknown_status_remains_unknown_and_data_loss_is_not_retryable():
    from databricks.sdk.errors import DataLoss, TemporarilyUnavailable

    assert storage_failure(TemporarilyUnavailable("private"), operation="list", attempts=1) == {
        "operation": "list", "http_status": None, "timeout": "none",
        "attempts": 1, "retryable": True,
    }
    assert storage_failure(DataLoss("private"), operation="read", attempts=1)["retryable"] is False
    assert storage_failure(DataLoss("private", status_code=500), operation="read", attempts=1)["retryable"] is False


def test_outermost_native_status_wins_over_an_earlier_nested_http_failure():
    forbidden_response, unavailable_response = requests.Response(), requests.Response()
    forbidden_response.status_code, unavailable_response.status_code = 403, 503
    forbidden = requests.HTTPError("private", response=forbidden_response)
    forbidden.__cause__ = requests.HTTPError("private", response=unavailable_response)
    failure = storage_failure(forbidden, operation="list", attempts=1)
    assert failure["http_status"] == 403 and failure["retryable"] is False


@pytest.mark.parametrize("sdk_type", ["PermissionDenied", "Unauthenticated", "BadRequest"])
@pytest.mark.parametrize("older_failure", ["timeout", "503"])
def test_terminal_sdk_response_does_not_inherit_an_older_transient(sdk_type, older_failure):
    from databricks.sdk import errors

    terminal = getattr(errors, sdk_type)("private")
    if older_failure == "timeout":
        terminal.__cause__ = TimeoutError("private")
    else:
        response = requests.Response()
        response.status_code = 503
        terminal.__cause__ = requests.HTTPError("private", response=response)
    failure = storage_failure(terminal, operation="list", attempts=1)
    assert failure["http_status"] is None and failure["retryable"] is False


def test_slow_successful_create_still_gets_read_only_visibility_confirmation(monkeypatch):
    store = FaultStore()
    source = emitter(store)
    create = store.create
    now = [0.0]
    monkeypatch.setattr("server.capture_journal.time.monotonic", lambda: now[0])

    def slow_create(name, raw):
        result = create(name, raw)
        now[0] += 6.0
        return result

    monkeypatch.setattr(store, "create", slow_create)
    store.create_attempts.clear()
    store.invisible_reads = 1
    assert source.emit("heartbeat", "system", {"keep": [1]}, idempotency_key="same") == 1
    assert len(store.create_attempts) == 1
    assert source.delivery_status()["retention_ready"] is True


def test_slow_sdk_failure_does_not_multiply_existing_timeout_budget(monkeypatch):
    now = iter([0.0, 61.0])
    monkeypatch.setattr("server.capture_journal.time.monotonic", lambda: next(now))
    pauses = []
    monkeypatch.setattr("server.capture_journal.time.sleep", pauses.append)
    assert CaptureJournal._retry_storage({"retryable": True}, 1, 0.0) is True
    assert CaptureJournal._retry_storage({"retryable": True}, 1, 0.0) is False
    assert pauses == [0.05]


def test_transient_initialization_retries_before_prepare_without_losing_intended_event():
    store = FaultStore()
    source = EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                          ingest_url="", ingest_token="")
    attempts = 0

    def initialize():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("private")
        source.configure_journal(CaptureJournal(store, BINDING, {}))

    source.defer_initialization(initialize, "pending")
    assert source.emit("heartbeat", "system", {"same": True}, idempotency_key="intended") == 1
    assert attempts == 2
    event = source.collect()["events"][0]["event"]
    assert event["payload"] == {"same": True} and event["idempotency_key"] == "intended"
    assert source.delivery_status()["retention_ready"] is True


def test_exhausted_initialization_is_retained_after_later_attachment_and_restart():
    store = FaultStore()
    source = EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                          ingest_url="", ingest_token="")
    blocked = True
    attempts = 0

    def initialize():
        nonlocal attempts
        attempts += 1
        if blocked:
            raise TimeoutError("private")
        source.configure_journal(CaptureJournal(store, BINDING, {}))

    source.defer_initialization(initialize, "pending")
    with pytest.raises(CaptureHeldError):
        source.emit("heartbeat", "system", {"not accepted": True}, idempotency_key="intended")
    assert attempts == 3 and not store.objects
    blocked = False
    assert source.delivery_status()["retention_ready"] is False
    state = source.journal.read()
    assert state["insight"]["next_seq"] == 0
    assert ":operation=initialize:status=unknown:timeout=timeout:attempts=3" in state["producer_issues"][0]
    replacement = emitter(store)
    assert replacement.delivery_status()["retention_ready"] is False
    assert replacement.journal.read()["producer_issues"] == state["producer_issues"]
