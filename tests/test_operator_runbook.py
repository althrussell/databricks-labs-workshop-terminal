"""The runbook has to be right about one thing above all others.

Omnigent's Claude and Codex share the runner's single credential with every
other Omnigent harness, so they fail together. A runbook that offers them as a
refuge from a failing Pi sends an operator in a circle mid-event, and the second
failure reads as an unrelated bug. That error was in the docs once; these tests
exist so it cannot come back quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[1] / "docs"
RUNBOOK = DOCS / "operator-runbook.md"


@pytest.fixture(scope="module")
def runbook() -> str:
    assert RUNBOOK.exists(), "the runbook the product and README point at must exist"
    return RUNBOOK.read_text()


def test_the_runbook_says_plainly_that_omnigent_harnesses_are_not_a_fallback(runbook):
    assert re.search(
        r"Omnigent'?s Claude and Omnigent'?s Codex are not a fallback", runbook
    ), "the correction has to be stated, not merely implied by omission"


def test_the_runbook_names_the_tier_that_actually_survives(runbook):
    lowered = runbook.lower()
    assert "bare" in lowered
    for surface in ("claude", "codex", "terminal"):
        assert surface in lowered, f"the fallback tier must name {surface}"


def test_the_runbook_explains_why_rather_than_just_asserting_it(runbook):
    """An operator under pressure obeys a reason; a bare rule gets second-guessed."""
    assert "_RunnerDatabricksAuth" in runbook or "one credential" in runbook


def test_restarting_the_apps_is_ruled_out_as_a_first_move(runbook):
    assert re.search(r"[Dd]o not restart the `wt` or `omni` app as a first move", runbook)


def test_the_runbook_refuses_to_send_attendees_at_runner_logs(runbook):
    assert "Never ask an attendee to read runner logs" in runbook


def test_the_ladder_is_ordered_by_what_it_costs_the_attendee(runbook):
    """Reload before bare CLIs before Recover before demoting the fleet."""
    rungs = [runbook.index(f"### Rung {n}") for n in (1, 2, 3, 4)]
    assert rungs == sorted(rungs)
    assert runbook.index("Demote Omnigent") > runbook.index("Reload the tab")


def test_every_lever_the_runbook_promises_actually_exists():
    """A runbook step with no button behind it is worse than no step."""
    from server import admin

    routes = {route.path for route in admin.router.routes}
    assert "/api/admin/omnigent-tier" in routes
    assert "/api/admin/recover" in routes


@pytest.mark.parametrize(
    "doc",
    sorted(p for p in DOCS.glob("*.md") if p.name != "operator-runbook.md"),
    ids=lambda p: p.name,
)
def test_no_other_doc_offers_an_omnigent_harness_as_the_way_out(doc: Path):
    """The framing that has to stay dead: bare CLIs as Omnigent's *demoted* tier."""
    text = doc.read_text()
    for line in text.splitlines():
        if "demoted fallback" in line.lower():
            assert "Correction" in text, (
                f"{doc.name} still frames the bare CLIs as demoted fallbacks "
                "without correcting it"
            )
