import os
import tempfile

import pytest
from fastapi.testclient import TestClient

ALICE = {"X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-Email": "bob@example.com"}


@pytest.fixture(scope="session", autouse=True)
def _test_env():
    os.environ["LOCAL_DEV"] = "1"  # skip CLI installers; allow header fallback
    os.environ["DATA_ROOT"] = tempfile.mkdtemp(prefix="workshop-test-")
    os.environ["DATABRICKS_HOST"] = "https://test.cloud.databricks.com"
    yield


@pytest.fixture()
def client(_test_env):
    from server.main import app

    with TestClient(app) as test_client:
        yield test_client
    # Terminate any PTYs the test created so nothing leaks between tests.
    from server.sessions import session_manager

    for session in session_manager.snapshot():
        session_manager.terminate(session)


@pytest.fixture(autouse=True)
def _isolate_user_registry():
    """Drop attendees cached by earlier tests, whose homes point elsewhere.

    ``User.home`` is computed once, at construction, from ``config.users_root()``
    — and many modules repoint that at their own ``tmp_path``. The registry is a
    process-wide singleton that nothing reset, so a test creating
    ``alice@example.com`` under its temporary root left every later test holding
    a home under a directory that no longer exists.

    That is an order-dependent failure, which is the expensive kind: two
    ``workspace_sync`` tests passed alone and reported ``amber`` instead of
    ``red`` after ``test_omnigent_remote`` ran first, because the record was
    written to the stale home while readiness recomputed the real one. During
    event week a suite that is red for this reason is a suite whose real
    regressions nobody looks at.
    """
    from server.users import user_manager

    user_manager._users.clear()
    yield
    user_manager._users.clear()


@pytest.fixture(autouse=True)
def _restore_content_state():
    """Content service is a module singleton — restore pack/phase per test."""
    from server.content import content_service

    pack, phase = content_service.pack, content_service.phase
    yield
    content_service.set_pack(pack)
    content_service.set_phase(phase)


@pytest.fixture()
def as_admin(monkeypatch):
    """Make group resolution report ADMIN_GROUP membership for everyone."""
    from server import auth, config

    monkeypatch.setattr(auth, "get_groups", lambda principal: {config.admin_group()})


@pytest.fixture()
def as_non_admin(monkeypatch):
    from server import auth

    monkeypatch.setattr(auth, "get_groups", lambda principal: set())


@pytest.fixture()
def launchable_agents(monkeypatch):
    """Run supported agent IDs on a harmless shell for endpoint tests.

    Production still executes the real CLIs. Tests that are about session
    ownership, presence, or metering should not need those binaries or a live
    workspace credential merely to keep a PTY open.
    """
    import server.main as main

    monkeypatch.setattr(
        main.install,
        "ready",
        lambda: {"claude": True, "codex": True, "omnigent": True},
    )
    monkeypatch.setattr(main, "ensure_user_credentials", lambda _user: None)
    monkeypatch.setattr(main.agents, "launch_command", lambda _agent: ["/bin/bash"])
    monkeypatch.setattr(main.identity, "observe", lambda _user: None)
