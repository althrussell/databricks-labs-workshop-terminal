"""An eight-hour event, compressed, and then deliberately broken four ways.

Nothing here waits eight real hours. The soak advances a controlled clock over
the same code paths a live event exercises — capture, watch, sample, gate, heal
— across enough renewals that a leak or a one-shot latch would show up. The
chaos half then forces the four failures we know how to cause: an expired
credential, a deleted mirror file, a host killed mid-turn, and a renewal that
never comes.

The bar for every one of them is the same: no attendee-visible auth failure the
product cannot explain, and no state the product cannot get itself out of once
the underlying problem clears.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from .test_obo import make_jwt

REMOTE_URL = "https://alice-omnigent.example.databricksapps.com"
ALICE = {"X-Forwarded-Email": "alice@example.com"}
EMAIL = "alice@example.com"

EVENT_HOURS = 8
TOKEN_LIFETIME = 3600.0


class Clock:
    """Simulated time, advanced explicitly so a soak costs milliseconds."""

    def __init__(self, start: float | None = None) -> None:
        self.now = start if start is not None else time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def soak(monkeypatch, tmp_path):
    """One attendee, one instance, everything installed and working."""
    from server import config, obo
    from server.bootstrap import install as install_mod
    from server.users import user_manager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("ENABLE_OBO", "true")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(
        install_mod,
        "ready",
        lambda: {"bash": True, "claude": True, "codex": True, "omnigent": True},
    )
    user = user_manager.get(EMAIL)
    manager = obo.OboManager()
    monkeypatch.setattr(obo, "obo_manager", manager)
    return manager, user


@pytest.fixture
def watched(soak, monkeypatch):
    """The freshness watcher, wired to a clock the test drives."""
    from server import obo

    manager, user = soak
    nudges: list[dict] = []
    watcher = obo.OboFreshnessWatcher(
        manager, interval=60, renew_lead=300, publish=nudges.append
    )
    return manager, user, watcher, nudges


def open_tab(manager, clock: Clock) -> None:
    """What a live browser tab does on every poll: hand over a current token."""
    manager.capture(EMAIL, make_jwt(clock.now + TOKEN_LIFETIME, clock.now))


def agents(client) -> dict[str, dict]:
    return {a["id"]: a for a in client.get("/api/agents", headers=ALICE).json()["agents"]}


# --- the soak -------------------------------------------------------------


def test_eight_hours_of_renewals_never_shows_the_attendee_an_auth_failure(
    client, watched, monkeypatch
):
    """The event, compressed: a tab polling every minute for eight hours."""
    from server import obo

    manager, _user, watcher, _nudges = watched
    clock = Clock()
    monkeypatch.setattr(obo.time, "time", clock)

    stale_samples = []
    for minute in range(EVENT_HOURS * 60):
        open_tab(manager, clock)
        state = watcher.sample(now=clock.now)[EMAIL]
        if state != "fresh":
            stale_samples.append((minute, state))
        clock.advance(60)

    assert stale_samples == [], f"credential went stale mid-event: {stale_samples[:3]}"


def test_a_soak_does_not_accumulate_state_per_renewal(client, watched, monkeypatch):
    """A per-capture leak would not show up in an hour and would in a day."""
    from server import obo

    manager, _user, _watcher, _nudges = watched
    clock = Clock()
    monkeypatch.setattr(obo.time, "time", clock)

    for _ in range(EVENT_HOURS * 60):
        open_tab(manager, clock)
        clock.advance(60)

    assert len(manager._by_email) == 1  # noqa: SLF001 — one attendee, one record
    assert len(manager._health) == 1  # noqa: SLF001


def test_the_watcher_asks_for_a_renewal_before_the_attendee_could_notice(
    client, watched, monkeypatch
):
    from server import obo

    manager, _user, watcher, nudges = watched
    clock = Clock()
    monkeypatch.setattr(obo.time, "time", clock)
    open_tab(manager, clock)

    # Drift to inside the renewal lead without the tab handing anything over.
    clock.advance(TOKEN_LIFETIME - 200)
    watcher.sample(now=clock.now)

    assert nudges == [{"t": "obo_refresh"}]
    # And it is still usable at the moment we ask — asking after it breaks is
    # what the incident did.
    assert manager.status(EMAIL)["fresh"] is True


# --- chaos ----------------------------------------------------------------


def test_chaos_force_expired_credential_recovers_when_the_tab_answers(
    client, watched, monkeypatch
):
    from server import obo, selfheal

    manager, _user, _watcher, _nudges = watched
    clock = Clock()
    monkeypatch.setattr(obo.time, "time", clock)
    open_tab(manager, clock)

    clock.advance(TOKEN_LIFETIME + 60)  # the credential is now dead
    assert manager.status(EMAIL)["fresh"] is False
    assert agents(client)["omnigent"]["blocked"] == "credential_stale"

    # Recovery alone cannot mint a token, and says so rather than pretending.
    assert selfheal.self_healer.recover(EMAIL, "chaos", force=True)["credential_fresh"] is False
    open_tab(manager, clock)  # the nudged tab answers

    assert manager.status(EMAIL)["fresh"] is True
    assert agents(client)["omnigent"]["ready"] is True


def test_chaos_deleting_the_mirror_file_is_repaired_without_an_attendee_noticing(
    client, watched
):
    """`auth_tokens.json` is the file the Omnigent host reads. Losing it used to
    be indistinguishable from a stale credential and needed a human."""
    from server import selfheal

    manager, user, _watcher, _nudges = watched
    clock = Clock()
    open_tab(manager, clock)
    mirror = Path(user.home) / ".omnigent" / "auth_tokens.json"
    assert mirror.exists()

    mirror.unlink()
    result = selfheal.self_healer.recover(EMAIL, "chaos: mirror deleted", force=True)

    assert mirror.exists(), "recovery must rewrite the file the host reads"
    assert result["credential_fresh"] is True
    assert json.loads(mirror.read_text())


def test_chaos_a_host_killed_mid_turn_is_woken_rather_than_left_waiting(
    client, watched, monkeypatch
):
    from server import omnigent_remote, selfheal

    manager, _user, _watcher, _nudges = watched
    open_tab(manager, Clock())
    woken: list[str] = []
    monkeypatch.setattr(
        omnigent_remote.remote_host_manager, "notify", lambda email: woken.append(email)
    )

    result = selfheal.self_healer.recover(EMAIL, "chaos: host killed", force=True)

    # Both the re-mirror and the explicit wake tell the host; waking a live host
    # twice costs nothing, and leaving a dead one asleep costs the attendee.
    assert set(woken) == {EMAIL}
    assert "woke host" in result["actions"]


def test_chaos_a_renewal_that_never_comes_degrades_instead_of_failing_open(
    client, watched, monkeypatch, launchable_agents
):
    """The tab is gone for good. Every Omnigent surface must refuse cleanly and
    the bare tier must be untouched — this is the shape of the incident."""
    from server import obo

    manager, _user, watcher, _nudges = watched
    clock = Clock()
    monkeypatch.setattr(obo.time, "time", clock)
    open_tab(manager, clock)

    for _ in range(120):  # two hours of nobody answering
        watcher.sample(now=clock.now)
        clock.advance(60)

    catalog = agents(client)
    assert catalog["omnigent"]["ready"] is False
    assert catalog["claude"]["ready"] is True
    refused = client.post("/api/sessions", json={"agent_id": "omnigent"}, headers=ALICE)
    assert refused.status_code == 503
    assert "Reload this tab" in refused.json()["detail"]
    assert client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE).status_code == 200


def test_chaos_leaves_one_health_transition_per_state_not_a_storm(
    client, watched, monkeypatch
):
    """An operator watching a room needs the change, not a per-sample stream."""
    from server import obo

    manager, _user, watcher, _nudges = watched
    emitted: list[tuple] = []
    from server import event_emitter as emitter_module

    monkeypatch.setattr(
        emitter_module.event_emitter,
        "emit",
        lambda kind, who, payload=None, **_k: emitted.append((kind, payload or {})),
    )
    clock = Clock()
    monkeypatch.setattr(obo.time, "time", clock)
    open_tab(manager, clock)

    for _ in range(180):
        watcher.sample(now=clock.now)
        clock.advance(60)

    states = [p.get("state") for kind, p in emitted if kind == "obo.health"]
    assert states.count("stale") == 1, states
