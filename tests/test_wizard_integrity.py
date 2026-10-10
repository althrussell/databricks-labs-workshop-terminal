"""The same offered task must survive save/reload and independent writers."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from server import config, discovery, user_content, wizard, wizard_selections


@pytest.fixture
def attendee(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "discovery_enabled", lambda: False)
    monkeypatch.setattr(wizard_selections.demo_data, "data_ready", lambda _: True)
    return SimpleNamespace(email="attendee@example.invalid", home=str(tmp_path))


def card():
    return {
        "id": "auto-part-failures", "label": "Bakery order queue",
        "outcome": "Staff can see and pack today's bakery orders.",
        "prompt": "Build an order queue for bakery staff with a packed action and remembered updates.",
        "industries": ["bakery"], "intents": ["business_problem"],
        "products": ["Databricks Apps", "Lakebase"], "shape": "app",
        "technical": False, "demo_tables": [], "data_mode": "generate",
        "data_ready": False, "first_version": "A queue and one packed action",
        "assumptions": ["Use clearly labelled sample orders for the demo"],
    }


def choose(attendee, *, source="generated"):
    return wizard_selections.offer(attendee, card(), source=source, model="approved-test-model")


def save_choice(attendee, offered, **fields):
    return wizard.save(attendee, {
        "operation": "complete", "expected_revision": 0,
        "idea_id": offered["id"], "selection_token": offered["selection_token"],
        "what_building": "Help my bakery staff with orders", "industry": "retail",
        "industry_stated": True, **fields,
    })


@pytest.mark.parametrize("source", ["generated", "catalog"])
def test_selected_task_is_immutable_and_authorship_survives_reload(attendee, monkeypatch, source):
    offered = choose(attendee, source=source)
    original = copy.deepcopy(offered)
    offered["prompt"] = "An altered browser copy must not become authoritative"
    brief = save_choice(attendee, offered)
    assert brief.selected_idea["prompt"] == original["prompt"]
    assert brief.what_building == "Help my bakery staff with orders"
    assert brief.stated_industry == "retail"
    assert brief.selected_idea["industries"] == ["bakery"]
    assert brief.selected_idea["source"] == source
    assert brief.revision == 1
    restored = wizard.read_brief(attendee, strict=True)
    assert restored.to_json() == brief.to_json()

    def mutable_lookup_is_forbidden(*_):
        raise AssertionError("Saved launch must not re-resolve a catalog ID")

    monkeypatch.setattr(wizard, "idea_by_id", mutable_lookup_is_forbidden)
    prompt = wizard.starter_prompt(restored)
    assert original["prompt"] in prompt
    assert "Help my bakery staff" in prompt
    assert "generate the data you need" not in prompt
    overlay = user_content._wizard_overlay(attendee)
    assert original["prompt"] in overlay
    assert "selected suggestions are not attendee-authored words" in overlay
    assert "already exists" not in overlay
    assert wizard.launch_context(restored)["selected_idea"] == brief.selected_idea
    assert "selection_token" not in wizard.launch_context(restored)


def test_bound_data_handoff_survives_selection_reload_and_launch(attendee, monkeypatch):
    from server import wizard_llm
    monkeypatch.setattr(wizard_llm.demo_data, "verify", lambda _: True)
    monkeypatch.setattr(wizard_llm.demo_data, "supports", lambda *_a, **_k: True)
    raw = card() | {
        "demo_tables": ["retail.orders"], "data_mode": "demo",
        "required_columns": [{"table": "retail.orders", "columns": ["order_id"]}],
        "fit_reason": "Use workshop samples; real bakery records are not connected.", "unresolved": [],
    }
    parsed = wizard_llm._coerce_idea(raw, "retail")
    assert parsed is not None
    offered = wizard_selections.offer(attendee, wizard.idea_payload(parsed), source="generated", model="approved-test-model")
    exact_handoff = offered["prompt"]
    assert raw["prompt"] in exact_handoff and "retail.orders" in exact_handoff
    assert "Only table/column metadata is checked" in exact_handoff
    saved = save_choice(attendee, offered, industry="bakery", industry_stated=True)
    monkeypatch.setattr(wizard, "idea_by_id", lambda *_a: pytest.fail("Must use the saved offered snapshot"))
    restored = wizard.read_brief(attendee, strict=True)
    assert restored.selected_idea == saved.selected_idea
    assert restored.selected_idea["prompt"] == exact_handoff
    assert restored.stated_industry == "bakery"
    assert restored.selected_idea["demo_tables"] == ["retail.orders"]
    assert exact_handoff in wizard.starter_prompt(restored)
    assert wizard.launch_context(restored)["selected_idea"]["prompt"] == exact_handoff


def test_raw_generated_id_cannot_collide_into_a_static_task(attendee):
    with pytest.raises(wizard_selections.SelectionError, match="fresh selection"):
        wizard.save(attendee, {"idea_id": "auto-part-failures", "what_building": "Bakery queue"})
    assert wizard.read_brief(attendee).revision == 0


def test_foreign_expired_and_mismatched_receipts_are_recoverable(attendee, tmp_path, monkeypatch):
    offered = choose(attendee)
    other = SimpleNamespace(email="other@example.invalid", home=str(tmp_path / "other"))
    with pytest.raises(wizard_selections.SelectionError):
        save_choice(other, offered)
    with pytest.raises(wizard_selections.SelectionError, match="does not match"):
        save_choice(attendee, offered, idea_id="catalog:another:task")
    monkeypatch.setattr(wizard_selections.time, "time", lambda: offered["selection_expires_at"] + 1)
    with pytest.raises(wizard_selections.SelectionError, match="expired"):
        save_choice(attendee, offered)
    assert wizard.read_brief(attendee).revision == 0


def test_committed_selection_outlives_offer_expiry_without_changing_task(attendee, monkeypatch):
    offered = choose(attendee)
    brief = save_choice(attendee, offered)
    monkeypatch.setattr(wizard_selections.time, "time", lambda: offered["selection_expires_at"] + 1)
    updated = wizard.save(attendee, {"expected_revision": brief.revision,
                                    "idea_id": offered["id"], "current_stack": ["Spreadsheets"]})
    assert updated.selected_idea == brief.selected_idea
    assert updated.record_id == brief.record_id


def test_missing_declared_data_requires_reselection(attendee, monkeypatch):
    payload = card() | {"demo_tables": ["retail.orders"], "data_mode": "demo"}
    offered = wizard_selections.offer(attendee, payload, source="generated")
    monkeypatch.setattr(wizard_selections.demo_data, "data_ready", lambda _: False)
    with pytest.raises(wizard_selections.SelectionError, match="data available"):
        save_choice(attendee, offered)


def test_stale_tabs_do_not_overwrite_and_parallel_first_saves_have_one_identity(attendee):
    def submit(words):
        try:
            return wizard.save(attendee, {"expected_revision": 0, "what_building": words})
        except wizard.BriefConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, ["Bakery queue", "Staff rota"]))
    accepted = [row for row in outcomes if isinstance(row, wizard.WizardBrief)]
    rejected = [row for row in outcomes if isinstance(row, wizard.BriefConflict)]
    assert len(accepted) == len(rejected) == 1
    assert rejected[0].brief.record_id == accepted[0].record_id
    assert wizard.read_brief(attendee).to_json() == accepted[0].to_json()


def test_draft_clear_and_text_edit_have_explicit_selection_semantics(attendee):
    offered = choose(attendee)
    brief = save_choice(attendee, offered)
    draft = wizard.save(attendee, {"expected_revision": brief.revision, "operation": "draft",
                                  "what_building": "I want a staff rota instead"})
    assert draft.selected_idea is None
    assert draft.idea_id == ""
    assert draft.stage == "draft"
    assert not wizard.starter_prompt(draft)
    completed = wizard.save(attendee, {"expected_revision": draft.revision, "operation": "complete"})
    assert "staff rota" in wizard.starter_prompt(completed)
    closed = wizard.save(attendee, {"operation": "skip", "expected_revision": completed.revision})
    assert closed.to_json() == completed.to_json()
    cleared = wizard.save(attendee, {"operation": "clear", "expected_revision": closed.revision})
    assert not cleared.has_content
    assert not wizard.starter_prompt(cleared)
    assert user_content._wizard_overlay(attendee) == ""


def test_corrupt_saved_goal_is_not_silently_replaced(attendee):
    path = wizard.brief_path(attendee)
    from pathlib import Path
    Path(path).parent.mkdir(parents=True)
    Path(path).write_text("{broken")
    with pytest.raises(wizard.BriefReadError):
        wizard.save(attendee, {"what_building": "Something else"})
    assert Path(path).read_text() == "{broken"


def test_explicit_no_industry_survives_reload_under_a_room_default(attendee, monkeypatch):
    monkeypatch.setattr(wizard, "default_industry", lambda: "automotive_mobility")
    brief = wizard.save(attendee, {"what_building": "Bakery order queue", "industry": "", "industry_stated": False})
    state = wizard.state(attendee)
    assert state["brief"]["industry"] == ""
    assert all(not idea["industries"] for idea in state["ideas"])
    assert brief.revision == 1


def test_legacy_selected_outcome_is_not_relabelled_as_attendee_authorship(attendee):
    from pathlib import Path
    path = Path(wizard.brief_path(attendee))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"record_id": "legacy", "idea_id": "old-generated-id", "what_building": "Model-authored old outcome", "seen": True}))
    restored = wizard.read_brief(attendee, strict=True)
    assert restored.words_source == "legacy_unverified"
    assert "In their own words" not in user_content._wizard_overlay(attendee)
    assert "legacy authorship unverified" in user_content._wizard_overlay(attendee)
    assert "needs review" in wizard.starter_prompt(restored)


def test_new_project_identity_does_not_reuse_the_previous_discovery_record(attendee):
    initial = wizard.save(attendee, {"what_building": "Bakery orders"})
    changed = wizard.save(attendee, {"operation": "change", "expected_revision": initial.revision, "what_building": "Staff rota"})
    assert changed.record_id != initial.record_id and changed.revision == 2
    assert changed.stage == "complete" and changed.what_building == "Staff rota"


def test_capture_enable_resave_is_truthful_and_partial_writers_keep_other_fields(attendee, monkeypatch):
    discovery.discovery_store.clear()
    brief = wizard.save(attendee, {"what_building": "Bakery queue"})
    assert not brief.discovery_record_id
    monkeypatch.setattr(config, "discovery_enabled", lambda: True)
    captured = wizard.save(attendee, {"expected_revision": brief.revision})
    assert captured.discovery_record_id == brief.record_id
    discovery.record(attendee.email, {"record_id": brief.record_id, "blockers": ["No production orders yet"]})
    updated = wizard.save(attendee, {"what_building": "Bakery queue with a packed action", "expected_revision": captured.revision})
    row = discovery.discovery_store.for_attendee(attendee.email)[0]
    assert row.blockers == ["No production orders yet"]
    assert row.goal == updated.what_building
    assert row.use_case_title == updated.what_building
    assert updated.discovery_record_id == row.record_id
    assert json.loads(json.dumps(wizard.launch_context(updated)))["discovery_record_id"] == row.record_id
    discovery.discovery_store.clear()
