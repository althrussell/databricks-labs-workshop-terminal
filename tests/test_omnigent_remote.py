"""Remote Omnigent host, attendee auth mirror, and TUI routing."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from .test_obo import make_jwt


REMOTE_URL = "https://alice-omnigent.example.databricksapps.com"


@pytest.fixture(autouse=True)
def _single_attendee_topology(monkeypatch, tmp_path):
    from server import config, topology

    monkeypatch.delenv("ALLOW_SHARED_TOPOLOGY", raising=False)
    monkeypatch.setenv("MAX_SESSIONS_GLOBAL", "3")
    monkeypatch.setenv("MAX_SESSIONS_PER_USER", "3")
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))


def _wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_remote_url_normalizes_and_local_fallback(monkeypatch):
    from server import config

    monkeypatch.delenv("OMNIGENT_APP_URL", raising=False)
    assert config.omnigent_app_url() == ""
    assert config.omnigent_remote_enabled() is False

    monkeypatch.setenv("OMNIGENT_APP_URL", f"{REMOTE_URL}///")
    assert config.omnigent_app_url() == REMOTE_URL
    assert config.omnigent_remote_enabled() is True

    monkeypatch.setenv(
        "OMNIGENT_APP_URL",
        "HTTPS://ALICE-OMNIGENT.EXAMPLE.DATABRICKSAPPS.COM:443/",
    )
    assert config.omnigent_app_url() == REMOTE_URL


def test_remote_url_rejects_non_https_outside_loopback_dev(monkeypatch):
    from server import config

    monkeypatch.setenv("LOCAL_DEV", "0")
    monkeypatch.setenv("OMNIGENT_APP_URL", "http://remote.example.com")
    with pytest.raises(ValueError, match="https"):
        config.omnigent_app_url()

    monkeypatch.setenv("LOCAL_DEV", "1")
    monkeypatch.setenv("OMNIGENT_APP_URL", "http://127.0.0.1:6767/")
    assert config.omnigent_app_url() == "http://127.0.0.1:6767"


def test_remote_start_rejects_malformed_but_tolerates_absent_attendee(monkeypatch):
    """An absent hint must not fail startup, or self-binding never runs."""
    from server.omnigent_remote import RemoteHostManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.delenv("WORKSHOP_ATTENDEE_EMAIL", raising=False)
    RemoteHostManager().start()

    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "not-an-email")
    with pytest.raises(ValueError, match="WORKSHOP_ATTENDEE_EMAIL"):
        RemoteHostManager().start()


def test_remote_attendee_enforced_despite_shared_topology_optin(
    client, monkeypatch, tmp_path
):
    """ALLOW_SHARED_TOPOLOGY must not open a remote instance to a second attendee."""
    from server import config

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "Alice@Example.COM")
    monkeypatch.setenv("ALLOW_SHARED_TOPOLOGY", "true")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))

    assert (
        client.get(
            "/api/config", headers={"X-Forwarded-Email": "alice@example.com"}
        ).status_code
        == 200
    )
    response = client.get(
        "/api/config", headers={"X-Forwarded-Email": "bob@example.com"}
    )

    assert response.status_code == 403
    assert "assigned to alice@example.com" in response.json()["detail"]


def test_misconfigured_remote_instance_fails_closed(client, monkeypatch):
    """A malformed remote URL denies access rather than surfacing a 500."""
    monkeypatch.setenv("OMNIGENT_APP_URL", "http://omnigent.example.com")

    response = client.get(
        "/api/config", headers={"X-Forwarded-Email": "alice@example.com"}
    )

    assert response.status_code == 403
    assert "https" in response.json()["detail"]


def test_only_configured_attendee_admitted_under_concurrency(
    client, monkeypatch, tmp_path
):
    from server import config, obo
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "attendee-7@example.com")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    monkeypatch.setattr("server.users.user_manager", users)
    monkeypatch.setattr(obo, "_notify_remote_host", lambda email: None)

    barrier = threading.Barrier(12)
    outcomes: list[tuple[str, int]] = []
    lock = threading.Lock()

    def attempt(email: str) -> None:
        barrier.wait()
        code = client.get(
            "/api/config", headers={"X-Forwarded-Email": email}
        ).status_code
        with lock:
            outcomes.append((email, code))

    threads = [
        threading.Thread(target=attempt, args=(f"attendee-{index}@example.com",))
        for index in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    admitted = [email for email, code in outcomes if code == 200]
    assert admitted == ["attendee-7@example.com"]
    assert len(outcomes) == 12
    assert users.peek("attendee-3@example.com") is None


def test_wrong_configured_attendee_rejected_before_home_or_obo_write(
    client, monkeypatch, tmp_path
):
    from server import config, obo, topology
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    monkeypatch.setattr("server.users.user_manager", users)
    monkeypatch.setattr(obo, "_notify_remote_host", lambda email: None)

    response = client.get(
        "/api/config",
        headers={
            "X-Forwarded-Email": "bob@example.com",
            "X-Forwarded-Access-Token": make_jwt(time.time() + 3600),
        },
    )

    assert response.status_code == 403
    assert users.peek("bob@example.com") is None
    assert not list((tmp_path / "users").glob("*bob*"))


def test_capture_mirrors_omnigent_token_without_enable_obo(
    client, monkeypatch, tmp_path
):
    from server import config, obo
    from server.users import UserManager

    monkeypatch.delenv("ENABLE_OBO", raising=False)
    monkeypatch.setenv("OMNIGENT_APP_URL", f"{REMOTE_URL}/")
    manager = UserManager()
    monkeypatch.setattr("server.users.user_manager", manager)
    monkeypatch.setattr(obo, "_notify_remote_host", lambda email: None)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))

    token = make_jwt(time.time() + 3600)
    obo.OboManager().capture("ALICE@EXAMPLE.COM", token)

    user = manager.peek("alice@example.com")
    assert user is not None
    path = Path(user.home) / ".omnigent" / "auth_tokens.json"
    data = json.loads(path.read_text())
    assert data[REMOTE_URL] == {
        "token": token,
        "user_id": "alice@example.com",
        "expires_at": pytest.approx(time.time() + 3600, abs=2),
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_token_mirror_atomically_preserves_other_servers(client, monkeypatch, tmp_path):
    from server import config, obo
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(obo, "_notify_remote_host", lambda email: None)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    manager = UserManager()
    monkeypatch.setattr("server.users.user_manager", manager)
    user = manager.get("alice@example.com")
    token_path = Path(user.home) / ".omnigent" / "auth_tokens.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps({"https://other.example": {"token": "keep"}}))

    obo.OboManager().capture("alice@example.com", make_jwt(time.time() + 3600))

    data = json.loads(token_path.read_text())
    assert data["https://other.example"] == {"token": "keep"}
    assert REMOTE_URL in data
    assert not list(token_path.parent.glob("*.tmp"))


def test_expired_capture_waits_and_fresh_capture_wakes(client, monkeypatch, tmp_path):
    from server import config, obo
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    manager = UserManager()
    monkeypatch.setattr("server.users.user_manager", manager)
    notifications: list[str] = []
    monkeypatch.setattr(obo, "_notify_remote_host", notifications.append)
    tokens = obo.OboManager()

    tokens.capture("alice@example.com", make_jwt(time.time() - 10))
    path = (
        Path(manager.get("alice@example.com").home) / ".omnigent" / "auth_tokens.json"
    )
    assert json.loads(path.read_text())[REMOTE_URL]["expires_at"] < time.time()
    assert notifications == []

    tokens.capture("alice@example.com", make_jwt(time.time() + 3600))
    assert notifications == ["alice@example.com"]


def test_late_expired_snapshot_cannot_clobber_fresh_mirror(
    client, monkeypatch, tmp_path
):
    from server import config, obo
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    manager = UserManager()
    monkeypatch.setattr("server.users.user_manager", manager)
    monkeypatch.setattr(obo, "_notify_remote_host", lambda email: None)
    tokens = obo.OboManager()
    fresh = make_jwt(time.time() + 3600)
    tokens.capture("alice@example.com", fresh)
    tokens.capture("alice@example.com", make_jwt(time.time() - 10))

    path = (
        Path(manager.get("alice@example.com").home) / ".omnigent" / "auth_tokens.json"
    )
    assert json.loads(path.read_text())[REMOTE_URL]["token"] == fresh


def test_older_still_valid_capture_cannot_clobber_newer_iat(
    client, monkeypatch, tmp_path
):
    from server import config, obo
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    manager = UserManager()
    monkeypatch.setattr("server.users.user_manager", manager)
    monkeypatch.setattr(obo, "_notify_remote_host", lambda email: None)
    now = time.time()
    newer = make_jwt(now + 1800, now)
    older = make_jwt(now + 3600, now - 60)
    tokens = obo.OboManager()
    tokens.capture("alice@example.com", newer)
    tokens.capture("alice@example.com", older)

    path = (
        Path(manager.get("alice@example.com").home) / ".omnigent" / "auth_tokens.json"
    )
    assert json.loads(path.read_text())[REMOTE_URL]["token"] == newer


def test_concurrent_out_of_order_capture_keeps_newest_iat(
    client, monkeypatch, tmp_path
):
    from server import config, obo
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    manager = UserManager()
    monkeypatch.setattr("server.users.user_manager", manager)
    monkeypatch.setattr(obo, "_notify_remote_host", lambda email: None)
    now = time.time()
    tokens = obo.OboManager()
    candidates = [make_jwt(now + 3600, now - offset) for offset in (40, 10, 30, 0, 20)]
    threads = [
        threading.Thread(target=tokens.capture, args=("alice@example.com", candidate))
        for candidate in candidates
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    path = (
        Path(manager.get("alice@example.com").home) / ".omnigent" / "auth_tokens.json"
    )
    assert json.loads(path.read_text())[REMOTE_URL]["token"] == candidates[3]


def test_host_launch_exact_argv_and_secret_free_environment(monkeypatch, tmp_path):
    from server.omnigent_remote import build_host_launch
    from server.users import User

    for key in (
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_TOKEN",
        "WORKSHOP_PAT",
        "X_FORWARDED_ACCESS_TOKEN",
        "OMNIGENT_HOST_TOKEN",
    ):
        monkeypatch.setenv(key, f"secret-{key}")
    user = User("alice@example.com")
    monkeypatch.setattr(user, "home", str(tmp_path / "alice"))
    user.bootstrap_home()

    argv, env, cwd = build_host_launch(user, "/opt/workshop/bin/omnigent", REMOTE_URL)

    assert argv == [
        "/opt/workshop/bin/omnigent",
        "host",
        "--server",
        REMOTE_URL,
        "--non-interactive",
    ]
    assert cwd == str(Path(user.home) / "projects")
    assert env["HOME"] == user.home
    assert env["OMNIGENT_APP_URL"] == REMOTE_URL
    assert re.fullmatch(r"[0-9a-f]{32}", env["OMNIGENT_HOST_ID"])
    assert env["OMNIGENT_HOST_NAME"].startswith("workshop-")
    assert re.fullmatch(r"[a-z0-9-]+", env["OMNIGENT_HOST_NAME"])
    assert len(env["OMNIGENT_HOST_NAME"]) <= 64
    assert "OMNIGENT_HOST_TOKEN" not in env
    assert env["DATABRICKS_CONFIG_PROFILE"] == "workshop-omnigent-no-credentials"
    isolated = Path(env["DATABRICKS_CONFIG_FILE"])
    assert (
        isolated
        == Path(user.home) / ".config" / "workshop" / "omnigent-empty-databrickscfg"
    )
    assert isolated.read_text() == ""
    assert isolated.stat().st_mode & 0o777 == 0o600
    assert (
        not {
            "DATABRICKS_TOKEN",
            "DATABRICKS_CLIENT_ID",
            "DATABRICKS_CLIENT_SECRET",
            "DATABRICKS_HOST",
        }
        & env.keys()
    )
    assert not any("secret-" in value for value in env.values())
    assert not any("secret-" in arg for arg in argv)


def test_stable_host_identity_is_domain_separated_and_restart_stable():
    from server.omnigent_remote import stable_host_identity
    from server.users import User

    first = stable_host_identity(User("Alice@Example.COM"), f"{REMOTE_URL}/")
    restarted = stable_host_identity(User("alice@example.com"), REMOTE_URL)
    expected = hashlib.sha256(
        b"databricks-workshop-terminal/omnigent-host-id/v1\0"
        + REMOTE_URL.encode()
        + b"\0alice@example.com"
    ).hexdigest()[:32]

    assert first == restarted
    assert first.host_id == expected
    assert re.fullmatch(r"[0-9a-f]{32}", first.host_id)
    assert len(first.name) <= 64


def test_stable_host_identity_differs_by_attendee_and_server():
    from server.omnigent_remote import stable_host_identity
    from server.users import User

    alice = stable_host_identity(User("alice@example.com"), REMOTE_URL)
    bob = stable_host_identity(User("bob@example.com"), REMOTE_URL)
    other_server = stable_host_identity(
        User("alice@example.com"),
        "https://other-omnigent.example.databricksapps.com",
    )

    assert len({alice.host_id, bob.host_id, other_server.host_id}) == 3


def test_stable_host_display_name_obeys_upstream_limit_without_email_domain():
    from server.omnigent_remote import stable_host_identity
    from server.users import User

    email = f"{'very-long-attendee-name-' * 5}@sensitive.example.com"
    identity = stable_host_identity(User(email), REMOTE_URL)

    assert re.fullmatch(r"[a-z0-9-]{1,64}", identity.name)
    assert "sensitive" not in identity.name
    assert identity.name.endswith(hashlib.sha256(email.encode()).hexdigest()[:8])


def test_host_identity_overrides_are_not_in_attendee_pty(monkeypatch, tmp_path):
    from server import config
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    user = User("alice@example.com")
    user.bootstrap_home()
    env = user.shell_env()

    assert "OMNIGENT_HOST_ID" not in env
    assert "OMNIGENT_HOST_NAME" not in env
    assert "OMNIGENT_HOST_TOKEN" not in env


def test_remote_launch_cannot_see_populated_attendee_default(monkeypatch, tmp_path):
    from server.omnigent_remote import build_host_launch
    from server.users import User

    user = User("alice@example.com")
    monkeypatch.setattr(user, "home", str(tmp_path / "alice"))
    user.bootstrap_home()
    default_cfg = Path(user.home) / ".databrickscfg"
    default_cfg.write_text(
        "[DEFAULT]\nhost = https://workspace.example\ntoken = app-sp-secret\n"
    )

    _, env, _ = build_host_launch(user, "/opt/bin/omnigent", REMOTE_URL)
    assert Path(env["DATABRICKS_CONFIG_FILE"]) != default_cfg
    assert "app-sp-secret" not in Path(env["DATABRICKS_CONFIG_FILE"]).read_text()


class _FakeProcess:
    _next_pid = 1000

    def __init__(self, *, exits: bool = False):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = 1 if exits else None
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("omnigent", timeout)
        return self.returncode


def test_one_host_process_per_attendee_under_concurrent_notifications(
    monkeypatch, tmp_path
):
    from server import config
    from server.omnigent_remote import RemoteHostManager
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    user = users.get("alice@example.com")
    token_path = Path(user.home) / ".omnigent" / "auth_tokens.json"
    token_path.write_text(
        json.dumps(
            {
                REMOTE_URL: {
                    "token": "secret",
                    "user_id": user.email,
                    "expires_at": time.time() + 3600,
                }
            }
        )
    )
    calls = []
    process = _FakeProcess()

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    def killpg(_pid, sig):
        process.returncode = -sig

    monkeypatch.setattr("server.omnigent_remote.os.killpg", killpg)

    hosts = RemoteHostManager(
        user_manager=users,
        popen_factory=popen,
        binary_resolver=lambda: "/opt/bin/omnigent",
        poll_interval=0.01,
    )
    hosts.start()
    threads = [
        threading.Thread(target=hosts.notify, args=("alice@example.com",))
        for _ in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _wait_for(lambda: hosts.status("alice@example.com")["status"] == "running")

    assert len(calls) == 1
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["stdout"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    assert "pid" not in hosts.status("alice@example.com")
    hosts.stop()


def test_first_notify_survives_reentrant_deferred_obo_flush(monkeypatch, tmp_path):
    """A deferred OBO flush re-enters notify(); _lock must not be held."""
    from server import config, obo
    from server.omnigent_remote import RemoteHostManager
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    monkeypatch.setattr("server.users.user_manager", users)
    manager = obo.OboManager()
    monkeypatch.setattr(obo, "obo_manager", manager)

    hosts = RemoteHostManager(
        user_manager=users,
        binary_resolver=lambda: None,
        poll_interval=0.01,
        shutdown_timeout=0.1,
    )
    # _notify_remote_host resolves the module singleton at call time, so the
    # deferred flush inside UserManager.get() re-enters *this* manager.
    monkeypatch.setattr("server.omnigent_remote.remote_host_manager", hosts)

    now = time.time()
    written = make_jwt(now + 1800, iat=now - 10)
    held = make_jwt(now + 3600, iat=now)
    users.get("alice@example.com")
    manager.capture("alice@example.com", written)

    # A newer capture that has not reached disk yet: the nested
    # UserManager.get() will take the user_ready() -> _write_user() ->
    # _notify_remote_host() path while the outer notify() is still in flight.
    with manager._lock:
        record = manager._by_email["alice@example.com"]
        record.token = held
        record.iat, record.exp = now, now + 3600
        record.written_token = written

    hosts.start()
    returned = threading.Event()

    def first_notify():
        try:
            hosts.notify("alice@example.com")
        finally:
            returned.set()

    worker = threading.Thread(target=first_notify, daemon=True)
    worker.start()
    assert returned.wait(timeout=10), "notify() re-acquired _lock on one thread"

    # Both frames must converge on a single host and a single worker thread.
    with hosts._lock:
        assert list(hosts._hosts) == ["alice@example.com"]
    hosts.stop()


def test_distinct_attendees_use_distinct_homes(monkeypatch, tmp_path):
    from server import config
    from server.omnigent_remote import build_host_launch
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    alice = User("alice@example.com")
    bob = User("bob@example.com")
    alice.bootstrap_home()
    bob.bootstrap_home()

    _, alice_env, alice_cwd = build_host_launch(alice, "/bin/omnigent", REMOTE_URL)
    _, bob_env, bob_cwd = build_host_launch(bob, "/bin/omnigent", REMOTE_URL)
    assert alice_env["HOME"] != bob_env["HOME"]
    assert alice_cwd != bob_cwd


def test_full_jitter_backoff_is_exponential_and_capped():
    from server.omnigent_remote import RemoteHostManager

    hosts = RemoteHostManager(random_fn=lambda: 0.5, backoff_base=2.0, backoff_cap=10.0)
    assert [hosts.backoff_delay(n) for n in range(5)] == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_meaningful_stable_runtime_resets_backoff():
    from server.omnigent_remote import RemoteHostManager

    hosts = RemoteHostManager(stable_runtime=30.0)
    assert hosts.attempt_after_exit(4, runtime=29.9) == 5
    assert hosts.attempt_after_exit(4, runtime=30.0) == 0


def test_shutdown_terms_then_kills_and_reaps(monkeypatch, tmp_path):
    from server import config
    from server.omnigent_remote import RemoteHostManager
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    user = users.get("alice@example.com")
    (Path(user.home) / ".omnigent" / "auth_tokens.json").write_text(
        json.dumps(
            {
                REMOTE_URL: {
                    "token": "secret",
                    "user_id": user.email,
                    "expires_at": time.time() + 3600,
                }
            }
        )
    )
    process = _FakeProcess()
    signals = []

    def killpg(pid, sig):
        signals.append(sig)
        if len(signals) == 2:
            process.returncode = -sig

    monkeypatch.setattr("server.omnigent_remote.os.killpg", killpg)
    hosts = RemoteHostManager(
        user_manager=users,
        popen_factory=lambda *a, **k: process,
        binary_resolver=lambda: "/opt/bin/omnigent",
        poll_interval=0.01,
        shutdown_timeout=0.01,
    )
    hosts.start()
    hosts.notify(user.email)
    _wait_for(lambda: hosts.status(user.email)["status"] == "running")
    hosts.stop()

    import signal

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.wait() == -signal.SIGKILL
    assert hosts.status(user.email)["status"] == "stopped"


def test_lifespan_starts_and_stops_remote_manager(monkeypatch):
    from fastapi.testclient import TestClient
    from server import main

    calls = []
    monkeypatch.setattr(
        main.remote_host_manager, "start", lambda: calls.append("start")
    )
    monkeypatch.setattr(main.remote_host_manager, "stop", lambda: calls.append("stop"))
    with TestClient(main.app):
        assert calls == ["start"]
    assert calls == ["start", "stop"]


def test_stop_waits_for_popen_publication_and_kills_child(monkeypatch, tmp_path):
    from server import config
    from server.omnigent_remote import RemoteHostManager
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "race@example.com")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    user = users.get("race@example.com")
    (Path(user.home) / ".omnigent" / "auth_tokens.json").write_text(
        json.dumps(
            {
                REMOTE_URL: {
                    "token": "secret",
                    "user_id": user.email,
                    "expires_at": time.time() + 3600,
                }
            }
        )
    )
    entered = threading.Event()
    release = threading.Event()
    process = _FakeProcess()
    signals = []

    def blocking_popen(*args, **kwargs):
        entered.set()
        assert release.wait(1)
        return process

    def killpg(pid, sig):
        signals.append(sig)
        process.returncode = -sig

    monkeypatch.setattr("server.omnigent_remote.os.killpg", killpg)
    hosts = RemoteHostManager(
        user_manager=users,
        popen_factory=blocking_popen,
        binary_resolver=lambda: "/opt/bin/omnigent",
        shutdown_timeout=0.5,
        poll_interval=0.01,
    )
    hosts.start()
    hosts.notify(user.email)
    assert entered.wait(1)
    stopped = threading.Event()
    stopper = threading.Thread(target=lambda: (hosts.stop(), stopped.set()))
    stopper.start()
    time.sleep(0.02)
    assert not stopped.is_set()
    release.set()
    stopper.join(1)
    assert stopped.is_set()
    assert signals
    assert process.returncode is not None


def test_generated_tui_helper_routes_remote_and_local(monkeypatch, tmp_path):
    from server import config
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    user = User("alice@example.com")
    user.bootstrap_home()
    helper = Path(user.home) / ".local" / "bin" / "workshop-omnigent"
    text = helper.read_text()
    assert 'exec omnigent "$@"' in text
    assert "unset DATABRICKS_TOKEN DATABRICKS_CLIENT_ID" in text
    assert (
        'DATABRICKS_CONFIG_FILE="$HOME/.config/workshop/omnigent-empty-databrickscfg"'
        in text
    )
    assert "DATABRICKS_CONFIG_PROFILE=workshop-omnigent-no-credentials" in text
    assert helper.stat().st_mode & 0o111


def _run_tui_helper(tmp_path, monkeypatch, argv, *, app_url, live_sessions=False):
    """Execute the generated helper with a stub ``omnigent`` that echoes argv.

    Running it beats string-matching: the agent-selection branch has edge cases
    (a flag must not be read as an agent name, the local branch must stay a bare
    passthrough) that a substring assertion cannot distinguish.

    ``live_sessions`` replaces the generated probe with a fixed verdict so these
    cases exercise the helper's branch rather than the App lookup, which
    ``test_live_sessions_probe_*`` covers against a real server.
    """
    import subprocess

    from server import config
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    user = User("argv-probe@example.com")
    user.bootstrap_home()
    probe = Path(user.home) / ".local" / "bin" / "workshop-omnigent-live-sessions"
    probe.write_text(f"#!/bin/sh\nexit {0 if live_sessions else 1}\n")
    probe.chmod(0o755)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "omnigent"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    completed = subprocess.run(
        ["/bin/sh", str(Path(user.home) / ".local" / "bin" / "workshop-omnigent"), *argv],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "HOME": user.home,
            "OMNIGENT_APP_URL": app_url,
        },
        check=True,
    )
    return completed.stdout.split()


def test_tui_helper_joins_an_existing_session_so_both_tools_show_one_conversation(
    monkeypatch, tmp_path
):
    """The whole point of the card: a conversation started in the App's UI and
    the terminal must be the SAME conversation. ``run --server`` with no agent
    is the thin client that joins one; naming an agent would fork a new one."""
    argv = _run_tui_helper(
        tmp_path,
        monkeypatch,
        [],
        app_url="https://app.example.com",
        live_sessions=True,
    )

    assert argv == ["run", "--server", "https://app.example.com"]


def test_tui_helper_creates_polly_when_there_is_nothing_to_join(monkeypatch, tmp_path):
    """Every attendee's first launch meets a fresh control plane, where the
    attach exits "No sessions found on the server". Naming an agent is what
    creates the session, and polly is the same orchestrator a bare ``omnigent``
    launches — so that path must stay exactly as it was."""
    argv = _run_tui_helper(tmp_path, monkeypatch, [], app_url="https://app.example.com")

    assert argv == ["polly", "--server", "https://app.example.com"]


def test_tui_helper_creates_polly_when_the_probe_is_missing(monkeypatch, tmp_path):
    """The probe is an optimization, not a dependency: losing it costs session
    sharing,     but must never cost the attendee a working card."""
    from server import config
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    user = User("argv-probe@example.com")
    user.bootstrap_home()
    (Path(user.home) / ".local" / "bin" / "workshop-omnigent-live-sessions").unlink()
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "omnigent").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    (stub_dir / "omnigent").chmod(0o755)
    completed = subprocess.run(
        ["/bin/sh", str(Path(user.home) / ".local" / "bin" / "workshop-omnigent")],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "HOME": user.home,
            "OMNIGENT_APP_URL": "https://app.example.com",
        },
        check=True,
    )

    assert completed.stdout.split() == [
        "polly",
        "--server",
        "https://app.example.com",
    ]
    assert completed.stderr == ""


def test_tui_helper_takes_an_agent_name_so_variants_get_their_own_cards(
    monkeypatch, tmp_path
):
    """This is what lets each model-set variant be a separate launch card
    instead of requiring the attendee to type a CLI invocation."""
    argv = _run_tui_helper(
        tmp_path, monkeypatch, ["polly-economy"], app_url="https://app.example.com"
    )

    assert argv == ["polly-economy", "--server", "https://app.example.com"]


def test_tui_helper_does_not_mistake_a_flag_for_an_agent_name(monkeypatch, tmp_path):
    """Otherwise ``workshop-omnigent --version`` would resolve the flag as an
    agent and fail instead of reaching the CLI."""
    argv = _run_tui_helper(
        tmp_path, monkeypatch, ["--version"], app_url="https://app.example.com"
    )

    assert argv == ["polly", "--server", "https://app.example.com", "--version"]


def test_tui_helper_local_mode_stays_a_bare_passthrough(monkeypatch, tmp_path):
    """The Control Tower contract pins bare ``omnigent`` for an empty URL, so the
    local branch must be reached before any positional is consumed."""
    assert _run_tui_helper(tmp_path, monkeypatch, [], app_url="") == []
    assert _run_tui_helper(tmp_path, monkeypatch, ["codex"], app_url="") == ["codex"]


def _run_live_sessions_probe(tmp_path, monkeypatch, *, sessions, write_token=True):
    """Run the generated probe against a real server standing in for the App."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from server import config
    from server.users import User

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    user = User("session-probe@example.com")
    user.bootstrap_home()
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["authorization"] = self.headers.get("Authorization")
            body = json.dumps({"object": "list", "data": sessions}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep pytest output readable
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if write_token:
            (Path(user.home) / ".omnigent" / "auth_tokens.json").write_text(
                json.dumps({url: {"token": "mirrored-bearer"}})
            )
        completed = subprocess.run(
            [str(Path(user.home) / ".local" / "bin" / "workshop-omnigent-live-sessions")],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": user.home, "OMNIGENT_APP_URL": url},
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
    return completed.returncode, seen


def test_live_sessions_probe_reports_a_joinable_session_as_the_attendee(
    monkeypatch, tmp_path
):
    """It must ask as the attendee, not as the app service principal: the App
    scopes sessions to the bearer's owner, so an SP bearer would answer about
    the wrong person's conversations."""
    code, seen = _run_live_sessions_probe(
        tmp_path,
        monkeypatch,
        sessions=[{"id": "abc", "archived": False, "status": "idle"}],
    )

    assert code == 0
    assert seen["path"] == "/v1/sessions"
    assert seen["authorization"] == "Bearer mirrored-bearer"


def test_live_sessions_probe_ignores_archived_sessions(monkeypatch, tmp_path):
    """An archived conversation is not something to drop an attendee back into,
    so a history of them still has to launch a fresh agent."""
    code, _ = _run_live_sessions_probe(
        tmp_path,
        monkeypatch,
        sessions=[{"id": "abc", "archived": True, "status": "idle"}],
    )

    assert code == 1


def test_live_sessions_probe_ignores_failed_sessions(monkeypatch, tmp_path):
    """Restarting this app fails every session it hosted a runner for, so after
    a redeploy the attendee's whole history reads failed. Offering a picker over
    only those would be strictly worse than launching a fresh agent."""
    code, _ = _run_live_sessions_probe(
        tmp_path,
        monkeypatch,
        sessions=[
            {"id": "abc", "archived": False, "status": "failed"},
            {"id": "def", "archived": False, "status": "failed"},
        ],
    )

    assert code == 1


def test_live_sessions_probe_joins_a_live_session_beside_failed_history(
    monkeypatch, tmp_path
):
    """One good conversation is enough — a pile of dead ones beside it must not
    argue the attendee back into a brand-new session."""
    code, _ = _run_live_sessions_probe(
        tmp_path,
        monkeypatch,
        sessions=[
            {"id": "abc", "archived": False, "status": "failed"},
            {"id": "def", "archived": False, "status": "idle"},
        ],
    )

    assert code == 0


def test_live_sessions_probe_treats_an_unrecognized_status_as_joinable(
    monkeypatch, tmp_path
):
    """The denylist is the point: a status this probe has never seen still means
    the attendee has a conversation, and guessing otherwise forks a new one."""
    code, _ = _run_live_sessions_probe(
        tmp_path,
        monkeypatch,
        sessions=[{"id": "abc", "archived": False, "status": "busy"}],
    )

    assert code == 0


def test_live_sessions_probe_reports_nothing_to_join_on_a_fresh_control_plane(
    monkeypatch, tmp_path
):
    code, _ = _run_live_sessions_probe(tmp_path, monkeypatch, sessions=[])

    assert code == 1


def test_live_sessions_probe_fails_closed_without_a_mirrored_token(
    monkeypatch, tmp_path
):
    """No token means no authenticated answer, so it must not guess — and must
    not reach the App unauthenticated either."""
    code, seen = _run_live_sessions_probe(
        tmp_path,
        monkeypatch,
        sessions=[{"id": "abc", "archived": False, "status": "idle"}],
        write_token=False,
    )

    assert code == 1
    assert seen == {}


def test_concurrent_first_requests_bootstrap_home_once(monkeypatch, tmp_path):
    from server import config
    from server.users import User, UserManager

    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    calls = []
    original = User._write_omnigent_helper

    def counted(user):
        calls.append(user.email)
        original(user)

    monkeypatch.setattr(User, "_write_omnigent_helper", counted)
    users = UserManager()
    threads = [
        threading.Thread(target=users.get, args=("alice@example.com",))
        for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == ["alice@example.com"]
    helper = (
        Path(users.get("alice@example.com").home)
        / ".local"
        / "bin"
        / "workshop-omnigent"
    )
    assert helper.exists()
    assert not list(helper.parent.glob("*.tmp"))


def test_only_omnigent_catalog_uses_helper():
    from server import agents

    catalog = {agent["id"]: agent for agent in agents.load_catalog()}
    assert catalog["omnigent"]["command"] == "workshop-omnigent"
    assert catalog["claude"]["command"] == "claude"
    assert catalog["codex"]["command"] == "codex"


def test_config_api_exposes_only_remote_url_and_sanitized_status(client, monkeypatch):
    from .conftest import ALICE

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "must-not-leak")
    body = client.get("/api/config", headers=ALICE).json()
    assert body["omnigent_remote"] == {"enabled": True, "url": REMOTE_URL}
    assert "must-not-leak" not in json.dumps(body)

    status = client.get("/api/omnigent-host", headers=ALICE)
    assert status.status_code == 200
    assert status.json()["status"] in {
        "disabled",
        "waiting_for_token",
        "starting",
        "running",
        "backoff",
        "stopped",
        "error",
    }
    assert "pid" not in status.json()


@pytest.mark.parametrize(
    ("remote_body", "http_status", "network_error", "connected"),
    [
        (
            {"host_id": "expected", "status": "online", "name": "workshop-alice"},
            200,
            False,
            True,
        ),
        (
            {"host_id": "expected", "status": "offline", "name": "workshop-alice"},
            200,
            False,
            False,
        ),
        (
            {"host_id": "different", "status": "online", "name": "workshop-other"},
            200,
            False,
            False,
        ),
        (
            {"host_id": "expected", "status": "online", "name": "workshop-alice"},
            401,
            False,
            False,
        ),
        (
            {"host_id": "expected", "status": "online", "name": "workshop-alice"},
            200,
            True,
            False,
        ),
    ],
)
def test_verified_readiness_requires_online_exact_host(
    monkeypatch, tmp_path, remote_body, http_status, network_error, connected
):
    from datetime import datetime, timezone

    import requests

    from server import config
    from server.omnigent_remote import RemoteHostManager, stable_host_identity
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    user = users.get("alice@example.com")
    expected = stable_host_identity(user, REMOTE_URL).host_id
    body = {
        **remote_body,
        "host_id": expected if remote_body["host_id"] == "expected" else "0" * 32,
    }
    token = make_jwt(time.time() + 3600)
    (Path(user.home) / ".omnigent" / "auth_tokens.json").write_text(
        json.dumps(
            {
                REMOTE_URL: {
                    "token": token,
                    "user_id": user.email,
                    "expires_at": time.time() + 3600,
                }
            }
        )
    )

    class Response:
        status_code = http_status

        @staticmethod
        def json():
            return body

        text = ""

    seen = {}

    def get(url, **kwargs):
        seen.update(url=url, **kwargs)
        if network_error:
            raise requests.ConnectionError("host API unavailable")
        return Response()

    monkeypatch.setattr("server.omnigent_remote.requests.get", get)
    verified_at = datetime(2026, 7, 29, 4, 38, 0, tzinfo=timezone.utc)
    hosts = RemoteHostManager(user_manager=users, now_fn=lambda: verified_at)
    hosts._hosts[user.email] = type(
        "Host",
        (),
        {"status": "running", "process": _FakeProcess(), "last_exit_code": None},
    )()

    readiness = hosts.readiness(user.email)

    assert readiness["status"] == "running"
    assert readiness["connected"] is connected
    assert readiness["expected_host_id"] == expected
    assert ("host_id" in readiness) is connected
    assert ("last_seen_at" in readiness) is connected
    if connected:
        assert readiness["last_seen_at"] == "2026-07-29T04:38:00Z"
    assert seen["url"] == f"{REMOTE_URL}/v1/hosts/{expected}"
    assert seen["headers"] == {"Authorization": f"Bearer {token}"}
    assert token not in json.dumps(readiness)


def test_admin_readiness_uses_admin_auth_and_returns_no_token(
    client, monkeypatch, as_admin
):
    from server import admin

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("WORKSHOP_ATTENDEE_EMAIL", "alice@example.com")
    payload = {
        "status": "running",
        "connected": True,
        "expected_host_id": "a" * 32,
        "host_id": "a" * 32,
        "last_seen_at": "2026-07-29T00:00:00Z",
    }
    monkeypatch.setattr(admin.remote_host_manager, "readiness", lambda email: payload)

    response = client.get(
        "/api/admin/omnigent-host-readiness",
        headers={"Authorization": "Bearer admin-service-principal"},
    )

    assert response.status_code == 200
    assert response.json() == payload
    assert "token" not in json.dumps(response.json()).lower()


def test_second_remote_attendee_is_rejected_before_home_or_obo_write(
    client, monkeypatch, tmp_path
):
    from server import config, obo, topology
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    monkeypatch.setattr("server.users.user_manager", users)
    monkeypatch.setattr(obo, "_notify_remote_host", lambda email: None)

    alice = {
        "X-Forwarded-Email": "alice@example.com",
        "X-Forwarded-Access-Token": make_jwt(time.time() + 3600),
    }
    bob = {
        "X-Forwarded-Email": "bob@example.com",
        "X-Forwarded-Access-Token": make_jwt(time.time() + 3600),
    }

    assert client.get("/api/config", headers=alice).status_code == 200
    response = client.get("/api/config", headers=bob)

    assert response.status_code == 403
    assert "assigned to alice@example.com" in response.json()["detail"]
    assert users.peek("bob@example.com") is None
    assert not list((tmp_path / "users").glob("*bob*"))


def test_remote_obo_refresh_cannot_create_or_switch_attendee_binding(
    client, monkeypatch, tmp_path
):
    from server import config, obo, topology
    from server.users import UserManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    users = UserManager()
    monkeypatch.setattr("server.users.user_manager", users)
    monkeypatch.setattr(obo, "_notify_remote_host", lambda email: None)

    unbound = client.post("/api/obo/refresh", json={"email": "bob@example.com"})
    assert unbound.status_code == 403
    assert users.peek("bob@example.com") is None

    alice = {
        "X-Forwarded-Email": "alice@example.com",
        "X-Forwarded-Access-Token": make_jwt(time.time() + 3600),
    }
    assert client.get("/api/config", headers=alice).status_code == 200

    switched = client.post(
        "/api/obo/refresh",
        json={"email": "bob@example.com"},
        headers={"X-Forwarded-Email": "bob@example.com"},
    )
    assert switched.status_code == 403
    assert users.peek("bob@example.com") is None


def test_forwarded_obo_refresh_applies_attendee_authorization(client, monkeypatch):
    from fastapi import HTTPException
    from server import topology

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setattr(
        "server.auth._check_access",
        lambda principal: (_ for _ in ()).throw(
            HTTPException(status_code=403, detail="not an attendee")
        ),
    )

    response = client.post(
        "/api/obo/refresh",
        json={},
        headers={
            "X-Forwarded-Email": "alice@example.com",
            "X-Forwarded-Access-Token": make_jwt(time.time() + 3600),
        },
    )

    assert response.status_code == 403


def test_local_mode_preserves_instance_attendee_binding(
    client, monkeypatch
):
    from server import topology

    monkeypatch.delenv("OMNIGENT_APP_URL", raising=False)

    assert (
        client.get(
            "/api/config", headers={"X-Forwarded-Email": "alice@example.com"}
        ).status_code
        == 200
    )
    response = client.get(
        "/api/config", headers={"X-Forwarded-Email": "bob@example.com"}
    )
    assert response.status_code == 403
    assert "assigned to alice@example.com" in response.json()["detail"]


def test_app_yaml_defaults_remote_off_and_pins_omnigent_070():
    import yaml

    app_yaml = yaml.safe_load((Path(__file__).parents[1] / "app.yaml").read_text())
    env = {item["name"]: item["value"] for item in app_yaml["env"]}
    assert env["OMNIGENT_APP_URL"] == ""
    assert env["WORKSHOP_ATTENDEE_EMAIL"] == ""
    assert env["OMNIGENT_VERSION"] == "0.7.0"


def test_remote_start_rejects_disabled_or_incompatible_omnigent(monkeypatch):
    from server.bootstrap import install
    from server.omnigent_remote import RemoteHostManager

    monkeypatch.setenv("OMNIGENT_APP_URL", REMOTE_URL)
    monkeypatch.setenv("OMNIGENT_ENABLED", "false")
    with pytest.raises(ValueError, match="OMNIGENT_ENABLED"):
        RemoteHostManager().start()

    monkeypatch.setenv("OMNIGENT_ENABLED", "true")
    monkeypatch.setattr(install, "OMNIGENT_VERSION", "0.6.0")
    with pytest.raises(ValueError, match="0.7.0"):
        RemoteHostManager().start()


def test_local_mode_allows_omnigent_install_override(monkeypatch):
    from server.bootstrap import install
    from server.omnigent_remote import RemoteHostManager

    monkeypatch.delenv("OMNIGENT_APP_URL", raising=False)
    monkeypatch.setattr(install, "OMNIGENT_VERSION", "0.6.0")
    hosts = RemoteHostManager()
    hosts.start()
    hosts.stop()


def test_pinned_cli_parser_supports_remote_commands_when_installed():
    """Optional no-network smoke against the actual shared pinned CLI."""
    from server import config

    binary = Path(config.shared_prefix()) / "bin" / "omnigent"
    if not binary.exists():
        pytest.skip("pinned Omnigent CLI is not installed in this test checkout")
    for command in ("host", "run"):
        result = subprocess.run(
            [str(binary), command, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "--server" in result.stdout
