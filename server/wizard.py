"""The opening wizard: what the attendee wants to build, before they build it.

Three screens over the landing page, all skippable. It exists because the worst
moment in this product is a working terminal, a blinking cursor and no idea what
to type — and because the answer to "what are you here to build" is the single
most valuable thing an account team can learn from the day.

Two jobs, one conversation. It is deliberately not a form:

- **Launch.** Whatever the attendee types (or picks) becomes the first prompt in
  the terminal, so the wizard ends with something running rather than with a
  saved profile.
- **Discovery.** The same answer becomes a ``discovery.record`` at
  ``confidence: high`` — stated by the attendee rather than inferred by an agent
  mid-conversation, which is the difference between a brief that quotes someone
  and one that guesses at them.

Two rules shape everything here:

1. **The record_id is minted here and reused.** The agent is told the id and
   refines that record as it learns more. Without this the wizard's version and
   the agent's version arrive at Control Tower as two unrelated use cases for one
   person, and the brief reads as though they wanted two different things.
2. **Only provably buildable ideas are shown.** See ``select_ideas``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config, content, demo_data, discovery
from .users import User

logger = logging.getLogger(__name__)

_BRIEF_RELATIVE = os.path.join(".workshop", "brief.json")

# Six cards. Enough that someone with no idea finds one that lands, few enough to
# read in the few seconds they will actually give it before their attention goes
# back to the room.
IDEA_COUNT = 6

# Matches discovery.SESSION_INTENTS. The wizard asks the question in the
# attendee's words ("Solve a problem at work"), and the value is the contract.
INTENTS = discovery.SESSION_INTENTS

MAX_TEXT = 2000
MAX_LIST_ITEMS = 12
MAX_ITEM_CHARS = 120

_DEFAULT_STACKS = [
    "Snowflake",
    "BigQuery",
    "Redshift",
    "Synapse / Fabric",
    "SQL Server",
    "Oracle",
    "Postgres",
    "Kafka",
    "dbt",
    "Airflow",
    "Tableau",
    "Power BI",
    "Spreadsheets",
]
_DEFAULT_INTENT_LABELS = {
    "business_problem": "Solving a real problem from work",
    "evaluation": "Seeing whether Databricks can do this",
    "learning": "Learning how this works",
    "fun": "Building something fun",
}


@dataclass
class WizardBrief:
    """What the attendee told the wizard. Every field optional but ``record_id``.

    ``seen`` and ``skipped`` live here rather than in the browser so that a
    reload, a second tab, or the reconnect after a wifi flap does not re-present
    a modal someone already dealt with. A workshop room is exactly where all
    three happen.
    """

    record_id: str = ""
    what_building: str = ""
    industry: str = ""
    intent: str = ""
    idea_id: str = ""
    current_stack: list[str] = field(default_factory=list)
    persona: str = ""
    seen: bool = False
    skipped: bool = False
    completed_at: str = ""
    # True only when the attendee continued with the industry chip visible, or
    # picked a card that named one. Pack/env default_industry may preselect the
    # UI; it must not reach discovery or the overlay until they confirm it.
    industry_stated: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "WizardBrief":
        stack = raw.get("current_stack") or []
        return cls(
            record_id=str(raw.get("record_id") or ""),
            what_building=str(raw.get("what_building") or ""),
            industry=str(raw.get("industry") or ""),
            intent=str(raw.get("intent") or ""),
            idea_id=str(raw.get("idea_id") or ""),
            current_stack=[str(v) for v in stack if str(v).strip()],
            persona=str(raw.get("persona") or ""),
            seen=bool(raw.get("seen")),
            skipped=bool(raw.get("skipped")),
            completed_at=str(raw.get("completed_at") or ""),
            industry_stated=bool(raw.get("industry_stated")),
        )

    @property
    def has_content(self) -> bool:
        """Whether the attendee actually said anything worth recording.

        Skipping sets ``seen`` and nothing else, and a brief with no content must
        not reach discovery: a row saying an attendee exists and wants nothing is
        worse than no row, because it looks like a finding.
        """
        stated = self.industry if self.industry_stated else ""
        return bool(self.what_building.strip() or self.idea_id or stated)

    @property
    def stated_industry(self) -> str:
        """The industry they confirmed, never the pack/env preselect."""
        return self.industry if self.industry_stated else ""


# -- storage (one file per attendee, beside the persona) --

_write_lock = threading.Lock()


def brief_path(user: User) -> str:
    return os.path.join(user.home, _BRIEF_RELATIVE)


def read_brief(user: User) -> WizardBrief:
    """The attendee's brief, or an empty one. Never raises."""
    try:
        with open(brief_path(user), encoding="utf-8") as fh:
            return WizardBrief.from_json(json.load(fh))
    except FileNotFoundError:
        return WizardBrief()
    except Exception as exc:  # noqa: BLE001 — a corrupt brief must not block the UI
        logger.warning("wizard brief unreadable for %s: %s", user.email, exc)
        return WizardBrief()


def write_brief(user: User, brief: WizardBrief) -> None:
    path = brief_path(user)
    with _write_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(brief.to_json(), fh)
        os.replace(tmp, path)


# -- sanitising --

def _clean_text(value: Any) -> str:
    return str(value or "").strip()[:MAX_TEXT]


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        return []
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item[:MAX_ITEM_CHARS])
    return out[:MAX_LIST_ITEMS]


# -- idea selection --

def default_industry() -> str:
    """The industry to preselect before the attendee says anything.

    ``WORKSHOP_DEFAULT_INDUSTRY`` (the Control Tower create-form choice) wins
    over the content pack, so an operator does not need a pack edit to name the
    room. Honoured only when that industry is actually seeded — a value the
    notebook never created would otherwise silently empty the grid.
    """
    configured = (
        config.workshop_default_industry().strip()
        or content.content_service.default_industry().strip()
    )
    if not configured:
        return ""
    slug = demo_data.industry_slug(configured)
    # Honoured when the notebook knows the industry, even if this deployment's
    # catalog is unseeded or briefly unreadable. It used to require live
    # inventory, which meant an unreachable Unity Catalog silently discarded the
    # operator's create-form choice and the room was told nothing.
    if demo_data.has_industry(configured) or slug in demo_data.KNOWN_INDUSTRIES:
        return slug
    return ""


def industry_of(idea: content.WizardIdea) -> str:
    """The schema this card belongs to, if it belongs to one.

    Tagged ``industries`` win; otherwise the schema of the first demo table.
    A generic card (no tags, no tables) returns empty — picking it is a choice
    *not* to steer the agent at a schema.
    """
    if idea.industries:
        return demo_data.industry_slug(idea.industries[0])
    if idea.demo_tables:
        schema, _, _ = idea.demo_tables[0].partition(".")
        return demo_data.industry_slug(schema)
    return ""


def _buildable(idea: content.WizardIdea) -> bool:
    """Whether this card can actually be built with the data in this deployment.

    When there is no demo catalog at all, every card passes. That looks
    permissive, but it is the honest reading: without demo data nobody's tables
    exist, so filtering on them would empty the grid for the one attendee least
    able to recover from an empty grid. They get the full catalogue and an agent
    that will generate what it needs.

    The same applies when a catalog is configured but unreadable — a permission
    error, a cold warehouse, a Unity Catalog blip. That used to be treated as
    "no tables exist", which silently withdrew every data-backed card from the
    grid for the duration, and the attendee saw a thin list of generics with
    nothing saying why. We do not know, so we do not filter; the ``data_ready``
    badge is what carries the uncertainty to the card.
    """
    if not demo_data.enabled() or not demo_data.readable():
        return True
    return demo_data.verify(idea.demo_tables)


def idea_payload(idea: content.WizardIdea) -> dict[str, Any]:
    """A card as the frontend reads it, with the data promise resolved here.

    The badge used to be inferred in the browser from ``demo_tables`` being
    non-empty, which was only correct while every unverified card was filtered
    out server-side. Now that an unreadable catalog no longer empties the grid,
    whether the data is really there has to travel with the card rather than be
    guessed from its shape.
    """
    payload = idea.model_dump()
    payload["data_ready"] = demo_data.data_ready(idea.demo_tables)
    # Honest to the attendee: a card that names no tables will generate data,
    # including generic padding. Do not infer "demo" from a non-empty list the
    # catalog could not verify — ``data_ready`` is the promise, this is the mode.
    payload["data_mode"] = "demo" if idea.demo_tables else "generate"
    return payload


_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if len(t) > 2}


def _score(
    idea: content.WizardIdea, industry: str, intent: str, query: str = ""
) -> int:
    score = 0
    if industry and industry in idea.industries:
        score += 100
    elif not idea.industries:
        score += 10  # generic: always plausible, never preferred over a match
    if intent and intent in idea.intents:
        score += 20
    if query:
        overlap = _tokens(query) & _tokens(f"{idea.label} {idea.outcome}")
        score += 5 * len(overlap)
    return score


def select_ideas(
    industry: str = "",
    intent: str = "",
    limit: int = IDEA_COUNT,
    *,
    rng: random.Random | None = None,
    query: str = "",
) -> list[content.WizardIdea]:
    """The cards to show someone who said they are not sure yet.

    Four properties, in priority order:

    1. **Every card is buildable.** Anything naming demo tables this deployment
       has not seeded is excluded outright, not shown-but-unbadged.
    2. **The grid stays inside the chosen industry.** Tagged matches first,
       then untagged generics. A foreign industry is never pulled in to fill a
       shape — that was how a healthcare attendee saw a random automotive ML
       card.
    3. **The shapes are spread.** Six dashboards tells an attendee who wanted
       to build an app that this workshop is not for them.
    4. **It is never empty.** An unseeded or unset industry degrades to generic
       cards, which need no demo data.
    """
    rng = rng or random.Random()
    buildable = [i for i in content.content_service.ideas() if _buildable(i)]
    if not buildable:
        return []

    # ``industry_slug``, not ``normalize_industry``: the latter answers with the
    # seeded schema or nothing, so an unreadable catalog discarded the chip the
    # attendee had just pressed and served them generics instead of their own
    # industry's cards.
    industry = demo_data.industry_slug(industry) if industry else ""
    generics = [i for i in buildable if not i.industries]
    if industry:
        tagged = [i for i in buildable if industry in i.industries]
        # Exhaust the industry catalogue before padding with generics, including
        # extra cards of a shape already taken — three retail ideas plus three
        # "build a pipeline" generics reads as nothing here for you.
        pool = tagged + generics
    elif demo_data.enabled():
        # No industry chosen: generics only. No generic has shape ``ml``, and
        # filling that hole from a random industry is the leak this filter
        # exists to close. Without a catalog the whole list is reachable.
        pool = list(generics) or buildable
    else:
        pool = buildable

    rng.shuffle(pool)
    pool.sort(key=lambda i: _score(i, industry, intent, query), reverse=True)

    chosen: list[content.WizardIdea] = []
    seen_shapes: set[str] = set()
    for idea in pool:
        if idea.shape not in seen_shapes:
            chosen.append(idea)
            seen_shapes.add(idea.shape)
        if len(chosen) >= limit:
            break
    if len(chosen) < limit:
        picked = {i.id for i in chosen}
        chosen += [i for i in pool if i.id not in picked][: limit - len(chosen)]

    chosen.sort(key=lambda i: _score(i, industry, intent, query), reverse=True)
    return chosen[:limit]


def surprise(industry: str = "", *, rng: random.Random | None = None) -> content.WizardIdea | None:
    """One idea at random, for the "Surprise me" button.

    Drawn from the same verified set the grid uses. It fills the text box locally
    and instantly — delegating to an agent would mean waiting on a model for
    something whose entire value is that it is faster than thinking.
    """
    rng = rng or random.Random()
    pool = select_ideas(industry, limit=12, rng=rng)
    return rng.choice(pool) if pool else None


def idea_by_id(idea_id: str) -> content.WizardIdea | None:
    if not idea_id:
        return None
    for idea in content.content_service.ideas():
        if idea.id == idea_id:
            return idea
    return None


# -- discovery --

def to_discovery(brief: WizardBrief) -> dict[str, Any]:
    """The brief as a ``discovery.record`` submission.

    ``confidence`` is high without qualification: the attendee typed this about
    their own work, unprompted by an agent's interpretation. That is the
    strongest provenance any record in this system has, and a brief built from it
    should not hedge the way one built from a mid-build inference must.

    ``timeline`` is never set. The wizard does not ask — a workshop attendee has
    no authority over their employer's timeline, so a captured answer would be a
    guess that reads downstream as a commitment.
    """
    idea = idea_by_id(brief.idea_id) if brief.idea_id else None
    title = brief.what_building.strip()
    if idea and not title:
        title = idea.label
    products = list(idea.products) if idea else []
    if idea and idea.demo_tables:
        signal = f"wizard_idea:{idea.id}"
    elif idea:
        signal = "wizard_mode:generate"
    elif brief.what_building.strip():
        signal = "wizard_mode:typed"
    else:
        signal = ""
    out: dict[str, Any] = {
        "record_id": brief.record_id,
        "agent": "wizard",
        "confidence": "high",
        "session_intent": brief.intent,
        "industry": brief.stated_industry,
        "use_case_title": title[:120],
        "use_case_summary": brief.what_building.strip(),
        "goal": brief.what_building.strip(),
        "current_stack": list(brief.current_stack),
    }
    if products:
        out["databricks_products"] = products
    if signal:
        out["interest_signals"] = [signal]
    return out


def save(user: User, payload: dict[str, Any]) -> WizardBrief:
    """Persist what the wizard collected and push it to discovery.

    Called when the attendee leaves the second step, not the third: by then they
    have said everything the record needs, and the third step is agent selection.
    Someone who picks an agent from the landing page instead of the wizard's own
    cards must not lose what they already told us.

    **An absent key means unchanged, not cleared.** The dismissal path sends only
    ``{"skipped": true}``, and it fires from the third step too — where the
    attendee has already told us everything. Rebuilding the brief from the
    payload alone would blank a completed brief the moment someone pressed Escape
    on the agent picker, taking the home recap and the agent's instruction
    overlay with it while the discovery record it no longer matches was already
    at Control Tower.
    """
    existing = read_brief(user)

    def given(key: str, clean: Any, previous: Any) -> Any:
        return clean(payload[key]) if key in payload else previous

    brief = WizardBrief(
        # Minted once and kept for the life of the session. Everything downstream
        # — the agent's refinements, CT's de-duplication — hangs off this id
        # being stable across saves.
        record_id=existing.record_id or uuid.uuid4().hex,
        what_building=given("what_building", _clean_text, existing.what_building),
        industry=existing.industry,
        intent=given("intent", lambda v: _clean_text(v).lower(), existing.intent),
        idea_id=given("idea_id", lambda v: _clean_text(v)[:64], existing.idea_id),
        current_stack=given("current_stack", _clean_list, existing.current_stack),
        persona=given("persona", lambda v: _clean_text(v).lower(), existing.persona),
        seen=True,
        completed_at=existing.completed_at,
        industry_stated=existing.industry_stated,
    )
    if brief.intent not in INTENTS:
        brief.intent = ""

    # A tagged card owns the industry: its schema is a fact, the chip is a
    # suggestion. A generic card must not wipe a chip the attendee already
    # confirmed — picking "build a pipeline" is not a choice to forget they
    # said retail. Only an empty derived industry *and* no chip in the payload
    # is a choice not to steer.
    if "idea_id" in payload and brief.idea_id:
        idea = idea_by_id(brief.idea_id)
        if idea:
            derived = industry_of(idea)
            if derived:
                brief.industry = derived
                brief.industry_stated = True
            elif "industry" in payload:
                brief.industry = demo_data.industry_slug(
                    _clean_text(payload["industry"])[:64]
                )
                if "industry_stated" in payload:
                    brief.industry_stated = bool(payload["industry_stated"])
                else:
                    brief.industry_stated = bool(brief.industry)
    elif "industry" in payload:
        # ``industry_slug`` rather than ``normalize_industry``: an attendee who
        # typed "shipping logistics" told us the most useful thing on the
        # record, and resolving that against seeded schemas — which is what
        # normalize does — discarded it for everyone whose industry the
        # notebook had not created.
        brief.industry = demo_data.industry_slug(_clean_text(payload["industry"])[:64])
        if "industry_stated" in payload:
            brief.industry_stated = bool(payload["industry_stated"])
        else:
            # Sending the key is a choice, including clearing it.
            brief.industry_stated = True
    elif "industry_stated" in payload:
        brief.industry_stated = bool(payload["industry_stated"])

    # Skipped means "I declined to tell you", which someone who has already told
    # us cannot retroactively do. Both the home recap and the instruction overlay
    # read this flag, so honouring it on a step-three dismissal would silently
    # withhold a brief the attendee filled in.
    brief.skipped = bool(payload.get("skipped")) and not brief.has_content
    if brief.has_content:
        brief.completed_at = brief.completed_at or discovery._now()

    write_brief(user, brief)

    if brief.skipped or not brief.has_content:
        # Skipping is an answer, and the answer is "leave me alone". Recording it
        # anyway would make the record the one thing the attendee explicitly
        # declined to give.
        return brief

    if existing.has_content and to_discovery(existing) == to_discovery(brief):
        # A dismissal, or a second save of an unchanged brief. Re-emitting would
        # burn a revision on identical content and make the record look like it
        # was being actively refined when nobody touched it.
        return brief

    # No-ops when capture is off, which is the whole consent boundary: a
    # deployment that never opted in holds nothing, wizard or not.
    discovery.record(user.email, to_discovery(brief))
    return brief


def starter_prompt(brief: WizardBrief) -> str:
    """The first thing typed into the terminal, unsent.

    A chosen idea card wins over the typed sentence, because its prompt was
    written to produce a good first build and the sentence was written to
    describe an ambition. When the attendee typed their own, it is handed to the
    agent as their words plus enough framing that the agent starts building
    rather than starts interviewing.
    """
    idea = idea_by_id(brief.idea_id)
    if idea:
        return idea.prompt
    text = brief.what_building.strip()
    if not text:
        return ""
    stated = brief.stated_industry
    if stated and demo_data.enabled() and demo_data.has_industry(stated):
        extra = (
            f" Use the {stated.replace('_', ' ')} demo data already in this "
            f"workspace (schema `{stated}`)."
        )
    elif stated:
        extra = (
            f" Their industry is {stated.replace('_', ' ')}; generate the data "
            "you need rather than substituting another industry's tables."
        )
    else:
        extra = ""
    return (
        f"{text}\n\n"
        "Start building this with me now. Ask me at most one question if you "
        "genuinely cannot start without the answer, otherwise make a sensible "
        "choice and tell me what you chose."
        f"{extra}"
    )


def _effective_model() -> str:
    """The wizard model in force, override included."""
    from . import wizard_llm

    return str(wizard_llm.effective_model()["model"])


def state(user: User, industry: str = "", query: str = "") -> dict[str, Any]:
    """Everything the frontend needs to render the wizard.

    ``industry`` is the filter chip the attendee just pressed, which is not yet
    in the brief — they are still choosing what to build, and the whole point of
    the chip is to see the grid change before committing to anything.

    The shuffle is seeded on the attendee's identity, so the grid is theirs and
    stays theirs. An unseeded selector answers the same question differently
    every time it is asked, which turns a reconnect, a second tab, or a pressed
    filter chip into six new cards under someone who was halfway through reading
    the old ones. Seeding keeps the property the shuffle exists for — the room
    does not all see the same six — without that cost.
    """
    brief = read_brief(user)
    industry = demo_data.industry_slug(_clean_text(industry)[:64]) or (
        brief.stated_industry or default_industry()
    )
    enabled = config.onboarding_wizard_enabled()
    seeded = demo_data.industries()
    offered = demo_data.offered_industries()
    llm_on = config.llm_wizard_enabled()
    stacks = content.content_service.wizard_stacks() or _DEFAULT_STACKS
    intent_labels = content.content_service.wizard_intent_labels() or _DEFAULT_INTENT_LABELS
    return {
        "brief": brief.to_json(),
        "enabled": enabled,
        # A run whose operator switched the wizard off asks nobody anything.
        # Answered on the server rather than in the browser so the decision
        # holds for a reload, a second tab and any future caller of this
        # endpoint at once.
        "should_show": enabled and not brief.seen,
        "default_industry": default_industry(),
        # Everything the notebook can seed, offered whether or not this
        # deployment got it. ``seeded_industries`` is the subset that is really
        # there, which the UI badges rather than filters on — an attendee whose
        # industry is missing should be told the data is missing, not told their
        # industry does not exist.
        "industries": offered,
        "industry_labels": {i: demo_data.industry_label(i) for i in offered},
        "seeded_industries": seeded,
        "demo_data_available": bool(seeded),
        "intents": list(INTENTS),
        "intent_labels": intent_labels,
        "stacks": stacks,
        "ideas": [
            idea_payload(i)
            for i in select_ideas(
                industry, rng=random.Random(user.email), query=query
            )
        ],
        "capture_enabled": config.discovery_enabled(),
        "llm_wizard": {
            "enabled": llm_on,
            # Whatever is in force right now, which after a live swap is not the
            # deployed pin. Imported here rather than at module scope because
            # ``wizard_llm`` imports this module.
            "model": _effective_model(),
        },
    }


__all__ = [
    "IDEA_COUNT",
    "INTENTS",
    "WizardBrief",
    "brief_path",
    "default_industry",
    "idea_by_id",
    "idea_payload",
    "industry_of",
    "read_brief",
    "save",
    "select_ideas",
    "starter_prompt",
    "state",
    "surprise",
    "to_discovery",
    "write_brief",
]
