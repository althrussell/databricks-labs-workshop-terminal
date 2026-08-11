"""Recovering from the auth error family without the attendee doing anything.

Omnigent raises once and stops, so the same credential failure repeats forever
until something outside it writes a fresh token. These tests pin that something
down: what counts as a credential failure, what recovery does, how often it is
allowed to do it, and that a launch which can be repaired is repaired instead of
reported.
"""

from __future__ import annotations

import time

import pytest

from .test_obo import make_jwt


REMOTE_URL = "https://alice-omnigent.example.databricksapps.com"
ALICE = {"X-Forwarded-Email": "alice@example.com"}


def test_the_codes_an_attendee_actually_sees_are_recognised():
    from server.selfheal import is_auth_error

    # Omnigent's sanitized codes name the call, not the cause — which is exactly
    # why they were unactionable during the incident.
    assert is_auth_error("spec_resolver_failed")
    assert is_auth_error("native_terminal_start_failed")
    # A code nobody has seen before still recovers if it is credential-shaped.
    assert is_auth_error("databricks_token_refresh_failed")
    assert is_auth_error("some_new_code", "server returned 401 Unauthorized")
    # And an unrelated failure does not trigger a credential rewrite.
    assert not is_auth_error("disk_full")
    assert not is_auth_error("turn_failed", "the model returned an empty response")


def test_recovery_rewrites_wakes_and_asks_the_tab_for_a_newer_token(monkeypatch):
    from server import selfheal

    calls: list[str] = []
    monkeypatch.setattr(selfheal.SelfHealer, "_emit", lambda *a, **k: None)
    healer = selfheal.SelfHealer(cooldown=0.0)
    monkeypatch.setattr(
        healer, "_remirror", lambda email, actions: calls.append("remirror") is None
    )
    monkeypatch.setattr(healer, "_wake", lambda email, actions: calls.append("wake"))
    monkeypatch.setattr(healer, "_nudge", lambda actions: calls.append("nudge"))
    monkeypatch.setattr(healer, "_is_fresh", lambda email: True)

    result = healer.recover("alice@example.com", "test")

    assert result["attempted"] is True
    assert result["credential_fresh"] is True
    # Order matters: writing the token we already hold is what makes the wake
    # meaningful, and asking the tab is the slow path behind both.
    assert calls == ["remirror", "wake", "nudge"]


def test_a_crash_loop_costs_one_recovery_not_hundreds(monkeypatch):
    from server import selfheal

    now = [0.0]
    monkeypatch.setattr(selfheal.SelfHealer, "_emit", lambda *a, **k: None)
    healer = selfheal.SelfHealer(cooldown=15.0, clock=lambda: now[0])
    for name in ("_remirror", "_wake", "_nudge"):
        monkeypatch.setattr(healer, name, lambda *a, **k: None)
    monkeypatch.setattr(healer, "_is_fresh", lambda email: False)

    first = healer.recover("alice@example.com", "loop")
    assert first["credential_fresh"] is False
    during = [healer.recover("alice@example.com", "loop")["attempted"] for _ in range(20)]
    now[0] = 20.0
    after = healer.recover("alice@example.com", "loop")
    # A launch the attendee is waiting on jumps the queue regardless.
    now[0] = 20.1
    forced = healer.recover("alice@example.com", "launch", force=True)

    assert first["attempted"] is True
    assert during == [False] * 20
    assert after["attempted"] is True
    assert forced["attempted"] is True


def test_a_collected_auth_error_triggers_recovery_and_a_disk_error_does_not(monkeypatch):
    from server import selfheal

    healed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        selfheal.self_healer,
        "recover",
        lambda email, reason, **_k: healed.append((email, reason)) or {},
    )

    selfheal.on_omnigent_error("alice@example.com", "spec_resolver_failed", "boom")
    selfheal.on_omnigent_error("alice@example.com", "disk_full", "no space left")

    assert len(healed) == 1
    assert healed[0][0] == "alice@example.com"
    assert "spec_resolver_failed" in healed[0][1]


def test_the_log_sweep_hands_credential_failures_to_the_healer(monkeypatch, tmp_path):
    """The collector sees the real exception seconds before the attendee acts."""
    from server import selfheal
    from server.diagnostics import Journal
    from server.log_collector import LogCollector

    home = tmp_path / "alice"
    logs = home / ".omnigent" / "logs" / "runner"
    logs.mkdir(parents=True)
    (logs / "runner.log").write_text(
        "ERROR 08-10 09:00:00.000 runner.app resolve | spec_resolver_failed\n"
    )

    class _Users:
        def all(self):
            return [type("U", (), {"email": "alice@example.com", "home": str(home)})()]

    healed: list[str] = []
    monkeypatch.setattr(
        selfheal.self_healer,
        "recover",
        lambda email, reason, **_k: healed.append(email) or {},
    )
    collector = LogCollector(
        journal=Journal(None, capacity=10),
        users=_Users(),
        emitter=type("E", (), {"emit": staticmethod(lambda *a, **k: None)})(),
    )
    collector.sweep()

    assert healed == ["alice@example.com"]


def test_a_repairable_launch_is_repaired_rather_than_refused(
    client, monkeypatch, tmp_path
):
    """The common case: the tab already delivered a token the mirror is behind.

    Refusing here would show an attendee an error they could clear by clicking
    the same button again — so the server clicks it for them.
    """
    from server import config, main, obo
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
    # The app service principal is a different plane and a different test.
    monkeypatch.setattr(main, "ensure_user_credentials", lambda user: None)
    user_manager.get("alice@example.com")
    manager = obo.OboManager()
    monkeypatch.setattr(obo, "obo_manager", manager)
    manager.capture("alice@example.com", make_jwt(time.time() + 5))

    # Recovery is what a live tab delivering a newer token looks like.
    def repair(email):
        manager.capture(email, make_jwt(time.time() + 3600))
        return True

    monkeypatch.setattr(manager, "force_refresh", repair)

    response = client.post("/api/sessions", json={"agent_id": "omnigent"}, headers=ALICE)

    assert response.status_code == 200


def test_an_attendee_report_gets_a_retry_verdict_not_just_an_ack(
    client, monkeypatch, tmp_path
):
    from server import config, obo, selfheal
    from server.users import user_manager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("ENABLE_OBO", "true")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(selfheal, "self_healer", selfheal.SelfHealer(cooldown=0.0))
    user_manager.get("alice@example.com")
    manager = obo.OboManager()
    monkeypatch.setattr(obo, "obo_manager", manager)
    manager.capture("alice@example.com", make_jwt(time.time() + 3600))

    healed = client.post(
        "/api/telemetry/error",
        json={"code": "spec_resolver_failed", "detail": "auth"},
        headers=ALICE,
    ).json()
    unrelated = client.post(
        "/api/telemetry/error",
        json={"code": "turn_failed", "detail": "the model gave up"},
        headers=ALICE,
    ).json()

    assert healed["retry"] is True
    # Nothing to repair means nothing to retry — do not send an attendee round
    # a loop that cannot end.
    assert unrelated["retry"] is False


def test_the_recover_button_repairs_the_attendee_who_pressed_it(
    client, monkeypatch, tmp_path
):
    """A banner an attendee can act on needs something behind the button."""
    from server import config, obo, selfheal
    from server.users import user_manager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("ENABLE_OBO", "true")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(selfheal, "self_healer", selfheal.SelfHealer(cooldown=15.0))
    user_manager.get("alice@example.com")
    manager = obo.OboManager()
    monkeypatch.setattr(obo, "obo_manager", manager)
    manager.capture("alice@example.com", make_jwt(time.time() + 3600))

    body = client.post("/api/recover", headers=ALICE).json()
    # Pressing it twice in a row must work: the cooldown protects the crash-loop
    # path, not a person who just pressed a button.
    again = client.post("/api/recover", headers=ALICE).json()

    assert body["recovered"] is True
    assert "remirrored" in body["actions"]
    assert again["recovered"] is True


def test_recover_only_ever_repairs_the_caller(client, monkeypatch):
    """No attendee id in the request: one attendee cannot recover another."""
    from server import selfheal

    seen: list[str] = []
    monkeypatch.setattr(
        selfheal,
        "self_healer",
        type(
            "H",
            (),
            {"recover": staticmethod(lambda email, reason, **_k: seen.append(email) or {})},
        )(),
    )

    client.post("/api/recover", json={"email": "bob@example.com"}, headers=ALICE)

    assert seen == ["alice@example.com"]


def test_a_broken_recovery_step_does_not_take_the_caller_with_it(monkeypatch):
    """This runs from a log sweep and from a launch request; neither may die."""
    from server import obo, selfheal

    def explode(_email):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(obo.obo_manager, "force_refresh", explode)
    monkeypatch.setattr(selfheal.SelfHealer, "_emit", lambda *a, **k: None)
    healer = selfheal.SelfHealer(cooldown=0.0)

    result = healer.recover("alice@example.com", "test")

    assert result["attempted"] is True
    assert result["remirrored"] is False
