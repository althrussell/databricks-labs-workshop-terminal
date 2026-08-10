"""Credential freshness: renew ahead of expiry, and never run behind a dead one.

The incident this covers: a tab-bound OBO token quietly expires, the Omnigent
host keeps reporting ``running``, and every session the attendee starts dies
with an auth error nobody can see. The behaviour asserted here is the whole
fix — notice early, say so, and stand the host down into a state that names the
problem and recovers by itself when a fresh token arrives.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from .test_obo import make_jwt
from .test_omnigent_remote import REMOTE_URL, _FakeProcess, _wait_for


@pytest.fixture
def emitted(monkeypatch):
    from server import event_emitter as emitter_module

    records: list[tuple[str, str, dict]] = []

    def capture(event_type, attendee, payload=None, **_kwargs):
        records.append((event_type, attendee, payload or {}))

    monkeypatch.setattr(emitter_module.event_emitter, "emit", capture)
    return records


def _health_states(records):
    return [payload.get("state") for kind, _who, payload in records if kind == "obo.health"]


def _write_mirror(user, *, expires_at: float) -> Path:
    path = Path(user.home) / ".omnigent" / "auth_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                REMOTE_URL: {
                    "token": "secret",
                    "user_id": user.email,
                    "expires_at": expires_at,
                }
            }
        )
    )
    return path


def test_the_mirror_carries_the_token_s_real_expiry(client, monkeypatch, tmp_path):
    """An assumed hour would leave the host trusting a credential already dead."""
    from server import config, obo
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    monkeypatch.setattr("server.users.user_manager", users)
    monkeypatch.setattr(obo, "_notify_remote_host", lambda email: None)

    exp = time.time() + 412
    obo.OboManager().capture("alice@example.com", make_jwt(exp))

    mirror = json.loads(
        (
            Path(users.get("alice@example.com").home)
            / ".omnigent"
            / "auth_tokens.json"
        ).read_text()
    )
    assert mirror[REMOTE_URL]["expires_at"] == pytest.approx(exp, abs=1)


def test_a_credential_going_stale_is_reported_once_not_per_sample(
    client, monkeypatch, tmp_path, emitted
):
    from server import config, obo
    from server.users import UserManager

    monkeypatch.setenv("ENABLE_OBO", "true")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    users.get("alice@example.com")
    monkeypatch.setattr("server.users.user_manager", users)
    manager = obo.OboManager()
    watcher = obo.OboFreshnessWatcher(manager, interval=0.01, publish=lambda _e: None)

    manager.capture("alice@example.com", make_jwt(time.time() + 3600))
    assert _health_states(emitted) == ["fresh"]

    # Same record, now past its expiry: repeated sampling must not repeat itself.
    with manager._lock:
        manager._by_email["alice@example.com"].last_capture_exp = time.time() - 5
    watcher.sample()
    watcher.sample()
    watcher.sample()

    assert _health_states(emitted) == ["fresh", "stale"]


def test_an_expiring_credential_nudges_the_tab_before_the_attendee_notices(
    client, monkeypatch, tmp_path
):
    """Renewal is pulled from a live tab, so asking early is the only lever."""
    from server import config, obo
    from server.users import UserManager

    monkeypatch.setenv("ENABLE_OBO", "true")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    users.get("alice@example.com")
    monkeypatch.setattr("server.users.user_manager", users)
    manager = obo.OboManager()
    published: list[dict] = []
    watcher = obo.OboFreshnessWatcher(
        manager, interval=0.0, renew_lead=600, publish=published.append
    )

    manager.capture("alice@example.com", make_jwt(time.time() + 3600))
    watcher.sample()
    assert published == []

    with manager._lock:
        manager._by_email["alice@example.com"].last_capture_exp = time.time() + 120
    watcher.sample()

    assert published == [{"t": "obo_refresh"}]


def test_a_host_behind_a_dead_mirror_stands_down_and_comes_back(
    monkeypatch, tmp_path, emitted
):
    """``running`` behind an expired token is the worst available state.

    It advertises itself as ready and fails every session with an error the
    attendee cannot act on. ``waiting_for_token`` is honest, visible in
    diagnostics, and wakes on the next capture.
    """
    import signal

    from server import config
    from server.omnigent_remote import RemoteHostManager
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    user = users.get("alice@example.com")
    mirror = _write_mirror(user, expires_at=time.time() + 3600)

    processes: list[_FakeProcess] = []

    def popen(*_args, **_kwargs):
        process = _FakeProcess()
        processes.append(process)
        return process

    def killpg(pid, sig):
        for process in processes:
            if process.pid == pid:
                process.returncode = -sig

    monkeypatch.setattr("server.omnigent_remote.os.killpg", killpg)
    hosts = RemoteHostManager(
        user_manager=users,
        popen_factory=popen,
        binary_resolver=lambda: "/opt/bin/omnigent",
        poll_interval=0.01,
        shutdown_timeout=0.1,
        stale_grace=0.0,
    )
    hosts.start()
    try:
        hosts.notify(user.email)
        _wait_for(lambda: hosts.status(user.email)["status"] == "running")

        _write_mirror(user, expires_at=time.time() - 5)
        _wait_for(
            lambda: hosts.status(user.email)["status"] == "waiting_for_token", 2.0
        )
        assert processes[0].returncode in (-signal.SIGTERM, -signal.SIGKILL)

        _write_mirror(user, expires_at=time.time() + 3600)
        hosts.notify(user.email)
        _wait_for(lambda: len(processes) == 2, 2.0)
        _wait_for(lambda: hosts.status(user.email)["status"] == "running", 2.0)
    finally:
        hosts.stop()

    assert mirror.exists()
    health = [
        payload for kind, _who, payload in emitted if kind == "omnigent.host_health"
    ]
    states = [payload["status"] for payload in health]
    assert "running" in states and "waiting_for_token" in states
    # Transitions only — a supervisor loop re-asserting a state every poll would
    # bury the one line an operator is looking for.
    assert all(a != b for a, b in zip(states, states[1:]))
    # A credential problem is not a crash: it must not spend the restart budget
    # that exists for a host which genuinely keeps dying.
    stood_down = next(p for p in health if p["status"] == "waiting_for_token")
    assert stood_down["attempts"] == 0
