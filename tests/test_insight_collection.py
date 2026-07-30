"""Collection: how attendee events actually leave this instance (contract C3b).

The original design had the terminal POST events to Control Tower's ingest
endpoint with a shared token. That cannot work: Control Tower is a Databricks App,
and every app sits behind a proxy that requires a Databricks identity on the
request, so a POST carrying only ``X-Ingest-Token`` is rejected before CT's code
runs. Nothing in either codebase noticed, because both sides were tested against
their own halves of the contract.

So Control Tower comes here instead, on the authenticated harvest it already
makes. That inverts two things these tests pin:

- **Nothing is configured.** CT injects no ingest URL, token or run id into the
  apps it deploys, so an instance must buffer and serve events without them. Every
  gate that treated "push configured" as "delivery possible" was silently
  discarding everything on exactly the instances that matter.
- **The cursor is an acknowledgement.** The buffer is bounded, so it has to be
  possible to release delivered events — but only ones the collector has committed,
  and only within the process that numbered them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.event_emitter import EventEmitter

from .schema_assert import assert_schema

ADMIN = {"X-Forwarded-Email": "op@example.com"}
ALICE = {"X-Forwarded-Email": "alice@example.com"}

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "workshop-insight-events.schema.json"
)


def _pull_only() -> EventEmitter:
    """An emitter configured the way Control Tower actually deploys one."""
    return EventEmitter(run_id="", workspace_id="", ingest_url="", ingest_token="")


def _fill(emitter: EventEmitter, count: int, *, prefix: str = "k") -> None:
    for i in range(count):
        emitter.emit("heartbeat", "alice@example.com", {"i": i},
                     idempotency_key=f"{prefix}{i}")


def _keys(response: dict) -> list[str]:
    return [entry["event"]["idempotency_key"] for entry in response["events"]]


# --- the buffer is the handover point ----------------------------------------


def test_a_terminal_with_no_ingest_configuration_still_has_events_to_collect():
    emitter = _pull_only()
    _fill(emitter, 3)

    response = emitter.collect()

    assert emitter.can_push is False
    assert response["delivery"] == "pull"
    assert _keys(response) == ["k0", "k1", "k2"]


def test_collection_hands_over_oldest_first():
    """Ordering is the terminal's only contribution to ordering: CT stores what it
    is given, and a signal superseded by an older one would misreport an attendee."""
    emitter = _pull_only()
    _fill(emitter, 5)

    assert _keys(emitter.collect()) == ["k0", "k1", "k2", "k3", "k4"]


def test_the_envelope_handed_over_is_exactly_the_ingest_contract():
    """``seq`` is transport state and must stay outside the envelope.

    Control Tower validates each envelope against the same schema the push path
    uses; a stray transport field would fail that validation and take the whole
    batch's worth of events with it.

    The one field a collected envelope cannot fill is ``run_id`` — a deployed
    terminal is never told which run it belongs to. That is why the collector
    stamps identity on arrival, and why the schema permits an empty run only here.
    """
    example = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "examples"
         / "workshop-signal.json").read_text()
    )
    emitter = _pull_only()
    emitter.emit(
        "workshop.signal",
        example["attendee"],
        example["payload"],
        idempotency_key=example["idempotency_key"],
    )

    [entry] = emitter.collect()["events"]

    assert entry["seq"] == 1
    assert "seq" not in entry["event"]
    schema = json.loads(SCHEMA_PATH.read_text())
    assert_schema(entry["event"], schema)
    assert entry["event"]["run_id"] == ""
    # And the stamped version — what CT actually ingests — validates too.
    assert_schema({**entry["event"], "run_id": example["run_id"]}, schema)


# --- the cursor releases what has been committed ------------------------------


def test_acknowledged_events_are_released():
    """Otherwise a day-long event drifts up to the ceiling and drop-oldest starts
    discarding the wrap summaries, which are the events worth the most."""
    emitter = _pull_only()
    _fill(emitter, 4)
    first = emitter.collect()

    second = emitter.collect(after=first["high_water"], stream=first["stream_id"])

    assert _keys(second) == []
    assert emitter.pending() == 0


def test_unacknowledged_events_are_kept():
    emitter = _pull_only()
    _fill(emitter, 4)
    first = emitter.collect(limit=2)

    assert _keys(first) == ["k0", "k1"]
    assert first["pending"] == 4  # nothing released yet
    second = emitter.collect(after=first["high_water"], stream=first["stream_id"])
    assert _keys(second) == ["k2", "k3"]


def test_a_partial_collection_leaves_the_remainder_for_next_time():
    """A collector that has fallen behind gets a bounded page, not a body big
    enough to fail the harvest it is riding on."""
    emitter = _pull_only()
    _fill(emitter, 10)

    assert len(emitter.collect(limit=3)["events"]) == 3
    assert emitter.pending() == 10


def test_a_cursor_from_another_process_is_refused():
    """The failure this prevents is total: sequence numbers restart with the
    process, so replaying ``after=400`` against a restarted terminal would release
    its entire fresh buffer without ever reading it, and no later harvest could
    recover it."""
    emitter = _pull_only()
    _fill(emitter, 3)

    response = emitter.collect(after=400, stream="a-cursor-from-a-dead-process")

    assert response["cursor_reset"] is True
    assert _keys(response) == ["k0", "k1", "k2"]
    assert emitter.pending() == 3


def test_a_cursor_with_no_stream_is_refused():
    """An unattributed cursor can't be proven to belong to this stream, so it is
    treated as the collector having lost its place — a re-collection, which
    idempotency makes free, rather than a release, which is irreversible."""
    emitter = _pull_only()
    _fill(emitter, 3)

    response = emitter.collect(after=2)

    assert response["cursor_reset"] is True
    assert _keys(response) == ["k0", "k1", "k2"]


def test_the_high_water_mark_holds_when_there_is_nothing_new():
    """An idle instance must not walk the collector's cursor backwards."""
    emitter = _pull_only()
    _fill(emitter, 2)
    first = emitter.collect()

    second = emitter.collect(after=first["high_water"], stream=first["stream_id"])

    assert second["high_water"] == first["high_water"]


# --- loss is counted, not hidden ---------------------------------------------


def test_overflow_is_reported_so_the_loss_is_attributable():
    """Drop-oldest is the right policy — memory is finite and the app is shared
    with an attendee's work — but a silent one turns a missing brief into a mystery
    for whoever reads it months later."""
    emitter = EventEmitter(
        run_id="", workspace_id="", ingest_url="", ingest_token="", max_buffer=3
    )
    _fill(emitter, 5)

    response = emitter.collect()

    assert response["dropped"] == 2
    assert _keys(response) == ["k2", "k3", "k4"]


def test_collection_is_observable_so_readiness_can_report_it():
    """Under pull a delivery path always exists, so configuration cannot prove the
    feature is working. Whether anyone has collected is the only evidence."""
    emitter = _pull_only()

    assert emitter.delivery_status()["collections"] == 0
    emitter.collect()
    status = emitter.delivery_status()

    assert status["collections"] == 1
    assert status["delivery"] == "pull"
    assert status["last_collected_at"] is not None


# --- the endpoint ------------------------------------------------------------


def test_the_endpoint_requires_an_admin_identity(client, as_non_admin):
    """The buffer holds attendee-authored discovery text. It travels to CT over an
    operator-authenticated route, which is the same protection the stats harvest
    has."""
    assert client.get("/api/admin/insight-events", headers=ALICE).status_code == 403


def test_the_endpoint_serves_the_process_buffer(client, as_admin, monkeypatch):
    from server import event_emitter as emitter_module

    emitter = _pull_only()
    _fill(emitter, 2)
    monkeypatch.setattr(emitter_module, "event_emitter", emitter)

    body = client.get("/api/admin/insight-events", headers=ADMIN).json()

    assert body["stream_id"] == emitter.stream_id
    assert _keys(body) == ["k0", "k1"]


def test_the_endpoint_passes_the_cursor_through(client, as_admin, monkeypatch):
    from server import event_emitter as emitter_module

    emitter = _pull_only()
    _fill(emitter, 3)
    monkeypatch.setattr(emitter_module, "event_emitter", emitter)

    body = client.get(
        f"/api/admin/insight-events?after=2&stream={emitter.stream_id}",
        headers=ADMIN,
    ).json()

    assert _keys(body) == ["k2"]
    assert emitter.pending() == 1


@pytest.mark.parametrize("final", [False, True])
def test_the_harvest_reports_what_is_still_owed(
    client, as_admin, monkeypatch, final: bool
):
    """CT's teardown reads this to know whether a collection is still outstanding.
    On the ``final=true`` pass it is the last chance: everything left in the buffer
    dies with the container moments later."""
    from server import event_emitter as emitter_module

    emitter = _pull_only()
    _fill(emitter, 4)
    monkeypatch.setattr(emitter_module, "event_emitter", emitter)

    suffix = "?final=true" if final else ""
    instance = client.get(f"/api/admin/stats{suffix}", headers=ADMIN).json()["instance"]

    assert instance["events_pending"] >= 4
    assert instance["event_stream_id"] == emitter.stream_id
    assert instance["events_dropped"] == 0
