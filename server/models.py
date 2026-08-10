"""Which model plays which role in an event, in one place.

Model names were previously chosen in four places — Claude's settings, Codex's
config.toml, Omnigent's provider block and the insight summariser — with the
Claude chains copy-pasted between two of them under a comment claiming they were
one source of truth. They drifted, as copies do: the Codex default sat on
``databricks-gpt-5-5`` long after better and cheaper endpoints shipped, and the
Opus chain never learned that Opus 5 existed. This module is that single source
of truth, so refreshing an event's models is one edit in one file.

A role, not a model, is what callers ask for. "The everyday driver" and "the
strongest thing available" are stable ideas; the endpoint that best serves each
one changes every few weeks, and a caller that names ``databricks-claude-opus-5``
directly has to be found and edited when it does.

Each role resolves through three layers, in order:

1. An explicit env pin, if the deployment set one and the workspace serves it.
   Pinning is how an event runs a model this code has never heard of.
2. The role's chain, newest and strongest first, filtered to endpoints the
   workspace reports READY. This is what makes the same deployment work in a
   region that is a release behind.
3. The pin, or failing that the head of the chain, unverified — reached only
   when nothing above was READY, which in practice means discovery failed.
   Guessing beats refusing to write a config.

A pin therefore *leads* the chain rather than replacing it, and a pin the
workspace does not serve degrades to the best thing it does serve. That looks
like ignoring an operator, so it is worth being explicit about why: the three
ways a pin ends up unserved are a typo, a region that never got that endpoint,
and a failed discovery call. In the first two, a working CLI on a neighbouring
model is a better outcome than a broken one, and /readyz reports the pin either
way. In the third nothing is READY, so the pin wins by falling through to (3).


The profile (``WORKSHOP_MODEL_PROFILE``) shifts several roles together, because
cost posture is an event-level decision rather than a per-role one: a free
community workshop and a paid customer POC want different answers to "may an
attendee reach Opus", and an operator should be able to say which without
knowing our model taxonomy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Anthropic-wire chains, newest first. Fable 5 is deliberately absent from the
# frontier chain despite being the strongest Claude on the gateway: at $10/$50
# per million tokens it is twice Opus, so an event that wants it should say so
# with ANTHROPIC_MODEL rather than have every attendee opted in by default.
_OPUS = (
    "databricks-claude-opus-5",
    "databricks-claude-opus-4-8",
    "databricks-claude-opus-4-7",
    "databricks-claude-opus-4-6",
)
_SONNET = (
    "databricks-claude-sonnet-5",
    "databricks-claude-sonnet-4-6",
    "databricks-claude-sonnet-4-5",
)
_HAIKU = ("databricks-claude-haiku-4-5",)

# Responses-API chains for Codex. The GPT-5.6 tiers are capability names that
# OpenAI intends to keep across generations — Sol the flagship, Terra the
# balanced default, Luna the cheap one — so these read as roles too.
#
# Terra rather than Sol as the everyday default because it lands within a point
# or two of Sol on coding evals at half the price, and a room of attendees each
# running an agent is exactly the high-volume case that argument is about. Both
# tails are previous-generation: 5-3-codex is coding-tuned and cheaper than 5-5
# ($1.75/$14 against $5/$30), so 5-5 sits last as the rung we used to ship
# rather than one we would choose.
_CODEX_STRONG = ("databricks-gpt-5-6-sol",)
_CODEX_BALANCED = ("databricks-gpt-5-6-terra",)
_CODEX_CHEAP = ("databricks-gpt-5-6-luna",)
_CODEX_TAIL = ("databricks-gpt-5-3-codex", "databricks-gpt-5-5")

# Summarising a day of prompts is small and well-bounded, so the cheap tier is
# correct here — spending Opus on it buys nothing an account team would notice
# and competes with the attendees' own budget. The tail crosses vendors on
# purpose: this runs unattended after teardown, and a summary is worth having
# from a weaker model rather than not at all.
_INSIGHT = (
    "databricks-claude-haiku-4-5",
    "databricks-claude-sonnet-5",
    "databricks-claude-sonnet-4-6",
    "databricks-gpt-oss-120b",
    "databricks-meta-llama-3-3-70b-instruct",
)
# At $0.15/$0.60 per million tokens this is roughly a sixth of Haiku, which is
# the whole point of the economy profile: the summary is the one LLM call an
# event makes on its own behalf rather than an attendee's.
_INSIGHT_CHEAP = ("databricks-gpt-oss-120b",) + tuple(
    m for m in _INSIGHT if m != "databricks-gpt-oss-120b"
)


# The model-comparison exercise, as Codex profiles.
#
# Comparing what different models cost for the same task used to require Pi,
# because Pi is the only harness that routes per model across the Anthropic,
# Responses and chat-completions surfaces. That made the most fragile component
# in the room load-bearing for its headline exercise. Bare Codex reaches the
# same models through one plain OpenAI-shaped surface — verified 200s on
# ``<host>/serving-endpoints/chat/completions`` — and touches no OBO token on
# the way, so the comparison survives everything Phase 2 is about.
#
# Each entry is a Codex profile name the attendee types (``codex --profile
# glm``), the endpoint behind it, and how it is described when we publish the
# set. Every one is overridable via ``CODEX_COMPARE_<NAME>`` so a renamed or
# withdrawn endpoint is a values change, not a release.
COMPARISON_MODELS: dict[str, tuple[str, str]] = {
    "glm": ("databricks-glm-5-2", "GLM 5.2"),
    "kimi": ("databricks-kimi-k3", "Kimi K3"),
    "gemini": ("databricks-gemini-3-6-flash", "Gemini 3.6 Flash"),
}


def comparison_supported() -> frozenset[str] | None:
    """The profiles ``scripts/smoke_models.py`` proved usable, or ``None``.

    Serving an endpoint is not the same as being usable from Codex: these models
    answer a plain turn readily and vary widely on tool calling, which is the
    only part that matters for an agent that has to edit files. The smoke matrix
    measures that and prints a ``WORKSHOP_CODEX_COMPARE`` line; setting it drops
    the failures for the next event without a release. Unset means unmeasured,
    and unmeasured advertises everything served — the same posture as before the
    matrix existed.
    """
    raw = os.environ.get("WORKSHOP_CODEX_COMPARE", "").strip()
    if not raw:
        return None
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def comparison_models(available: set[str] | None = None) -> dict[str, str]:
    """Profile name -> endpoint for every comparison model an attendee can use.

    Filtered two ways. Against discovery, so a region a release behind advertises
    the profiles it has rather than the ones it does not — an attendee typing
    ``codex --profile kimi`` into a 404 learns nothing about model cost. And
    against the smoke matrix's verdict, so a model that is served but cannot hold
    a tool call is not put in front of a room. An empty or missing ``available``
    means discovery failed, not that the workspace serves nothing, so everything
    survives that filter.
    """
    supported = comparison_supported()
    resolved: dict[str, str] = {}
    for name, (default, _label) in COMPARISON_MODELS.items():
        if supported is not None and name not in supported:
            continue
        model = os.environ.get(f"CODEX_COMPARE_{name.upper()}", "").strip() or default
        if not available or model in available:
            resolved[name] = model
    return resolved


@dataclass(frozen=True)
class Role:
    """One job a model does, and how to fill it.

    :param env: The pin that overrides everything, or empty for roles a
        deployment cannot address directly. The Claude slot roles are addressable
        only through the profile, because Claude Code reads three model slots at
        once and letting an operator pin them individually invites the
        combination where /model opus is cheaper than the default.
    :param chain: Candidates, best first.
    """

    name: str
    env: str
    chain: tuple[str, ...]


@dataclass(frozen=True)
class Profile:
    """An event's cost posture, as the set of roles it implies."""

    name: str
    driver: tuple[str, ...]
    frontier: tuple[str, ...]
    standard: tuple[str, ...]
    fast: tuple[str, ...]
    codex: tuple[str, ...]
    insight: tuple[str, ...]


# ``balanced`` reproduces what the terminal shipped before this module existed,
# so an unset WORKSHOP_MODEL_PROFILE changes nothing about a running event: a
# Sonnet driver that falls through to Opus when no Sonnet is READY, Opus in
# Claude's Opus slot, Haiku in its fast slot.
#
# ``economy`` is the one profile that takes something away, and does it where it
# costs the most: Claude's Opus slot resolves to Sonnet, so an attendee typing
# /model opus gets Sonnet at a fifth of the output price. That is a real ceiling
# rather than a suggestion, which is what a large free event needs.
#
# ``frontier`` promotes the everyday driver to Opus and Codex to Sol. It is the
# posture for a small paid engagement where an hour of an architect's attention
# costs more than the tokens.
PROFILES: dict[str, Profile] = {
    "economy": Profile(
        name="economy",
        driver=_SONNET,
        frontier=_SONNET,
        standard=_SONNET,
        fast=_HAIKU,
        codex=_CODEX_CHEAP + _CODEX_BALANCED + _CODEX_TAIL,
        insight=_INSIGHT_CHEAP,
    ),
    "balanced": Profile(
        name="balanced",
        driver=_SONNET + _OPUS,
        frontier=_OPUS,
        standard=_SONNET,
        fast=_HAIKU,
        codex=_CODEX_BALANCED + _CODEX_STRONG + _CODEX_TAIL,
        insight=_INSIGHT,
    ),
    "frontier": Profile(
        name="frontier",
        driver=_OPUS + _SONNET,
        frontier=_OPUS,
        standard=_SONNET,
        fast=_HAIKU,
        codex=_CODEX_STRONG + _CODEX_BALANCED + _CODEX_TAIL,
        insight=_INSIGHT,
    ),
}

DEFAULT_PROFILE = "balanced"

# The env pin per role. ANTHROPIC_MODEL and CODEX_MODEL are the two an operator
# already knows by name; the rest are addressable only through the profile. All
# three are optional — /readyz reports which are set and requires none, because
# naming a posture is the supported way to configure an event.
_PINS = {
    "driver": "ANTHROPIC_MODEL",
    "frontier": "",
    "standard": "",
    "fast": "",
    "codex": "CODEX_MODEL",
    "insight": "INSIGHT_SUMMARY_MODEL",
}


def profile() -> Profile:
    """The event's profile, falling back to ``balanced`` on anything unknown.

    A typo in a deployment variable should not stop a workshop, and the value it
    would fall back to is the one every event ran before profiles existed.
    """
    requested = os.environ.get("WORKSHOP_MODEL_PROFILE", "").strip().lower()
    return PROFILES.get(requested) or PROFILES[DEFAULT_PROFILE]


def pinned(role: str) -> str:
    """The explicit pin for a role, or empty when the deployment set none."""
    name = _PINS.get(role, "")
    return os.environ.get(name, "").strip() if name else ""


def role(name: str) -> Role:
    """Resolve a role name against the active profile."""
    active = profile()
    chain = getattr(active, name, None)
    if chain is None:
        raise KeyError(f"no such model role: {name}")
    return Role(name=name, env=_PINS.get(name, ""), chain=chain)


def chain(name: str) -> tuple[str, ...]:
    """Candidates for a role, pin first when one is set.

    The pin leads rather than replaces so that a pinned model which is not READY
    in this region degrades to the next best thing instead of failing — see the
    module docstring for why that is the kinder reading of an operator's intent.
    """
    pin = pinned(name)
    candidates = role(name).chain
    if not pin:
        return candidates
    return (pin,) + tuple(c for c in candidates if c != pin)


def resolve(name: str, available: set[str]) -> str:
    """The model to use for a role, given what the workspace reports READY.

    :param available: READY endpoint names. A workspace serving nothing from the
        chain is indistinguishable from a discovery call that failed, and both
        are better served by returning the pin (or the chain head) unverified
        than by returning nothing.
    """
    candidates = chain(name)
    for candidate in candidates:
        if candidate in available:
            return candidate
    return pinned(name) or candidates[0]


def resolve_all(available: set[str]) -> dict[str, str]:
    """Every role at once, for callers that write several into one config."""
    return {name: resolve(name, available) for name in _PINS}


__all__ = [
    "COMPARISON_MODELS",
    "DEFAULT_PROFILE",
    "PROFILES",
    "Profile",
    "Role",
    "chain",
    "comparison_models",
    "comparison_supported",
    "pinned",
    "profile",
    "resolve",
    "resolve_all",
    "role",
]
