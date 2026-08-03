"""What server/models.py promises, in the order the promises matter.

The module exists because model names had drifted between four copies, so most of
these tests are about the properties that keep a single source of truth honest —
that a profile is complete, that a pin always wins, that degradation is ordered —
rather than about any particular endpoint name. Where a name is asserted it is
because that name is a decision someone made for a reason worth defending.
"""

import pytest

from server import models


@pytest.fixture(autouse=True)
def _no_ambient_pins(monkeypatch):
    """A developer's own env must not decide what these tests observe."""
    for name in (
        "WORKSHOP_MODEL_PROFILE",
        "ANTHROPIC_MODEL",
        "CODEX_MODEL",
        "INSIGHT_SUMMARY_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


ROLES = ("driver", "frontier", "standard", "fast", "codex", "insight")


def test_every_profile_fills_every_role():
    """A profile missing a role would resolve to an AttributeError at the moment
    an attendee signs in, in whichever config writer got there first."""
    for name, profile in models.PROFILES.items():
        for role in ROLES:
            chain = getattr(profile, role)
            assert chain, f"{name}.{role} is empty"
            assert all(isinstance(c, str) and c for c in chain)


def test_the_default_profile_exists_and_is_balanced():
    assert models.DEFAULT_PROFILE in models.PROFILES
    assert models.profile().name == "balanced"


def test_an_unknown_profile_falls_back_rather_than_failing(monkeypatch):
    """A typo in a deployment variable should cost an event nothing: the value it
    lands on is what every event ran before profiles existed."""
    monkeypatch.setenv("WORKSHOP_MODEL_PROFILE", "cheep")
    assert models.profile().name == "balanced"


def test_profile_names_are_matched_case_insensitively(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MODEL_PROFILE", "  Economy ")
    assert models.profile().name == "economy"


# -- the roles the balanced profile fills, which is what deployments run today --

def test_the_everyday_driver_is_sonnet_before_opus():
    """Sonnet 5 is $2/$10 per million against Opus's $5/$25 and is the right
    everyday driver; an event that wants Opus by default says so with a profile
    or a pin. Opus stays on the tail so a region with no Sonnet still works."""
    driver = models.chain("driver")
    assert driver[0] == "databricks-claude-sonnet-5"
    assert "databricks-claude-opus-5" in driver
    assert driver.index("databricks-claude-sonnet-5") < driver.index(
        "databricks-claude-opus-5"
    )


def test_the_frontier_slot_leads_with_opus_5():
    """The reason this module exists: the Opus chain had not learned that Opus 5
    shipped, so /model opus resolved to a superseded endpoint."""
    assert models.chain("frontier")[0] == "databricks-claude-opus-5"


def test_fable_is_not_the_frontier_default():
    """Fable 5 is the strongest Claude on the gateway and twice Opus's price
    ($10/$50 per million). An event opts into that with ANTHROPIC_MODEL; nobody
    gets opted in by shipping a release."""
    assert "databricks-claude-fable-5" not in models.chain("frontier")
    assert "databricks-claude-fable-5" not in models.chain("driver")


def test_codex_no_longer_defaults_to_gpt_5_5():
    """gpt-5-5 is $5/$30 per million. Terra is stronger on coding evals and
    half the price, so the old default was losing on both axes at once."""
    codex = models.chain("codex")
    assert codex[0] == "databricks-gpt-5-6-terra"
    assert codex.index("databricks-gpt-5-5") == len(codex) - 1


def test_the_summariser_stays_on_the_cheap_tier():
    """This is the one call an event makes on its own behalf rather than an
    attendee's, and it competes with their budget."""
    assert models.chain("insight")[0] == "databricks-claude-haiku-4-5"


# -- pins --

def test_a_pin_leads_the_chain_without_replacing_it(monkeypatch):
    """Leading rather than replacing is what lets a pinned model that this region
    does not serve degrade to the next best thing instead of failing."""
    monkeypatch.setenv("ANTHROPIC_MODEL", "databricks-claude-fable-5")
    chain = models.chain("driver")
    assert chain[0] == "databricks-claude-fable-5"
    assert "databricks-claude-sonnet-5" in chain


def test_a_pin_appears_once_even_when_it_is_already_in_the_chain(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "databricks-claude-opus-5")
    chain = models.chain("driver")
    assert chain.count("databricks-claude-opus-5") == 1
    assert chain[0] == "databricks-claude-opus-5"


def test_a_pin_wins_over_a_ready_chain_member(monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "databricks-gpt-5-4")
    resolved = models.resolve(
        "codex", {"databricks-gpt-5-4", "databricks-gpt-5-6-terra"}
    )
    assert resolved == "databricks-gpt-5-4"


def test_a_pin_the_workspace_does_not_serve_degrades_to_one_it_does(monkeypatch):
    """The uncomfortable case, pinned deliberately. A pin lands unserved through
    a typo, a region that never got the endpoint, or a failed discovery call; in
    the first two a working CLI on a neighbouring model beats a broken one, and
    /readyz still reports what was asked for."""
    monkeypatch.setenv("CODEX_MODEL", "databricks-gpt-6")
    resolved = models.resolve("codex", {"databricks-gpt-5-6-terra"})
    assert resolved == "databricks-gpt-5-6-terra"


def test_an_unserved_pin_wins_when_discovery_found_nothing_at_all(monkeypatch):
    """The third case. Nothing is READY, so there is no evidence against the pin
    and it is the most informed guess available."""
    monkeypatch.setenv("CODEX_MODEL", "databricks-gpt-6")
    assert models.resolve("codex", set()) == "databricks-gpt-6"


def test_roles_without_a_pin_ignore_a_same_named_variable(monkeypatch):
    """Claude reads three model slots at once, so pinning them individually
    invites the combination where /model opus is cheaper than the default. The
    profile is the only way to move them."""
    monkeypatch.setenv("frontier", "databricks-claude-fable-5")
    monkeypatch.setenv("ANTHROPIC_MODEL", "databricks-claude-sonnet-5")
    assert models.chain("frontier")[0] == "databricks-claude-opus-5"


# -- resolution against what a workspace actually serves --

def test_resolution_walks_past_what_is_not_ready():
    available = {"databricks-claude-opus-4-7"}
    assert models.resolve("frontier", available) == "databricks-claude-opus-4-7"


def test_empty_discovery_returns_the_chain_head_unverified():
    """Empty means the API call failed, not that the workspace serves nothing.
    Writing a config with the newest model we know of beats writing none."""
    assert models.resolve("frontier", set()) == "databricks-claude-opus-5"


def test_resolution_prefers_the_earliest_ready_candidate():
    available = {
        "databricks-claude-opus-4-6",
        "databricks-claude-opus-5",
        "databricks-claude-sonnet-5",
    }
    assert models.resolve("frontier", available) == "databricks-claude-opus-5"


def test_resolve_all_covers_every_pinnable_role():
    resolved = models.resolve_all({"databricks-claude-sonnet-5"})
    assert set(resolved) == set(ROLES)
    assert all(resolved.values())


def test_an_unknown_role_is_a_programming_error_not_a_silent_default():
    with pytest.raises(KeyError):
        models.role("cheapest")


# -- what the profiles are for --

def test_economy_puts_a_ceiling_on_the_opus_slot(monkeypatch):
    """The lever a large free event needs: an attendee typing /model opus gets
    Sonnet, at a fifth of Opus's output price. A real ceiling, not advice."""
    monkeypatch.setenv("WORKSHOP_MODEL_PROFILE", "economy")
    assert models.chain("frontier")[0] == "databricks-claude-sonnet-5"
    assert not any("opus" in c for c in models.chain("frontier"))
    assert not any("opus" in c for c in models.chain("driver"))


def test_economy_moves_codex_to_the_cheap_tier(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MODEL_PROFILE", "economy")
    assert models.chain("codex")[0] == "databricks-gpt-5-6-luna"


def test_economy_moves_the_summariser_to_the_cheapest_thing_that_works(monkeypatch):
    """gpt-oss-120b is around a sixth of Haiku."""
    monkeypatch.setenv("WORKSHOP_MODEL_PROFILE", "economy")
    assert models.chain("insight")[0] == "databricks-gpt-oss-120b"


def test_frontier_promotes_the_everyday_driver(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MODEL_PROFILE", "frontier")
    assert models.chain("driver")[0] == "databricks-claude-opus-5"
    assert models.chain("codex")[0] == "databricks-gpt-5-6-sol"


def test_every_profile_still_degrades_across_at_least_two_generations():
    """A single-entry chain is a chain that fails in a region a release behind."""
    for name, profile in models.PROFILES.items():
        for role in ("driver", "frontier", "codex", "insight"):
            assert len(getattr(profile, role)) >= 2, f"{name}.{role}"


def test_no_profile_can_be_cheaper_at_the_frontier_slot_than_the_driver():
    """Claude Code shows the Opus slot as the upgrade from the default. A profile
    where /model opus resolves to something weaker than the default would make
    the upgrade a downgrade."""
    rank = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}

    def tier(model: str) -> int:
        return next((v for k, v in rank.items() if k in model), 1)

    for name, profile in models.PROFILES.items():
        assert tier(profile.frontier[0]) >= tier(profile.driver[0]), name
