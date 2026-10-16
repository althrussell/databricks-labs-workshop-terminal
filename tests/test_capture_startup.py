"""Production capture attaches before recovery and never degrades to memory."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import attendee_names, capture, discovery, help as help_module, identity, insight_summary, users
from server.capture_journal import CaptureHeldError
from server.event_emitter import EventEmitter


BINDING = {
    "run_id": "ctq-startup-run", "unit_id": "ctq-startup-unit",
    "workspace_id": "123456", "owner": "labuser+199@awsbricks.com",
}


class WorkspaceFiles:
    """Existing Workspace Files permissions and immutable writes, in memory."""

    def __init__(self):
        self.objects = {}
        self.directories = set()
        self.blocked = False
        self.paths = []

    @staticmethod
    def canonical(path):
        return path.removeprefix("/Workspace")

    def mkdirs(self, path):
        self.paths.append(path)
        if self.blocked:
            raise PermissionError("existing parent grant has not converged")
        self.directories.add(self.canonical(path))

    def list(self, path):
        parent = self.canonical(path).rstrip("/") + "/"
        return [SimpleNamespace(path=name, object_type="FILE")
                for name in sorted(self.objects)
                if name.startswith(parent) and "/" not in name[len(parent):]]

    def download(self, path, *, format):
        from databricks.sdk.errors import NotFound

        value = self.objects.get(self.canonical(path))
        if value is None:
            raise NotFound("missing")
        return io.BytesIO(value)

    def upload(self, path, content, *, format, overwrite):
        from databricks.sdk.errors import ResourceAlreadyExists

        assert overwrite is False
        name = self.canonical(path)
        if name in self.objects:
            raise ResourceAlreadyExists("already retained")
        self.objects[name] = bytes(content)


@pytest.fixture(autouse=True)
def capture_modules(monkeypatch, tmp_path):
    # Resolve modules with imported singleton references before replacing the
    # coordinator's registry; their references must survive this fixture intact.
    from server import main  # noqa: F401

    for name, value in {
        "WORKSHOP_RUN_ID": BINDING["run_id"], "WORKSHOP_UNIT_ID": BINDING["unit_id"],
        "DATABRICKS_WORKSPACE_ID": BINDING["workspace_id"],
        "WORKSHOP_ATTENDEE_EMAIL": BINDING["owner"],
        "WORKSHOP_INSIGHT_CAPTURE": "true", "SESSION_STATE_PATH": "",
        "DATA_ROOT": str(tmp_path / "container-one"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(users, "user_manager", users.UserManager())
    monkeypatch.setattr(discovery, "discovery_store", discovery.DiscoveryStore())
    monkeypatch.setattr(insight_summary, "stamps", insight_summary._Stamps())
    monkeypatch.setattr(identity, "_capture_emitter", None)
    monkeypatch.setattr(identity, "_snapshots", {})
    monkeypatch.setattr(identity, "_in_flight", set())
    monkeypatch.setattr(attendee_names, "_capture_emitter", None)
    monkeypatch.setattr(help_module, "_capture_journal", None)
    monkeypatch.setattr(help_module, "_require_ready", None)
    monkeypatch.setattr(help_module, "_state", help_module._empty_state())


def emitter():
    return EventEmitter(run_id=BINDING["run_id"], workspace_id=BINDING["workspace_id"],
                        ingest_url="", ingest_token="")


def test_workspace_path_is_bound_to_every_source_component():
    path = capture.workspace_path(BINDING)
    assert path.startswith(f"/Workspace/Users/{BINDING['owner']}/projects/.workshop-capture/")
    assert path == capture.workspace_path(dict(reversed(list(BINDING.items()))))
    for name in BINDING:
        assert capture.workspace_path({**BINDING, name: BINDING[name] + "-another"}) != path


def test_fresh_container_recovers_exact_epoch_records_ack_and_help_without_home(
    tmp_path, monkeypatch,
):
    sdk = SimpleNamespace(workspace=WorkspaceFiles())
    first = emitter()
    capture.configure(first, client=sdk)
    assert first.delivery_status()["persistence"]["durable"] is True
    first.emit("heartbeat", BINDING["owner"], {"value": 1}, idempotency_key="first")
    first.emit("heartbeat", BINDING["owner"], {"value": 2}, idempotency_key="second")
    original = first.collect()
    first.acknowledge(schema_version=2, stream_id=first.stream_id, through_seq=1)
    monkeypatch.setattr(help_module, "_push_control_tower_message", lambda *a, **k: False)
    monkeypatch.setattr(help_module, "_push_control_tower", lambda *a, **k: False)
    help_module.post_attendee_message("Need a hand", sender=BINDING["owner"])
    original_help = help_module.outbox_snapshot()

    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "container-two"))
    users.user_manager._users.clear()
    help_module.reset_for_tests()
    discovery.discovery_store = discovery.DiscoveryStore()
    insight_summary.stamps.clear()
    identity.reset_for_tests()
    attendee_names.reset_for_tests()
    replacement = emitter()
    capture.configure(replacement, client=sdk)

    assert replacement.stream_id == original["stream_id"]
    assert replacement.delivery_status()["acked_through"] == 1
    assert replacement.collect()["events"] == original["events"][1:]
    assert help_module.outbox_snapshot() == original_help
    owner = users.user_manager.peek(BINDING["owner"])
    assert owner is not None
    assert owner.first_seen == 0
    assert owner._bootstrapped is False
    assert not Path(owner.home).exists()
    assert all(path.startswith("/Workspace/Users/") for path in sdk.workspace.paths)


def test_grant_convergence_retries_without_accepting_memory_output(monkeypatch):
    workspace = WorkspaceFiles()
    workspace.blocked = True
    retained = emitter()
    capture.configure(retained, client=SimpleNamespace(workspace=workspace))
    held = retained.delivery_status()
    assert held["retention_ready"] is False
    assert held["pending"] is None
    with pytest.raises(CaptureHeldError):
        retained.emit("heartbeat", BINDING["owner"], {}, idempotency_key="blocked")
    with pytest.raises(CaptureHeldError):
        help_module.post_attendee_message("Must not be accepted in memory")
    assert workspace.objects == {}

    workspace.blocked = False
    assert retained.collect()["events"] == []
    assert retained.delivery_status()["retention_ready"] is True
    assert retained.emit("heartbeat", BINDING["owner"], {}, idempotency_key="retried") == 1


def test_held_startup_blocks_attendee_bootstrap_and_counter_checkpoint(monkeypatch):
    workspace = WorkspaceFiles()
    workspace.blocked = True
    retained = emitter()
    capture.configure(retained, client=SimpleNamespace(workspace=workspace))

    def forbidden(*_args, **_kwargs):
        pytest.fail("held production capture must refuse work before HOME/OBO bootstrap")

    monkeypatch.setattr(users.User, "bootstrap_home", forbidden)
    with pytest.raises(CaptureHeldError):
        users.user_manager.get(BINDING["owner"])
    with pytest.raises(CaptureHeldError):
        users.user_manager.checkpoint(users.User(BINDING["owner"]))
    assert users.user_manager.all() == []
    assert workspace.objects == {}


@pytest.mark.parametrize("surface", ["events", "sessions"])
def test_held_startup_closes_websockets_before_subscribing(monkeypatch, surface):
    from server import ws

    workspace = WorkspaceFiles()
    workspace.blocked = True
    retained = emitter()
    capture.configure(retained, client=SimpleNamespace(workspace=workspace))
    calls = []

    async def authenticate(_socket):
        return SimpleNamespace(name=BINDING["owner"])

    class Socket:
        async def accept(self):
            calls.append("accepted")

        async def close(self, *, code, reason):
            calls.append((code, reason))

    def forbidden(*_args, **_kwargs):
        pytest.fail("held capture must close before subscribing to live output")

    monkeypatch.setattr(ws, "get_ws_user", authenticate)
    monkeypatch.setattr(ws, "user_manager", users.user_manager)
    monkeypatch.setattr(ws.session_manager, "get", lambda *_args: object())
    monkeypatch.setattr(ws.session_manager, "attach_with_replay", forbidden)
    monkeypatch.setattr(ws.event_hub, "subscribe", forbidden)
    if surface == "events":
        asyncio.run(ws.events_socket(Socket()))
    else:
        asyncio.run(ws.session_socket(Socket(), "existing-session"))
    assert calls == ["accepted", (4413, "workshop capture is held")]


@pytest.mark.parametrize("name,value", [
    ("WORKSHOP_RUN_ID", ""), ("WORKSHOP_UNIT_ID", ""),
    ("DATABRICKS_WORKSPACE_ID", ""), ("WORKSHOP_ATTENDEE_EMAIL", ""),
    ("WORKSHOP_ATTENDEE_EMAIL", "invalid"),
    ("WORKSHOP_ATTENDEE_EMAIL", "bad/name@example.com"),
    ("WORKSHOP_ATTENDEE_EMAIL", "bad\\name@example.com"),
])
def test_missing_or_malformed_binding_holds_collection_and_help(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    workspace = WorkspaceFiles()
    retained = emitter()
    capture.configure(retained, client=SimpleNamespace(workspace=workspace))
    assert retained.delivery_status()["retention_ready"] is False
    with pytest.raises(CaptureHeldError):
        retained.collect()
    with pytest.raises(CaptureHeldError):
        help_module.raise_hand("Unqualified source")
    assert workspace.objects == {}
    assert users.user_manager.all() == []


def test_help_remains_durable_when_insight_consent_is_off(monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "false")
    workspace = WorkspaceFiles()
    workspace.blocked = True
    retained = emitter()
    capture.configure(retained, client=SimpleNamespace(workspace=workspace))
    with pytest.raises(CaptureHeldError):
        help_module.raise_hand("Needs retained help")
    workspace.blocked = False
    monkeypatch.setattr(help_module, "_push_control_tower", lambda *a, **k: False)
    help_module.raise_hand("Now retained")
    assert retained.delivery_status()["retention_ready"] is True
    assert help_module._capture_journal is retained.journal
    assert discovery.discovery_store._capture_emitter is None
    assert identity._capture_emitter is retained
    assert attendee_names._capture_emitter is None
    assert insight_summary.stamps._journal is None
    assert retained.journal.read()["help"]["raised"] is True


def test_known_owner_initializes_without_home_bootstrap_or_obo(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("headless capture startup must not bootstrap a HOME or request OBO")

    from server import obo

    monkeypatch.setattr(users.User, "bootstrap_home", forbidden)
    monkeypatch.setattr(obo.obo_manager, "user_ready", forbidden)
    retained = emitter()
    capture.configure(retained, client=SimpleNamespace(workspace=WorkspaceFiles()))
    assert retained.delivery_status()["retention_ready"] is True
    [owner] = users.user_manager.all()
    assert owner.email == BINDING["owner"]
    assert owner.first_seen == owner.last_seen == 0
    assert not Path(owner.home).exists()


def test_lifespan_attaches_capture_before_session_restore_and_producers(monkeypatch, tmp_path):
    from server import main

    order = []
    sdk = SimpleNamespace(workspace=WorkspaceFiles())
    retained = emitter()
    real_configure = capture.configure

    def configure(selected):
        order.append("capture")
        real_configure(selected, client=sdk)
        assert selected.delivery_status()["retention_ready"] is True

    def restore(_store):
        order.append("sessions")
        assert users.user_manager.peek(BINDING["owner"]) is not None
        assert retained.journal.read()["help"]["seq"] == 0

    monkeypatch.setattr(main.config, "local_dev", lambda: False)
    monkeypatch.setattr(main.config, "session_state_path", lambda: str(tmp_path / "sessions.json"))
    monkeypatch.setattr(main, "event_emitter", retained)
    monkeypatch.setattr(main, "initialize_app_identity", lambda: order.append("identity"))
    monkeypatch.setattr(capture, "configure", configure)
    monkeypatch.setattr(main.session_manager, "configure_store", restore)
    monkeypatch.setattr(main.install, "run_in_background", lambda: order.append("install"))
    monkeypatch.setattr(main, "install_app_error_journal", lambda: None)
    monkeypatch.setattr(main, "_start_control_tower_threads", lambda *_args: order.append("producers") or [])
    monkeypatch.setattr(main, "_stop_control_tower_threads", lambda *_args: None)
    for service in (main.remote_host_manager, main.obo.obo_watcher, main.log_collector,
                    main.credential_manager, main.entitlement_manager):
        for method in ("start", "stop"):
            if hasattr(service, method):
                monkeypatch.setattr(service, method, lambda: None)

    async def exercise():
        async with main.lifespan(main.app):
            assert order == ["identity", "capture", "sessions", "install", "producers"]

    asyncio.run(exercise())
