import os
import sys
import tempfile

import pytest
from contextlib import suppress
from fastapi.testclient import TestClient

def pytest_configure(config):
    config.addinivalue_line("markers", "write_behind: run capture on the production write-behind journal")


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
def _no_background_build_restore(monkeypatch):
    """Never run the after-restart build restore behind an unrelated test.

    It would reach for real credentials and the Databricks CLI on a daemon
    thread that outlives the test. tests/test_build_restore.py drives its own
    instances directly.
    """
    from types import SimpleNamespace

    from server import build_restore

    monkeypatch.setattr(build_restore, "restorer", SimpleNamespace(observe=lambda *args, **kwargs: None))


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

    user_manager.reset_for_tests()
    yield
    user_manager.reset_for_tests()


@pytest.fixture(autouse=True)
def _forced_write_behind_predecessors_exit(monkeypatch):
    """Under WT_CAPTURE_WRITE_BEHIND=1 every journal is write-behind. A journal
    built over storage another live journal uses models a successor process,
    which in production starts only after its predecessor retained and exited."""
    if os.environ.get("WT_CAPTURE_WRITE_BEHIND", "").strip() != "1":
        yield
        return
    from server import capture_journal

    original = capture_journal.CaptureJournal.__init__
    live: dict[int, list] = {}

    def init(self, storage, *args, **kwargs):
        target = getattr(storage, "storage", storage)  # audit snapshots wrap the live storage
        for predecessor in live.get(id(target), []):
            if predecessor.write_behind and predecessor._pending:
                with suppress(Exception):
                    predecessor.flush()
        original(self, storage, *args, **kwargs)
        live.setdefault(id(target), []).append(self)

    monkeypatch.setattr(capture_journal.CaptureJournal, "__init__", init)
    yield


@pytest.fixture(autouse=True)
def _isolate_capture_producers(monkeypatch):
    """Retained producer bindings belong to one simulated deployment/test."""
    from server import attendee_names, discovery, help as help_module, identity, insight_summary, stats, ws
    from server import event_emitter as emitter_module

    retained = emitter_module.build_default_emitter()
    monkeypatch.setattr(emitter_module, "event_emitter", retained)
    main = sys.modules.get("server.main")
    if main is not None:
        monkeypatch.setattr(main, "event_emitter", retained)

    def reset():
        # Direct-handler tests represent a new process without entering lifespan.
        # A previous ordinary shutdown deliberately froze that process registry.
        ws.start_capture_operations()
        attendee_names.reset_for_tests()
        identity.reset_for_tests()
        help_module.reset_for_tests()
        insight_summary.stamps.clear()
        with discovery.discovery_store._lock:
            discovery.discovery_store._capture_emitter = None
            discovery.discovery_store._records = {}
            discovery.discovery_store._legacy_issues = set()
            discovery.discovery_store.path = ""
        stats._code_cache.clear()

    reset()
    # A replacement process only starts after its predecessor exited, and an
    # ordinary exit retains write-behind capture (see main._retain_capture).
    # Tests that reconfigure capture over the same storage model exactly that.
    from server import capture as capture_module

    configure = capture_module.configure
    previous: list = []

    def configure_after_predecessor_exit(emitter, *, client=None):
        for journal in previous:
            if journal is not emitter.journal and getattr(journal, "write_behind", False):
                with suppress(Exception):
                    journal.close()
        configure(emitter, client=client)
        previous.append(emitter.journal)

    monkeypatch.setattr(capture_module, "configure", configure_after_predecessor_exit)
    yield
    for journal in previous:
        if getattr(journal, "write_behind", False):
            journal._closed = True
            journal._flush_wake.set()
    reset()


@pytest.fixture(autouse=True)
def _isolate_model_policy():
    """A live policy is process-global and must never leak into another test."""
    from server import cli_config, config, model_policy

    path = os.path.join(config.data_root(), "model-policy-v1.json")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    model_policy.store.reset_for_tests()
    cli_config._gateway_resolved = None
    yield
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    model_policy.store.reset_for_tests()
    cli_config._gateway_resolved = None


@pytest.fixture()
def retained_model_policy(monkeypatch):
    """Provision the policy dependency for tests that switch to production mode.

    This uses the production durable Workspace Files adapter and its in-memory
    SDK seam, rather than bypassing the missing-journal guard. Tests still own
    their source binding and any applied policy; unconfigured tests keep the
    production refusal intact.
    """
    from types import SimpleNamespace

    from server import capture, model_policy
    from server.capture_journal import CaptureJournal, WorkspaceCaptureStorage
    from .test_capture_startup import WorkspaceFiles

    def provision():
        binding = capture.source_binding()
        sdk = SimpleNamespace(workspace=WorkspaceFiles())
        journal = CaptureJournal(
            WorkspaceCaptureStorage(sdk, capture.workspace_path(binding)), binding, {}
        )
        policy = model_policy.PolicyStore()
        policy.configure_capture(journal)
        monkeypatch.setattr(model_policy, "store", policy)
        return policy

    return provision


@pytest.fixture()
def auth_model_policy(monkeypatch, retained_model_policy):
    """Isolate provisioned model state from tests of attendee/auth binding.

    Those tests deliberately remove the owner hint and change it after local
    startup. Their model dependency represents a separately configured retained
    source, so it must not inject an attendee hint or conceal auth behavior.
    """
    from server import capture

    binding = {"run_id": "auth-policy-run", "unit_id": "auth-policy-unit",
               "workspace_id": "123", "owner": "policy-owner@example.com"}
    monkeypatch.setattr(capture, "source_binding", lambda: binding)
    return retained_model_policy()


@pytest.fixture(autouse=True)
def _isolate_agent_control():
    from server import spend

    spend.reset()
    yield
    spend.reset()


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
