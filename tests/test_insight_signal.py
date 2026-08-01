"""The workshop.signal behavioural rollup (contract C6, phase 1).

Everything here is derived from counters the app already keeps, so the risk isn't
that the data is wrong — it's that the *derivation* quietly changes what a brief
claims about a customer. These tests pin the derivation, not the plumbing.
"""

import json
from pathlib import Path

import pytest

from server import config, stats
from server.event_emitter import EventEmitter
from server.users import User

from .schema_assert import assert_schema

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "tests" / "fixtures"
     / "workshop-insight-events.schema.json").read_text()
)


@pytest.fixture
def user(monkeypatch, tmp_path):
    """A user whose repos live in an empty tmp home, so code stats are zero."""
    monkeypatch.setattr(config, "data_root", lambda: str(tmp_path))
    u = User("labuser007@example.com")
    stats._code_cache.pop(u.email, None)
    return u


def _signal(user, code=None, resources=None):
    return stats._signal(user, code or {}, resources)


# --- Engagement banding ------------------------------------------------------


def test_observer_has_no_agent_session(user):
    assert _signal(user)["engagement"] == "observer"


def test_bash_alone_is_still_an_observer(user):
    """Bash is free and proves nothing about interest in the platform."""
    user.sessions_launched["bash"] = 4
    assert _signal(user)["engagement"] == "observer"


def test_agent_session_without_commits_is_an_explorer(user):
    user.sessions_launched["claude"] = 2
    assert _signal(user)["engagement"] == "explorer"


def test_commits_make_a_builder(user):
    user.sessions_launched["claude"] = 2
    assert _signal(user, {"commits": 3})["engagement"] == "builder"


def test_commits_outrank_a_missing_agent_session(user):
    """Committed code is direct evidence; a launch counter is a proxy for it.

    An attendee whose session counters were lost to a restart but whose repo has
    commits must not be downgraded to observer — the artifact outlives the
    counter.
    """
    assert _signal(user, {"commits": 1})["engagement"] == "builder"


def test_shipped_tracks_commits_not_files(user):
    """Files without commits is a scratch directory, not shipped work."""
    assert _signal(user, {"files": 20, "lines": 500, "commits": 0})["shipped"] is False
    assert _signal(user, {"commits": 1})["shipped"] is True


# --- Topic depth -------------------------------------------------------------


def test_primary_topic_is_the_most_hit(user):
    user.topics = {"lakebase": 1.0, "genie": 1.0}
    user.topic_hits = {"lakebase": 17, "genie": 3}
    assert _signal(user)["primary_topic"] == "lakebase"


def test_primary_topic_is_none_without_hits(user):
    """Topic detection can be off; absence must read as unknown, not as a topic."""
    assert _signal(user)["primary_topic"] is None


def test_primary_topic_breaks_ties_deterministically(user):
    """Repeated harvests of unchanged state must produce an identical payload.

    A flapping primary topic would make CT store a different "main interest" on
    every poll for an attendee who did nothing in between.
    """
    user.topic_hits = {"genie": 5, "apps": 5, "lakebase": 5}
    first = _signal(user)["primary_topic"]
    assert first == "apps"
    assert _signal(user)["primary_topic"] == first


def test_topic_hits_are_sorted_and_drop_zeroes(user):
    user.topic_hits = {"genie": 3, "apps": 0, "lakebase": 12}
    assert _signal(user)["topic_hits"] == {"genie": 3, "lakebase": 12}


def test_topic_hits_count_repeat_exposure(user):
    """Presence and depth are different facts, and only depth ranks interest."""
    user.topics = {"lakebase": 1.0, "genie": 1.0}
    user.topic_hits = {"lakebase": 40, "genie": 1}
    signal = _signal(user)
    assert signal["products"] == ["genie", "lakebase"]
    assert signal["primary_topic"] == "lakebase"


# --- Products ----------------------------------------------------------------


def test_lifecycle_topics_are_not_products(user):
    """A brief listing "wrap" as a product interest would mislead an AE."""
    user.topics = {"lakebase": 1.0, "wrap": 1.0, "troubleshooting": 1.0}
    assert _signal(user)["products"] == ["lakebase"]


def test_products_are_sorted_and_deduped(user):
    user.topics = {"unity-catalog": 1.0, "apps": 1.0, "genie": 1.0}
    assert _signal(user)["products"] == ["apps", "genie", "unity-catalog"]


def test_resource_kinds_report_only_non_empty_census_entries(user):
    resources = {"jobs": 2, "pipelines": 0, "apps": 1, "dashboards": 0}
    assert _signal(user, resources=resources)["resource_kinds"] == ["apps", "jobs"]


def test_resource_kinds_survive_a_missing_census(user):
    """The census is a best-effort HTTP call and returns {} when it fails."""
    assert _signal(user, resources=None)["resource_kinds"] == []
    assert _signal(user, resources={})["resource_kinds"] == []


# --- Wiring into the stats payload -------------------------------------------


def test_gather_user_carries_the_signal(user):
    row = stats.gather_user(user)
    assert row["signal"]["engagement"] == "observer"


def test_signal_is_present_without_capture_enabled(monkeypatch, user):
    """Operators must be able to see what capture would send before enabling it.

    The flag gates *transmission*, not derivation; hiding the block until the
    flag is on would mean the first time anyone sees the payload is after it has
    already been sent to CT.
    """
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    assert "signal" in stats.gather_user(user)


def test_schema_version_is_three():
    assert stats.STATS_SCHEMA_VERSION == 3


def test_gather_all_passes_one_census_to_every_row(monkeypatch, tmp_path):
    """The census is instance-level and must be fetched once, not per attendee."""
    monkeypatch.setattr(config, "data_root", lambda: str(tmp_path))
    calls = []

    def census():
        calls.append(1)
        return {"jobs": 1, "apps": 0}

    monkeypatch.setattr(stats, "_workspace_resources", census)
    out = stats.gather_all([User("labuser001@x.com"), User("labuser002@x.com")])

    assert len(calls) == 1
    for row in out["users"]:
        assert row["signal"]["resource_kinds"] == ["jobs"]


def test_census_is_not_copied_into_the_per_user_row(monkeypatch, tmp_path):
    """A per-user `resources` key would imply the census is per-attendee.

    On a shared instance it is the whole cohort's workspace, so attributing it to
    one attendee would inflate what that person appears to have built.
    """
    monkeypatch.setattr(config, "data_root", lambda: str(tmp_path))
    monkeypatch.setattr(stats, "_workspace_resources", lambda: {"jobs": 9})
    out = stats.gather_all([User("labuser001@x.com")])
    assert "resources" not in out["users"][0]
    assert out["resources"] == {"jobs": 9}


# --- Emission ----------------------------------------------------------------


def _harvest():
    return {
        "schema_version": 3,
        "resources": {"jobs": 1, "pipelines": 0},
        "instance": {"phase": "build"},
        "users": [
            {
                "email": "labuser007@example.com",
                "minutes_building": 74,
                "agent_sessions": 3,
                "terminal_sessions": 4,
                "topics": ["lakebase"],
                "code": {"projects": 1, "commits": 6, "files": 3, "lines": 90},
                "errors": 2,
                "idle_seconds": 45,
                "signal": {
                    "engagement": "builder",
                    "primary_topic": "lakebase",
                    "topic_hits": {"lakebase": 12},
                    "products": ["lakebase"],
                    "resource_kinds": ["jobs"],
                    "shipped": True,
                },
            }
        ],
    }


def _emitter():
    return EventEmitter(
        run_id="3f1b8c2e-9a44-4d21-8f0e-7c5b1a2d6e90",
        workspace_id="4217334529551234",
        ingest_url="https://ct.example.com",
        ingest_token="secret",
    )


def test_emitted_event_matches_the_shared_schema(monkeypatch):
    """The producer's output is validated against the contract, not just its shape."""
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    emitter = _emitter()
    assert stats.emit_signals(_harvest(), emitter) == 1

    delivered = []
    emitter.drain(lambda ev: delivered.append(ev) or True)
    assert len(delivered) == 1
    assert_schema(delivered[0], SCHEMA)


def test_emission_is_off_unless_capture_is_on(monkeypatch):
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    emitter = _emitter()
    assert stats.emit_signals(_harvest(), emitter) == 0
    assert emitter.pending() == 0


def test_emission_does_not_depend_on_push_configuration(monkeypatch):
    """An instance Control Tower deployed has none of the push settings.

    CT injects no ingest URL, token or run id — it collects from
    ``/api/admin/insight-events`` instead. Gating emission on those variables (the
    original behaviour) meant the signal was silent on precisely the instances the
    feature exists for.
    """
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    emitter = EventEmitter(
        run_id="", workspace_id="", ingest_url="", ingest_token=""
    )

    assert stats.emit_signals(_harvest(), emitter) == 1
    assert emitter.pending() == 1


def test_a_signal_emitted_without_a_run_id_is_still_attributable(monkeypatch):
    """The collector stamps the run; the terminal must supply the attendee.

    Control Tower knows which unit it polled and rewrites ``run_id`` and the
    idempotency key on arrival, but it cannot invent which attendee a signal
    describes — a signal missing that would be unusable and unrepairable.
    """
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    emitter = EventEmitter(
        run_id="", workspace_id="", ingest_url="", ingest_token=""
    )
    stats.emit_signals(_harvest(), emitter)

    event = emitter.collect()["events"][0]["event"]

    assert event["attendee"] == "labuser007@example.com"
    assert event["type"] == "workshop.signal"


def test_repeat_harvests_in_one_bucket_share_a_key(monkeypatch):
    """Bounded growth: CT polls every ~10 minutes for hours.

    Without a bucketed key a six-hour workshop would write a row per poll per
    attendee, all near-identical, and the brief would have to de-duplicate them.
    """
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    keys = {
        key
        for _ in range(3)
        for _, _, key in stats.signal_events(_harvest(), "run-1")
    }
    assert len(keys) == 1


def test_key_carries_run_attendee_and_bucket(monkeypatch):
    monkeypatch.setattr(stats.time, "time", lambda: 1769764800.0)
    (_, _, key) = stats.signal_events(_harvest(), "run-1")[0]
    assert key == "signal:run-1:labuser007@example.com:1769764800"


def test_bucket_is_not_shorter_than_the_poll_interval():
    """A bucket below the poll interval would never de-duplicate anything."""
    assert stats._SIGNAL_BUCKET_SECONDS >= 600


def test_distinct_attendees_get_distinct_keys():
    harvest = _harvest()
    harvest["users"].append({**harvest["users"][0], "email": "labuser008@x.com"})
    keys = {key for _, _, key in stats.signal_events(harvest, "run-1")}
    assert len(keys) == 2


def test_rows_without_an_email_are_skipped():
    """An unattributable signal is noise — CT has nothing to resolve it against."""
    harvest = _harvest()
    harvest["users"].append({"email": "", "signal": {}})
    assert len(stats.signal_events(harvest, "run-1")) == 1


def test_phase_is_omitted_when_unknown():
    """The schema allows phase to be absent; an empty string would read as a phase."""
    harvest = _harvest()
    harvest["instance"] = {}
    (_, payload, _) = stats.signal_events(harvest, "run-1")[0]
    assert "phase" not in payload


def test_payload_reports_the_stats_version_it_was_built_from():
    """CT keys its tolerance off this, so it must reflect the harvest, not a constant."""
    harvest = _harvest()
    harvest["schema_version"] = 2
    (_, payload, _) = stats.signal_events(harvest, "run-1")[0]
    assert payload["stats_schema_version"] == 2


def test_emission_never_raises_on_a_degenerate_harvest(monkeypatch):
    """The harvest endpoint must not fail because insight capture tripped."""
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    assert stats.emit_signals({}, _emitter()) == 0


def test_signal_payload_carries_no_attendee_text(monkeypatch):
    """Tier 1 is derived counters only — consent for it rests on that.

    Topic *names* come from the operator's content pack, not from the attendee,
    so the payload's strings are all operator- or app-authored.
    """
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    (_, payload, _) = stats.signal_events(_harvest(), "run-1")[0]
    assert set(payload) <= {
        "stats_schema_version", "phase", "minutes_building", "agent_sessions",
        "terminal_sessions", "topics", "code", "resources", "errors",
        "idle_seconds", "discovery_records", "signal",
    }
