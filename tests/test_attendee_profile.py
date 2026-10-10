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
    assert "not stated an experience level" in user_content._persona_overlay(attendee)


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
    assert "keep it concise" in user_content._persona_overlay(attendee)


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
