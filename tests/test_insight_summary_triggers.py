"""When the edge summary runs: every harvest, bounded (contract C6).

The trigger design is the load-bearing part of phase 4, because the inputs are
destroyed by teardown. Summaries used to run only at the ``wrap`` phase flip,
which meant a run whose operator never touched the phase control — the normal
case — reached teardown having captured counters and nothing else. These tests
pin the properties that decide whether a run produces anything at all:

- an ordinary harvest rolls the summary forward, so nothing depends on an
  operator action;
- two gates bound what that costs: an interval floor per attendee and a material
  fingerprint, so Control Tower polling all day does not mean a model call per
  poll;
- wrap and teardown force a pass past the interval, because at the end of a
  session there is no later chance;
- neither trigger blocks its caller — wrap happens in front of a live room, and
  the ``final=true`` harvest additionally flushes synchronously, because CT
  deletes the app moments after that response and the periodic flusher runs on a
  15-second timer.
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

    def fake_all(users, *, phase, allow_llm, force=False, emitter=None):
        calls.append({
            "attendees": sorted(u.email for u in users),
            "phase": phase,
            "allow_llm": allow_llm,
            "force": force,
        })
        return len(users)

    monkeypatch.setattr(insight_summary, "summarise_all", fake_all)
    return calls


def _seen(client):
    """Register an attendee, as launching a terminal would."""
    from server.users import user_manager

    user_manager.get("alice@example.com")


def _mark_everyone_summarised(generator: str = "llm") -> None:
    """Stamp every registered attendee as just summarised.

    Every attendee, not just alice: the pre-check that decides whether to spawn a
    pass asks whether *anyone* is due, so one attendee left over from another test
    would make an interval test pass for the wrong reason.
    """
    from server import insight_summary
    from server.users import user_manager

    for user in user_manager.all():
        insight_summary.stamps.mark(user.email, generator, "fp-1")


# --- wrap: still a trigger, now a forcing one ---------------------------------


def test_wrap_triggers_the_edge_summary(client, as_admin, capture_on, recorded):
    _seen(client)
    resp = client.post("/api/admin/phase", json={"phase": "wrap"}, headers=ADMIN)

    assert resp.status_code == 200
    _join_summary_threads()
    [call] = recorded
    assert "alice@example.com" in call["attendees"]
    assert call["phase"] == "wrap"
    # Wrap is still the best trigger: the app is warm and the attendee is still in
    # the room to be told what was captured.
    assert call["allow_llm"] is True


def test_wrap_forces_a_pass_past_the_interval(client, as_admin, capture_on, recorded):
    """An operator flipping to wrap is asking for the current picture, not the one
    from up to twenty minutes ago."""
    _seen(client)
    _mark_everyone_summarised()

    client.post("/api/admin/phase", json={"phase": "wrap"}, headers=ADMIN)

    _join_summary_threads()
    [call] = recorded
    assert call["force"] is True


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

    def slow(users, *, phase, allow_llm, force=False, emitter=None):
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


# --- the ordinary harvest: the trigger that does not need an operator ----------


def test_an_ordinary_harvest_summarises_when_due(
    client, as_admin, capture_on, recorded
):
    """The whole point of rolling the summary: a run whose operator never touches
    the phase control still produces briefs that describe the session."""
    _seen(client)
    client.post("/api/admin/phase", json={"phase": "build"}, headers=ADMIN)
    recorded.clear()

    body = client.get("/api/admin/stats", headers=ADMIN).json()

    assert body["instance"]["final"] is False
    assert body["instance"]["summary_pass_started"] is True
    _join_summary_threads()
    [call] = recorded
    assert "alice@example.com" in call["attendees"]
    # The live phase, so the payload says how far the run had got rather than
    # claiming a wrap that never happened.
    assert call["phase"] == "build"
    assert call["allow_llm"] is True
    # Not forced: the interval floor is what keeps a fleet-wide poll cheap.
    assert call["force"] is False


def test_an_ordinary_harvest_does_not_summarise_before_the_interval_elapses(
    client, as_admin, capture_on, recorded
):
    """Control Tower polls this endpoint every few minutes all day. Without the
    floor, every poll would walk each attendee's home and call a model."""
    from server import insight_summary

    _seen(client)
    _mark_everyone_summarised()

    body = client.get("/api/admin/stats", headers=ADMIN).json()

    assert insight_summary.stamps.revision_for("alice@example.com") == 1
    assert body["instance"]["summary_pass_started"] is False
    _join_summary_threads()
    assert recorded == [], "a thread was spawned for an attendee that was not due"


def test_an_elapsed_interval_makes_an_attendee_due_again(
    client, as_admin, capture_on, recorded, monkeypatch
):
    from server import config

    _seen(client)
    _mark_everyone_summarised()
    monkeypatch.setattr(config, "insight_summary_min_interval_seconds", lambda: 0)

    body = client.get("/api/admin/stats", headers=ADMIN).json()

    assert body["instance"]["summary_pass_started"] is True
    _join_summary_threads()
    assert recorded, "the interval elapsed and nothing was summarised"


def test_an_ordinary_harvest_does_not_summarise_when_capture_is_off(
    client, as_admin, recorded, monkeypatch
):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "false")
    _seen(client)

    body = client.get("/api/admin/stats", headers=ADMIN).json()

    _join_summary_threads()
    assert recorded == []
    assert "summary_pass_started" in body["instance"]
    assert body["instance"]["summary_pass_started"] is False


def test_an_ordinary_harvest_still_returns_stats_if_summarising_breaks(
    client, as_admin, capture_on, monkeypatch
):
    """CT stores this response as the durable snapshot. Insight is the optional
    half of the payload; the counters are the half that has always been promised."""
    from server import insight_summary

    monkeypatch.setattr(
        insight_summary, "summarise_in_background",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _seen(client)

    resp = client.get("/api/admin/stats", headers=ADMIN)

    assert resp.status_code == 200
    assert resp.json()["users"]


# --- the fingerprint gate: unchanged material costs no model call -------------


def test_unchanged_material_does_not_call_the_model(capture_on, monkeypatch):
    """The interval says "you may look again"; the fingerprint says "there is
    something new to say". Without the second gate a quiet attendee would
    accumulate identical summaries at rising revisions all afternoon."""
    from server import artifacts, insight_summary
    from server.users import user_manager

    user = user_manager.get("alice@example.com")
    harvest = artifacts.Harvest(
        prompts=["build me space invaders"],
        artifacts=[artifacts.Artifact(kind="readme", title="README.md", bytes=512)],
        prompt_count=4,
    )
    monkeypatch.setattr(artifacts, "harvest_user", lambda *a, **k: harvest)
    monkeypatch.setattr(insight_summary.config, "insight_summary_min_interval_seconds", lambda: 0)

    asked: list[int] = []

    def fake_model(harvest, signal):
        asked.append(1)
        return {"headline": "Built Space Invaders."}, "test-endpoint"

    monkeypatch.setattr(insight_summary, "_ask_model", fake_model)
    emitter = _RecordingEmitter()

    first = insight_summary.summarise_user(user, phase="build", emitter=emitter)
    second = insight_summary.summarise_user(user, phase="build", emitter=emitter)

    assert first is not None
    assert second is None, "an unchanged session was summarised twice"
    assert len(asked) == 1
    assert len(emitter.events) == 1


def test_new_material_rolls_the_summary_forward(capture_on, monkeypatch):
    """A revision per regeneration, in the payload and the key — without it
    Control Tower would discard every refresh after the first as a duplicate."""
    from server import artifacts, insight_summary
    from server.users import user_manager

    user = user_manager.get("alice@example.com")
    harvest = artifacts.Harvest(prompts=["build me space invaders"], prompt_count=4)
    monkeypatch.setattr(artifacts, "harvest_user", lambda *a, **k: harvest)
    monkeypatch.setattr(insight_summary.config, "insight_summary_min_interval_seconds", lambda: 0)
    monkeypatch.setattr(
        insight_summary, "_ask_model",
        lambda h, s: ({"headline": "Built Space Invaders."}, "test-endpoint"),
    )
    emitter = _RecordingEmitter()

    insight_summary.summarise_user(user, phase="build", emitter=emitter)
    # The attendee kept working: a new prompt and a shipped artifact.
    harvest.prompts.append("add a high score table")
    harvest.prompt_count = 9
    harvest.artifacts.append(
        artifacts.Artifact(kind="readme", title="README.md", bytes=512)
    )
    insight_summary.summarise_user(user, phase="wrap", emitter=emitter)

    first, second = emitter.events
    assert [e["payload"]["revision"] for e in emitter.events] == [1, 2]
    # One session, so one summary_id — CT supersedes rather than accumulates.
    assert first["payload"]["summary_id"] == second["payload"]["summary_id"]
    assert first["idempotency_key"] != second["idempotency_key"]
    assert second["idempotency_key"].endswith(":llm:2")


class _RecordingEmitter:
    run_id = "3f1b8c2e-9a44-4d21-8f0e-7c5b1a2d6e90"

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, type_, attendee, payload, *, idempotency_key=""):
        self.events.append({
            "type": type_,
            "attendee": attendee,
            "payload": payload,
            "idempotency_key": idempotency_key,
        })


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
