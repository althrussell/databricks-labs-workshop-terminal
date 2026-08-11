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
        )

    @property
    def has_content(self) -> bool:
        """Whether the attendee actually said anything worth recording.

        Skipping sets ``seen`` and nothing else, and a brief with no content must
        not reach discovery: a row saying an attendee exists and wants nothing is
        worse than no row, because it looks like a finding.
        """
        return bool(self.what_building.strip() or self.idea_id or self.industry)


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

    A workshop is almost always one customer, so the operator knows this before
    the doors open. Honoured only when that industry is actually seeded — a pack
    naming an industry the notebook never created would otherwise silently empty
    the grid.
    """
    configured = content.content_service.default_industry().strip()
    if configured and (not demo_data.enabled() or demo_data.has_industry(configured)):
        return configured
    return ""


def _buildable(idea: content.WizardIdea) -> bool:
    """Whether this card can actually be built with the data in this deployment.

    When there is no demo catalog at all, every card passes. That looks
    permissive, but it is the honest reading: without demo data nobody's tables
    exist, so filtering on them would empty the grid for the one attendee least
    able to recover from an empty grid. They get the full catalogue and an agent
    that will generate what it needs.
    """
    if not demo_data.enabled():
        return True
    return demo_data.verify(idea.demo_tables)


def _score(idea: content.WizardIdea, industry: str, intent: str) -> int:
    score = 0
    if industry and industry in idea.industries:
        score += 100
    elif not idea.industries:
        score += 10  # generic: always plausible, never preferred over a match
    if intent and intent in idea.intents:
        score += 20
    return score


def select_ideas(
    industry: str = "",
    intent: str = "",
    limit: int = IDEA_COUNT,
    *,
    rng: random.Random | None = None,
) -> list[content.WizardIdea]:
    """The cards to show someone who said they are not sure yet.

    Three properties, in priority order:

    1. **Every card is buildable.** Anything naming demo tables this deployment
       has not seeded is excluded outright, not shown-but-unbadged. Somebody who
       clicked "show me ideas" has told us they have no idea and is trusting the
       grid; the one unforgivable outcome is handing them something that cannot
       be built.
    2. **The shapes are spread.** Six dashboards tells an attendee who wanted to
       build an app that this workshop is not for them. So the selector takes the
       best card of each shape first and only then fills up, which is worth more
       than the marginal industry score it costs.
    3. **It is never empty.** Industry match degrades to generic cards, which
       need no demo data and are therefore always available.
    """
    rng = rng or random.Random()
    pool = [i for i in content.content_service.ideas() if _buildable(i)]
    if not pool:
        return []

    # Shuffle before sorting so equal-scoring cards vary between attendees rather
    # than the whole room seeing the same six in the same order.
    rng.shuffle(pool)
    pool.sort(key=lambda i: _score(i, industry, intent), reverse=True)

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

    # One final sort so the strongest match leads, now that the spread is locked in.
    chosen.sort(key=lambda i: _score(i, industry, intent), reverse=True)
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
    title = brief.what_building.strip()
    if brief.idea_id and not title:
        idea = idea_by_id(brief.idea_id)
        if idea:
            title = idea.label
    return {
        "record_id": brief.record_id,
        "agent": "wizard",
        "confidence": "high",
        "session_intent": brief.intent,
        "industry": brief.industry,
        "use_case_title": title[:120],
        "use_case_summary": brief.what_building.strip(),
        "goal": brief.what_building.strip(),
        "current_stack": list(brief.current_stack),
    }


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
        industry=given("industry", lambda v: _clean_text(v)[:64], existing.industry),
        intent=given("intent", lambda v: _clean_text(v).lower(), existing.intent),
        idea_id=given("idea_id", lambda v: _clean_text(v)[:64], existing.idea_id),
        current_stack=given("current_stack", _clean_list, existing.current_stack),
        persona=given("persona", lambda v: _clean_text(v).lower(), existing.persona),
        seen=True,
        completed_at=existing.completed_at,
    )
    if brief.intent not in INTENTS:
        brief.intent = ""

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
    return (
        f"{text}\n\n"
        "Start building this with me now. Ask me at most one question if you "
        "genuinely cannot start without the answer, otherwise make a sensible "
        "choice and tell me what you chose."
    )


def state(user: User, industry: str = "") -> dict[str, Any]:
    """Everything the frontend needs to render the wizard.

    ``industry`` is the filter chip the attendee just pressed, which is not yet
    in the brief — they are still choosing what to build, and the whole point of
    the chip is to see the grid change before committing to anything.
    """
    brief = read_brief(user)
    industry = _clean_text(industry)[:64] or brief.industry or default_industry()
    return {
        "brief": brief.to_json(),
        "should_show": not brief.seen,
        "default_industry": default_industry(),
        "industries": demo_data.industries(),
        "demo_data_available": bool(demo_data.inventory()),
        "intents": list(INTENTS),
        "ideas": [i.model_dump() for i in select_ideas(industry)],
        "capture_enabled": config.discovery_enabled(),
    }


__all__ = [
    "IDEA_COUNT",
    "INTENTS",
    "WizardBrief",
    "brief_path",
    "default_industry",
    "idea_by_id",
    "read_brief",
    "save",
    "select_ideas",
    "starter_prompt",
    "state",
    "surprise",
    "to_discovery",
    "write_brief",
]
