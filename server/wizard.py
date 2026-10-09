"""Goal-first onboarding with immutable selections and revisioned attendee saves.

The two optional steps capture a task and open a ready agent. Product context is
independent of optional discovery capture; a selected recommendation never
becomes attendee-authored wording or a confirmed industry by implication.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from . import attendee_state, config, content, demo_data, discovery, wizard_selections
from .users import User

logger = logging.getLogger(__name__)

_BRIEF_RELATIVE = os.path.join(".workshop", "brief.json")

# At most six curated choices. A stated task can yield a shorter relevant set.
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
    # Only an explicit industry choice is stated. Room defaults and idea tags
    # remain suggestions and never establish an attendee's industry.
    industry_stated: bool = False
    schema_version: int = 2
    revision: int = 0
    stage: str = "draft"
    selected_idea: dict[str, Any] | None = None
    selection_token: str = ""
    discovery_record_id: str = ""
    words_source: str = "attendee"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "WizardBrief":
        if not isinstance(raw, dict) or raw.get("stage", "draft") not in {"draft", "complete", "skipped"}:
            raise ValueError("Invalid saved goal")
        version = raw.get("schema_version", 1)
        if version not in {1, 2} or ("revision" in raw and (type(raw["revision"]) is not int or raw["revision"] < 0)):
            raise ValueError("Unsupported saved goal version or revision")
        if raw.get("words_source", "attendee") not in {"attendee", "legacy_unverified", "confirmed_goal"}:
            raise ValueError("Invalid saved goal authorship")
        if raw.get("selected_idea") is not None:
            if not isinstance(raw["selected_idea"], dict):
                raise ValueError("Invalid saved idea")
            wizard_selections.validate_snapshot(raw["selected_idea"])
            if raw.get("idea_id") != raw["selected_idea"].get("id"):
                raise ValueError("Saved idea does not match the saved goal")
        stack = _clean_list(raw.get("current_stack"))
        return cls(
            record_id=str(raw.get("record_id") or ""),
            what_building=str(raw.get("what_building") or ""),
            industry=str(raw.get("industry") or ""),
            intent=str(raw.get("intent") or ""),
            idea_id=str(raw.get("idea_id") or ""),
            current_stack=stack,
            persona=str(raw.get("persona") or ""),
            seen=bool(raw.get("seen")),
            skipped=bool(raw.get("skipped")),
            completed_at=str(raw.get("completed_at") or ""),
            industry_stated=bool(raw.get("industry_stated")),
            revision=max(0, int(raw.get("revision") or 0)),
            stage=str(raw.get("stage") or ("skipped" if raw.get("skipped") else "complete" if raw.get("seen") else "draft")),
            selected_idea=raw.get("selected_idea") if isinstance(raw.get("selected_idea"), dict) else None,
            selection_token=str(raw.get("selection_token") or ""),
            discovery_record_id=str(raw.get("discovery_record_id") or ""),
            words_source=str(raw.get("words_source") or ("legacy_unverified" if version == 1 and raw.get("idea_id") else "attendee")),
        )

    @property
    def has_content(self) -> bool:
        """Whether the attendee actually said anything worth recording.

        Skipping sets ``seen`` and nothing else, and a brief with no content must
        not reach discovery: a row saying an attendee exists and wants nothing is
        worse than no row, because it looks like a finding.
        """
        return bool(self.what_building.strip() or self.selected_idea)

    @property
    def stated_industry(self) -> str:
        """The industry they confirmed, never the pack/env preselect."""
        return self.industry if self.industry_stated else ""


# -- storage (one file per attendee, beside the persona) --

class BriefConflict(ValueError):
    def __init__(self, brief: WizardBrief):
        super().__init__("Your goal changed in another tab. Review the saved goal before replacing it.")
        self.brief = brief


class BriefReadError(RuntimeError):
    pass


def brief_path(user: User) -> str:
    return os.path.join(user.home, _BRIEF_RELATIVE)


def read_brief(user: User, *, strict: bool = False) -> WizardBrief:
    """Read a goal; strict callers preserve corrupt files and receive an error."""
    try:
        with open(brief_path(user), encoding="utf-8") as fh:
            return WizardBrief.from_json(json.load(fh))
    except FileNotFoundError:
        return WizardBrief()
    except Exception as exc:  # noqa: BLE001 — a corrupt brief must not block the UI
        logger.warning("wizard brief unreadable for %s: %s", user.email, exc)
        if strict:
            raise BriefReadError("Your saved goal could not be loaded. Retry before saving changes.") from exc
        return WizardBrief()


def write_brief(user: User, brief: WizardBrief) -> None:
    path = brief_path(user)
    with attendee_state.locked(path):
        attendee_state.write_json(path, brief.to_json())


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


def _buildable(idea: content.WizardIdea, *, deadline: float | None = None) -> bool:
    """Check declared table/column metadata; generic choices survive faults.

    Metadata is not a promise of SELECT access, resources or meaningful joins.
    Those still need checking under the execution identity at use.
    """
    if not idea.demo_tables:
        return True
    if set(idea.required_columns) != set(idea.demo_tables) or not demo_data.verify(idea.demo_tables):
        return False
    remaining = .25 if deadline is None else max(0, deadline - time.monotonic())
    return demo_data.supports(idea.required_columns, timeout=remaining)


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
    # This badge means table presence. It never claims query access or that a
    # generated app already has the necessary resources.
    payload["data_mode"] = "demo" if idea.demo_tables else "generate"
    payload["fit_reason"] = idea.fit_reason or idea.outcome
    payload["first_version"] = idea.first_version or idea.prompt
    payload["assumptions"] = idea.assumptions or [
        "Prepared workshop data is synthetic; validate its columns and access before use."
        if idea.demo_tables else "Explore available workshop data first; label any sample data used."
    ]
    payload["unresolved"] = idea.unresolved or ["Your agent will confirm the useful first task and check data access."]
    return payload


_TOKEN = re.compile(r"[a-z0-9]+")
_DECLINED_APP = re.compile(
    r"\b(?:no|not|without|instead of|rather than|don't (?:want|need|build)|do not (?:want|need|build))"
    r"(?:\s+(?:an?|any|new|another|web|custom|separate)){0,3}\s+(?:app|website)\b", re.I)
_APP_SOURCE_METRICS = re.compile(
    r"\b(?:app|website)\s+(?:visits?|sessions?|traffic|events?|usage|logs?|"
    r"telemetry|performance|crashes|downloads|ratings?|reviews?)\b", re.I)


def app_intent(query: str) -> str:
    """Respect explicit app choices; a generic page or tool leaves format open."""
    if _DECLINED_APP.search(query):
        return "no_app"
    # An existing app/website can be the data source for a chart or analysis.
    # Keep a separate explicit output choice: "an app for website visits".
    output_words = _APP_SOURCE_METRICS.sub("source metrics", query)
    return "app" if re.search(r"\b(app|website)\b", output_words, re.I) else ""


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if len(t) > 2}


_TASK_FILLER = frozenset((
    "a about an and app apps are as build can could dashboard don easy for from "
    "give help how i in instead internal is it keep let make me my need new not "
    "of on only our own page phone please rather read see separate show simple "
    "small staff team teams than that the their them there these this to tool "
    "use want way we web website what where which with without would you your "
    "friendly manager managers view running know work next should prepared into some useful try turning"
).split())

_TASK_EQUIVALENTS = {
    "aggregate": "summary", "aggregated": "summary", "aggregating": "summary",
    "aggregation": "summary", "summaries": "summary", "summarise": "summary",
    "summarize": "summary", "summarised": "summary", "summarized": "summary",
}


def _task_tokens(text: str) -> set[str]:
    # Format and generic workshop wording cannot make an unrelated task fit.
    # Singular/plural matching is useful for catalog labels such as orders.
    words = {_TASK_EQUIVALENTS.get(word, word) for word in _tokens(text) if word not in _TASK_FILLER}
    words = {word[:-1] if word.endswith("s") and not word.endswith("ss") and len(word) > 4 else word
             for word in words}
    return {_TASK_EQUIVALENTS.get(word, word) for word in words}


def _task_terms(idea: content.WizardIdea) -> set[str]:
    return _task_tokens(f"{idea.label} {idea.outcome} {idea.prompt}")


def _task_matches(idea: content.WizardIdea, task: set[str]) -> bool:
    terms = _task_terms(idea)
    # Cleaning readings does not fulfil a requested summary. Normalised
    # aggregate/summary wording preserves that action across curated sources.
    if "summary" in task and "summary" not in terms:
        return False
    return len(task & terms) / len(task) >= .5


def uses_app(idea: content.WizardIdea) -> bool:
    return idea.shape == "app" or any(
        re.search(r"\bapps?\b", product, re.I) for product in idea.products
    ) or app_intent(idea.prompt) == "app"


def _score(
    idea: content.WizardIdea, industry: str, intent: str, query: str = ""
) -> int:
    score = 0
    if industry and industry in idea.industries:
        score += 20
    elif not idea.industries:
        score += 10  # generic: always plausible, never preferred over a match
    if intent and intent in idea.intents:
        score += 20
    if query:
        terms = _task_terms(idea)
        overlap = _task_tokens(query) & terms
        score += 30 * len(overlap)
        # Prefer a focused stock task to a broad stock/sales page on a tie.
        score += int(100 * len(overlap) / max(1, len(terms)))
        # A device constraint must influence the first card, rather than only
        # appearing in a broader alternative below it. Task overlap still
        # determines eligibility in select_ideas.
        device_words = {"phone", "mobile", "tablet"}
        requested_devices = _tokens(query) & device_words
        card_devices = _tokens(f"{idea.label} {idea.outcome} {idea.prompt}") & device_words
        if requested_devices:
            score += 100 if requested_devices & card_devices else -100
        if app_intent(query) == "app":
            score += 100 if idea.shape == "app" else -100
    return score


def select_ideas(
    industry: str = "",
    intent: str = "",
    limit: int = IDEA_COUNT,
    *,
    rng: random.Random | None = None,
    query: str = "",
) -> list[content.WizardIdea]:
    """Stable task-ranked curated choices, with one shared metadata budget.

    Named-table choices require declared columns. A stated task is never padded
    with unrelated generic ideas. Shape variety applies when no task was stated.
    """
    # Curated ordering is stable across refreshes and restarts. Surprise owns
    # deliberate randomness; ranking owns task fit before shape variety.
    rng = rng or random.Random(0)
    deadline = time.monotonic() + .25

    # ``industry_slug``, not ``normalize_industry``: the latter answers with the
    # seeded schema or nothing, so an unreadable catalog discarded the chip the
    # attendee had just pressed and served them generics instead of their own
    # industry's cards.
    industry = demo_data.industry_slug(industry) if industry else ""
    available = content.content_service.ideas()
    generics = [i for i in available if not i.industries]
    if industry:
        tagged = [i for i in available if industry in i.industries]
        # Exhaust the industry catalogue before padding with generics, including
        # extra cards of a shape already taken — three retail ideas plus three
        # "build a pipeline" generics reads as nothing here for you.
        pool = tagged + generics
    elif demo_data.enabled():
        # A stated task may find prepared data in another sector. Unstated
        # goals still get generics rather than a random industry assignment.
        pool = list(available) if _task_tokens(query) else list(generics)
    else:
        pool = available

    task = _task_tokens(query)
    if query.strip() and not task and app_intent(query) != "app":
        # Generic wording cannot justify a sector-specific task. The goal is
        # still usable directly; explicit idea exploration has an empty query.
        return []
    if task:
        # An industry/shape match cannot replace the actual task. If no curated
        # task fits, the attendee can keep their own words and open the agent.
        pool = [idea for idea in pool if _task_matches(idea, task)]
        pool = sorted(pool, key=lambda idea: _score(idea, industry, intent, query), reverse=True)

    app_choice = app_intent(query)
    if app_choice == "app":
        pool = [idea for idea in pool if idea.shape == "app"]
    elif app_choice == "no_app":
        pool = [idea for idea in pool if not uses_app(idea)]

    # One bounded background read warms relevant dependencies rather than paying
    # a separate cold metadata wait per card. It never blocks the goal path.
    requirements: dict[str, list[str]] = {}
    for idea in pool[:max(limit, 12)] if task else pool:
        for table, columns in idea.required_columns.items():
            requirements[table] = sorted(set(requirements.get(table, ())) | set(columns))
    if requirements and demo_data.enabled():
        demo_data.supports(requirements, timeout=0)
    pool = [idea for idea in pool if _buildable(idea, deadline=deadline)]

    rng.shuffle(pool)
    pool.sort(key=lambda i: _score(i, industry, intent, query), reverse=True)

    if query.strip():
        # Do not pad a clear request with unrelated shapes to fill six slots.
        return pool[:limit]
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

    Completed choices have explicit provenance. Legacy selected-card wording
    has unverified authorship and receives medium confidence.

    ``timeline`` is never set. The wizard does not ask — a workshop attendee has
    no authority over their employer's timeline, so a captured answer would be a
    guess that reads downstream as a commitment.
    """
    idea = content.WizardIdea.model_validate(brief.selected_idea) if brief.selected_idea else None
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
        "confidence": "medium" if brief.words_source == "legacy_unverified" else "high",
        "session_intent": brief.intent,
        "industry": brief.stated_industry,
        "use_case_title": title[:120],
        "use_case_summary": brief.what_building.strip(),
        "goal": brief.what_building.strip(),
        "current_stack": list(brief.current_stack),
    }
    out["databricks_products"] = products
    out["interest_signals"] = [signal] if signal else []
    if idea and not brief.what_building.strip():
        out["use_case_summary"] = f"Selected idea: {idea.label}. {idea.outcome}"
        out["goal"] = idea.outcome
    return out


def save(user: User, payload: dict[str, Any]) -> WizardBrief:
    """Serialize the entire revisioned update; optional capture never owns the goal."""
    with attendee_state.locked(brief_path(user)):
        existing = read_brief(user, strict=True)
        expected = payload.get("expected_revision")
        operation = payload.get("operation") or ("skip" if payload.get("skipped") else "complete")
        if expected is not None and expected != existing.revision:
            # A lost response or two identical first saves is safe to retry.
            # Different content always requires explicit stale-write recovery.
            matches = operation in {"draft", "complete"} and existing.stage == operation
            for name in ("what_building", "industry", "intent", "idea_id", "industry_stated", "current_stack"):
                if name in payload and payload[name] != getattr(existing, name):
                    matches = False
            if payload.get("selection_token") and payload["selection_token"] != existing.selection_token:
                matches = False
            if matches:
                return existing
            raise BriefConflict(existing)
        if operation not in {"draft", "complete", "skip", "clear", "change"}:
            raise ValueError("Unknown goal operation")
        brief = WizardBrief.from_json(existing.to_json())
        brief.record_id = existing.record_id or uuid.uuid4().hex
        if operation == "change":
            brief = WizardBrief(record_id=uuid.uuid4().hex, revision=existing.revision)
        if operation == "clear":
            brief = WizardBrief(record_id=brief.record_id, revision=existing.revision,
                                seen=True, skipped=True, stage="skipped",
                                discovery_record_id=existing.discovery_record_id)
        elif operation == "skip":
            # Closing a completed goal does not revoke or replace it. Skipping
            # an unfinished draft withholds it from launch and discovery.
            brief.seen = True
            if existing.stage != "complete" or not existing.has_content:
                brief.skipped = True
                brief.stage = "skipped"
        else:
            what_changed = ("what_building" in payload
                            and _clean_text(payload["what_building"]) != existing.what_building)
            for name in ("what_building", "industry", "intent", "persona"):
                if name in payload:
                    setattr(brief, name, _clean_text(payload[name]))
            if "what_building" in payload:
                brief.words_source = "confirmed_goal" if existing.words_source == "legacy_unverified" and not what_changed else "attendee"
            if "industry" in payload:
                brief.industry = demo_data.industry_slug(brief.industry[:64])
                brief.industry_stated = bool(payload.get("industry_stated", True))
            elif "industry_stated" in payload:
                brief.industry_stated = bool(payload["industry_stated"])
            if brief.intent not in INTENTS:
                brief.intent = ""
            if "current_stack" in payload:
                brief.current_stack = _clean_list(payload["current_stack"])
            requested = _clean_text(payload.get("idea_id"))
            if "idea_id" in payload and requested:
                token = _clean_text(payload.get("selection_token"))
                # A committed snapshot outlives its offer expiry. Only the same
                # saved selection can reuse it; new choices need a live receipt.
                if not (existing.selected_idea and requested == existing.idea_id
                        and (not token or token == existing.selection_token)):
                    brief.selected_idea = wizard_selections.resolve(user, token, requested)
                    brief.selection_token = token
                brief.idea_id = requested
            elif "idea_id" in payload or what_changed:
                brief.idea_id = ""
                brief.selected_idea = None
                brief.selection_token = ""
            brief.stage = "draft" if operation == "draft" else "complete"
            brief.seen = operation != "draft"
            brief.skipped = False
            if brief.stage == "complete" and not brief.has_content:
                raise ValueError("Add a goal or choose an idea before continuing.")
            if brief.stage == "complete" and brief.has_content:
                brief.completed_at = existing.completed_at or discovery._now()
        if brief.to_json() != existing.to_json():
            brief.revision = existing.revision + 1
        attendee_state.write_json(brief_path(user), brief.to_json())
        # Drafts are product state, not a finding about the attendee. Capture is
        # an independent, optional projection of the explicitly completed goal.
        records = discovery.discovery_store.for_attendee(user.email) if config.discovery_enabled() else []
        captured = next((row for row in records if row.record_id == brief.record_id), None)
        should_capture = (operation == "clear" and captured is not None) or (
            brief.stage == "complete" and brief.has_content and operation != "skip"
            and (captured is None or to_discovery(existing) != to_discovery(brief)))
        if should_capture:
            try:
                stored = discovery.record(user.email, to_discovery(brief))
                if stored is not None:
                    brief.discovery_record_id = stored.record_id
                    attendee_state.write_json(brief_path(user), brief.to_json())
            except Exception:
                logger.exception("optional wizard discovery capture failed")
        return brief


def launch_context(brief: WizardBrief) -> dict[str, Any]:
    """One portable product context for home, project and harness adapters."""
    return {
        "schema_version": 1,
        "brief_revision": brief.revision,
        "project_id": brief.record_id,
        "stage": brief.stage,
        "attendee_words": brief.what_building,
        "words_source": brief.words_source,
        "selected_idea": brief.selected_idea,
        "stated_industry": brief.stated_industry,
        "suggested_industry": brief.industry if not brief.industry_stated else "",
        "intent": brief.intent,
        "current_stack": list(brief.current_stack),
        "discovery_record_id": brief.discovery_record_id,
        "consultation": "Clarify only material unknowns, recommend a small first version, then build.",
    }


def starter_prompt(brief: WizardBrief) -> str:
    """Use the committed task, never a mutable catalog ID or model-authored quote."""
    if brief.skipped or brief.stage != "complete" or not brief.has_content:
        return ""
    lines = []
    if brief.what_building.strip():
        lines.append(brief.what_building.strip())
    idea = brief.selected_idea
    if idea:
        lines.extend(["", f"I picked the idea: {idea['label']}.", idea["prompt"]])
        if idea.get("first_version"):
            lines.append("Suggested first version: " + idea["first_version"])
        for assumption in idea.get("assumptions", []):
            lines.append("Demo assumption to check: " + assumption)
    elif brief.idea_id:
        lines.append("A previous idea selection needs review; use my words above and confirm the intended task.")
    if brief.stated_industry:
        lines.append("My industry is " + brief.stated_industry.replace("_", " ") + ".")
    lines.extend(["", "Help me make a useful workshop demo. Reuse what I've told you, explore the workshop's prepared data first, and follow the workshop interaction contract."])
    return "\n".join(lines)


def _effective_model() -> str:
    """The wizard model in force, override included."""
    from . import wizard_llm

    return str(wizard_llm.effective_model()["model"])


def state(user: User, industry: str | None = None, query: str = "") -> dict[str, Any]:
    """Everything the frontend needs to render the wizard.

    ``industry`` is the filter chip the attendee just pressed, which is not yet
    in the brief — they are still choosing what to build, and the whole point of
    the chip is to see the grid change before committing to anything.

    Stable ordering survives reconnects. A deliberate industry filter affects
    candidates but cannot replace a committed selection.
    """
    brief = read_brief(user, strict=True)
    if industry is None:
        industry = brief.industry if brief.revision or brief.seen else default_industry()
    else:
        industry = demo_data.industry_slug(_clean_text(industry)[:64])
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
            wizard_selections.offer(user, idea_payload(i), source="catalog")
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
