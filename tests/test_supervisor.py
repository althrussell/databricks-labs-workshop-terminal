"""The terminal survives its own agents (25 Oct 2026, test1025 seat 2).

An agent freed port 8000 for a test server by killing the Workshop Terminal.
These tests run the real supervisor around a stand-in server process; the
signal tests need Linux (``sigtimedwait``), so CI covers them.
"""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request

import pytest

from server import otel_bootstrap, supervisor

ROOT = Path(__file__).resolve().parents[1]
linux_only = pytest.mark.skipif(
    not supervisor.enabled({}), reason="the kill guard needs Linux sigtimedwait"
)

# Stands in for uvicorn: records how it was started, relays supervisor events
# like server.main does, and drains on SIGTERM.
SERVER = textwrap.dedent('''
    import json, os, signal, subprocess, sys, time
    from pathlib import Path

    from server import supervisor

    state = Path(sys.argv[1])
    pid = os.getpid()
    blocked = next(line.split()[1] for line in open("/proc/self/status")
                   if line.startswith("SigBlk:"))
    def record(name, payload):
        with open(state / "events.jsonl", "a") as events:
            events.write(json.dumps({"name": name, **payload}) + "\\n")
    supervisor.relay_events(record)
    def drain(*_):
        (state / f"drained-{pid}").write_text("1")
        sys.exit(0)
    signal.signal(signal.SIGTERM, drain)
    (state / f"server-{pid}").write_text(json.dumps({
        "ppid": os.getppid(), "blocked": blocked,
        "supervisor": os.environ.get(supervisor.SUPERVISOR_PID_ENV),
    }))
    mode = os.environ.get("SERVER_MODE", "")
    if mode == "crash":
        sys.exit(3)
    if mode == "kill-supervisor":
        os.kill(os.getppid(), signal.SIGTERM)
    if mode == "orphan-kills-supervisor":
        # The shell exits at once, so the subshell is orphaned before it signals.
        subprocess.Popen(["sh", "-c", f"(sleep 0.3; kill -TERM {os.getppid()}; sleep 2) & exit 0"])
    while True:
        time.sleep(0.05)
''')

HARNESS = textwrap.dedent('''
    import sys
    from server import supervisor

    assert supervisor.harden()
    sys.exit(supervisor.supervise(
        [sys.executable, sys.argv[1], sys.argv[2]],
        delays=(0.2, 0.4), max_restarts=3, window=60.0, healthy_after=60.0, grace=5.0,
    ))
''')


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


class Terminal:
    def __init__(self, tmp_path, mode=""):
        self.state = tmp_path / "state"
        self.state.mkdir()
        server = tmp_path / "fake_uvicorn.py"
        server.write_text(SERVER)
        self.process = subprocess.Popen(
            [sys.executable, "-c", HARNESS, str(server), str(self.state)],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT), "SERVER_MODE": mode},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )

    def servers(self):
        return sorted(
            (path for path in self.state.glob("server-*")), key=lambda p: p.stat().st_mtime_ns
        )

    def server_pid(self, index=0):
        return int(_wait(lambda: len(self.servers()) > index and self.servers())[index].name[7:])

    def events(self):
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def finish(self, timeout=8.0):
        _, stderr = self.process.communicate(timeout=timeout)
        return self.process.returncode, stderr

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.communicate()
        for path in self.servers():
            try:
                os.kill(int(path.name[7:]), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture
def terminal(tmp_path):
    started = []

    def start(mode=""):
        started.append(Terminal(tmp_path, mode))
        return started[-1]

    yield start
    for item in started:
        item.close()


def _stat(proc, pid, ppid, comm="bash"):
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1\n")


def test_a_sender_inside_the_app_tree_is_refused(tmp_path):
    _stat(tmp_path, 300, 200, comm="weird) (name")
    _stat(tmp_path, 200, 100)
    _stat(tmp_path, 50, 1)
    _stat(tmp_path, 60, 50)

    assert supervisor.classify_sender(300, 100, proc=tmp_path) == "descendant"
    assert supervisor.classify_sender(200, 100, proc=tmp_path) == "descendant"
    assert supervisor.classify_sender(60, 100, proc=tmp_path) == "platform"
    assert supervisor.classify_sender(999, 100, proc=tmp_path) == "vanished"
    for sender in (0, 1, 100):
        assert supervisor.classify_sender(sender, 100, proc=tmp_path) == "platform"


def test_the_supervisor_can_be_switched_off():
    for value in ("0", "false", "off", "no"):
        assert not supervisor.enabled({supervisor.DISABLE_ENV: value})


class Executed(BaseException):
    pass


@pytest.fixture
def launcher(monkeypatch):
    """main() with the environment merge stubbed and exec recorded."""
    executed = []

    def execvp(_file, argv):
        executed.append(argv)
        raise Executed

    monkeypatch.setattr(otel_bootstrap, "prepare_environment", lambda _env: None)
    monkeypatch.setattr(otel_bootstrap.os, "execvp", execvp)
    return executed


def test_launcher_execs_uvicorn_when_not_supervised(monkeypatch, launcher):
    monkeypatch.setattr(supervisor, "enabled", lambda _env: False)
    with pytest.raises(Executed):
        otel_bootstrap.main()

    assert launcher == [otel_bootstrap.uvicorn_command(os.environ)]


def test_launcher_execs_uvicorn_when_it_cannot_hide_the_secret(monkeypatch, launcher):
    monkeypatch.setattr(supervisor, "enabled", lambda _env: True)
    monkeypatch.setattr(supervisor, "harden", lambda: False)
    monkeypatch.setattr(supervisor, "supervise", lambda _command: pytest.fail("supervised"))
    with pytest.raises(Executed):
        otel_bootstrap.main()

    assert launcher == [otel_bootstrap.uvicorn_command(os.environ)]


def test_launcher_supervises_the_same_uvicorn_command(monkeypatch, launcher):
    seen = []
    monkeypatch.setattr(supervisor, "enabled", lambda _env: True)
    monkeypatch.setattr(supervisor, "harden", lambda: True)
    monkeypatch.setattr(supervisor, "supervise", lambda command: seen.append(command) or 7)
    with pytest.raises(SystemExit) as exited:
        otel_bootstrap.main()

    assert exited.value.code == 7
    assert not launcher
    assert seen == [otel_bootstrap.uvicorn_command(os.environ)]
    assert seen[0][seen[0].index("--workers") + 1] == "1"


def test_the_server_relays_only_supervisor_events(monkeypatch):
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(supervisor.SUPERVISOR_PID_ENV, str(os.getppid()))
    monkeypatch.setenv(supervisor.EVENTS_FD_ENV, str(read_fd))
    recorded = []

    thread = supervisor.relay_events(lambda name, payload: recorded.append((name, payload)))
    os.write(write_fd, b'{"event":"server.restarted","exit_code":-9,"signal":"SIGKILL"}\n')
    os.write(write_fd, b'not json\n{"event":"something.else"}\n')
    os.write(write_fd, b'{"event":"server.kill_blocked","code":"descendant"}\n')
    os.close(write_fd)
    thread.join(timeout=5)

    assert recorded == [
        ("server.restarted", {"exit_code": -9, "signal": "SIGKILL"}),
        ("server.kill_blocked", {"code": "descendant"}),
    ]


def test_a_server_not_started_by_the_supervisor_relays_nothing(monkeypatch):
    monkeypatch.setenv(supervisor.SUPERVISOR_PID_ENV, "999999999")
    monkeypatch.setenv(supervisor.EVENTS_FD_ENV, "0")
    assert supervisor.relay_events(lambda *_args: None) is None


@linux_only
def test_a_killed_server_is_restarted_and_says_so(terminal):
    running = terminal()
    first = running.server_pid()
    os.kill(first, signal.SIGKILL)

    second = running.server_pid(1)
    assert second != first
    restarted = _wait(lambda: [e for e in running.events() if e["name"] == "server.restarted"])
    assert restarted[0]["signal"] == "SIGKILL"
    assert restarted[0]["exit_code"] == -9
    assert restarted[0]["attempts"] == 1

    running.process.terminate()
    code, stderr = running.finish()
    assert code == 0
    assert (running.state / f"drained-{second}").exists()
    assert "server.restarted" in stderr


@linux_only
@pytest.mark.parametrize("sent", [signal.SIGTERM, signal.SIGINT])
def test_a_platform_stop_drains_the_server_and_exits(terminal, sent):
    running = terminal()
    pid = running.server_pid()
    running.process.send_signal(sent)

    code, _ = running.finish()
    assert code == 0
    # Forwarded as SIGTERM either way, so Uvicorn always drains.
    assert (running.state / f"drained-{pid}").exists()
    assert len(running.servers()) == 1


@linux_only
def test_a_stop_signal_from_the_server_itself_is_ignored_and_recorded(terminal):
    running = terminal("kill-supervisor")
    pid = running.server_pid()

    blocked = _wait(lambda: [e for e in running.events() if e["name"] == "server.kill_blocked"])
    assert blocked[0]["code"] == "descendant"
    assert blocked[0]["signal"] == "SIGTERM"
    assert blocked[0]["outcome"] == "blocked"
    time.sleep(0.5)
    assert running.process.poll() is None
    assert len(running.servers()) == 1

    running.process.terminate()
    code, stderr = running.finish()
    assert code == 0
    assert (running.state / f"drained-{pid}").exists()
    assert f"server.kill_blocked signal=SIGTERM sender_pid={pid} reason=descendant" in stderr


@linux_only
def test_an_orphaned_attendee_process_still_cannot_stop_it(terminal):
    running = terminal("orphan-kills-supervisor")
    running.server_pid()

    blocked = _wait(lambda: [e for e in running.events() if e["name"] == "server.kill_blocked"])
    # Reparented to the supervisor (a subreaper), not to init.
    assert blocked[0]["code"] == "descendant"
    assert running.process.poll() is None


@linux_only
def test_restarts_are_bounded_then_the_platform_sees_a_crash(terminal):
    running = terminal("crash")

    code, stderr = running.finish(timeout=10)
    assert code == 1
    assert len(running.servers()) == 4  # the first start and three restarts
    assert "server.restart_exhausted" in stderr


@linux_only
def test_the_server_starts_with_a_clean_signal_mask(terminal):
    """Attendee shells inherit Uvicorn's mask; a blocked SIGINT breaks Ctrl-C."""
    running = terminal()
    pid = running.server_pid()
    started = json.loads((running.state / f"server-{pid}").read_text())

    assert int(started["blocked"], 16) == 0
    assert started["ppid"] == running.process.pid
    assert started["supervisor"] == str(running.process.pid)


@linux_only
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every process")
def test_attendee_processes_cannot_read_the_supervisors_secret(terminal):
    running = terminal()
    running.server_pid()

    with pytest.raises(PermissionError):
        Path(f"/proc/{running.process.pid}/environ").read_bytes()


@linux_only
def test_the_server_drains_if_the_supervisor_is_killed(terminal):
    running = terminal()
    pid = running.server_pid()
    running.process.kill()
    running.process.communicate()

    _wait(lambda: (running.state / f"drained-{pid}").exists())


def _healthy(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def _servers_of(parent):
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rpartition(") ")[2].split()
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if fields[0] != "Z" and int(fields[1]) == parent and b"uvicorn" in command:
            found.append(int(entry.name))
    return found


@linux_only
def test_the_real_terminal_comes_back_after_its_server_is_killed(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("OTEL_EXPORTER_OTLP")
    } | {
        "LOCAL_DEV": "1", "DATA_ROOT": str(tmp_path / "data"),
        "DATABRICKS_APP_PORT": str(port), "PYTHONPATH": str(ROOT),
    }
    log = tmp_path / "terminal.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "server.otel_bootstrap"],
            cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
        )
    try:
        first = _wait(lambda: _healthy(port) and _servers_of(process.pid), timeout=60)
        os.kill(first[0], signal.SIGKILL)
        _wait(lambda: _healthy(port) and set(_servers_of(process.pid)) - set(first), timeout=60)

        process.send_signal(signal.SIGTERM)
        process.wait(timeout=15)
        assert process.returncode == 0, log.read_text()
        assert "server.restarted" in log.read_text()
        assert not _servers_of(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
