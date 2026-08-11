"""An operator can see any error an attendee can see, without their browser.

These endpoints are the operator half of the privacy contract: process logs and
classified error codes leave the box, PTY scrollback never does.
"""

from pathlib import Path

import pytest

from server.diagnostics import Journal

from .synthetic_secrets import DAPI_TOKEN


@pytest.fixture
def attendee_with_a_failure(client, tmp_path, monkeypatch):
    """One bound attendee whose Omnigent runner has already fallen over."""
    from server.log_collector import log_collector
    from server.users import user_manager

    user = user_manager.get("alice@example.com")
    monkeypatch.setattr(user, "home", str(tmp_path / "alice"))
    logs = Path(user.home) / ".omnigent" / "logs" / "runner"
    logs.mkdir(parents=True)
    (logs / "runner-1.log").write_text(
        "ERROR 11-04 09:12:03.221 runner.app  _resolve  | "
        "harness resolution failed: spec_resolver_failed session_id=sess-9f2b1a\n"
        "Traceback (most recent call last):\n"
        '  File "/opt/omnigent/runner/app.py", line 8578, in _resolve_harness_config\n'
        "    raise SpecResolverError(detail)\n"
        f"SpecResolverError: 401 Unauthorized token={DAPI_TOKEN}\n"
    )
    monkeypatch.setattr(log_collector, "journal", Journal(None, capacity=20))
    monkeypatch.setattr(log_collector, "_cursors", {})
    monkeypatch.setattr(log_collector, "_users", None)
    return user


def test_the_traceback_behind_an_attendee_error_is_readable_by_an_operator(
    client, as_admin, attendee_with_a_failure
):
    client.post("/api/admin/diagnostics/sweep")

    payload = client.get("/api/admin/diagnostics").json()

    codes = [entry["code"] for entry in payload["errors"]]
    assert "spec_resolver_failed" in codes
    entry = next(e for e in payload["errors"] if e["code"] == "spec_resolver_failed")
    assert "SpecResolverError: 401 Unauthorized" in entry["detail"]
    assert entry["attendee"] == "alice@example.com"


def test_a_credential_in_a_captured_log_never_reaches_the_operator(
    client, as_admin, attendee_with_a_failure
):
    client.post("/api/admin/diagnostics/sweep")

    body = client.get("/api/admin/diagnostics").text
    assert DAPI_TOKEN not in body
    assert "<pat>" in body


def test_diagnostics_reports_readiness_and_collector_state(
    client, as_admin, attendee_with_a_failure
):
    payload = client.get("/api/admin/diagnostics").json()

    assert "ready" in payload["readyz"]
    assert payload["collector"]["sweeps"] >= 0
    assert isinstance(payload["identity"], list)


def test_the_log_tail_endpoint_returns_redacted_process_logs(
    client, as_admin, attendee_with_a_failure
):
    payload = client.get("/api/admin/diagnostics/logs?source=runner").json()

    assert payload["logs"], "the attendee's runner log should be listed"
    log = payload["logs"][0]
    assert log["attendee"] == "alice@example.com"
    assert log["source"] == "runner"
    assert "spec_resolver_failed" in log["tail"]
    assert DAPI_TOKEN not in log["tail"]


def test_the_log_tail_endpoint_can_be_scoped_to_one_attendee(
    client, as_admin, attendee_with_a_failure
):
    empty = client.get("/api/admin/diagnostics/logs?attendee=nobody@example.com").json()
    assert empty["logs"] == []


def test_one_broken_section_costs_its_own_contents_and_nothing_else(
    client, as_admin, attendee_with_a_failure, monkeypatch
):
    from server import readiness

    def explode():
        raise RuntimeError("readiness is having a day")

    monkeypatch.setattr(readiness, "evaluate_runtime", explode)
    client.post("/api/admin/diagnostics/sweep")

    response = client.get("/api/admin/diagnostics")

    # This panel is the operator's only window once something is already wrong.
    # A live instance once returned 500 here and so hid the traceback that
    # caused it — the one thing the operator had come to read.
    assert response.status_code == 200
    payload = response.json()
    assert payload["readyz"] == {"error": "RuntimeError: readiness is having a day"}
    assert [e["code"] for e in payload["errors"]], "the journal must still be readable"


def test_an_app_side_traceback_reaches_the_operator_panel(
    client, as_admin, attendee_with_a_failure
):
    import logging

    from server.log_collector import install_app_error_journal

    install_app_error_journal()
    try:
        raise ValueError(f"bootstrap fell over token={DAPI_TOKEN}")
    except ValueError:
        logging.getLogger("server.users").exception("bootstrap_home failed")

    payload = client.get("/api/admin/diagnostics").json()

    entry = next(e for e in payload["errors"] if e["source"] == "app")
    # The failure that started this work was in this app, not in Omnigent, and
    # was visible only on the platform log page.
    assert entry["code"] == "ValueError"
    assert "bootstrap_home failed" in entry["message"]
    assert "ValueError: bootstrap fell over" in entry["detail"]
    assert DAPI_TOKEN not in entry["detail"]


def test_the_app_error_sink_cannot_loop_or_raise(client, as_admin):
    import logging

    from server.diagnostics import Journal
    from server.log_collector import AppErrorJournal

    class Hostile(Journal):
        def __init__(self):
            super().__init__(None, capacity=5)
            self.calls = 0

        def record(self, key, entry):
            self.calls += 1
            logging.getLogger("hostile").error("failing while recording a failure")
            raise OSError("journal volume is full")

    journal = Hostile()
    handler = AppErrorJournal(journal)
    logger = logging.getLogger("test.hostile")
    logger.addHandler(handler)
    try:
        logger.error("something went wrong")
    finally:
        logger.removeHandler(handler)

    # One record in, one attempt out: a sink fed by logging that logs on failure
    # is an outage, and a diagnostics sink that raises breaks the caller it was
    # meant to observe.
    assert journal.calls == 1


def test_diagnostics_is_refused_to_a_non_admin(client, as_non_admin):
    for path in (
        "/api/admin/diagnostics",
        "/api/admin/diagnostics/logs",
    ):
        assert client.get(path).status_code == 403
    assert client.post("/api/admin/diagnostics/sweep").status_code == 403
