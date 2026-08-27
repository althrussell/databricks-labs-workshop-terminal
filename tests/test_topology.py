"""Topology guard (gap P1-11a)."""

import pytest

from server import topology


def test_single_session_validator_rejects_any_override(monkeypatch):
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "3")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "3")
    with pytest.raises(ValueError, match="must.*MAX_SESSIONS_PER_USER=1|requires MAX_SESSIONS_PER_USER=1"):
        topology.validate_single_session()


def test_single_session_validator_accepts_exactly_one(monkeypatch):
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "1")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "1")
    topology.validate_single_session()


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
