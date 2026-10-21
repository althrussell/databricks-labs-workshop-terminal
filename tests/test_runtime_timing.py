"""Diagnostic timings remain bounded, concurrent and free of operation content."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def timing():
    # Isolate process aggregates from other tests/background bootstrap work.
    spec = importlib.util.spec_from_file_location(
        "isolated_runtime_timing", Path(__file__).parents[1] / "server/runtime_timing.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nested_scopes_use_inclusive_monotonic_time_and_restore_component(timing, monkeypatch):
    ticks = iter((10.0, 11.0, 13.0, 15.0, 16.0, 17.0))
    monkeypatch.setattr(timing, "time", SimpleNamespace(time=lambda: 1000.0, monotonic=lambda: next(ticks)))
    with timing.stage("install.total", component="omnigent"):
        with timing.stage("install.dependencies"):
            pass
    with timing.stage("proof.total"):
        pass
    data = timing.snapshot()
    by_key = {(row["stage"], row["component"]): row for row in data["stages"]}
    assert by_key["install.total", "omnigent"]["total_ms"] == 5000
    assert by_key["install.dependencies", "omnigent"]["total_ms"] == 2000
    assert by_key["proof.total", "runtime"]["total_ms"] == 1000
    assert data["durations_are_inclusive"] is True
    assert data["count_unit"] == "function_or_scope_invocations"


def test_decorator_preserves_result_exception_and_repeated_calls(timing):
    @timing.stage("identity.validation")
    def validate(value):
        """A controlled operation."""
        if value == 0:
            raise ValueError("never retain this message")
        return value + 1
    assert validate.__name__ == "validate"
    assert validate(2) == 3
    with pytest.raises(ValueError, match="never retain"):
        validate(0)
    with pytest.raises(KeyboardInterrupt):
        with timing.stage("identity.validation"):
            raise KeyboardInterrupt("also private")
    row, = timing.snapshot()["stages"]
    assert (row["started"], row["completed"], row["errors"], row["inflight"]) == (3, 3, 2, 0)
    assert "never retain" not in json.dumps(timing.snapshot())
    assert "also private" not in json.dumps(timing.snapshot())


def test_concurrent_calls_keep_inflight_counts_and_snapshot_is_a_copy(timing):
    entered = threading.Barrier(6)
    release = threading.Event()
    def work():
        with timing.stage("capture.workspace_read"):
            entered.wait(timeout=5)
            assert release.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=5) as executor:
        pending = [executor.submit(work) for _ in range(5)]
        entered.wait(timeout=5)
        row, = timing.snapshot()["stages"]
        assert (row["started"], row["completed"], row["inflight"]) == (5, 0, 5)
        row["inflight"] = 999
        release.set()
        for future in pending:
            future.result(timeout=5)
    result = timing.snapshot()
    row, = result["stages"]
    assert (row["started"], row["completed"], row["errors"], row["inflight"]) == (5, 5, 0, 0)
    result["recent"][0]["stage"] = "mutated"
    assert timing.snapshot()["recent"][0]["stage"] == "capture.workspace_read"


def test_untrusted_labels_are_not_retained_and_recent_history_is_bounded(timing):
    secret = "attendee@example.com:private-token-and-path"
    with timing.stage(secret):
        pass
    with timing.stage("proof.total", component=secret):
        pass
    for _ in range(300):
        with timing.stage("proof.binary", component="claude"):
            pass
    data = timing.snapshot()
    assert data["unsupported_labels"] == 2
    assert secret not in json.dumps(data)
    assert len(data["recent"]) == data["recent_limit"] == 128
    assert len(data["stages"]) == 1
    assert data["stages"][0]["completed"] == 300
    assert data["retention"] == "current_process"
    assert data["process_id"]
