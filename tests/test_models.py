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


def test_no_chain_lists_the_same_model_twice():
    """A repeat is harmless to resolve() and a sign someone built a chain by
    prepending to another one, which is how a role ends up with a rung that can
    never be reached."""
    for name, profile in models.PROFILES.items():
        for role in ROLES:
            chain = getattr(profile, role)
            assert len(chain) == len(set(chain)), f"{name}.{role}: {chain}"


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
    assert driver[0] == "claude-sonnet-5"
    assert "claude-opus-5" in driver
    assert driver.index("claude-sonnet-5") < driver.index(
        "claude-opus-5"
    )


def test_the_frontier_slot_leads_with_opus_5():
    """The reason this module exists: the Opus chain had not learned that Opus 5
    shipped, so /model opus resolved to a superseded endpoint."""
    assert models.chain("frontier")[0] == "claude-opus-5"


def test_fable_is_not_the_frontier_default():
    """Fable 5 is the strongest Claude on the gateway and twice Opus's price
    ($10/$50 per million). An event opts into that with ANTHROPIC_MODEL; nobody
    gets opted in by shipping a release."""
    assert "claude-fable-5" not in models.chain("frontier")
    assert "claude-fable-5" not in models.chain("driver")


def test_codex_has_no_gpt_5_5_rung_left():
    """gpt-5-5 was $5/$30 per million — the dearest rung in the chain and the
    weakest of the three on coding. It sat last while the legacy endpoints
    existed; the retirement is a good moment to stop carrying it, since a
    fallback nobody would choose is not a fallback."""
    codex = models.chain("codex")
    assert codex[0] == "gpt-5-6-terra"
    assert not any("gpt-5-5" in c for c in codex)


def test_the_summariser_stays_on_the_cheap_tier():
    """This is the one call an event makes on its own behalf rather than an
    attendee's, and it competes with their budget."""
    assert models.chain("insight")[0] == "claude-haiku-4-5"


# -- pins --

def test_a_pin_leads_the_chain_without_replacing_it(monkeypatch):
    """Leading rather than replacing is what lets a pinned model that this region
    does not serve degrade to the next best thing instead of failing."""
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-fable-5")
    chain = models.chain("driver")
    assert chain[0] == "claude-fable-5"
    assert "claude-sonnet-5" in chain


def test_a_pin_appears_once_even_when_it_is_already_in_the_chain(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5")
    chain = models.chain("driver")
    assert chain.count("claude-opus-5") == 1
    assert chain[0] == "claude-opus-5"


def test_a_pin_wins_over_a_served_chain_member(monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "gpt-5-4")
    resolved = models.resolve(
        "codex", {"gpt-5-4", "gpt-5-6-terra"}
    )
    assert resolved == "system.ai.gpt-5-4"


def test_a_pin_the_workspace_does_not_serve_degrades_to_one_it_does(monkeypatch):
    """The uncomfortable case, pinned deliberately. A pin lands unserved through
    a typo, a region that never got the model, or a failed discovery call; in
    the first two a working CLI on a neighbouring model beats a broken one, and
    /readyz still reports what was asked for."""
    monkeypatch.setenv("CODEX_MODEL", "gpt-6")
    resolved = models.resolve("codex", {"gpt-5-6-terra"})
    assert resolved == "system.ai.gpt-5-6-terra"


def test_an_unserved_pin_wins_when_discovery_found_nothing_at_all(monkeypatch):
    """The third case. The catalogue is empty, so there is no evidence against
    the pin and it is the most informed guess available."""
    monkeypatch.setenv("CODEX_MODEL", "gpt-6")
    assert models.resolve("codex", set()) == "system.ai.gpt-6"


def test_a_pin_written_fully_qualified_moves_the_rung_it_names(monkeypatch):
    """An operator may reasonably write the name the gateway answers to. That
    must move the rung already in the chain rather than adding a second spelling
    of it in front, or the chain grows a duplicate that can never be reached."""
    monkeypatch.setenv("ANTHROPIC_MODEL", "system.ai.claude-opus-5")
    chain = models.chain("driver")
    assert chain[0] == "claude-opus-5"
    assert chain.count("claude-opus-5") == 1


def test_roles_without_a_pin_ignore_a_same_named_variable(monkeypatch):
    """Claude reads three model slots at once, so pinning them individually
    invites the combination where /model opus is cheaper than the default. The
    profile is the only way to move them."""
    monkeypatch.setenv("frontier", "claude-fable-5")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    assert models.chain("frontier")[0] == "claude-opus-5"


# -- resolution against what a workspace actually serves --

def test_resolution_walks_past_what_the_workspace_does_not_serve():
    """A region a release behind serves an older rung and nothing newer, which
    is the case the chain exists for."""
    available = {"claude-opus-4-6"}
    assert models.resolve("frontier", available) == "system.ai.claude-opus-4-6"


def test_empty_discovery_returns_the_chain_head_unverified():
    """Empty means the API call failed, not that the workspace serves nothing.
    Writing a config with the newest model we know of beats writing none."""
    assert models.resolve("frontier", set()) == "system.ai.claude-opus-5"


def test_resolution_prefers_the_earliest_served_candidate():
    available = {
        "claude-opus-4-6",
        "claude-opus-5",
        "claude-sonnet-5",
    }
    assert models.resolve("frontier", available) == "system.ai.claude-opus-5"


def test_resolve_all_covers_every_pinnable_role():
    resolved = models.resolve_all({"claude-sonnet-5"})
    assert set(resolved) == set(ROLES)
    assert all(resolved.values())


# -- rendering: short names inside, qualified names on the wire --

def test_resolve_returns_a_name_the_gateway_will_answer():
    """The gateway 404s a bare short name and answers the qualified one, so
    resolving and qualifying have to be the same step. A caller that had to
    remember to add the prefix is a caller that eventually forgets, which is
    how the whole class of bug this migration fixed got in."""
    for role in ROLES:
        assert models.resolve(role, ()).startswith("system.ai.")


def test_chains_stay_short_so_a_prefix_is_written_once():
    for name, profile in models.PROFILES.items():
        for role in ROLES:
            for candidate in getattr(profile, role):
                assert not candidate.startswith("system.ai."), f"{name}.{role}"
                assert not candidate.startswith("databricks-"), f"{name}.{role}"


def test_qualifying_is_idempotent_and_reversible():
    assert models.service_name("claude-opus-5") == "system.ai.claude-opus-5"
    assert models.service_name("system.ai.claude-opus-5") == "system.ai.claude-opus-5"
    assert models.short_name("system.ai.claude-opus-5") == "claude-opus-5"
    assert models.short_name("claude-opus-5") == "claude-opus-5"


def test_a_context_window_variant_survives_to_the_wire_but_matches_its_base():
    """`[1m]` selects a million-token variant of the same model. Discovery lists
    the base, so the suffix has to come off to match — and go back on for the
    request, because the suffix is how the variant is asked for."""
    pinned = "system.ai.claude-sonnet-4-6[1m]"
    assert models.catalogue_key(pinned) == "claude-sonnet-4-6"
    assert models.service_name(pinned) == pinned


def test_a_variant_pin_resolves_against_the_base_model_discovery_reports(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "system.ai.claude-sonnet-4-6[1m]")
    resolved = models.resolve(
        "driver", {"claude-sonnet-4-6": {models.ANTHROPIC_MESSAGES}}
    )
    assert resolved == "system.ai.claude-sonnet-4-6[1m]"


# -- the wire filter --

def test_a_chat_only_model_is_never_chosen_for_codex():
    """The failure this filter exists to make impossible. glm-5-2 really does
    serve `mlflow/v1/chat/completions` and really does not serve
    `openai/v1/responses` — codex-cli speaks only the latter, so a codex config
    naming GLM is broken before the attendee types anything. This used to be a
    comment asking the next reader not to do it."""
    catalogue = {
        "glm-5-2": {models.CHAT_COMPLETIONS, "mlflow/v1/responses"},
        "gpt-5-6-terra": {models.OPENAI_RESPONSES},
    }
    assert models.resolve("codex", catalogue) == "system.ai.gpt-5-6-terra"


def test_a_chat_only_model_is_refused_for_codex_even_when_pinned(monkeypatch):
    """A pin leads the chain but does not exempt it from the wire check: the
    operator asked for something the harness cannot speak to, and honouring it
    would hand every attendee a Codex that dies on the first turn."""
    monkeypatch.setenv("CODEX_MODEL", "glm-5-2")
    catalogue = {
        "glm-5-2": {models.CHAT_COMPLETIONS},
        "gpt-5-6-terra": {models.OPENAI_RESPONSES},
    }
    assert models.resolve("codex", catalogue) == "system.ai.gpt-5-6-terra"


def test_the_same_chat_only_model_is_welcome_where_the_wire_fits():
    """The filter is about the wire, not about the model being second-rate."""
    catalogue = {"gpt-oss-120b": {models.CHAT_COMPLETIONS}}
    assert models.resolve("insight", catalogue) == "system.ai.gpt-oss-120b"


def test_a_catalogue_without_wire_information_does_not_reject_everything():
    """Callers that hold a bare list of names — and a discovery response that
    omitted the field — must resolve on membership rather than failing a check
    they have no data for."""
    assert models.resolve("codex", {"gpt-5-6-terra"}) == "system.ai.gpt-5-6-terra"
    assert models.resolve("codex", {"gpt-5-6-terra": None}) == "system.ai.gpt-5-6-terra"


def test_every_role_declares_the_wire_it_speaks():
    """A role with no wire silently accepts any model, which is the state this
    module was in before and the reason GLM could reach Codex."""
    for role in ROLES:
        assert models.wire(role), role


def test_an_unknown_role_is_a_programming_error_not_a_silent_default():
    with pytest.raises(KeyError):
        models.role("cheapest")


# -- what the profiles are for --

def test_economy_puts_a_ceiling_on_the_opus_slot(monkeypatch):
    """The lever a large free event needs: an attendee typing /model opus gets
    Sonnet, at a fifth of Opus's output price. A real ceiling, not advice."""
    monkeypatch.setenv("WORKSHOP_MODEL_PROFILE", "economy")
    assert models.chain("frontier")[0] == "claude-sonnet-5"
    assert not any("opus" in c for c in models.chain("frontier"))
    assert not any("opus" in c for c in models.chain("driver"))


def test_economy_moves_codex_to_the_cheap_tier(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MODEL_PROFILE", "economy")
    assert models.chain("codex")[0] == "gpt-5-6-luna"


def test_economy_moves_the_summariser_to_the_cheapest_thing_that_works(monkeypatch):
    """gpt-oss-120b is around a sixth of Haiku."""
    monkeypatch.setenv("WORKSHOP_MODEL_PROFILE", "economy")
    assert models.chain("insight")[0] == "gpt-oss-120b"


def test_frontier_promotes_the_everyday_driver(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MODEL_PROFILE", "frontier")
    assert models.chain("driver")[0] == "claude-opus-5"
    assert models.chain("codex")[0] == "gpt-5-6-sol"


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
