"""Which model plays which role in an event, in one place.

Model names were previously chosen in four places — Claude's settings, Codex's
config.toml, Omnigent's provider block and the insight summariser — with the
Claude chains copy-pasted between two of them under a comment claiming they were
one source of truth. They drifted, as copies do: the Codex default sat on
``gpt-5-5`` long after better and cheaper endpoints shipped, and the Opus chain
never learned that Opus 5 existed. This module is that single source of truth,
so refreshing an event's models is one edit in one file.

A role, not a model, is what callers ask for. "The everyday driver" and "the
strongest thing available" are stable ideas; the model that best serves each one
changes every few weeks, and a caller that names ``claude-opus-5`` directly has
to be found and edited when it does.

Names here are *canonical short names* — ``claude-opus-5``, not
``system.ai.claude-opus-5`` and not the retired ``databricks-claude-opus-5``.
Unity AI Gateway wants the fully-qualified Unity Catalog model service name on
the wire, so :func:`resolve` renders one; the chains stay short because a
prefix repeated forty times is forty chances to typo it. Bare short names do
*not* work on the wire — the gateway answers ``NOT_FOUND`` for ``gpt-5-6-terra``
and 200 for ``system.ai.gpt-5-6-terra`` — which is why resolving and rendering
are the same step rather than two a caller has to remember to pair.

Each role resolves through three layers, in order:

1. An explicit env pin, if the deployment set one and the workspace serves it.
   Pinning is how an event runs a model this code has never heard of.
2. The role's chain, newest and strongest first, filtered to model services the
   workspace actually serves *on the wire that role speaks*. This is what makes
   the same deployment work in a region that is a release behind.
3. The pin, or failing that the head of the chain, unverified — reached only
   when nothing above survived, which in practice means discovery failed.
   Guessing beats refusing to write a config.

A pin therefore *leads* the chain rather than replacing it, and a pin the
workspace does not serve degrades to the best thing it does serve. That looks
like ignoring an operator, so it is worth being explicit about why: the three
ways a pin ends up unserved are a typo, a region that never got that model, and
a failed discovery call. In the first two, a working CLI on a neighbouring model
is a better outcome than a broken one, and /readyz reports the pin either way.
In the third the catalogue is empty, so the pin wins by falling through to (3).

The wire filter in (2) is the part that is new, and it exists because the old
one was worse than useless. Legacy discovery reported ``READY`` for endpoints
that answered 501 ``no longer available``, so a chain "verified" against it
resolved confidently onto a dead model. Unity Catalog model services publish a
``supported_api_types`` list instead, which is both a liveness signal and a
statement of which wire each model speaks — so "GLM cannot serve the Responses
API" stops being a comment someone has to remember and becomes a fact the
resolver reads.

The profile (``WORKSHOP_MODEL_PROFILE``) shifts several roles together, because
cost posture is an event-level decision rather than a per-role one: a free
community workshop and a paid customer POC want different answers to "may an
attendee reach Opus", and an operator should be able to say which without
knowing our model taxonomy.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Unity Catalog model services live in one schema, and every name a caller puts
# on the wire is qualified into it. ``system.ai`` is Databricks-owned and
# undeletable, which is what lets this be a constant rather than configuration.
SERVICE_SCHEMA = "system.ai"
_SERVICE_PREFIX = SERVICE_SCHEMA + "."

# The retired namespace. Kept only so a pin carrying it can be recognised and
# folded to its canonical short name — see :func:`short_name`. Nothing here
# ever writes this prefix.
_RETIRED_PREFIX = "databricks-"

# A trailing bracketed token selects a context-window variant of the same model
# — ``system.ai.claude-sonnet-4-6[1m]`` is the million-token Sonnet 4.6. It is a
# real convention (Databricks' own managed Claude Code settings use it) but it
# is gated per workspace, so nothing here ships one by default. What matters is
# that a pin carrying one still matches its base model during discovery, which
# is why the suffix is stripped for catalogue lookups and preserved on the wire.
_VARIANT_SUFFIX = re.compile(r"\[[^\]]*\]$")

# The wires a model can answer on, as Unity AI Gateway reports them in a model
# service's ``supported_api_types``. A role speaks exactly one of these, and a
# model that does not list it cannot fill that role however good it is.
ANTHROPIC_MESSAGES = "anthropic/v1/messages"
OPENAI_RESPONSES = "openai/v1/responses"
CHAT_COMPLETIONS = "mlflow/v1/chat/completions"


def service_name(name: str) -> str:
    """The fully-qualified model service name to put on the wire.

    Idempotent, so a pin an operator already qualified is left exactly as
    written — including any context-window suffix, which the gateway resolves
    and we have no business rewriting.
    """
    name = name.strip()
    if not name or name.startswith(_SERVICE_PREFIX):
        return name
    return _SERVICE_PREFIX + short_name(name)


def short_name(name: str) -> str:
    """The canonical short name, with either namespace prefix removed.

    A fully-qualified name is taken at its word and only loses the schema, so a
    service whose own name happens to begin ``databricks-`` survives being
    named explicitly. An *unqualified* ``databricks-`` prefix can only be the
    retired endpoint spelling, because that prefix was the legacy namespace
    marker and no short name inside ``system.ai`` carries it — so it is folded
    away, and a stale ``ANTHROPIC_MODEL=databricks-claude-sonnet-5`` moves the
    rung already in the chain instead of qualifying into
    ``system.ai.databricks-claude-sonnet-5``, which names nothing.

    Folding here and refusing the same spelling in ``scripts/deploy_ct_sim.py``
    is deliberate rather than inconsistent. A deploy is where someone is still
    watching and the two catalogues are not the same set, so an operator should
    look at what this event's workspace serves. At runtime the choice is
    between a name that cannot resolve and the model the chain would otherwise
    have picked, which is the pin-degradation policy this module already
    applies to a pin the workspace does not serve.
    """
    name = name.strip()
    if name.startswith(_SERVICE_PREFIX):
        return name[len(_SERVICE_PREFIX):]
    return name[len(_RETIRED_PREFIX):] if name.startswith(_RETIRED_PREFIX) else name


def catalogue_key(name: str) -> str:
    """How a name is looked up in a discovered catalogue.

    Discovery reports base models, so a context-window variant has to be
    matched against the model it varies: ``claude-sonnet-4-6[1m]`` is served
    exactly when ``claude-sonnet-4-6`` is.
    """
    return _VARIANT_SUFFIX.sub("", short_name(name))


# What a workspace serves, as discovery reports it: canonical short name ->
# the wires that model answers on. ``None`` for a value means "served, wires
# unknown" — the shape a caller passes when it has a list of names and no
# capability information, and the only case where the wire filter stands down.
Catalogue = Mapping[str, "frozenset[str] | None"]


def _as_catalogue(available: Catalogue | Iterable[str] | None) -> dict[str, frozenset[str] | None]:
    """Normalise the several shapes callers hold into one.

    A mapping arrives from discovery and carries wires. A bare iterable of
    names arrives from tests and from callers that only know what exists;
    those entries resolve on membership alone rather than silently failing a
    wire check they have no data for.
    """
    if not available:
        return {}
    if isinstance(available, Mapping):
        return {
            catalogue_key(name): (None if wires is None else frozenset(wires))
            for name, wires in available.items()
        }
    return {catalogue_key(name): None for name in available}


def _serves(
    catalogue: Mapping[str, frozenset[str] | None], name: str, wire: str | None
) -> bool:
    """Whether the workspace serves ``name`` on ``wire``.

    Absent from the catalogue is a no. Present with unknown wires is a yes,
    because the alternative is rejecting every candidate on missing evidence.
    Present with wires that do not include this one is a no, and that is the
    check that keeps a chat-only model out of a Responses-wire role.
    """
    key = catalogue_key(name)
    if key not in catalogue:
        return False
    wires = catalogue[key]
    return wire is None or wires is None or wire in wires


# Anthropic-wire chains, newest first. Fable 5 is deliberately absent from the
# frontier chain despite being the strongest Claude on the gateway: at $10/$50
# per million tokens it is twice Opus, so an event that wants it should say so
# with ANTHROPIC_MODEL rather than have every attendee opted in by default.
#
# Opus 4.7 used to sit between 4.8 and 4.6 here and is the model that broke a
# live event: it was the first name the legacy retirement took out, and legacy
# discovery kept calling it READY while it answered 501. It is gone rather than
# demoted because a rung that exists in one region and not the next is exactly
# the drift this chain is meant to absorb, and three rungs already do that.
_OPUS = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-6",
)
_SONNET = (
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
)
_HAIKU = ("claude-haiku-4-5",)

# Responses-API chains for Codex. The GPT-5.6 tiers are capability names that
# OpenAI intends to keep across generations — Sol the flagship, Terra the
# balanced default, Luna the cheap one — so these read as roles too.
#
# Terra rather than Sol as the everyday default because it lands within a point
# or two of Sol on coding evals at half the price, and a room of attendees each
# running an agent is exactly the high-volume case that argument is about. The
# tail is previous-generation: 5-3-codex is coding-tuned and cheap ($1.75/$14).
# gpt-5-5 used to sit behind it and is gone with the legacy retirement — it was
# both the most expensive rung ($5/$30) and the weakest of the three on coding,
# so nothing is owed to it.
_CODEX_STRONG = ("gpt-5-6-sol",)
_CODEX_BALANCED = ("gpt-5-6-terra",)
_CODEX_CHEAP = ("gpt-5-6-luna",)
_CODEX_TAIL = ("gpt-5-3-codex",)

# Summarising a day of prompts is small and well-bounded, so the cheap tier is
# correct here — spending Opus on it buys nothing an account team would notice
# and competes with the attendees' own budget. The tail crosses vendors on
# purpose: this runs unattended after teardown, and a summary is worth having
# from a weaker model rather than not at all.
_INSIGHT = (
    "claude-haiku-4-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "gpt-oss-120b",
    "meta-llama-3-3-70b-instruct",
)
# At $0.15/$0.60 per million tokens this is roughly a sixth of Haiku, which is
# the whole point of the economy profile: the summary is the one LLM call an
# event makes on its own behalf rather than an attendee's.
_INSIGHT_CHEAP = ("gpt-oss-120b",) + tuple(
    m for m in _INSIGHT if m != "gpt-oss-120b"
)


# The model-comparison exercise: same task, different vendor, read the cost.
#
# Three vendors on the chat-completions wire and nowhere else. They were Codex
# profiles until codex-cli 0.144.6 dropped the chat wire entirely — see the note
# in cli_config — and the Responses surface still declines them, which is now
# something the resolver can see for itself rather than a claim in a comment:
# ``system.ai.glm-5-2`` lists ``mlflow/v1/chat/completions`` in its
# ``supported_api_types`` and not ``openai/v1/responses``. The set is still
# published and still smoke-tested; what is gone is the ``codex --profile``
# transport.
#
# Kimi K3 and Gemini 3.6 Flash held two of these slots and did not survive the
# legacy retirement. Their replacements are chosen to keep three *distinct*
# vendors — the whole exercise is reading one task priced by Zhipu, Google and
# Alibaba side by side — and to be present on every catalogue we have seen,
# since a slot that resolves nowhere teaches an attendee nothing.
#
# Each entry is the short name, the model behind it, and how it is described
# when we publish the set. Every one is overridable via ``CODEX_COMPARE_<NAME>``
# so a renamed or withdrawn model is a values change, not a release.
COMPARISON_MODELS: dict[str, tuple[str, str]] = {
    "glm": ("glm-5-2", "GLM 5.2"),
    "gemini": ("gemini-3-5-flash-lite", "Gemini 3.5 Flash Lite"),
    "qwen": ("qwen35-122b-a10b", "Qwen 3.5 122B"),
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


def comparison_models(available: Catalogue | None = None) -> dict[str, str]:
    """Profile name -> model service for every comparison model an attendee can use.

    Filtered two ways. Against discovery, so a region a release behind advertises
    the models it has rather than the ones it does not — an attendee pointed at a
    404 learns nothing about model cost. And
    against the smoke matrix's verdict, so a model that is served but cannot hold
    a tool call is not put in front of a room. An empty or missing ``available``
    means discovery failed, not that the workspace serves nothing, so everything
    survives that filter.
    """
    supported = comparison_supported()
    catalogue = _as_catalogue(available)
    resolved: dict[str, str] = {}
    for name, (default, _label) in COMPARISON_MODELS.items():
        if supported is not None and name not in supported:
            continue
        model = os.environ.get(f"CODEX_COMPARE_{name.upper()}", "").strip() or default
        if not catalogue or _serves(catalogue, model, CHAT_COMPLETIONS):
            resolved[name] = service_name(model)
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

# The wire each role speaks, which is what a candidate has to be able to answer
# on to fill it. The four Claude slots are the Anthropic Messages API because
# that is the only surface Claude Code speaks; codex is the OpenAI Responses API
# because codex-cli 0.144.6 dropped every other wire; insight and the wizard are
# plain chat completions because they are one server-side turn with no tools.
_WIRES = {
    "driver": ANTHROPIC_MESSAGES,
    "frontier": ANTHROPIC_MESSAGES,
    "standard": ANTHROPIC_MESSAGES,
    "fast": ANTHROPIC_MESSAGES,
    "codex": OPENAI_RESPONSES,
    "insight": CHAT_COMPLETIONS,
    "wizard": CHAT_COMPLETIONS,
}


def wire(role_name: str) -> str | None:
    """The API type a role's candidates must support, if it constrains one."""
    return _WIRES.get(role_name)


def serves(
    available: Catalogue | Iterable[str] | None, name: str, role_name: str
) -> bool:
    """Whether ``available`` offers ``name`` on the wire ``role_name`` speaks.

    For the callers that walk a chain themselves rather than handing it to
    :func:`resolve` — the summariser and the wizard both try candidates against
    a live endpoint — so that "is this served?" means the same thing everywhere
    including the wire check. A plain membership test does not: it would hand a
    chat-only model to a role that needs another surface and call it verified.
    """
    return _serves(_as_catalogue(available), name, wire(role_name))


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
    """Candidates for a role in canonical short form, pin first when one is set.

    The pin leads rather than replaces so that a pinned model this region does
    not serve degrades to the next best thing instead of failing — see the
    module docstring for why that is the kinder reading of an operator's intent.

    A pin is normalised to short form before it is compared, so pinning
    ``system.ai.claude-opus-5`` moves the rung that is already in the chain
    rather than adding a second spelling of it in front.
    """
    pin = short_name(pinned(name))
    candidates = role(name).chain
    if not pin:
        return candidates
    return (pin,) + tuple(c for c in candidates if c != pin)


def resolve(name: str, available: Catalogue | Iterable[str]) -> str:
    """The model service to use for a role, ready to put on the wire.

    Returns a fully-qualified name — ``system.ai.claude-sonnet-5``, not
    ``claude-sonnet-5`` — because that is the only form Unity AI Gateway
    answers, and a caller that has to remember to qualify it is a caller that
    will eventually forget.

    :param available: what the workspace serves, from
        :func:`server.cli_config.discover_model_services`. A workspace serving
        nothing from the chain is indistinguishable from a discovery call that
        failed, and both are better served by returning the pin (or the chain
        head) unverified than by returning nothing.
    """
    catalogue = _as_catalogue(available)
    required = wire(name)
    candidates = chain(name)
    for candidate in candidates:
        if _serves(catalogue, candidate, required):
            return service_name(candidate)
    return service_name(pinned(name) or candidates[0])


def resolve_all(available: Catalogue | Iterable[str]) -> dict[str, str]:
    """Every role at once, for callers that write several into one config."""
    return {name: resolve(name, available) for name in _PINS}


# Opening-wizard idea generation. Kept off Profile so an event's cost posture
# does not quietly swap the attendee's first-arrival model, and so the existing
# role tests do not have to learn a sixth slot. Pin with WORKSHOP_WIZARD_MODEL.
_WIZARD = (
    "gpt-5-4-mini",
    "gpt-5-6-luna",
    "claude-haiku-4-5",
    "gpt-oss-120b",
)


def wizard_chain() -> tuple[str, ...]:
    """Candidates for the opening wizard as wire-ready service names, pin first.

    Unlike :func:`chain` this renders, because its caller walks the whole list
    itself rather than handing it to :func:`resolve` — the wizard tries each
    candidate against a live endpoint and takes the first that answers.
    """
    pin = short_name(os.environ.get("WORKSHOP_WIZARD_MODEL", "").strip())
    names = (pin,) + tuple(n for n in _WIZARD if n != pin) if pin else _WIZARD
    return tuple(service_name(n) for n in names)


__all__ = [
    "ANTHROPIC_MESSAGES",
    "CHAT_COMPLETIONS",
    "COMPARISON_MODELS",
    "DEFAULT_PROFILE",
    "OPENAI_RESPONSES",
    "PROFILES",
    "SERVICE_SCHEMA",
    "Catalogue",
    "Profile",
    "Role",
    "catalogue_key",
    "chain",
    "comparison_models",
    "comparison_supported",
    "pinned",
    "profile",
    "resolve",
    "resolve_all",
    "role",
    "serves",
    "service_name",
    "short_name",
    "wire",
    "wizard_chain",
]
