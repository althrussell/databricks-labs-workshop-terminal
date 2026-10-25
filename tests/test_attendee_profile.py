"""An optional preference cannot rewrite the product goal or infer expertise."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from server import attendee_profile, config, user_content, wizard


@pytest.fixture
def attendee(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "discovery_enabled", lambda: False)
    return SimpleNamespace(email="synthetic@example.invalid", home=str(tmp_path))


def test_seeded_business_persona_does_not_establish_a_preference(attendee):
    persona = Path(attendee.home, ".workshop", "persona")
    persona.parent.mkdir()
    persona.write_text("business\n")
    profile = attendee_profile.read(attendee)
    assert profile.help_preference == "" and profile.source == "default"
    overlay = user_content._persona_overlay(attendee)
    assert "(default; they have not chosen one)" in overlay and "adapt explanation detail" in overlay
    assert "plain-language" not in overlay and "technical discussion" not in overlay


def test_explicit_legacy_style_migrates_conservatively(attendee):
    persona = Path(attendee.home, ".workshop", "persona")
    persona.parent.mkdir()
    persona.write_text("technical\n")
    profile = attendee_profile.read(attendee)
    assert profile.help_preference == "technical" and profile.source == "legacy_persona"
    migrated = attendee_profile.save(attendee, "technical", expected_revision=0)
    assert migrated.source == "attendee" and migrated.revision == 1


def test_new_profile_wins_over_old_brief_persona(attendee, monkeypatch):
    brief = wizard.save(attendee, {"what_building": "Bakery orders", "persona": "technical"})
    attendee_profile.save(attendee, "concise", expected_revision=0)
    monkeypatch.setattr(user_content, "_write_instructions", lambda _: None)
    user_content.set_wizard_brief(attendee, brief)
    assert attendee_profile.read(attendee).help_preference == "concise"
    assert wizard.read_brief(attendee).to_json() == brief.to_json()
    # Legacy "concise" migrates to brief explanations and proves nothing else.
    overlay = user_content._persona_overlay(attendee)
    assert "keep explanations brief" in overlay and "(default; they have not chosen one)" in overlay


def test_stale_preference_conflict_and_explicit_reset(attendee):
    current = attendee_profile.save(attendee, "guided", expected_revision=0)
    with pytest.raises(attendee_profile.ProfileConflict) as caught:
        attendee_profile.save(attendee, "technical", expected_revision=0)
    assert caught.value.profile == current
    reset = attendee_profile.save(attendee, "", expected_revision=current.revision)
    assert reset.source == "attendee" and reset.help_preference == ""


def test_corrupt_profile_is_preserved_and_read_error_is_actionable(attendee):
    path = Path(attendee_profile.path(attendee))
    path.parent.mkdir()
    path.write_text('{"bad')
    with pytest.raises(attendee_profile.ProfileReadError):
        attendee_profile.save(attendee, "concise", expected_revision=0)
    assert path.read_text() == '{"bad'


def test_parallel_first_saves_are_idempotent_across_processes(attendee):
    script = '''import json, sys
from types import SimpleNamespace
from server import config, wizard
config.discovery_enabled = lambda: False
user = SimpleNamespace(email="synthetic@example.invalid", home=sys.argv[1])
print(json.dumps(wizard.save(user, {"what_building":"Bakery order queue", "expected_revision":0}).to_json()))
'''
    processes = [subprocess.Popen([sys.executable, "-c", script, attendee.home], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=os.environ.copy()) for _ in range(2)]
    results = []
    for process in processes:
        output, errors = process.communicate(timeout=10)
        assert process.returncode == 0, errors
        results.append(json.loads(output))
    assert results[0] == results[1]
    assert results[0]["revision"] == 1
    assert wizard.read_brief(attendee, strict=True).to_json() == results[0]


def test_selected_snapshot_survives_a_fresh_python_process(attendee):
    from .test_wizard_integrity import choose, save_choice
    offered = choose(attendee)
    saved = save_choice(attendee, offered)
    script = '''import json, sys
from types import SimpleNamespace
from server import wizard
brief = wizard.read_brief(SimpleNamespace(email="synthetic@example.invalid", home=sys.argv[1]), strict=True)
print(json.dumps({"brief":brief.to_json(),"prompt":wizard.starter_prompt(brief)}))
'''
    result = subprocess.run([sys.executable, "-c", script, attendee.home], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    restored = json.loads(result.stdout)
    assert restored["brief"] == saved.to_json()
    assert restored["prompt"] == wizard.starter_prompt(saved)



def test_preferences_are_independent_dimensions_with_provenance(attendee):
    saved = attendee_profile.save(attendee, None, expected_revision=0, changes={
        "working_approach": "learn", "explanation_detail": "brief"}, choice_source="chat")
    assert saved.revision == 1 and saved.help_preference == ""
    assert saved.choice_sources == {"working_approach": "chat", "explanation_detail": "chat"}
    # Changing explanation style never touches the approach, the path or the goal.
    detail = attendee_profile.save(attendee, None, expected_revision=1, changes={"explanation_detail": "detailed"})
    assert detail.working_approach == "learn" and detail.engagement_path == ""
    assert detail.choice_sources["explanation_detail"] == "ui"
    overlay = user_content._persona_overlay(attendee)
    assert "Teach me while we build" in overlay and "explain in detail" in overlay
    assert "default; they have not chosen" not in overlay


def test_technical_plus_guided_ideation_and_concise_plus_teaching_are_both_expressible(attendee):
    attendee_profile.save(attendee, "technical", expected_revision=0,
                          changes={"engagement_path": "guided", "working_approach": "lead"})
    overlay = user_content._persona_overlay(attendee)
    assert "technical discussion" in overlay and "Default path for a new idea: guided" in overlay
    assert "Let me drive" in overlay
    attendee_profile.save(attendee, "concise", expected_revision=1, changes={"working_approach": "learn"})
    overlay = user_content._persona_overlay(attendee)
    assert "keep explanations brief" in overlay and "Teach me while we build" in overlay


@pytest.mark.parametrize("legacy, expected", [
    ("concise", {"explanation_detail": "brief", "working_approach": "co_build"}),
    ("technical", {"explanation_detail": "adaptive", "technical_vocabulary": True}),
    ("guided", {"explanation_detail": "adaptive", "plain_language": True, "engagement_path": ""}),
])
def test_legacy_preferences_migrate_conservatively(attendee, legacy, expected):
    attendee_profile.save(attendee, legacy, expected_revision=0)
    effective = attendee_profile.read(attendee).effective()
    for key, value in expected.items():
        assert effective[key] == value
    # None of them is a stated working approach or consent to a guided interview.
    assert effective["working_approach_stated"] is False and effective["engagement_path"] == ""


def test_unknown_dimension_values_and_a_stale_write_are_refused(attendee):
    with pytest.raises(ValueError):
        attendee_profile.save(attendee, None, expected_revision=0, changes={"working_approach": "boss"})
    with pytest.raises(ValueError):
        attendee_profile.save(attendee, None, expected_revision=0, changes={"expertise": "high"})
    attendee_profile.save(attendee, None, expected_revision=0, changes={"working_approach": "lead"})
    with pytest.raises(attendee_profile.ProfileConflict):
        attendee_profile.save(attendee, None, expected_revision=0, changes={"working_approach": "learn"})


def test_an_older_release_reads_a_newer_profile(attendee):
    """Additive fields on schema 1: rollback to a release without them keeps working."""
    saved = attendee_profile.save(attendee, "concise", expected_revision=0, changes={"working_approach": "learn"})
    legacy_fields = {"schema_version", "revision", "help_preference", "source", "legacy_persona"}
    legacy_view = {k: v for k, v in saved.to_json().items() if k in legacy_fields}
    assert attendee_profile.from_json(legacy_view).help_preference == "concise"
