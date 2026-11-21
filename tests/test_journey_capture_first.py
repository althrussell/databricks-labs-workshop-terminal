"""Wrap and teardown exports capture fresh screenshots before they upload (server/journey.py).

Before v2, the screenshot refresh ran after the export, so the kept shots were one
trigger stale. For wrap and teardown the capture now comes first, within a bounded
budget. Other reasons keep the historical post-export refresh.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from server import journey


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    journey.reset_for_tests()
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "true")
    yield
    journey.reset_for_tests()


@pytest.fixture
def events(monkeypatch):
    """Record the order of captures and uploads, and stop the background refresh thread."""
    order: list[str] = []
    envs: list[dict] = []

    def capture(user, deadline):
        order.append("capture")

    def run(command, **kwargs):
        order.append("upload")
        envs.append(kwargs.get("env", {}))
        return subprocess.CompletedProcess(command, 0, json.dumps({"volume_path": "/Volumes/x/y/z"}), "")

    class _NoThread:
        def __init__(self, *args, **kwargs):
            self._args = kwargs.get("args")

        def start(self):
            order.append("background-capture")

    monkeypatch.setattr(journey, "_capture_projects_safely", capture)
    monkeypatch.setattr(journey.subprocess, "run", run)
    # journey's own reference only: the global threading module is left alone.
    monkeypatch.setattr(journey, "threading", SimpleNamespace(Thread=_NoThread))
    return SimpleNamespace(order=order, envs=envs)


def _user(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return SimpleNamespace(email="labuser001@x.com", home=str(home), shell_env=lambda: {"PATH": "/usr/bin"})


def test_a_wrap_export_captures_before_it_uploads(events, tmp_path):
    journey.export(_user(tmp_path), reason="wrap_up", reuse=False)

    assert events.order[:2] == ["capture", "upload"]


def test_a_teardown_export_captures_before_it_uploads(events, tmp_path):
    journey.export(_user(tmp_path), reason="teardown", reuse=False)

    assert events.order[:2] == ["capture", "upload"]


def test_an_attendee_export_does_not_capture_first(events, tmp_path):
    journey.export(_user(tmp_path), reason="attendee", reuse=False)

    assert events.order[0] == "upload", "only wrap and teardown wait for a fresh capture"
    assert "capture" not in events.order[:1]


def test_with_the_flag_off_a_wrap_export_is_exactly_as_before(events, tmp_path, monkeypatch):
    monkeypatch.delenv("WORKSHOP_INSIGHT_V2", raising=False)

    journey.export(_user(tmp_path), reason="wrap_up", reuse=False)

    assert events.order[0] == "upload", "capture-first is part of v2, so flag-off waits for the upload"


def test_the_capture_budget_is_twenty_seconds():
    assert journey.SCREENSHOT_BUDGET_SECONDS == 20.0


def test_the_v2_switch_reaches_the_export_helper_only_when_enabled(events, tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "true")
    journey.export(_user(tmp_path), reason="wrap_up", reuse=False)
    assert events.envs[-1].get("WORKSHOP_INSIGHT_V2") == "1"

    monkeypatch.delenv("WORKSHOP_INSIGHT_V2", raising=False)
    journey.export(_user(tmp_path), reason="wrap_up", reuse=False)
    assert "WORKSHOP_INSIGHT_V2" not in events.envs[-1]


def test_a_broken_browser_never_blocks_the_wrap_export(tmp_path, monkeypatch):
    """The real capture path, with the browser lookup failing: the export still runs."""
    from server import browser_evidence

    def broken():
        raise RuntimeError("browser crashed")

    uploads: list[str] = []

    def run(command, **kwargs):
        uploads.append("upload")
        return subprocess.CompletedProcess(command, 0, json.dumps({"volume_path": "/Volumes/x"}), "")

    monkeypatch.setattr(browser_evidence, "browser_binary", broken)
    monkeypatch.setattr(journey.subprocess, "run", run)

    result = journey.export(_user(tmp_path), reason="wrap_up", reuse=False)

    assert uploads == ["upload"]
    assert result["path"] == "/Volumes/x"
