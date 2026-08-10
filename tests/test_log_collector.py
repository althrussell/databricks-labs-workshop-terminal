"""The sweep that means an operator never needs the attendee's browser.

Every test here is a fact about the incident: the traceback existed on disk, the
attendee-visible code existed in the message, and nobody could see either.
"""

from pathlib import Path

import pytest

from server.diagnostics import Journal
from server.log_collector import LogCollector

from .synthetic_secrets import JWT


class _Attendee:
    def __init__(self, email: str, home: Path) -> None:
        self.email = email
        self.home = str(home)


class _Roster:
    def __init__(self, *users):
        self._users = list(users)

    def all(self):
        return list(self._users)


class _Emitter:
    def __init__(self):
        self.events = []

    def emit(self, event_type, attendee, payload=None, **_kwargs):
        self.events.append((event_type, attendee, payload or {}))


@pytest.fixture
def attendee(tmp_path):
    home = tmp_path / "alice"
    (home / ".omnigent" / "logs" / "runner").mkdir(parents=True)
    (home / ".omnigent" / "logs" / "host").mkdir(parents=True)
    return _Attendee("alice@example.com", home)


def _collector(attendee, tmp_path, emitter=None):
    return LogCollector(
        journal=Journal(tmp_path / "diagnostics.json", capacity=50),
        users=_Roster(attendee),
        emitter=emitter or _Emitter(),
        interval=0.01,
    )


def _log(attendee, destination: str, name: str = "runner-1.log") -> Path:
    return Path(attendee.home) / ".omnigent" / "logs" / destination / name


_SPEC_FAILURE = """INFO  11-04 09:12:01.004 runner.app                       start_session      | starting session sess-9f2b1a
ERROR 11-04 09:12:03.221 runner.app                       _resolve_harness   | harness resolution failed: spec_resolver_failed for session_id=sess-9f2b1a
Traceback (most recent call last):
  File "/opt/omnigent/runner/app.py", line 8578, in _resolve_harness_config
    raise SpecResolverError(detail)
SpecResolverError: 401 Unauthorized
"""


def test_a_traceback_behind_an_attendee_visible_code_reaches_the_journal(
    attendee, tmp_path
):
    collector = _collector(attendee, tmp_path)
    _log(attendee, "runner").write_text(_SPEC_FAILURE)

    assert collector.sweep() == 1

    entry = collector.journal.recent()[0]
    assert entry["code"] == "spec_resolver_failed"
    assert entry["attendee"] == "alice@example.com"
    assert entry["source"] == "runner"
    assert entry["session"] == "sess-9f2b1a"
    # The reason the attendee's screenshot could not answer: the exception.
    assert "SpecResolverError: 401 Unauthorized" in entry["detail"]
    assert "app.py" in entry["detail"]


def test_the_healthy_lines_around_a_failure_are_not_journalled(attendee, tmp_path):
    collector = _collector(attendee, tmp_path)
    _log(attendee, "runner").write_text(_SPEC_FAILURE)
    collector.sweep()

    codes = [entry["code"] for entry in collector.journal.recent()]
    assert codes == ["spec_resolver_failed"]


def test_a_crash_loop_costs_one_entry_and_a_counter(attendee, tmp_path):
    collector = _collector(attendee, tmp_path)
    path = _log(attendee, "runner")
    path.write_text(_SPEC_FAILURE)
    collector.sweep()
    for _ in range(9):
        with open(path, "a") as handle:
            handle.write(_SPEC_FAILURE)
        collector.sweep()

    entries = collector.journal.recent()
    assert len(entries) == 1
    assert entries[0]["count"] == 10


def test_two_different_failures_stay_apart(attendee, tmp_path):
    collector = _collector(attendee, tmp_path)
    _log(attendee, "runner").write_text(
        _SPEC_FAILURE
        + "ERROR 11-04 09:14:00.001 host.daemon                      connect            | "
        "native_terminal_start_failed for session_id=sess-9f2b1a\n"
    )
    collector.sweep()

    codes = sorted(entry["code"] for entry in collector.journal.recent())
    assert codes == ["native_terminal_start_failed", "spec_resolver_failed"]


def test_only_new_lines_are_read_on_the_next_sweep(attendee, tmp_path):
    collector = _collector(attendee, tmp_path)
    path = _log(attendee, "runner")
    path.write_text(_SPEC_FAILURE)
    assert collector.sweep() == 1
    assert collector.sweep() == 0

    with open(path, "a") as handle:
        handle.write(
            "ERROR 11-04 09:20:00.001 runner.app  turn  | model_call_failed upstream 503\n"
        )
    assert collector.sweep() == 1


def test_a_rotated_log_is_read_from_the_top_again(attendee, tmp_path):
    collector = _collector(attendee, tmp_path)
    path = _log(attendee, "runner")
    path.write_text(_SPEC_FAILURE)
    collector.sweep()

    path.unlink()
    path.write_text(
        "ERROR 11-04 09:30:00.001 runner.app  turn  | token_expired refreshing failed\n"
    )
    collector.sweep()

    codes = sorted(entry["code"] for entry in collector.journal.recent())
    assert codes == ["spec_resolver_failed", "token_expired"]


def test_a_token_in_a_traceback_is_masked_before_it_is_stored(attendee, tmp_path):
    token = JWT
    collector = _collector(attendee, tmp_path)
    _log(attendee, "host", "host-stdio.log").write_text(
        "ERROR 11-04 09:12:03.221 host.auth  mirror  | auth_token_expired "
        f"token={token}\n"
    )
    collector.sweep()

    entry = collector.journal.recent()[0]
    assert token.split(".")[0] not in entry["message"]
    assert "<jwt>" in entry["message"]


def test_a_bare_stderr_traceback_is_captured_even_without_a_log_header(
    attendee, tmp_path
):
    """A host that dies before logging is configured still writes to stderr."""
    collector = _collector(attendee, tmp_path)
    _log(attendee, "host", "host-stdio.log").write_text(
        'Traceback (most recent call last):\n'
        '  File "/opt/omnigent/host/_daemon_entry.py", line 48, in main\n'
        "    configure_process_logging('host', force=True)\n"
        "PermissionError: [Errno 13] Permission denied\n"
    )
    collector.sweep()

    entry = collector.journal.recent()[0]
    assert entry["code"] == "PermissionError"
    assert entry["source"] == "host"


def test_an_unknown_future_code_is_captured_without_a_code_change(attendee, tmp_path):
    collector = _collector(attendee, tmp_path)
    _log(attendee, "runner").write_text(
        "ERROR 11-04 10:00:00.001 runner.newthing  boot  | quantum_harness_unavailable\n"
    )
    collector.sweep()

    assert collector.journal.recent()[0]["code"] == "quantum_harness_unavailable"


def test_the_first_occurrence_is_announced_to_control_tower_and_repeats_are_not(
    attendee, tmp_path
):
    emitter = _Emitter()
    collector = _collector(attendee, tmp_path, emitter=emitter)
    path = _log(attendee, "runner")
    path.write_text(_SPEC_FAILURE)
    collector.sweep()
    with open(path, "a") as handle:
        handle.write(_SPEC_FAILURE)
    collector.sweep()

    assert [event[0] for event in emitter.events] == ["omnigent.error_detected"]
    _, who, payload = emitter.events[0]
    assert who == "alice@example.com"
    assert payload["code"] == "spec_resolver_failed"


def test_a_half_written_line_is_not_read_until_it_is_finished(attendee, tmp_path):
    collector = _collector(attendee, tmp_path)
    path = _log(attendee, "runner")
    path.write_text("ERROR 11-04 10:00:00.001 runner.app  boot  | partial_line_fai")
    assert collector.sweep() == 0

    with open(path, "a") as handle:
        handle.write("led here\n")
    assert collector.sweep() == 1
    assert collector.journal.recent()[0]["code"] == "partial_line_failed"


def test_a_traceback_split_by_a_sweep_boundary_stays_one_entry(attendee, tmp_path):
    collector = _collector(attendee, tmp_path)
    path = _log(attendee, "runner")
    head, _, tail = _SPEC_FAILURE.partition('    raise SpecResolverError(detail)\n')
    path.write_text(head + '    raise SpecResolverError(detail)\n')
    assert collector.sweep() == 0

    with open(path, "a") as handle:
        handle.write(tail)
    assert collector.sweep() == 1

    entries = collector.journal.recent()
    assert len(entries) == 1
    assert "SpecResolverError: 401 Unauthorized" in entries[0]["detail"]


def test_the_journal_survives_a_restart_of_the_app(attendee, tmp_path):
    collector = _collector(attendee, tmp_path)
    _log(attendee, "runner").write_text(_SPEC_FAILURE)
    collector.sweep()

    reopened = Journal(tmp_path / "diagnostics.json", capacity=50)
    assert [entry["code"] for entry in reopened.recent()] == ["spec_resolver_failed"]


def test_a_sweep_survives_an_attendee_whose_home_does_not_exist_yet(tmp_path):
    missing = _Attendee("bob@example.com", tmp_path / "bob")
    collector = LogCollector(
        journal=Journal(None), users=_Roster(missing), emitter=_Emitter()
    )
    assert collector.sweep() == 0
