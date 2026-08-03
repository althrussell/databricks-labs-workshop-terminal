"""A spotted topic becomes a product claim in front of an account team.

The contextual panel and the `products` field on the insight payload read the
same detector. That coupling is fine only while the detector reports *activity*;
the moment it reports *mentions*, the workshop's own instructions and skills —
which name every Databricks product, because they have to — become the loudest
source of "evidence" about what an attendee did.

That happened. A Space Invaders clone with no data and no persistence produced a
brief claiming the attendee touched Genie and Lakebase, with suggested follow-ups
about the business problem their Lakebase work solved. These tests pin the two
properties that stop it: keywords describe commands rather than products, and
documentation on screen is not activity.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "content" / "default_pack.json"

# Topics that describe where someone is in the lab, not a product they used.
# `stats._NON_PRODUCT_TOPICS` already keeps these out of `products`.
_LAB_STATE_TOPICS = {"build-complete"}


@pytest.fixture(scope="module")
def service():
    from server.content import ContentService

    return ContentService()


def _product_topics() -> dict[str, list[str]]:
    topics = json.loads(PACK.read_text())["topics"]
    return {k: v for k, v in topics.items() if k not in _LAB_STATE_TOPICS}


def test_no_keyword_is_a_bare_product_name():
    """The original bug in one assertion.

    `lakebase` matched the word "lakebase". Our own always-loaded instructions
    contain that word, as does every sentence an agent writes explaining why it
    is *not* using it. A keyword has to be something you only type when you are
    doing the thing.
    """
    bare = {
        "lakebase", "genie", "mlflow", "postgresql", "unity catalog", "appkit",
        "streamlit", "gradio", "lakeview", "lakeflow", "embeddings",
        "vector search", "vector index", "model serving", "serving endpoint",
        "foundation model", "databricks job", "databricks apps",
    }
    offenders = {
        topic: [k for k in keywords if k.lower() in bare]
        for topic, keywords in _product_topics().items()
    }
    offenders = {t: k for t, k in offenders.items() if k}
    assert not offenders, (
        f"these keywords match a product being named rather than used: {offenders}"
    )


@pytest.mark.parametrize(
    "path",
    sorted(glob.glob(str(ROOT / "assets" / "instructions" / "*.md"))),
    ids=lambda p: Path(p).name,
)
def test_the_instructions_an_agent_always_loads_are_not_activity(service, path):
    """CLAUDE.md names Lakebase, Genie and Unity Catalog because it has to tell
    an agent when to reach for them. It is loaded into every session, and echoing
    it must not make the attendee look like they used all three."""
    assert service.scan_topics(Path(path).read_text()) == set()


def test_the_skill_loaded_on_every_app_build_is_not_activity(service):
    """`databricks-apps` is mandatory for every app in this workshop and quotes
    both Lakebase and Genie commands as examples. If reading it counts as using
    them, then *every* attendee brief carries both — which is exactly the shape
    of the failure that was reported."""
    skill = ROOT / "assets" / "skills" / "databricks-apps" / "SKILL.md"
    hits = service.scan_topics(skill.read_text())
    assert "lakebase" not in hits
    assert "genie" not in hits


def test_agent_prose_that_declines_a_product_reports_nothing(service):
    for text in (
        "This app has zero data, so I skipped the Lakebase question entirely.",
        "No Genie space is needed for a game.",
        "We don't need Unity Catalog here — nothing is persisted.",
        "You could add a Genie space later, or a Lakebase database.",
    ):
        assert service.scan_topics(text) == set(), text


def test_the_main_instructions_anchor_the_discovery_call(monkeypatch):
    """Why the brief was empty even though capture was on.

    `discovery.md` is appended after ~360 lines of "build fast, no process,
    never announce process", and nothing in the body ever referred to it. The
    agent followed the speed mandate and skipped what looked like ceremony, so a
    whole session reached an account team as "no use case recorded" — which no
    amount of downstream classification can repair.

    The anchor sits at the moment that always happens: the URL going out.
    """
    from server.user_content import _base_instructions

    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    body = _base_instructions()
    # Flattened: the anchor is hard-wrapped, so every phrase below spans a line
    # break in the source file.
    gate = " ".join(body.split("The ship gate")[1].split("## ")[0].split()).lower()
    assert "workshop-discovery" in gate, (
        "the shipping moment must point at the call, or the appended discovery "
        "section is the only thing asking for it and it gets read as ceremony"
    )
    assert "`fun` is a complete answer" in gate, (
        "a game must be recorded as a game — an unrecorded session is what got "
        "read as an unqualified opportunity"
    )
    assert "always" in gate, (
        "the call has to be unconditional: an agent left to judge whether it "
        "learned enough decides it didn't, which is how a whole run recorded nothing"
    )
    assert "must never delay showing them the app" in gate, (
        "the anchor cannot become a fourth ship-gate step"
    )


def test_the_discovery_helper_is_installed_even_when_capture_is_off():
    """The anchor names a command unconditionally, so the command has to exist
    unconditionally — otherwise an event with capture disabled turns one line of
    instructions into `command not found` in front of an attendee."""
    from server import user_content

    src = Path(user_content.__file__).resolve().parent.parent / "assets" / "bin"
    assert (src / "workshop-discovery").is_file()
    assert "workshop-discovery" in Path(user_content.__file__).read_text()


def test_real_use_of_each_product_is_still_detected(service):
    """Precision is only worth having if recall survives it. Each line here is
    something an attendee's terminal genuinely shows when they use the product."""
    expected = {
        "lakebase": "$ databricks lakebase create-database-instance workshop-db",
        "genie": "$ databricks genie list-spaces --profile DEFAULT",
        "unity-catalog": "CREATE SCHEMA IF NOT EXISTS wsh_alice.bronze",
        "jobs": "$ databricks jobs run-now --job-id 411",
        "serving": "$ databricks serving-endpoints query --name my-endpoint",
        "mlflow": "import mlflow\nmlflow.start_run()",
        "apps": "Deployed: https://space-invaders.aws.databricksapps.com",
        "dashboards": "$ databricks lakeview create --display-name Sales",
        "pipelines": "CREATE STREAMING TABLE bronze_events",
        "vector-search": "client.create_delta_sync_index(index_name='idx')",
    }
    for topic, line in expected.items():
        assert topic in service.scan_topics(line), f"{topic} no longer detected: {line}"
