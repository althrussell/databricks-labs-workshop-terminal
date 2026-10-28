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


def test_a_setting_named_like_a_code_is_not_an_error(attendee, tmp_path):
    # Every EQL-Pre-Flight seat reported this INFO startup line to CT as
    # omnigent.error_detected; a real code in the same shape still counts.
    emitter = _Emitter()
    collector = _collector(attendee, tmp_path, emitter)
    _log(attendee, "runner").write_text(
        "INFO  11-04 09:00:00.001 terminals.pane_reaper  start  | "
        "native pane reaper started (idle_timeout=3600.0s, interval=60.0s)\n"
        "WARNING 11-04 09:05:00.001 runner.app  turn  | retrying after error=token_expired\n"
    )
    collector.sweep()

    assert [entry["code"] for entry in collector.journal.recent()] == ["token_expired"]
    assert [payload["code"] for _type, _attendee, payload in emitter.events] == ["token_expired"]


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


def test_version_guard_matches_omnigent_upstream_lock():
    import json
    from server.log_collector import OMNIGENT_LIFECYCLE_LOCKED_VERSION

    lock = json.loads(
        (Path(__file__).resolve().parents[1] / "deploy" / "omnigent-app" / "upstream-lock.json").read_text()
    )
    assert lock["package"] == f"omnigent[databricks]=={OMNIGENT_LIFECYCLE_LOCKED_VERSION}"


def test_info_forwarder_drops_debug_and_noisy_202_and_caps_rate(attendee, tmp_path):
    emitter = _Emitter()
    collector = LogCollector(
        journal=Journal(tmp_path / "diagnostics.json", capacity=50),
        users=_Roster(attendee),
        emitter=emitter,
        interval=0.01,
        rate_limit_per_sec=3,
    )
    (Path(attendee.home) / ".omnigent" / "logs" / "cli").mkdir(parents=True, exist_ok=True)
    _log(attendee, "cli", "cli-1.log").write_text(
        'INFO  10-27 02:43:01.020 cli_diagnostics  setup_cli_logging | CLI start — argv=["polly", "-p", "secret prompt"] pid=1036\n'
    )
    _log(attendee, "runner", "runner-8c0251b5-20261027.log").write_text(
        "DEBUG 10-27 02:44:48.438 asyncio  __init__ | Using selector: EpollSelector\n"
        'INFO  10-27 02:44:49.178 httpx  _single_request | HTTP Request: POST https://omni.example/v1/sessions/8c0251b5/events "HTTP/1.1 202 Accepted"\n'
        'INFO  10-27 02:44:49.200 httpx  _single_request | HTTP Request: GET https://omni.example/health "HTTP/1.1 200 OK"\n'
        "INFO  10-27 02:44:49.300 runner.app  step1 | line one\n"
        "INFO  10-27 02:44:49.400 runner.app  step2 | line two\n"
        "INFO  10-27 02:44:49.500 runner.app  step3 | line three\n"
        "INFO  10-27 02:44:49.600 runner.app  step4 | line four\n"
    )
    collector.sweep()
    messages = [item["message"] for item in collector.forwarded_lines]
    assert not any("EpollSelector" in m for m in messages)
    assert not any("202 Accepted" in m for m in messages)
    assert not any("secret prompt" in m for m in messages)
    assert any("[REDACTED_ARGV]" in m for m in messages)
    assert any("dropped" in m for m in messages)


def test_seat22_lifecycle_fixtures_detect_ready_first_message_missing_and_early_exit(
    attendee, tmp_path
):
    import time as time_mod

    emitter = _Emitter()
    collector = LogCollector(
        journal=Journal(tmp_path / "diagnostics.json", capacity=50),
        users=_Roster(attendee),
        emitter=emitter,
        interval=0.01,
        first_message_timeout_s=200.0,
    )
    # Seat 22 fixture 1: cfc71bc2 (interactive in ~3s, first message in ~1s)
    _log(attendee, "runner", "runner-cfc71bc293574bb0b0a94457bcecb06a-20261027.log").write_text(
        "INFO  10-27 03:01:40.000 runner.app  _claude_terminal | Claude terminal tmux launch requested: session=cfc71bc293574bb0b0a94457bcecb06a command=claude\n"
        "INFO  10-27 03:01:42.862 runner.app  _nal_interactive | Native terminal became interactive: session=cfc71bc293574bb0b0a94457bcecb06a harness=claude-native terminal_id=terminal_claude_main\n"
        "INFO  10-27 03:01:43.861 runner.app  _session_events | post_session_events: conv=cfc71bc293574bb0b0a94457bcecb06a type=message active=False buffer_len=0 content_types=['input_text'] model_override=None\n"
    )
    # Seat 22 fixture 2: 8c0251b5 (interactive in ~3s, never receives first message)
    _log(attendee, "runner", "runner-8c0251b52eaf462b83fc948244cbab30-20261027.log").write_text(
        "INFO  10-27 02:44:49.452 runner.app  _claude_terminal | Claude terminal tmux launch requested: session=8c0251b52eaf462b83fc948244cbab30 command=claude\n"
        "INFO  10-27 02:44:52.100 runner.app  _nal_interactive | Native terminal became interactive: session=8c0251b52eaf462b83fc948244cbab30 harness=claude-native terminal_id=terminal_claude_main\n"
    )
    # Seat 4 fixture 3: 49270f8f (launched with -p, stopped after 28s)
    _log(attendee, "runner", "runner-49270f8f738d4fcd8122a46dce5ad264-20261027.log").write_text(
        "INFO  10-27 02:43:03.820 runner.app  _spawn_entry | harness started for conversation 49270f8f738d4fcd8122a46dce5ad264 (harness=claude-sdk)\n"
        "INFO  10-27 02:43:32.297 runner.app  _session_events | post_session_events: conv=49270f8f738d4fcd8122a46dce5ad264 type=stop_session active=False buffer_len=0 content_types=[] model_override=None\n"
    )

    collector.sweep()
    collector.sweep(now_mono=time_mod.monotonic() + 250.0)

    by_type = {}
    for event_type, _who, payload in emitter.events:
        by_type.setdefault(event_type, []).append(payload)

    assert len(by_type.get("omnigent.session_ready", [])) == 2
    assert len(by_type.get("omnigent.first_message", [])) == 1
    assert by_type["omnigent.first_message"][0]["session_id"] == "cfc71bc293574bb0b0a94457bcecb06a"
    assert by_type["omnigent.first_message"][0]["ready_to_first_message_ms"] == 999

    assert len(by_type.get("omnigent.first_message_missing", [])) == 1
    assert by_type["omnigent.first_message_missing"][0]["session_id"] == "8c0251b52eaf462b83fc948244cbab30"

    assert len(by_type.get("omnigent.terminal_exited_early", [])) == 1
    assert by_type["omnigent.terminal_exited_early"][0]["session_id"] == "49270f8f738d4fcd8122a46dce5ad264"



def test_a_terminal_repl_at_an_idle_prompt_is_ready_not_stalled(attendee, tmp_path):
    # 28 Oct E2E, seat 1: the terminal Omnigent REPL came up in 0.3 s and sat at
    # `polly · ready` with no greeting (by design since WT #132), yet the
    # detector reported omnigent.session_start_stalled at 120 s, which CT filed
    # as blocked friction on a healthy seat.
    import time as time_mod

    emitter = _Emitter()
    collector = LogCollector(
        journal=Journal(tmp_path / "diagnostics.json", capacity=50),
        users=_Roster(attendee),
        emitter=emitter,
        interval=0.01,
        first_message_timeout_s=200.0,
        start_stall_timeout_s=120.0,
    )
    _log(attendee, "runner", "runner-ac9e54d36ca3452abc226f88352bf79a-20261028.log").write_text(
        "INFO  10-28 05:27:13.852 runner.app  _spawn_entry | harness started for conversation ac9e54d36ca3452abc226f88352bf79a (harness=claude-sdk)\n"
        "INFO  10-28 05:27:13.854 runner.app  _auto_create_repl | Auto-created omnigent REPL terminal for session ac9e54d36ca3452abc226f88352bf79a: terminal_id=terminal_tui_main server_url=https://omni.example elapsed_ms=305\n"
    )
    # A launch that never reaches its REPL is still a stall.
    _log(attendee, "runner", "runner-5f1c0d2e9a8b4c7d6e5f4a3b2c1d0e9f-20261028.log").write_text(
        "INFO  10-28 05:40:01.000 runner.app  _spawn_entry | harness started for conversation 5f1c0d2e9a8b4c7d6e5f4a3b2c1d0e9f (harness=claude-sdk)\n"
    )

    collector.sweep()
    collector.sweep(now_mono=time_mod.monotonic() + 250.0)

    by_type = {}
    for event_type, _who, payload in emitter.events:
        by_type.setdefault(event_type, []).append(payload["session_id"])

    assert by_type.get("omnigent.session_ready") == ["ac9e54d36ca3452abc226f88352bf79a"]
    assert by_type.get("omnigent.session_start_stalled") == ["5f1c0d2e9a8b4c7d6e5f4a3b2c1d0e9f"]
    assert "omnigent.first_message_missing" not in by_type
