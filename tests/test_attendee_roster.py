"""The industry handover from Control Tower, and what the agent does with it (§1).

Control Tower pushes what its roster knows about a seat's owner to
``POST /api/admin/attendee-profile``. These tests pin the contract CT builds
against: the admin gate, the validation, revisions, the attendee's own answer
winning, what the meeting says, what ``workshop-workflow show`` prints, and the
discovery helper that keeps one build on one record (DISC-3).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "assets" / "bin" / "workshop-workflow"
DISCOVERY = ROOT / "assets" / "bin" / "workshop-discovery"
URL = "/api/admin/attendee-profile"


def _body(email: str, **overrides) -> dict:
    body = {
        "email": email,
        "full_name": "Tama Rewi",
        "company": "Pacific Test Telco",
        "job_title": "Customer Experience Lead",
        "suggested_industry": "telco",
        "industry_confidence": 0.92,
        "industry_source": "llm",
        "revision": 3,
    }
    body.update(overrides)
    return body


@pytest.fixture()
def owner() -> str:
    return f"labuser+{uuid.uuid4().hex[:8]}@example.com"


# --- the endpoint -----------------------------------------------------------------


def test_the_push_is_refused_to_a_non_admin(client, as_non_admin, owner):
    assert client.post(URL, json=_body(owner)).status_code == 403


def test_an_accepted_push_is_kept_in_the_owners_home(client, as_admin, owner):
    from server import attendee_roster

    response = client.post(URL, json=_body(owner))

    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": True, "revision": 3}
    record = attendee_roster.read(owner)
    assert record["suggested_industry"] == "telco"
    assert record["industry_label"] == "Telco"
    assert os.path.isfile(attendee_roster.path_for(owner))


def test_a_malformed_push_is_422(client, as_admin, owner):
    missing_revision = _body(owner)
    del missing_revision["revision"]
    for bad in (
        {"email": "no-at-sign", "industry_confidence": 0.1, "industry_source": "llm", "revision": 1},
        _body(owner, industry_confidence=1.5),
        _body(owner, industry_source="guess"),
        _body(owner, suggested_industry="Telco Corp!"),
        _body(owner, revision=-1),
        missing_revision,
    ):
        assert client.post(URL, json=bad).status_code == 422, bad


def test_an_older_or_repeated_revision_changes_nothing(client, as_admin, owner):
    from server import attendee_roster

    assert client.post(URL, json=_body(owner, revision=3)).json() == {"accepted": True, "revision": 3}

    older = client.post(URL, json=_body(owner, revision=2, suggested_industry="retail", industry_confidence=0.95))
    assert older.json() == {"accepted": True, "revision": 3}
    repeat = client.post(URL, json=_body(owner, revision=3, company="Someone Else"))
    assert repeat.json()["revision"] == 3

    record = attendee_roster.read(owner)
    assert record["suggested_industry"] == "telco"
    assert record["company"] == "Pacific Test Telco"

    newer = client.post(URL, json=_body(owner, revision=4, suggested_industry="retail"))
    assert newer.json() == {"accepted": True, "revision": 4}
    assert attendee_roster.read(owner)["suggested_industry"] == "retail"


def test_the_attendees_own_answer_is_never_overwritten(client, as_admin, owner):
    from server import attendee_profile
    from server.users import user_manager

    user = user_manager.get(owner)
    attendee_profile.save(user, expected_revision=0, changes={"industry": "retail"})

    assert client.post(URL, json=_body(owner)).status_code == 200

    assert attendee_profile.read(user).industry == "retail"


def test_a_suggestion_this_deployment_does_not_offer_is_dropped(client, as_admin, owner):
    from server import attendee_roster

    # No schema exists for logistics, so CT's "no confident match" is stored as empty.
    client.post(URL, json=_body(owner, suggested_industry="logistics", revision=1))

    record = attendee_roster.read(owner)
    assert record["suggested_industry"] == ""
    assert record["industry_confidence"] == 0.92


def test_a_pushed_suggestion_reaches_the_next_instructions(client, as_admin, owner):
    from server.users import user_manager
    from server import user_content

    user = user_manager.get(owner)
    user_content._write_instructions(user)
    assert "Suggested **Telco**" not in open(os.path.join(user.home, ".claude", "CLAUDE.md")).read()

    client.post(URL, json=_body(owner))

    assert "Suggested **Telco**" in open(os.path.join(user.home, ".claude", "CLAUDE.md")).read()


# --- what the meeting says ---------------------------------------------------------


def test_a_confident_suggestion_is_confirmed_first(owner):
    from server import attendee_roster, user_content
    from server.users import user_manager

    attendee_roster.store(attendee_roster.RosterProfileIn(**_body(owner)))
    text = user_content._persona_overlay(user_manager.get(owner))

    assert "Suggested **Telco**" in text and "confidence 0.92" in text
    assert "Offer all of these" not in text


def test_without_a_confident_suggestion_every_seeded_industry_is_offered(owner):
    from server import attendee_roster, demo_data, user_content
    from server.users import user_manager

    attendee_roster.store(attendee_roster.RosterProfileIn(**_body(owner, industry_confidence=0.5)))
    text = user_content._persona_overlay(user_manager.get(owner))

    assert "Suggested" not in text
    assert "offer all of these, plus Something else" in text
    for slug in demo_data.KNOWN_INDUSTRIES:
        assert slug.replace("_", " ") in text, slug


def test_the_attendees_own_industry_beats_the_suggestion(owner):
    from server import attendee_profile, attendee_roster, user_content
    from server.users import user_manager

    user = user_manager.get(owner)
    attendee_roster.store(attendee_roster.RosterProfileIn(**_body(owner)))
    attendee_profile.save(user, expected_revision=0, changes={"industry": "retail"})

    text = user_content._persona_overlay(user)

    assert "**retail**" in text
    assert "Suggested" not in text


# --- workshop-workflow show ----------------------------------------------------------


def _show(home: str, email: str, project: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": home, "WORKSHOP_USER_EMAIL": email}
    return subprocess.run(
        [sys.executable, str(WORKFLOW), "--project", str(project), "show", *extra],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_show_carries_the_attendee_block(client, as_admin, owner, tmp_path):
    from server.users import user_manager

    client.post(URL, json=_body(owner))
    home = user_manager.get(owner).home
    project = tmp_path / "fraud-app"
    project.mkdir()

    as_json = _show(home, owner, project, "--json")
    assert as_json.returncode == 0, as_json.stderr
    block = json.loads(as_json.stdout)["attendee"]
    assert block["company"] == "Pacific Test Telco"
    assert block["job_title"] == "Customer Experience Lead"
    assert block["suggested_industry"] == "telco"
    assert block["industry_label"] == "Telco"
    assert block["industry_confidence"] == 0.92

    as_text = _show(home, owner, project)
    assert ("Attendee: Customer Experience Lead, Pacific Test Telco. Suggested industry: Telco "
            "(telco, confidence 0.92, llm).") in as_text.stdout


def test_show_gives_nothing_for_another_attendee(client, as_admin, owner, tmp_path):
    from server.users import user_manager

    client.post(URL, json=_body(owner))
    project = tmp_path / "other-app"
    project.mkdir()

    other = f"someone-else-{uuid.uuid4().hex[:6]}@example.com"
    result = _show(user_manager.get(owner).home, other, project)

    assert result.returncode == 0, result.stderr
    assert "Attendee:" not in result.stdout


# --- workshop-discovery: one build, one record (DISC-3) -------------------------------


def _fake_curl(bin_dir: Path, body_file: Path) -> None:
    """Stand in for curl: keep the posted body and answer with $FAKE_REPLY."""
    script = bin_dir / "curl"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        while [ $# -gt 0 ]; do
          if [ "$1" = "-d" ]; then printf '%s' "$2" > "{body_file}"; fi
          shift
        done
        printf '%s' "$FAKE_REPLY"
        """))
    script.chmod(0o755)


def _discover(tmp_path: Path, project: Path, record: dict, reply: dict):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    body = tmp_path / "posted.json"
    _fake_curl(bin_dir, body)
    home = tmp_path / "home"
    (home / ".config" / "workshop").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "workshop" / "callback-capability").write_text("capability\n")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "WORKSHOP_USER_EMAIL": "labuser+1@example.com",
        "FAKE_REPLY": json.dumps(reply),
    }
    project.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["bash", str(DISCOVERY), json.dumps(record)], cwd=project, env=env,
                            capture_output=True, text=True, timeout=60)
    posted = json.loads(body.read_text()) if body.exists() else None
    return result, posted


def _state(project: Path):
    path = project / ".workshop" / "discovery.json"
    return json.loads(path.read_text()) if path.is_file() else None


def test_an_agent_switch_reuses_the_builds_record_id(tmp_path):
    project = tmp_path / "fraud-app"

    first, _ = _discover(tmp_path, project, {"record_id": "fraud-scoring", "confidence": "medium"},
                         {"captured": True, "record_id": "fraud-scoring"})
    assert first.returncode == 0, first.stderr
    assert _state(project) == {"record_id": "fraud-scoring"}

    second, posted = _discover(tmp_path, project, {"record_id": "ed-flow-board", "confidence": "high"},
                               {"captured": True, "record_id": "fraud-scoring"})
    assert second.returncode == 0, second.stderr
    assert posted["record_id"] == "fraud-scoring"
    assert _state(project) == {"record_id": "fraud-scoring"}


def test_a_new_build_starts_a_new_record(tmp_path):
    _discover(tmp_path, tmp_path / "first-build", {"record_id": "fraud-scoring"},
              {"captured": True, "record_id": "fraud-scoring"})

    _, posted = _discover(tmp_path, tmp_path / "second-build", {"record_id": "stock-levels"},
                          {"captured": True, "record_id": "stock-levels"})

    assert posted["record_id"] == "stock-levels"
    assert _state(tmp_path / "second-build") == {"record_id": "stock-levels"}


def test_a_refused_record_stores_no_id(tmp_path):
    project = tmp_path / "withdrawn-app"

    result, _ = _discover(tmp_path, project, {"record_id": "fun-game"}, {"captured": False, "reason": "disabled"})

    assert result.returncode == 0
    assert '"captured": false' in result.stdout
    assert _state(project) is None
