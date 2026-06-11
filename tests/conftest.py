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


@pytest.fixture()
def as_admin(monkeypatch):
    """Make group resolution report ADMIN_GROUP membership for everyone."""
    from server import auth, config

    monkeypatch.setattr(auth, "get_groups", lambda principal: {config.admin_group()})


@pytest.fixture()
def as_non_admin(monkeypatch):
    from server import auth

    monkeypatch.setattr(auth, "get_groups", lambda principal: set())
