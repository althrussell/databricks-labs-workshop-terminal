"""Attendee event emitter — buffer, idempotency, fail-soft drain (C3b)."""

from server.event_emitter import EventEmitter


def _emitter(**over) -> EventEmitter:
    kwargs = dict(run_id="run-1", workspace_id="100",
                  ingest_url="https://ct.example.com", ingest_token="tok")
    kwargs.update(over)
    return EventEmitter(**kwargs)


def test_unconfigured_means_no_push_not_no_capture():
    """The buffer is the handover point, not the HTTP call.

    Control Tower collects from ``/api/admin/insight-events``, so an instance with
    no ingest configuration still has a delivery path. Discarding at ``emit`` —
    which is what this did while push looked like the only route — threw away every
    event on every instance CT deploys, since CT injects none of these three.
    """
    for missing in ("ingest_url", "ingest_token", "run_id"):
        e = _emitter(**{missing: ""})
        assert e.can_push is False
        e.emit("session.started", "a@x.com")
        assert e.pending() == 1


def test_emit_buffers_well_formed_event():
    e = _emitter()
    e.emit("build.commit", "a@x.com", {"sha": "abc"}, idempotency_key="k1")
    assert e.pending() == 1
    delivered = []
    e.drain(lambda ev: (delivered.append(ev) or True))
    ev = delivered[0]
    assert ev["schema_version"] == 1
    assert ev["run_id"] == "run-1" and ev["workspace_id"] == "100"
    assert ev["type"] == "build.commit" and ev["attendee"] == "a@x.com"
    assert ev["payload"] == {"sha": "abc"} and ev["idempotency_key"] == "k1"
    assert "occurred_at" in ev


def test_drain_delivers_all_on_success_and_empties():
    e = _emitter()
    for i in range(5):
        e.emit("heartbeat", "a@x.com", {"i": i})
    n = e.drain(lambda ev: True)
    assert n == 5 and e.pending() == 0


def test_drain_stops_at_first_failure_and_keeps_remainder():
    e = _emitter()
    for i in range(5):
        e.emit("heartbeat", "a@x.com", {"i": i}, idempotency_key=f"k{i}")
    # Deliver first 2, then fail.
    calls = {"n": 0}

    def post(ev):
        calls["n"] += 1
        return calls["n"] <= 2

    n = e.drain(post)
    assert n == 2 and e.pending() == 3
    # Order preserved — the remaining are k2,k3,k4.
    remaining = []
    e.drain(lambda ev: (remaining.append(ev["idempotency_key"]) or True))
    assert remaining == ["k2", "k3", "k4"]


def test_exception_in_post_is_fail_soft():
    e = _emitter()
    e.emit("heartbeat", "a@x.com")

    def boom(ev):
        raise RuntimeError("network down")

    assert e.drain(boom) == 0
    assert e.pending() == 1  # kept for retry


def test_buffer_is_bounded_drop_oldest():
    e = _emitter(max_buffer=3)
    for i in range(5):
        e.emit("heartbeat", "a@x.com", {"i": i}, idempotency_key=f"k{i}")
    assert e.pending() == 3
    seen = []
    e.drain(lambda ev: (seen.append(ev["idempotency_key"]) or True))
    assert seen == ["k2", "k3", "k4"]  # oldest two dropped


def test_auto_idempotency_keys_are_unique():
    e = _emitter()
    e.emit("heartbeat", "a@x.com")
    e.emit("heartbeat", "a@x.com")
    keys = []
    e.drain(lambda ev: (keys.append(ev["idempotency_key"]) or True))
    assert len(set(keys)) == 2
