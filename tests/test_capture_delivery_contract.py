"""The WT producer must serve the same golden records CT-01 commits.

Fixtures are copied byte-for-byte from CT PR182. Negative consumer fixtures are
retained alongside the valid pages so both repositories share one corpus.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

from server import event_emitter as emitter_module
from server.event_emitter import EventEmitter


FIXTURES = Path(__file__).parent / "fixtures" / "capture-delivery"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def canonical_sha256(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


@pytest.fixture
def golden_emitter(monkeypatch) -> EventEmitter:
    observed_at = datetime.fromisoformat("2026-11-17T06:30:00+00:00")

    class FrozenDatetime:
        @staticmethod
        def now(_timezone=None):
            return observed_at

    monkeypatch.setattr(emitter_module, "datetime", FrozenDatetime)
    emitter = EventEmitter(run_id="", workspace_id="", ingest_url="", ingest_token="")
    entries = fixture("v2-first-page.json")["events"] + fixture("v2-second-page.json")["events"]
    for entry in entries:
        event = entry["event"]
        sequence = emitter.emit(
            event["type"], event["attendee"], deepcopy(event["payload"]),
            idempotency_key=event["idempotency_key"],
        )
        assert sequence == entry["seq"]
    return emitter


def with_epoch(page: dict, epoch: str) -> dict:
    result = deepcopy(page)
    result["stream_id"] = epoch
    return result


def test_golden_canonical_digests_preserve_content_and_unicode():
    expected = {entry["seq"]: entry["raw_entry_sha256"]
                for entry in fixture("canonical-digests.json")["records"]}
    for name in ("v2-first-page.json", "v2-second-page.json"):
        for entry in fixture(name)["events"]:
            assert canonical_sha256(entry) == expected[entry["seq"]]
            assert canonical_sha256(dict(reversed(list(entry.items())))) == expected[entry["seq"]]
    assert "Zoë" in json.dumps(fixture("v2-first-page.json"), ensure_ascii=False)


def test_producer_pages_and_ack_match_ct_golden_wire_contract(golden_emitter):
    emitter = golden_emitter
    first = emitter.collect(limit=2)
    epoch = first["stream_id"]
    assert first == with_epoch(fixture("v2-first-page.json"), epoch)
    assert first["pending"] == 4  # Includes the two records in this page.

    request = with_epoch(fixture("v2-ack-request.json"), epoch)
    assert emitter.acknowledge(**request) == with_epoch(fixture("v2-ack-response.json"), epoch)
    second = emitter.collect(after=2, stream=epoch, limit=2)
    assert second == with_epoch(fixture("v2-second-page.json"), epoch)

    emitter.acknowledge(schema_version=2, stream_id=epoch, through_seq=4)
    assert emitter.collect(after=4, stream=epoch) == with_epoch(fixture("v2-empty-after-ack.json"), epoch)


def test_reads_and_lost_ack_reply_preserve_replayable_prefix(golden_emitter):
    emitter = golden_emitter
    first = emitter.collect(limit=2)
    epoch = first["stream_id"]
    assert emitter.collect(after=2, stream=epoch, limit=2)["events"] == fixture("v2-second-page.json")["events"]
    assert emitter.collect(limit=2) == first
    assert emitter.pending() == 4

    request = with_epoch(fixture("v2-ack-request.json"), epoch)
    committed_ack = emitter.acknowledge(**request)
    # The collector lost the response. Repeating the committed ACK is safe.
    assert emitter.acknowledge(**request) == committed_ack
    assert emitter.pending() == 2
    assert emitter.collect(after=2, stream=epoch, limit=2) == with_epoch(fixture("v2-second-page.json"), epoch)


@pytest.mark.parametrize("updates", [
    {"schema_version": 1}, {"stream_id": "different-epoch"},
    {"through_seq": -1}, {"through_seq": 5},
])
def test_invalid_ack_never_releases_retained_output(golden_emitter, updates):
    emitter = golden_emitter
    before = emitter.collect()
    request = {"schema_version": 2, "stream_id": before["stream_id"], "through_seq": 2, **updates}
    with pytest.raises(ValueError):
        emitter.acknowledge(**request)
    assert emitter.collect() == before


def test_same_logical_record_after_ack_does_not_resurrect_or_renumber(golden_emitter):
    emitter = golden_emitter
    first = emitter.collect(limit=2)
    emitter.acknowledge(schema_version=2, stream_id=first["stream_id"], through_seq=2)
    event = first["events"][0]["event"]
    assert emitter.emit(
        event["type"], event["attendee"], deepcopy(event["payload"]),
        idempotency_key=event["idempotency_key"],
    ) == 1
    assert emitter.pending() == 2
    assert emitter.collect(after=2, stream=first["stream_id"], limit=2) == with_epoch(fixture("v2-second-page.json"), first["stream_id"])
