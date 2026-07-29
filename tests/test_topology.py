"""Topology guard (gap P1-11a)."""

import threading

import pytest

from server import topology


def test_config_permits_multi_attendee():
    # global > per-user → a second attendee could also get sessions.
    assert topology.config_permits_multi_attendee(30, 3) is True
    # global == per-user → a single attendee can saturate; effectively single.
    assert topology.config_permits_multi_attendee(3, 3) is False
    assert topology.config_permits_multi_attendee(2, 3) is False


def test_startup_warning_fires_for_multi_attendee_caps(monkeypatch):
    monkeypatch.delenv("ALLOW_SHARED_TOPOLOGY", raising=False)
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "30")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "3")
    warning = topology.startup_warning()
    assert warning and "one disposable workspace per attendee" in warning


def test_startup_warning_silent_when_opted_in(monkeypatch):
    monkeypatch.setenv("ALLOW_SHARED_TOPOLOGY", "true")
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "30")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "3")
    assert topology.startup_warning() is None


def test_startup_warning_silent_for_single_attendee_caps(monkeypatch):
    monkeypatch.delenv("ALLOW_SHARED_TOPOLOGY", raising=False)
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "3")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "3")
    assert topology.startup_warning() is None


def test_second_attendee_warning(monkeypatch):
    monkeypatch.delenv("ALLOW_SHARED_TOPOLOGY", raising=False)
    assert topology.second_attendee_warning(1) is None
    assert topology.second_attendee_warning(2) is not None


def test_second_attendee_warning_silent_when_opted_in(monkeypatch):
    monkeypatch.setenv("ALLOW_SHARED_TOPOLOGY", "1")
    assert topology.second_attendee_warning(5) is None


def test_remote_omnigent_rejects_explicit_shared_topology(monkeypatch):
    monkeypatch.setenv(
        "OMNIGENT_APP_URL",
        "https://alice-omnigent.example.databricksapps.com",
    )
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")
    monkeypatch.setenv("ALLOW_SHARED_TOPOLOGY", "true")
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "3")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "3")

    with pytest.raises(ValueError, match="one attendee per instance"):
        topology.validate_remote_omnigent()


def test_remote_omnigent_rejects_multi_attendee_session_caps(monkeypatch):
    monkeypatch.setenv(
        "OMNIGENT_APP_URL",
        "https://alice-omnigent.example.databricksapps.com",
    )
    monkeypatch.delenv("ALLOW_SHARED_TOPOLOGY", raising=False)
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "30")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "3")

    with pytest.raises(ValueError, match="MAX_SESSIONS_GLOBAL"):
        topology.validate_remote_omnigent()


def test_local_omnigent_allows_shared_topology(monkeypatch):
    monkeypatch.delenv("OMNIGENT_APP_URL", raising=False)
    monkeypatch.setenv("ALLOW_SHARED_TOPOLOGY", "true")
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "30")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "3")

    topology.validate_remote_omnigent()


def test_remote_binding_rejects_a_different_attendee(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "OMNIGENT_APP_URL",
        "https://alice-omnigent.example.databricksapps.com",
    )
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")
    monkeypatch.setattr(
        topology.config, "users_root", lambda: str(tmp_path / "users")
    )
    binding = topology.AttendeeBinding()

    assert binding.bind("Alice@Example.COM") == "alice@example.com"
    assert binding.bind("alice@example.com") == "alice@example.com"
    with pytest.raises(topology.AttendeeBindingConflict, match="configured"):
        binding.bind("bob@example.com")
    assert binding.status() == {"enforced": True, "status": "bound"}


def test_remote_binding_survives_manager_restart(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "OMNIGENT_APP_URL",
        "https://alice-omnigent.example.databricksapps.com",
    )
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")
    monkeypatch.setattr(
        topology.config, "users_root", lambda: str(tmp_path / "users")
    )

    assert topology.AttendeeBinding().bind("alice@example.com") == "alice@example.com"
    restarted = topology.AttendeeBinding()

    assert restarted.bind("alice@example.com") == "alice@example.com"
    with pytest.raises(topology.AttendeeBindingConflict, match="configured"):
        restarted.bind("bob@example.com")
    marker = tmp_path / ".omnigent-attendee-binding"
    assert not marker.exists()


def test_remote_binding_is_concurrency_safe(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "OMNIGENT_APP_URL",
        "https://alice-omnigent.example.databricksapps.com",
    )
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "attendee-7@example.com")
    monkeypatch.setattr(
        topology.config, "users_root", lambda: str(tmp_path / "users")
    )
    barrier = threading.Barrier(12)
    outcomes = []

    def bind(email):
        barrier.wait()
        try:
            outcomes.append(("bound", topology.AttendeeBinding().bind(email)))
        except topology.AttendeeBindingConflict:
            outcomes.append(("rejected", email))

    threads = [
        threading.Thread(target=bind, args=(f"attendee-{index}@example.com",))
        for index in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [email for outcome, email in outcomes if outcome == "bound"]
    assert len(winners) == 1
    assert len(outcomes) == 12
    assert topology.AttendeeBinding().bind(winners[0]) == winners[0]


def test_local_mode_does_not_bind_attendee(monkeypatch):
    monkeypatch.delenv("OMNIGENT_APP_URL", raising=False)
    binding = topology.AttendeeBinding()

    assert binding.bind("alice@example.com") is None
    assert binding.bind("bob@example.com") is None
    assert binding.status() == {"enforced": False, "status": "disabled"}
