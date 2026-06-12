"""Topology guard (gap P1-11a)."""

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
