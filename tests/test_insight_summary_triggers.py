"""When the edge summary runs: wrap primary, final harvest backstop (contract C6).

The trigger design is the load-bearing part of phase 4, because the inputs are
destroyed by teardown. These tests pin the two properties that decide whether a
run produces anything at all:

- wrap summarises without blocking the operator's phase flip, which happens in
  front of a live room;
- the ``final=true`` harvest summarises *and flushes synchronously*, because
  Control Tower deletes the app moments after that response and the periodic
  flusher runs on a 15-second timer.
"""

from __future__ import annotations

import pytest

ADMIN = {"X-Forwarded-Email": "op@example.com"}
ALICE = {"X-Forwarded-Email": "alice@example.com"}


@pytest.fixture
def capture_on(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    from server import insight_summary

    insight_summary.stamps.clear()
    yield
    insight_summary.stamps.clear()


@pytest.fixture
def recorded(monkeypatch):
    """Record summarisation calls instead of harvesting or calling a model."""
    from server import insight_summary

    calls: list[dict] = []

    def fake_all(users, *, phase, allow_llm, emitter=None):
        calls.append({
            "attendees": sorted(u.email for u in users),
            "phase": phase,
            "allow_llm": allow_llm,
        })
        return len(users)

    monkeypatch.setattr(insight_summary, "summarise_all", fake_all)
    return calls


def _seen(client):
    """Register an attendee, as launching a terminal would."""
    from server.users import user_manager

    user_manager.get("alice@example.com")


# --- wrap: the primary trigger ------------------------------------------------


def test_wrap_triggers_the_edge_summary(client, as_admin, capture_on, recorded):
    _seen(client)
    resp = client.post("/api/admin/phase", json={"phase": "wrap"}, headers=ADMIN)

    assert resp.status_code == 200
    _join_summary_threads()
    [call] = recorded
    assert "alice@example.com" in call["attendees"]
    assert call["phase"] == "wrap"
    # Wrap is the trigger that gets a model: the app is warm and the attendee is
    # still in the room to be told what was captured.
    assert call["allow_llm"] is True


def test_earlier_phases_do_not_summarise(client, as_admin, capture_on, recorded):
    """Mid-workshop the session isn't over, and the model call is not free."""
    _seen(client)
    for phase in ("intro", "setup", "build"):
        client.post("/api/admin/phase", json={"phase": phase}, headers=ADMIN)

    _join_summary_threads()
    assert recorded == []


def test_wrap_does_not_summarise_when_capture_is_off(client, as_admin, recorded, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "false")
    _seen(client)
    client.post("/api/admin/phase", json={"phase": "wrap"}, headers=ADMIN)

    _join_summary_threads()
    assert recorded == []


def test_the_phase_flip_does_not_wait_on_the_model(client, as_admin, capture_on, monkeypatch):
    """The operator is standing in front of a room; a per-attendee model call on
    the request thread would stall the projector."""
    import threading

    from server import insight_summary

    released = threading.Event()
    entered = threading.Event()

    def slow(users, *, phase, allow_llm, emitter=None):
        entered.set()
        released.wait(timeout=5)
        return 0

    monkeypatch.setattr(insight_summary, "summarise_all", slow)
    _seen(client)
    resp = client.post("/api/admin/phase", json={"phase": "wrap"}, headers=ADMIN)

    assert resp.status_code == 200, "the phase flip returned before summarising"
    assert entered.wait(timeout=5), "summarisation never started"
    released.set()
    _join_summary_threads()


def test_a_summary_failure_does_not_fail_the_phase_flip(
    client, as_admin, capture_on, monkeypatch
):
    from server import insight_summary

    monkeypatch.setattr(
        insight_summary, "summarise_all",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model exploded")),
    )
    _seen(client)
    resp = client.post("/api/admin/phase", json={"phase": "wrap"}, headers=ADMIN)

    assert resp.status_code == 200
    _join_summary_threads()
    assert client.get("/api/admin/state", headers=ADMIN).json()["phase"] == "wrap"


# --- final harvest: the backstop ----------------------------------------------


def test_the_final_harvest_summarises_without_a_model(
    client, as_admin, capture_on, recorded
):
    """Teardown can run hours later on a cold app, so the backstop is extraction."""
    _seen(client)
    resp = client.get("/api/admin/stats?final=true", headers=ADMIN)

    assert resp.status_code == 200
    assert resp.json()["instance"]["final"] is True
    [call] = recorded
    assert "alice@example.com" in call["attendees"]
    assert call["allow_llm"] is False


def test_an_ordinary_harvest_does_not_summarise(client, as_admin, capture_on, recorded):
    """Control Tower polls this endpoint all day; summarising per poll would burn
    the run-once stamp long before the attendee finished working."""
    _seen(client)
    resp = client.get("/api/admin/stats", headers=ADMIN)

    assert resp.json()["instance"]["final"] is False
    assert recorded == []


def test_the_final_harvest_flushes_the_buffer_before_the_app_is_deleted(
    client, as_admin, capture_on, monkeypatch
):
    """The single most fragile point in phase 4: everything harvested at wrap is
    worthless if it is still sitting in the emitter's buffer when the container
    goes away."""
    from server import admin, event_emitter

    flushed: list[bool] = []
    monkeypatch.setattr(
        event_emitter, "flush_now", lambda emitter=None: flushed.append(True) or 3
    )
    monkeypatch.setattr(admin, "user_manager", admin.user_manager)
    _seen(client)

    body = client.get("/api/admin/stats?final=true", headers=ADMIN).json()

    assert flushed == [True]
    assert body["instance"]["events_flushed"] == 3


def test_the_summary_count_is_reported_back_to_control_tower(
    client, as_admin, capture_on, recorded
):
    """CT logs it against the teardown, which is the only place an operator can
    later find out whether the run produced any insight at all."""
    _seen(client)
    body = client.get("/api/admin/stats?final=true", headers=ADMIN).json()

    assert body["instance"]["summaries_emitted"] == len(body["users"])


def test_the_final_harvest_still_returns_stats_if_summarising_breaks(
    client, as_admin, capture_on, monkeypatch
):
    """Teardown reads this response for the impact record; losing it to a summary
    bug would cost the durable stats as well as the insight."""
    from server import insight_summary

    monkeypatch.setattr(
        insight_summary, "summarise_all",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _seen(client)
    resp = client.get("/api/admin/stats?final=true", headers=ADMIN)

    assert resp.status_code == 200
    assert resp.json()["users"], "the impact record must survive a summary failure"


def _join_summary_threads(timeout: float = 5.0) -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name == "insight-summary":
            thread.join(timeout=timeout)
