"""Product preferences and goals survive losing HOME without stale overwrites."""

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import attendee_profile, attendee_state, capture, config, discovery, users, wizard
from server.capture_journal import CaptureHeldError, CaptureJournal, CaptureStorageError, WorkspaceCaptureStorage
from server.closeout_guard import CaptureClosedError, admitted_writer, control_transaction, initial_state
from .test_capture_startup import BINDING, WorkspaceFiles, capture_modules, emitter  # noqa: F401
from .test_wizard_integrity import choose

OWNER = {"X-Forwarded-Email": BINDING["owner"]}


def attach(monkeypatch, sdk):
    from server import event_emitter, main

    monkeypatch.setattr(users, "user_manager", users.UserManager())
    from server.event_emitter import EventEmitter
    binding = capture.source_binding()
    retained = EventEmitter(run_id=binding["run_id"], workspace_id=binding["workspace_id"],
                            ingest_url="", ingest_token="")
    monkeypatch.setattr(event_emitter, "event_emitter", retained)
    monkeypatch.setattr(main, "event_emitter", retained)
    capture.configure(retained, client=sdk)
    return retained, users.user_manager.peek(BINDING["owner"])


@pytest.fixture
def product_http(client, monkeypatch):
    from server import obo, user_content

    monkeypatch.setattr(users.User, "bootstrap_home", lambda _user: None)
    monkeypatch.setattr(obo.obo_manager, "user_ready", lambda _user: None)
    monkeypatch.setattr(user_content, "_write_instructions", lambda _user: None)
    monkeypatch.setattr(wizard.demo_data, "industries", lambda: [])
    monkeypatch.setattr(wizard.demo_data, "offered_industries", lambda: [])
    monkeypatch.setattr(wizard, "select_ideas", lambda *_a, **_k: [])
    return client


def test_http_saved_full_snapshots_survive_empty_replacement_home(product_http, monkeypatch, tmp_path):
    sdk = SimpleNamespace(workspace=WorkspaceFiles())
    retained, user = attach(monkeypatch, sdk)
    profile = product_http.post("/api/profile", headers=OWNER,
        json={"help_preference": "concise", "expected_revision": 0})
    assert profile.status_code == 200, profile.text
    offered = choose(user)
    response = product_http.post("/api/wizard", headers=OWNER, json={
        "operation": "complete", "expected_revision": 0,
        "what_building": "Help bakery staff with orders", "industry": "retail",
        "industry_stated": True, "intent": "business_problem", "current_stack": ["Spreadsheets"],
        "idea_id": offered["id"], "selection_token": offered["selection_token"],
    })
    assert response.status_code == 200, response.text
    saved = response.json()["brief"]
    original_events = retained.collect()["events"]
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "completely-empty-replacement"))
    discovery.discovery_store = discovery.DiscoveryStore()
    replacement, restored = attach(monkeypatch, sdk)
    assert not Path(restored.home).exists()
    assert product_http.get("/api/profile", headers=OWNER).json() == profile.json()
    recovered = product_http.get("/api/wizard", headers=OWNER)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["brief"] == saved
    assert saved["selected_idea"]["source"] == "generated"
    assert saved["selection_token"] == offered["selection_token"]
    assert wizard.starter_prompt(wizard.read_brief(restored)) == response.json()["starter_prompt"]
    assert replacement.collect()["events"] == original_events
    assert replacement.journal.audit()["attendee_product_state"]["documents"] == {
        "profile": profile.json(), "brief": saved,
    }


@pytest.mark.parametrize("operation", ["draft", "skip"])
def test_unfinished_and_skipped_goals_survive_without_optional_discovery(monkeypatch, tmp_path, operation):
    monkeypatch.setattr(config, "discovery_enabled", lambda: False)
    sdk = SimpleNamespace(workspace=WorkspaceFiles())
    _, user = attach(monkeypatch, sdk)
    saved = wizard.save(user, {"operation": operation, "what_building": "An unfinished rota",
                               "expected_revision": 0})
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "replacement"))
    _, restored = attach(monkeypatch, sdk)
    assert wizard.read_brief(restored, strict=True).to_json() == saved.to_json()
    assert saved.stage == ("skipped" if operation == "skip" else "draft")


def test_remote_snapshot_overrides_corrupt_local_cache(monkeypatch):
    _, user = attach(monkeypatch, SimpleNamespace(workspace=WorkspaceFiles()))
    saved = attendee_profile.save(user, "guided", expected_revision=0)
    Path(attendee_profile.path(user)).write_text('{"broken')
    assert attendee_profile.read(user) == saved
    assert json.loads(Path(attendee_profile.path(user)).read_text()) == saved.to_json()


def test_new_source_never_imports_unbound_previous_home(monkeypatch):
    sdk = SimpleNamespace(workspace=WorkspaceFiles())
    _, user = attach(monkeypatch, sdk)
    attendee_profile.save(user, "technical", expected_revision=0)
    wizard.save(user, {"operation": "draft", "what_building": "Previous run goal"})
    previous_home = user.home
    monkeypatch.setenv("WORKSHOP_RUN_ID", BINDING["run_id"] + "-next")
    _, next_user = attach(monkeypatch, sdk)
    assert next_user.home == previous_home
    assert attendee_profile.read(next_user) == attendee_profile.Profile()
    assert wizard.read_brief(next_user) == wizard.WizardBrief()
    assert "attendee_product_state" not in next_user._capture_journal.read()


class ProductFailingFiles(WorkspaceFiles):
    fail_product = False
    fail_reads = False

    def list(self, path):
        if self.fail_reads:
            raise PermissionError("unavailable remote journal")
        return super().list(path)

    def upload(self, path, content, **kwargs):
        doc = json.loads(content)
        if self.fail_product and any(row["path"] and row["path"][0] == "attendee_product_state"
                                     for row in doc.get("patch", [])):
            raise PermissionError("product commit unavailable")
        return super().upload(path, content, **kwargs)


def test_storage_failure_never_reaches_the_attendee_and_retains_after_recovery(product_http, monkeypatch):
    files = ProductFailingFiles()
    retained, user = attach(monkeypatch, SimpleNamespace(workspace=files))
    accepted = attendee_profile.save(user, "guided", expected_revision=0)
    retained.journal.flush()
    files.fail_product = True
    response = product_http.post("/api/profile", headers=OWNER,
        json={"help_preference": "technical", "expected_revision": accepted.revision})
    # A storage blip is never the attendee's problem: accepted in memory.
    assert response.status_code == 200, response.text
    with pytest.raises(CaptureStorageError):
        retained.journal.flush()
    assert retained.journal.health()["pending_operations"] >= 1
    files.fail_product = False
    retained.journal.flush()
    assert retained.journal.health()["pending_operations"] == 0
    files.fail_reads = True
    assert product_http.get("/api/profile", headers=OWNER).json()["help_preference"] == "technical"
    files.fail_reads = False
    _, restored = attach(monkeypatch, SimpleNamespace(workspace=files))
    assert attendee_profile.read(restored).help_preference == "technical"


@pytest.mark.parametrize("endpoint,payload,readback", [
    ("profile", {"help_preference": "technical", "expected_revision": 0}, "help_preference"),
    ("wizard", {"what_building": "Saved bakery goal", "expected_revision": 0}, "what_building"),
])
@pytest.mark.parametrize("failure", [OSError, CaptureStorageError])
def test_instruction_refresh_failure_returns_durable_acceptance(
    product_http, monkeypatch, endpoint, payload, readback, failure,
):
    from server import user_content

    _, user = attach(monkeypatch, SimpleNamespace(workspace=WorkspaceFiles()))

    def unavailable(_user):
        raise failure("generated instruction cache unavailable")

    monkeypatch.setattr(user_content, "_write_instructions", unavailable)
    response = product_http.post("/api/" + endpoint, headers=OWNER, json=payload)
    assert response.status_code == 200, response.text
    accepted = response.json() if endpoint == "profile" else response.json()["brief"]
    assert accepted[readback] == payload[readback]
    stored = attendee_profile.read(user) if endpoint == "profile" else wizard.read_brief(user)
    assert stored.to_json() == accepted


def test_cache_write_failure_does_not_reject_remote_snapshot(monkeypatch):
    _, user = attach(monkeypatch, SimpleNamespace(workspace=WorkspaceFiles()))

    def unavailable(*_args):
        raise OSError("disposable home is unavailable")

    monkeypatch.setattr(attendee_state, "write_json", unavailable)
    accepted = attendee_profile.save(user, "concise", expected_revision=0)
    assert attendee_profile.read(user) == accepted
    assert user._capture_journal.read()["attendee_product_state"]["documents"]["profile"] == accepted.to_json()


def test_cross_container_concurrent_revision_checks_happen_at_remote_commit(monkeypatch):
    class ContendedFiles(WorkspaceFiles):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.barrier = threading.Barrier(2)
            self.attempts = 0

        def upload(self, path, content, **kwargs):
            product = any(row["path"] and row["path"][0] == "attendee_product_state"
                          for row in json.loads(content).get("patch", []))
            if product:
                with self.lock:
                    self.attempts += 1
                    wait = self.attempts <= 2
                if wait:
                    self.barrier.wait(timeout=5)
            with self.lock:
                return super().upload(path, content, **kwargs)

    sdk = SimpleNamespace(workspace=ContendedFiles())
    _, first = attach(monkeypatch, sdk)
    second = SimpleNamespace(email=first.email, home=first.home + "-other-container",
        _capture_journal=CaptureJournal(WorkspaceCaptureStorage(sdk, capture.workspace_path(BINDING)), BINDING, {}))

    def save(pair):
        user, preference = pair
        try:
            return attendee_profile.save(user, preference, expected_revision=0)
        except attendee_profile.ProfileConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, [(first, "guided"), (second, "technical")]))
    accepted = [row for row in outcomes if isinstance(row, attendee_profile.Profile)]
    rejected = [row for row in outcomes if isinstance(row, attendee_profile.ProfileConflict)]
    assert len(accepted) == len(rejected) == 1
    assert rejected[0].profile == accepted[0]
    assert attendee_profile.read(first) == attendee_profile.read(second) == accepted[0]


@pytest.mark.parametrize("malformed", [None,
    {"schema_version": 1, "owner": "foreign@example.com", "documents": {}},
    {"schema_version": 1, "owner": BINDING["owner"], "documents": {"profile": {"revision": "1"}}},
    {"schema_version": 1, "owner": BINDING["owner"], "documents": {"brief": {"stage": "other"}}},
])
def test_malformed_retained_product_holds_restart_reads_and_writes(monkeypatch, malformed):
    sdk = SimpleNamespace(workspace=WorkspaceFiles())
    retained, _ = attach(monkeypatch, sdk)
    retained.journal.transaction(lambda state: state.update(attendee_product_state=malformed))
    replacement, user = attach(monkeypatch, sdk)
    assert replacement.delivery_status()["retention_ready"] is False
    for action in (lambda: attendee_profile.read(user),
                   lambda: wizard.read_brief(user),
                   lambda: attendee_profile.save(user, "guided", expected_revision=0)):
        with pytest.raises(CaptureHeldError):
            action()


@pytest.mark.parametrize("phase", ["CLOSING", "SEALED"])
def test_closed_epoch_readbacks_are_remote_read_only_and_writes_refuse(monkeypatch, phase):
    sdk = SimpleNamespace(workspace=WorkspaceFiles())
    retained, user = attach(monkeypatch, sdk)
    accepted = attendee_profile.save(user, "guided", expected_revision=0)
    control_transaction(retained.journal, lambda state: state.update(closeout={**initial_state(), "phase": phase}))
    replacement, restored = attach(monkeypatch, sdk)
    before = copy.deepcopy(sdk.workspace.objects)
    assert attendee_profile.read(restored) == accepted
    assert wizard.read_brief(restored) == wizard.WizardBrief()
    assert sdk.workspace.objects == before
    with pytest.raises(CaptureClosedError):
        attendee_profile.save(restored, "concise", expected_revision=accepted.revision)
    assert replacement.journal.read()["closeout"]["phase"] == phase


def test_previously_admitted_writer_can_finish_product_during_closing(monkeypatch):
    retained, user = attach(monkeypatch, SimpleNamespace(workspace=WorkspaceFiles()))
    ticket = admitted_writer(retained.journal, "test:accepted-product")
    control_transaction(retained.journal, lambda state: state["closeout"].update(phase="CLOSING"))
    with ticket:
        saved = attendee_profile.save(user, "guided", expected_revision=0)
    assert attendee_profile.read(user) == saved
    with pytest.raises(CaptureClosedError):
        attendee_profile.save(user, "technical", expected_revision=saved.revision)


def test_production_refuses_missing_or_volatile_product_journal(monkeypatch, tmp_path):
    from server.capture_journal import MemoryCaptureStorage

    monkeypatch.setattr(config, "local_dev", lambda: False)
    user = SimpleNamespace(email=BINDING["owner"], home=str(tmp_path))
    for journal in (None, CaptureJournal(MemoryCaptureStorage(), BINDING, {})):
        user._capture_journal = journal
        with pytest.raises(CaptureHeldError):
            attendee_profile.read(user)
        with pytest.raises(CaptureHeldError):
            wizard.save(user, {"what_building": "Must not be accepted"})
    assert not (tmp_path / ".workshop").exists()
