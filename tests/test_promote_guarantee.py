"""Every attendee leaves with a record, shipped or not (contract C6, phase 4).

Promote used to fire only after a *successful* build and only if the attendee said
yes to an offer. That produced the exact inverse of what the workshop needs: the
attendees who hit a wall — the ones whose blocker is the most actionable thing an
account team could hear, and who most need notes when they pick the work up again —
produced nothing at all, while the polished demo sessions were documented twice.

These tests pin the fix across the four surfaces that have to agree, because
nothing at runtime can enforce it: the skill an agent reads, the base instructions
Codex and Omnigent follow, the nuggets and chips the attendee sees, and the
harvester that has to find whatever was written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "assets" / "skills" / "promote" / "SKILL.md"
INSTRUCTIONS = ROOT / "assets" / "instructions" / "CLAUDE.md"
PACK = ROOT / "content" / "default_pack.json"

PROMOTE_DOCS = (
    "architecture.md",
    "security.md",
    "jira-stories.md",
    "test-cases.md",
    "build-prompt.md",
)


@pytest.fixture(scope="module")
def skill() -> str:
    return SKILL.read_text()


@pytest.fixture(scope="module")
def instructions() -> str:
    return INSTRUCTIONS.read_text()


@pytest.fixture(scope="module")
def pack() -> dict:
    return json.loads(PACK.read_text())


# --- the trigger is no longer conditional -------------------------------------


def test_the_skill_fires_at_wrap_and_not_only_after_a_build(skill: str):
    assert "wrap phase" in skill
    assert "without asking" in skill
    # The gate that produced the coverage hole.
    assert "Just say yes" not in skill


def test_the_skill_runs_for_a_session_that_shipped_nothing(skill: str):
    assert "Nothing shipped" in skill
    assert "still write the set" in skill
    assert "heading towards" in skill


def test_the_skill_forbids_inventing_a_finished_build(skill: str):
    """The failure mode of an unconditional trigger: a document that reads like a
    finished system because the agent felt obliged to produce one."""
    assert "do not invent a deployment" in skill
    assert "Do not describe an unfinished session as complete" in skill


def test_the_skill_names_the_blocker_rather_than_omitting_it(skill: str):
    assert "Where this stopped" in skill
    assert "what would unblock it" in skill


def test_the_base_instructions_carry_the_same_wrap_rule(instructions: str):
    """Codex and Omnigent never read the skill file — this is their only copy."""
    assert "run Promote regardless" in instructions
    assert "whether or not anything shipped" in instructions
    assert "never invent a deployment" in instructions


def test_the_instructions_explain_why_it_cannot_wait_for_consent(instructions: str):
    """Without the reason, this reads as pushiness and gets edited back out."""
    assert "deleted after the workshop" in instructions


# --- the artifacts land where the harvester looks ------------------------------


def test_the_skill_writes_to_the_home_directory(skill: str):
    assert "~/promote" in skill
    assert "$HOME/promote" in skill


def test_shared_tmp_is_explicitly_rejected(skill: str):
    """/tmp is shared by uid across attendees on one container, so a doc written
    there can be attributed to the wrong person — and their employer."""
    assert "shared by every attendee" in skill
    assert 'databricks files upload "/tmp/promote' not in skill


def test_the_instructions_also_point_away_from_tmp(instructions: str):
    assert "~/promote/<doc>.md" in instructions
    assert "not `/tmp/promote`" in instructions


def test_the_harvester_scans_the_documented_location():
    """The contract between the skill's output path and the harvest is untested at
    runtime — an agent writing to a directory nobody reads fails silently."""
    from server import artifacts

    assert "promote" in artifacts._DOCUMENT_ROOTS


@pytest.mark.parametrize("name", PROMOTE_DOCS)
def test_every_promote_document_is_harvestable(tmp_path, name: str):
    """A marker-list filter would drop jira-stories.md and build-prompt.md — the
    two that carry the business-value statement and the restated goal."""
    from server import artifacts

    promote = tmp_path / "promote"
    promote.mkdir()
    (promote / name).write_text(f"# {name}\ncontent\n")
    result = artifacts.harvest_user(_FakeUser(str(tmp_path)))

    assert [a.title for a in result.artifacts] == [name]
    assert result.artifacts[0].kind == "promote_doc"


def test_an_upload_failure_does_not_discard_the_local_copy(skill: str):
    """The Volume dies with the catalog at teardown; ~/promote is what the harvest
    reads. Treating a failed upload as a failed run would throw away the record."""
    assert "do not delete the local copies" in skill


def test_reruns_update_rather_than_regenerate(skill: str):
    assert "update it" in skill
    assert "rather than starting over" in skill


# --- the attendee is actually prompted ----------------------------------------


def _nugget(pack: dict, nugget_id: str) -> dict:
    for nugget in pack["nuggets"]:
        if nugget["id"] == nugget_id:
            return nugget
    raise AssertionError(f"no nugget {nugget_id!r} in the default pack")


def test_the_wrap_prompt_reaches_attendees_who_never_built_anything(pack: dict):
    """The pre-existing promote nugget is gated on topic:build-complete, so an
    explorer never saw it. This one has no trigger at all."""
    nugget = _nugget(pack, "wrap-promote-anyway")

    assert nugget["phases"] == ["wrap"]
    assert nugget["triggers"] == [], "a trigger here reintroduces the coverage hole"
    assert nugget["pinned"] is True


def test_the_wrap_prompt_addresses_the_unfinished_session(pack: dict):
    nugget = _nugget(pack, "wrap-promote-anyway")

    assert "Didn't finish?" in nugget["markdown"]
    assert "where it stopped" in nugget["prompt"]


def test_the_build_gated_nugget_still_exists_for_builders(pack: dict):
    """The two are complementary: one fires on the build, one on the phase."""
    assert _nugget(pack, "promote-cta")["triggers"] == ["topic:build-complete"]


def test_wrap_chips_do_not_assume_a_build_happened(pack: dict):
    """"Sum up what I built" is the only wrap chip an explorer could click, and it
    presupposes the thing they didn't manage to do."""
    chips = pack["prompts"]["wrap"]
    labels = [chip["label"] for chip in chips]

    assert "Save my work" in labels
    assert "Where did I get stuck?" in labels
    stuck = next(c for c in chips if c["label"] == "Where did I get stuck?")
    assert "blocked me" in stuck["prompt"]


def test_the_pack_still_parses_as_a_content_pack(pack: dict):
    """The nuggets and chips are validated on load, so a malformed addition takes
    the whole pack — and every nugget in it — down with it."""
    from server.content import ContentPack

    parsed = ContentPack.model_validate(pack)
    assert {n.id for n in parsed.nuggets} >= {"promote-cta", "wrap-promote-anyway"}
    assert all(chip.label and chip.prompt for chip in parsed.prompts["wrap"])


# --- the app-side guarantee ---------------------------------------------------


def test_a_session_with_no_artifacts_still_produces_a_summary(monkeypatch):
    """The backstop behind all of the above: even if no agent ever runs promote,
    an explorer's prompts and errors alone are enough to ship a record."""
    from server import artifacts, insight_summary
    from server.artifacts import Harvest
    from server.event_emitter import EventEmitter

    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    insight_summary.stamps.clear()
    monkeypatch.setattr(
        artifacts, "harvest_user",
        lambda user, **kw: Harvest(
            prompts=["can Lakeflow read from our on-prem Oracle?"],
            errors=["Connector unavailable in this workspace"],
            prompt_count=9,
        ),
    )
    monkeypatch.setattr(
        insight_summary, "_ask_model",
        lambda h, s: (_ for _ in ()).throw(
            insight_summary.ModelUnavailable("no endpoint")
        ),
    )
    emitter = EventEmitter(
        run_id="11111111-2222-3333-4444-555555555555", workspace_id="w",
        ingest_url="https://ct.example.com", ingest_token="t",
    )

    payload = insight_summary.summarise_user(
        _FakeUser("/nonexistent"), phase="wrap",
        signal={"engagement": "explorer", "shipped": False}, emitter=emitter,
    )
    insight_summary.stamps.clear()

    assert payload is not None, "an explorer must not vanish from the record"
    assert payload["blockers"] == ["Connector unavailable in this workspace"]


class _FakeUser:
    def __init__(self, home: str, email: str = "labuser001@example.com"):
        self.home = home
        self.email = email
