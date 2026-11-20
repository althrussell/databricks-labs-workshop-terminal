"""Buffer headroom for priority events, and a truthful ``dropped`` (server/event_emitter.py).

The account-team data (summaries, discovery, identity, signals, manifests) must
survive a backlog that is filling with routine telemetry. So, once the backlog
reaches the reserve line, only priority types are accepted; a refusal is counted,
never silent.
"""

from __future__ import annotations

import pytest

from server.capture_journal import CaptureCapacityError
from server.event_emitter import (
    PRIORITY_EVENT_TYPES,
    PRIORITY_HEADROOM,
    CaptureHeadroomError,
    EventEmitter,
)

RUN_ID = "11111111-2222-3333-4444-555555555555"
ROUTINE = "omnigent.session_ready"


def _emitter(max_buffer: int) -> EventEmitter:
    return EventEmitter(run_id=RUN_ID, workspace_id="ws1", ingest_url="", ingest_token="",
                        max_buffer=max_buffer)


def _fill(emitter: EventEmitter, count: int, event_type: str, prefix: str) -> None:
    for n in range(count):
        emitter.emit(event_type, "labuser001@x.com", {"n": n}, idempotency_key=f"{prefix}{n}")


def test_the_priority_types_are_the_five_the_contract_names():
    assert PRIORITY_EVENT_TYPES == {
        "insight.summary", "discovery.record", "attendee.identity", "workshop.signal", "app.manifest",
    }
    assert PRIORITY_HEADROOM == 500


def test_routine_events_stop_at_the_reserve_line_and_priority_events_still_land():
    emitter = _emitter(1000)
    _fill(emitter, 500, ROUTINE, "r")  # backlog reaches max - headroom

    with pytest.raises(CaptureHeadroomError):
        emitter.emit(ROUTINE, "labuser001@x.com", {}, idempotency_key="r-refused")
    emitter.emit("insight.summary", "labuser001@x.com", {"headline": "h"}, idempotency_key="p-summary")

    assert emitter.pending() == 501
    assert emitter.dropped == 1


def test_a_headroom_refusal_is_counted_but_is_not_a_delivery_fault():
    emitter = _emitter(1000)
    _fill(emitter, 500, ROUTINE, "r")
    with pytest.raises(CaptureHeadroomError):
        emitter.emit(ROUTINE, "labuser001@x.com", {}, idempotency_key="r-refused")

    status = emitter.delivery_status()

    assert status["dropped"] == 1
    assert status["retention_ready"] is True
    assert status["retention_issues"] == []


def test_dropped_is_reported_on_the_collect_response_and_on_readiness():
    emitter = _emitter(1000)
    _fill(emitter, 500, ROUTINE, "r")
    with pytest.raises(CaptureHeadroomError):
        emitter.emit(ROUTINE, "labuser001@x.com", {}, idempotency_key="r-refused")

    assert emitter.collect()["dropped"] == 1
    assert emitter.delivery_status()["dropped"] == 1


def test_a_full_backlog_refuses_priority_events_too_and_marks_delivery():
    emitter = _emitter(1000)
    _fill(emitter, 500, ROUTINE, "r")
    _fill(emitter, 500, "insight.summary", "p")

    with pytest.raises(CaptureCapacityError) as refused:
        emitter.emit("discovery.record", "labuser001@x.com", {}, idempotency_key="late")

    assert not isinstance(refused.value, CaptureHeadroomError)
    assert emitter.dropped == 1
    assert emitter.pending() == 1000
    assert emitter.delivery_status()["retention_ready"] is False, "a full backlog is a real fault"


def test_acknowledged_events_free_the_reserve_for_routine_output_again():
    emitter = _emitter(1000)
    _fill(emitter, 500, ROUTINE, "r")
    with pytest.raises(CaptureHeadroomError):
        emitter.emit(ROUTINE, "labuser001@x.com", {}, idempotency_key="r-refused")
    collected = emitter.collect()
    emitter.acknowledge(schema_version=2, stream_id=collected["stream_id"], through_seq=10)

    emitter.emit(ROUTINE, "labuser001@x.com", {}, idempotency_key="r-after-ack")

    assert emitter.pending() == 491


def test_a_small_buffer_keeps_no_reserve_so_it_behaves_as_it_always_did():
    emitter = _emitter(3)
    _fill(emitter, 3, ROUTINE, "s")

    with pytest.raises(CaptureCapacityError) as refused:
        emitter.emit(ROUTINE, "labuser001@x.com", {}, idempotency_key="s3")

    assert not isinstance(refused.value, CaptureHeadroomError)
    assert emitter.pending() == 3


def test_a_duplicate_key_is_not_a_refusal_even_at_the_reserve_line():
    emitter = _emitter(1000)
    _fill(emitter, 500, ROUTINE, "r")

    emitter.emit(ROUTINE, "labuser001@x.com", {"n": 0}, idempotency_key="r0")

    assert emitter.dropped == 0
    assert emitter.pending() == 500
