"""Insight-capture config gating.

The default matters more than usual here: capture off is how an event whose terms
don't cover it stays compliant. An operator gets that by doing nothing, so the
default is the compliance control and is tested as one.
"""

from server import config


def test_capture_is_off_by_default(monkeypatch):
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    assert config.insight_capture_enabled() is False


def test_blank_value_does_not_enable_capture(monkeypatch):
    """An override CT sets to "" must not read as truthy."""
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "   ")
    assert config.insight_capture_enabled() is False


def test_capture_opt_in(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    assert config.insight_capture_enabled() is True


def test_discovery_is_off_while_capture_is_off(monkeypatch):
    """Discovery must not be reachable via its own flag alone.

    DISCOVERY_ENABLED defaults true, so a discovery gate that only read its own
    env var would turn the conversational tier on for every deployment that never
    asked for capture at all.
    """
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    monkeypatch.setenv("DISCOVERY_ENABLED", "true")
    assert config.discovery_enabled() is False


def test_discovery_on_by_default_within_capture(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "1")
    monkeypatch.delenv("DISCOVERY_ENABLED", raising=False)
    assert config.discovery_enabled() is True


def test_discovery_can_be_dropped_while_keeping_the_signal(monkeypatch):
    """The two tiers carry different consent weight and must be separable."""
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "1")
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    assert config.insight_capture_enabled() is True
    assert config.discovery_enabled() is False


def test_flags_are_read_at_call_time(monkeypatch):
    """CT edits app.yaml env in place, so a cached value would go stale."""
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    assert config.insight_capture_enabled() is False
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "on")
    assert config.insight_capture_enabled() is True
