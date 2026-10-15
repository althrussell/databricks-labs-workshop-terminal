"""Retained capture operations: atomicity, contention and replacement recovery."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from server.capture_journal import (
    CaptureCapacityError,
    CaptureHeldError,
    CaptureJournal,
    CaptureStorageError,
    LocalCaptureStorage,
    MemoryCaptureStorage,
    WorkspaceCaptureStorage,
    canonical_json,
)

BINDING = {
    "run_id": "run-1",
    "unit_id": "unit-1",
    "workspace_id": "123",
    "owner": "a@example.com",
}


def journal(storage=None, **limits):
    return CaptureJournal(
        storage if storage is not None else MemoryCaptureStorage(),
        BINDING,
        {},
        **limits,
    )


def rehash(document):
    body = {key: value for key, value in document.items() if key != "hash"}
    document["hash"] = hashlib.sha256(canonical_json(body)).hexdigest()
    return canonical_json(document)


def test_replacement_replays_original_state_and_binding(tmp_path):
    first = journal(LocalCaptureStorage(tmp_path))
    first.transaction(
        lambda state: state.update(
            {"events": [{"seq": 1, "text": "こんにちは"}], "revision": 4}
        )
    )
    replacement = journal(LocalCaptureStorage(tmp_path))
    assert replacement.read() == {
        "events": [{"seq": 1, "text": "こんにちは"}],
        "revision": 4,
    }
    assert replacement.health() == {
        "ok": True,
        "held": False,
        "durable": False,
        "mode": "local",
        "revision": 1,
        "error": None,
        "checkpoint_error": None,
    }
    assert not list(tmp_path.glob(".capture-stage-*"))
    assert (tmp_path / "00000000000000000001.json").stat().st_mode & 0o777 == 0o600


def test_read_and_mutator_return_cannot_change_committed_state():
    retained = journal()
    result = retained.transaction(
        lambda state: state.setdefault("nested", {"queue": [1]})
    )
    result["queue"].append(2)
    readback = retained.read()
    readback["nested"]["queue"].append(3)
    assert retained.read() == {"nested": {"queue": [1]}}


def test_noop_transaction_does_not_add_operation():
    retained = journal()
    assert retained.transaction(lambda state: 42) == 42
    assert retained.health()["revision"] == 0
    assert len(retained.storage.objects) == 1


def test_domain_rejection_does_not_poison_health():
    retained = journal()

    def reject(state):
        state["not accepted"] = True
        raise ValueError("business rule")

    with pytest.raises(ValueError, match="business rule"):
        retained.transaction(reject)
    assert retained.read() == {}
    assert retained.health()["ok"] is True


def test_operation_delta_appends_and_ack_removes_without_rewriting_queue():
    retained = journal()
    records = [{"seq": index, "body": "X" * 100} for index in range(100)]
    retained.transaction(lambda state: state.update({"events": records}))
    retained.transaction(
        lambda state: state["events"].append({"seq": 100, "body": "tail"})
    )
    append = json.loads(retained.storage.objects["00000000000000000002.json"])
    assert append["patch"] == [
        {
            "op": "splice",
            "path": ["events"],
            "start": 100,
            "delete": 0,
            "items": [{"seq": 100, "body": "tail"}],
        }
    ]
    retained.transaction(lambda state: state.update(events=state["events"][50:]))
    ack = json.loads(retained.storage.objects["00000000000000000003.json"])
    assert ack["patch"] == [
        {"op": "splice", "path": ["events"], "start": 0, "delete": 50, "items": []}
    ]
    assert journal(retained.storage).read()["events"][0]["seq"] == 50


def test_same_body_concurrent_transactions_both_apply():
    store = MemoryCaptureStorage()
    left, right = journal(store), journal(store)
    left.read()
    right.read()
    barrier = threading.Barrier(2)
    calls = [0, 0]

    def increment(retained, index):
        def mutate(state):
            calls[index] += 1
            if calls[index] == 1:
                barrier.wait(timeout=5)
            state["count"] = state.get("count", 0) + 1
            return state["count"]

        return retained.transaction(mutate)

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda args: increment(*args), [(left, 0), (right, 1)]))
    assert sorted(values) == [1, 2]
    assert journal(store).read() == {"count": 2}
    assert sum(calls) == 3


def test_many_independent_writers_do_not_overwrite():
    store = MemoryCaptureStorage()
    journal(store).read()

    def append(index):
        retained = journal(store, max_retries=256)
        retained.transaction(lambda state: state.setdefault("seen", []).append(index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(50)))
    assert sorted(journal(store).read()["seen"]) == list(range(50))
    assert len(store.objects) == 51


class UncertainStore(MemoryCaptureStorage):
    fail_before = False
    fail_after = False

    def create(self, name, content):
        if self.fail_before:
            raise TimeoutError("request never arrived")
        created = super().create(name, content)
        if self.fail_after:
            raise TimeoutError("response lost")
        return created


def test_lost_create_reply_reads_exact_committed_object():
    store = UncertainStore()
    retained = journal(store)
    retained.read()
    store.fail_after = True
    assert (
        retained.transaction(lambda state: state.setdefault("accepted", []).append(1))
        is None
    )
    assert journal(store).read() == {"accepted": [1]}
    assert len(store.objects) == 2


def test_write_failure_keeps_previous_state_and_recovers_on_retry():
    store = UncertainStore()
    retained = journal(store)
    retained.transaction(lambda state: state.update({"accepted": [1]}))
    store.fail_before = True
    with pytest.raises(CaptureStorageError):
        retained.transaction(lambda state: state["accepted"].append(2))
    assert retained.health()["ok"] is False
    store.fail_before = False
    assert retained.read() == {"accepted": [1]}
    assert retained.health()["ok"] is True
    retained.transaction(lambda state: state["accepted"].append(2))
    assert journal(store).read() == {"accepted": [1, 2]}


def test_uncertain_create_with_another_winners_bytes_retries_mutator():
    store = UncertainStore()
    retained = journal(store)
    retained.read()
    original = store.create
    competitor = journal(store)
    raced = False

    def racing_create(name, content):
        nonlocal raced
        if not raced:
            raced = True
            store.create = original
            competitor.transaction(
                lambda state: state.update({"count": state.get("count", 0) + 1})
            )
            raise TimeoutError("competing write won")
        return original(name, content)

    store.create = racing_create
    retained.transaction(
        lambda state: state.update({"count": state.get("count", 0) + 1})
    )
    assert journal(store).read() == {"count": 2}


@pytest.mark.parametrize(
    "change",
    [
        "binding",
        "schema",
        "hash",
        "gap",
        "extra",
        "predecessor",
        "state_digest",
        "invalid_patch",
    ],
)
def test_corruption_is_held_and_never_replaced(change):
    store = MemoryCaptureStorage()
    retained = journal(store)
    retained.transaction(lambda state: state.update({"accepted": [1]}))
    name = "00000000000000000001.json"
    doc = json.loads(store.objects[name])
    if change == "binding":
        doc["binding"]["run_id"] = "other"
    elif change == "schema":
        doc["schema_version"] = 99
    elif change == "hash":
        store.objects[name] = store.objects[name].replace(b"accepted", b"modified")
    elif change == "gap":
        store.objects["00000000000000000003.json"] = store.objects.pop(name)
    elif change == "extra":
        store.objects["unexpected.json"] = b"{}"
    elif change == "predecessor":
        doc["previous_hash"] = "a" * 64
    elif change == "state_digest":
        doc["state_sha256"] = "a" * 64
    elif change == "invalid_patch":
        doc["patch"] = [
            {
                "op": "splice",
                "path": ["missing"],
                "start": 0,
                "delete": 100,
                "items": [],
            }
        ]
    if change not in {"hash", "gap", "extra"}:
        store.objects[name] = rehash(doc)
    preserved = copy.deepcopy(store.objects)
    broken = journal(store)
    with pytest.raises(CaptureHeldError):
        broken.read()
    with pytest.raises(CaptureHeldError):
        broken.transaction(lambda state: state.update({"replacement": True}))
    assert broken.health()["held"] is True
    assert store.objects == preserved


def test_previously_observed_history_disappearance_is_held():
    retained = journal()
    retained.transaction(lambda state: state.update({"a": 1}))
    retained.storage.objects.clear()
    with pytest.raises(CaptureHeldError, match="disappeared"):
        retained.read()
    assert retained.storage.objects == {}


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), (1, 2), {1: "bad"}, {"x": {1, 2}}]
)
def test_non_json_capture_is_rejected_before_commit(value):
    retained = journal()
    retained.read()
    with pytest.raises(CaptureCapacityError):
        retained.transaction(lambda state: state.update({"value": value}))
    assert len(retained.storage.objects) == 1
    assert retained.read() == {}


@pytest.mark.parametrize(
    "limits,value",
    [({"max_record_bytes": 800}, "X" * 1000), ({"max_state_bytes": 30}, "X" * 100)],
)
def test_capacity_failure_retains_prior_accepted_output(limits, value):
    retained = journal(**limits)
    retained.transaction(lambda state: state.update({"a": 1}))
    with pytest.raises(CaptureCapacityError):
        retained.transaction(lambda state: state.update({"value": value}))
    assert journal(retained.storage).read() == {"a": 1}
    assert len(retained.storage.objects) == 2


def test_operation_capacity_is_visible_without_deleting_history():
    retained = journal(max_operations=1)
    retained.transaction(lambda state: state.update({"a": 1}))
    with pytest.raises(CaptureCapacityError, match="capacity is exhausted"):
        retained.transaction(lambda state: state.update({"a": 2}))
    assert retained.read() == {"a": 1}


def test_checkpoint_matches_anchor_and_accelerates_cold_read():
    store = MemoryCaptureStorage()
    retained = journal(store, checkpoint_interval=3)
    for number in range(7):
        retained.transaction(
            lambda state, number=number: state.setdefault("events", []).append(number)
        )
    assert "checkpoint-00000000000000000006.json" in store.objects
    original = store.read
    reads = []

    def counted(name):
        reads.append(name)
        return original(name)

    store.read = counted
    replacement = journal(store)
    assert replacement.read() == {"events": list(range(7))}
    assert reads == [
        "checkpoint-00000000000000000006.json",
        "00000000000000000006.json",
        "00000000000000000007.json",
    ]
    reads.clear()
    assert replacement.audit() == {"events": list(range(7))}
    assert reads == [f"{index:020d}.json" for index in range(8)]


def test_corrupt_checkpoint_is_held_without_empty_fallback():
    store = MemoryCaptureStorage()
    retained = journal(store, checkpoint_interval=1)
    retained.transaction(lambda state: state.update({"accepted": [1]}))
    name = "checkpoint-00000000000000000001.json"
    doc = json.loads(store.objects[name])
    doc["state"]["accepted"] = [999]
    store.objects[name] = rehash(doc)
    with pytest.raises(CaptureHeldError, match="checkpoint"):
        journal(store).read()


def test_checkpoint_failure_does_not_make_committed_append_ambiguous():
    store = MemoryCaptureStorage()
    retained = journal(store, checkpoint_interval=1)
    original = store.create

    def fail_checkpoint(name, content):
        if name.startswith("checkpoint-"):
            raise OSError("checkpoint unavailable")
        return original(name, content)

    store.create = fail_checkpoint
    retained.transaction(lambda state: state.update({"accepted": [1]}))
    assert retained.health()["ok"] is True
    assert retained.health()["checkpoint_error"]
    assert journal(store).read() == {"accepted": [1]}


def test_subprocess_concurrent_writers_and_clean_process_recovery(tmp_path):
    script = """
import json,sys
from server.capture_journal import CaptureJournal,LocalCaptureStorage
binding=json.loads(sys.argv[2])
j=CaptureJournal(LocalCaptureStorage(sys.argv[1]),binding,{},max_retries=256)
for n in range(10):
    j.transaction(lambda state: state.setdefault('values',[]).append([sys.argv[3],n]))
"""
    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path),
                json.dumps(BINDING),
                str(index),
            ],
            cwd=os.getcwd(),
        )
        for index in range(3)
    ]
    for child in children:
        assert child.wait(timeout=30) == 0
    recovered = journal(LocalCaptureStorage(tmp_path)).audit()["values"]
    assert len(recovered) == 30
    assert sorted(recovered) == [
        [str(worker), n] for worker in range(3) for n in range(10)
    ]


def test_workspace_adapter_uses_create_only_auto_files_and_canonical_listing():
    from databricks.sdk.errors import NotFound, ResourceAlreadyExists
    from databricks.sdk.service.workspace import ExportFormat, ImportFormat

    objects = {}
    uploads = []

    class Workspace:
        def mkdirs(self, path):
            assert path == "/Workspace/Users/a/projects/.workshop-capture/binding"

        def upload(self, path, content, *, format, overwrite):
            uploads.append((path, format, overwrite))
            if path in objects:
                raise ResourceAlreadyExists("exists")
            objects[path] = content

        def download(self, path, *, format):
            assert format == ExportFormat.AUTO
            if path not in objects:
                raise NotFound("missing")
            return io.BytesIO(objects[path])

        def list(self, path):
            return [
                SimpleNamespace(
                    path=name.removeprefix("/Workspace"),
                    object_type=SimpleNamespace(value="FILE"),
                )
                for name in objects
            ]

    store = WorkspaceCaptureStorage(
        SimpleNamespace(workspace=Workspace()),
        "/Workspace/Users/a/projects/.workshop-capture/binding",
    )
    retained = journal(store)
    retained.transaction(lambda state: state.update({"accepted": True}))
    assert journal(store).read() == {"accepted": True}
    assert retained.health()["durable"] is True
    assert all(
        format == ImportFormat.AUTO and overwrite is False
        for _, format, overwrite in uploads
    )
    assert store.create("00000000000000000001.json", b"overwrite") is False
    assert retained.audit() == {"accepted": True}


def test_workspace_nonclosable_download_is_supported():
    class Stream:
        def read(self):
            return b"payload"

    client = SimpleNamespace(
        workspace=SimpleNamespace(
            mkdirs=lambda path: None, download=lambda *args, **kwargs: Stream()
        )
    )
    store = WorkspaceCaptureStorage(
        client, "/Workspace/Users/a/projects/.workshop-capture/binding"
    )
    assert store.read("00000000000000000000.json") == b"payload"


def test_missing_immutable_anchor_field_is_held_even_with_checkpoint():
    store = MemoryCaptureStorage()
    retained = journal(store, checkpoint_interval=1)
    retained.transaction(lambda state: state.update({"a": 1}))
    operation_name = "00000000000000000001.json"
    anchor = json.loads(store.objects[operation_name])
    del anchor["operation_id"]
    store.objects[operation_name] = rehash(anchor)
    checkpoint_name = "checkpoint-00000000000000000001.json"
    checkpoint = json.loads(store.objects[checkpoint_name])
    checkpoint["head_hash"] = json.loads(store.objects[operation_name])["hash"]
    store.objects[checkpoint_name] = rehash(checkpoint)
    with pytest.raises(CaptureHeldError, match="operation schema"):
        journal(store).read()


def test_duplicate_json_keys_are_corruption():
    store = MemoryCaptureStorage()
    retained = journal(store)
    retained.read()
    name = "00000000000000000000.json"
    store.objects[name] = store.objects[name].replace(
        b'{"binding":', b'{"schema_version":1,"binding":', 1
    )
    with pytest.raises(CaptureHeldError, match="corrupt"):
        journal(store).read()


def test_full_audit_detects_old_history_corruption_before_checkpoint():
    store = MemoryCaptureStorage()
    retained = journal(store, checkpoint_interval=2)
    for index in range(3):
        retained.transaction(
            lambda state, index=index: state.setdefault("accepted", []).append(index)
        )
    store.objects["00000000000000000001.json"] = b"corrupt old immutable object"
    replacement = journal(store)
    # The checkpoint retains all accepted content even if older evidence later
    # becomes unreadable. A full audit explicitly verifies that earlier evidence.
    assert replacement.read() == {"accepted": [0, 1, 2]}
    with pytest.raises(CaptureHeldError, match="corrupt"):
        replacement.audit()


def test_large_backlog_spans_pages_without_eviction_and_replays(tmp_path):
    retained = journal(LocalCaptureStorage(tmp_path), checkpoint_interval=100)
    for index in range(1003):
        retained.transaction(
            lambda state, index=index: state.setdefault("events", []).append(
                {
                    "seq": index + 1,
                    "event": {"idempotency_key": f"key-{index}", "text": "retained"},
                }
            )
        )
    replacement = journal(LocalCaptureStorage(tmp_path))
    state = replacement.read()
    assert len(state["events"]) == 1003
    assert [entry["seq"] for entry in state["events"]] == list(range(1, 1004))
    assert len(list(tmp_path.glob("[0-9]*.json"))) == 1004


def test_domain_validator_can_mark_semantically_invalid_retained_state_held():
    retained = journal()
    retained.transaction(lambda state: state.update({"help": {"seq": -1}}))
    retained.mark_held("retained help sequence is invalid")
    assert retained.health()["held"] is True
    with pytest.raises(CaptureHeldError, match="help sequence"):
        retained.read()
    assert len(retained.storage.objects) == 2


@pytest.mark.parametrize(
    "before,after",
    [
        (1, True),
        (0, False),
        (1, 1.0),
        ([1], [True]),
        ({"nested": [1]}, {"nested": [True]}),
    ],
)
def test_delta_preserves_json_scalar_types_in_nested_containers(before, after):
    retained = journal()
    retained.transaction(lambda state: state.update({"value": before}))
    retained.transaction(lambda state: state.update({"value": after}))
    replacement = journal(retained.storage)
    assert canonical_json(replacement.read()["value"]) == canonical_json(after)
    assert replacement.health()["revision"] == 2
