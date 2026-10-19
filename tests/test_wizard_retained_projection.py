"""A delayed optional projection cannot supersede a newer accepted goal."""

from types import SimpleNamespace

import pytest

from server import attendee_state, config, discovery, event_emitter, wizard
from server.capture_journal import CaptureJournal, CaptureStorageError, MemoryCaptureStorage
from server.event_emitter import EventEmitter


@pytest.fixture
def retained_goal(tmp_path, monkeypatch):
    owner = "attendee@example.invalid"
    journal = CaptureJournal(MemoryCaptureStorage(), {
        "run_id": "run", "unit_id": "unit", "workspace_id": "workspace", "owner": owner,
    })
    emitter = EventEmitter(run_id="run", workspace_id="workspace", ingest_url="",
                           ingest_token="", journal=journal)
    store = discovery.DiscoveryStore()
    store.configure_capture(emitter)
    monkeypatch.setattr(config, "discovery_enabled", lambda: True)
    monkeypatch.setattr(discovery, "discovery_store", store)
    monkeypatch.setattr(event_emitter, "event_emitter", emitter)
    return SimpleNamespace(email=owner, home=str(tmp_path), _capture_journal=journal), store


def test_delayed_goal_projection_cannot_supersede_newer_accepted_goal(retained_goal, monkeypatch):
    user, store = retained_goal
    original = discovery.record
    nested = False

    def publish(attendee, raw, *args, **kwargs):
        nonlocal nested
        if not nested:
            nested = True
            wizard.save(user, {"what_building": "Newer accepted goal"})
        return original(attendee, raw, *args, **kwargs)

    monkeypatch.setattr(discovery, "record", publish)
    wizard.save(user, {"what_building": "Older delayed goal"})

    assert wizard.read_brief(user).what_building == "Newer accepted goal"
    records = store.for_attendee(user.email)
    assert [(row.use_case_title, row.revision) for row in records] == [("Newer accepted goal", 1)]
    events = user._capture_journal.read()["insight"]["events"].values()
    assert [row["payload"]["use_case_title"] for row in events] == ["Newer accepted goal"]


def test_projection_fence_is_rechecked_after_remote_commit_contention(retained_goal, monkeypatch):
    user, store = retained_goal
    # Leave only the authoritative primary goal, then arrange a competing
    # container commit after the old projection has prepared its candidate.
    monkeypatch.setattr(config, "discovery_enabled", lambda: False)
    older = wizard.save(user, {"what_building": "Older prepared goal"})
    monkeypatch.setattr(config, "discovery_enabled", lambda: True)
    storage = user._capture_journal.storage
    other = CaptureJournal(storage, user._capture_journal.binding)
    newer = wizard.WizardBrief.from_json(older.to_json())
    newer.what_building = "Newer competing goal"
    newer.revision += 1
    original_create = storage.create
    competed = False

    def create(name, content):
        nonlocal competed
        if not competed:
            competed = True
            other.transaction(lambda state: state["attendee_product_state"]["documents"].__setitem__(
                "brief", newer.to_json()))
        return original_create(name, content)

    monkeypatch.setattr(storage, "create", create)
    stored = discovery.record(user.email, wizard.to_discovery(older), expected_brief=older.to_json())

    assert competed
    assert stored is None
    assert wizard.read_brief(user).what_building == "Newer competing goal"
    assert store.for_attendee(user.email) == []
    assert user._capture_journal.read()["insight"]["events"] == {}


def test_optional_receipt_failure_does_not_reject_already_retained_goal(retained_goal, monkeypatch):
    user, store = retained_goal
    original = attendee_state.update_document
    calls = 0

    def update(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CaptureStorageError("optional receipt unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(attendee_state, "update_document", update)
    saved = wizard.save(user, {"what_building": "Accepted goal"})

    assert calls == 2
    assert saved.what_building == "Accepted goal"
    assert wizard.read_brief(user).what_building == "Accepted goal"
    assert store.for_attendee(user.email)[0].use_case_title == "Accepted goal"


def test_optional_projection_read_failure_does_not_reject_retained_goal(retained_goal, monkeypatch):
    user, store = retained_goal

    def unavailable(_attendee):
        raise CaptureStorageError("optional discovery read unavailable")

    monkeypatch.setattr(store, "for_attendee", unavailable)
    saved = wizard.save(user, {"what_building": "Accepted without optional projection"})

    assert saved.what_building == "Accepted without optional projection"
    assert wizard.read_brief(user).to_json() == saved.to_json()
