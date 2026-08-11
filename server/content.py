"""Workshop content: nugget packs, phases, triggers, broadcasts, shell config.

The pack ships in the repo (content/default_pack.json), can be replaced at
deploy time (CONTENT_PACK_PATH env) and live via the admin API. State is
in-memory by design — a redeploy resets to the deployed pack, which is the
correct behaviour for tear-down-after-the-event infrastructure.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time

from pydantic import BaseModel, Field

from . import config

logger = logging.getLogger(__name__)

_DEFAULT_PACK = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "content", "default_pack.json")
)

KNOWN_TRIGGERS = {"always", "claude_active", "codex_active", "bash_active"}
_ELAPSED_PREFIX = "elapsed_gt_"
_TOPIC_PREFIX = "topic:"
_IDLE_RE = re.compile(r"^idle_(\d+)m$")
TOPIC_TTL_SECONDS = 600  # a spotted topic stays "live" for 10 minutes

# Markdown code spans and fences, stripped before topic matching.
#
# Backticks are the one reliable tell that the terminal is *displaying
# documentation* rather than running something. Skills and instructions are full
# of lines like "scaffold with `--features lakebase`" or "`databricks genie
# list-spaces`" — reference material, printed when an agent loads a skill. The
# `databricks-apps` skill is loaded on every single app build and mentions both
# Genie and Lakebase that way, so without this every attendee who built anything
# was recorded as having touched both. That reached a customer-facing brief.
#
# Real activity never looks like this: a command the agent actually runs, and the
# output it produces, carry no backticks.
_FENCED = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def strip_documentation_markup(text: str) -> str:
    """Remove markdown code spans/fences so quoted commands aren't read as use."""
    return _INLINE_CODE.sub(" ", _FENCED.sub(" ", text))


class NuggetLink(BaseModel):
    url: str
    label: str = "Learn more"


class Nugget(BaseModel):
    id: str
    title: str
    markdown: str
    link: NuggetLink | None = None
    tags: list[str] = Field(default_factory=list)
    phases: list[str] = Field(default_factory=list)  # empty = all phases
    triggers: list[str] = Field(default_factory=list)  # empty = always
    cta: str | None = None  # badge shown when the card is contextually matched
    prompt: str | None = None  # typed (unsent) into the active session on CTA click
    weight: int = 1
    pinned: bool = False


class ShellLink(BaseModel):
    label: str
    url: str
    icon: str = "link"
    highlight: bool = False  # promoted CTA treatment in the nav


class WorkspaceLink(BaseModel):
    """Deep link into the attendee's own workspace (path joined to the
    workspace URL at render time)."""

    label: str
    path: str
    icon: str = "link"
    description: str = ""


class IdeaPrompt(BaseModel):
    """An ideation chip: clicking types `prompt` into an agent session,
    unsent — the attendee presses Enter."""

    label: str
    prompt: str


class WizardIdea(BaseModel):
    """One card in the "I'm not sure yet — show me ideas" grid.

    Tagged rather than bucketed. An idea belongs to any number of industries and
    intents, so one flat list serves every combination the wizard can ask for
    without a card having to be written twice.

    ``demo_tables`` is the load-bearing field. It names the tables the idea needs,
    unqualified by catalog (``automotive_mobility.parts360``), and the selector
    checks them against what is actually seeded before the card is allowed on
    screen. Someone who clicked "I'm not sure yet" has said they have no idea and
    is trusting the grid; handing them something that cannot be built with the
    data present is the worst thing the wizard can do. An empty list means the
    idea needs no demo data and is always buildable.
    """

    id: str
    label: str
    outcome: str  # one line, framed as what the attendee ends up with
    prompt: str  # typed into the terminal on launch
    industries: list[str] = Field(default_factory=list)  # empty = generic
    intents: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    # dashboard | app | pipeline | ai | ml | fun. The selector spreads the six
    # cards across these, so someone who wanted an app is never shown six
    # dashboards and told there is nothing here for them.
    shape: str = "dashboard"
    technical: bool = False
    demo_tables: list[str] = Field(default_factory=list)


class ShellConfig(BaseModel):
    links: list[ShellLink] = Field(default_factory=list)
    workspace_links: list[WorkspaceLink] = Field(default_factory=list)
    features: dict[str, bool] = Field(default_factory=dict)


class ContentPack(BaseModel):
    version: int = 1
    phases: list[str] = Field(default_factory=lambda: ["intro", "setup", "build", "wrap"])
    shell: ShellConfig = Field(default_factory=ShellConfig)
    # topic -> keywords spotted in terminal output. Nuggets reference topics
    # with a "topic:<name>" trigger and rank first while the topic is live.
    topics: dict[str, list[str]] = Field(default_factory=dict)
    # phase -> ideation chips ("all" applies to every phase).
    prompts: dict[str, list[IdeaPrompt]] = Field(default_factory=dict)
    nuggets: list[Nugget] = Field(default_factory=list)
    # The wizard's idea grid. See WizardIdea.
    ideas: list[WizardIdea] = Field(default_factory=list)
    # A workshop is almost always one customer, so the industry is known before
    # anyone arrives. Setting it here means the first render of the idea grid is
    # already right and the industry chips become a correction rather than a
    # prerequisite. Empty falls back to a deliberate cross-industry spread.
    default_industry: str = ""


class Broadcast(BaseModel):
    """An operator message pushed to this instance.

    ``surface`` decides where it lands and is set by Control Tower, not guessed
    here. ``banner`` pins persistent room state that survives a page reload
    because the condition survives it — a lunch time, a phase, a standing
    notice. ``toast`` is a discrete message that stacks and clears.

    ``durability`` applies to toasts: ``transient`` auto-dismisses, ``sticky``
    waits for the attendee, ``critical`` is a lock or suspension.
    """

    message: str
    level: str = "info"  # info | success | warning | error
    ttl_s: int = 300
    surface: str = "toast"  # toast | banner
    durability: str = "transient"  # transient | sticky | critical
    # Clears a pinned banner without sending anything.
    clear: bool = False


class ContentService:
    def __init__(self):
        self._lock = threading.Lock()
        self._pack = self._load_initial_pack()
        self._phase = config.workshop_phase_default()
        self._broadcast: Broadcast | None = None
        self._broadcast_at = 0.0
        self._topic_regex = self._compile_topics(self._pack)
        self.started_at = time.time()

    @staticmethod
    def _compile_topics(pack: ContentPack):
        patterns = {}
        for topic, keywords in pack.topics.items():
            words = [re.escape(k) for k in keywords if k.strip()]
            if words:
                patterns[topic] = re.compile(r"(?i)\b(?:" + "|".join(words) + r")\b")
        return patterns

    def scan_topics(self, text: str) -> set[str]:
        """Topics whose keywords appear in a terminal output chunk. Only the
        topic names leave this function — the text is never stored.

        Documentation markup is stripped first, and the keywords themselves are
        command-shaped rather than product nouns, because a topic here is not
        cosmetic: it drives the contextual panel *and* the `products` list on the
        insight payload an account team reads. Matching the word "lakebase"
        anywhere on screen meant an attendee who explicitly declined to use it
        was reported as having done so.
        """
        with self._lock:
            patterns = self._topic_regex
        text = strip_documentation_markup(text)
        return {topic for topic, regex in patterns.items() if regex.search(text)}

    def _load_initial_pack(self) -> ContentPack:
        path = config.content_pack_path() or _DEFAULT_PACK
        try:
            with open(path) as f:
                pack = ContentPack.model_validate(json.load(f))
            logger.info("content pack loaded from %s (%d nuggets)", path, len(pack.nuggets))
            return pack
        except Exception as e:
            logger.error("content pack unreadable at %s: %s — starting empty", path, e)
            return ContentPack()

    # -- read --

    @property
    def pack(self) -> ContentPack:
        with self._lock:
            return self._pack

    @property
    def phase(self) -> str:
        with self._lock:
            return self._phase

    def prompts_for_phase(self) -> list[dict]:
        with self._lock:
            pack, phase = self._pack, self._phase
        chips = list(pack.prompts.get("all", [])) + list(pack.prompts.get(phase, []))
        return [c.model_dump() for c in chips]

    def ideas(self) -> list[WizardIdea]:
        with self._lock:
            return list(self._pack.ideas)

    def default_industry(self) -> str:
        with self._lock:
            return self._pack.default_industry

    def active_broadcast(self) -> Broadcast | None:
        """The pinned notice, if one is standing.

        Only banners are retained: a toast is a message to a person and has
        already been shown, so replaying it on the next page load would resurrect
        an announcement the attendee dealt with an hour ago.
        """
        with self._lock:
            if (
                self._broadcast
                and self._broadcast.surface == "banner"
                and time.time() - self._broadcast_at < self._broadcast.ttl_s
            ):
                return self._broadcast
            return None

    def clear_broadcast(self) -> None:
        with self._lock:
            self._broadcast = None
            self._broadcast_at = 0.0

    def nuggets_for(self, active_triggers: set[str], live_topics: set[str] | None = None,
                    idle_minutes: float = 0.0, limit: int = 8) -> list[dict]:
        """Nuggets for the current phase whose triggers are satisfied.

        Order: pinned, then nudges (idle-triggered next steps / ideas — the
        attendee is paused, so these are the most relevant thing on screen),
        then nuggets matching a topic spotted in the user's terminal, then a
        stable weighted shuffle of the rest."""
        with self._lock:
            pack, phase = self._pack, self._phase

        elapsed_min = (time.time() - self.started_at) / 60
        live_topics = live_topics or set()

        def matched_topic(n: Nugget) -> str | None:
            for t in n.triggers:
                if t.startswith(_TOPIC_PREFIX) and t[len(_TOPIC_PREFIX):] in live_topics:
                    return t[len(_TOPIC_PREFIX):]
            return None

        def is_nudge(n: Nugget) -> bool:
            for t in n.triggers:
                m = _IDLE_RE.match(t)
                if m and idle_minutes >= int(m.group(1)):
                    return True
            return False

        def eligible(n: Nugget) -> bool:
            if n.phases and phase not in n.phases:
                return False
            if not n.triggers:
                return True
            if matched_topic(n) or is_nudge(n):
                return True
            for t in n.triggers:
                if t == "always" or t in active_triggers:
                    return True
                if t.startswith(_ELAPSED_PREFIX):
                    try:
                        if elapsed_min > int(t[len(_ELAPSED_PREFIX):]):
                            return True
                    except ValueError:
                        pass
            return False

        candidates = [n for n in pack.nuggets if eligible(n)]
        pinned = [n for n in candidates if n.pinned]
        unpinned = [n for n in candidates if not n.pinned]
        nudges = [n for n in unpinned if is_nudge(n)]
        topical = [n for n in unpinned if not is_nudge(n) and matched_topic(n)]
        rest = [n for n in unpinned if not is_nudge(n) and not matched_topic(n)]
        # Weighted shuffle, reseeded every 5 minutes so the rotation feels
        # alive but doesn't reshuffle on every poll.
        rng = random.Random(int(time.time() // 300))
        rest = sorted(rest, key=lambda n: rng.random() / max(n.weight, 1))
        nudges = sorted(nudges, key=lambda n: -n.weight)

        results = []
        for n in (pinned + nudges + topical + rest)[:limit]:
            item = n.model_dump()
            item["matched_topic"] = matched_topic(n)
            item["nudge"] = is_nudge(n)
            results.append(item)
        return results

    # -- write (admin) --

    def set_pack(self, pack: ContentPack) -> None:
        with self._lock:
            self._pack = pack
            self._topic_regex = self._compile_topics(pack)
            if self._phase not in pack.phases and pack.phases:
                self._phase = pack.phases[0]
        logger.info("content pack replaced (%d nuggets, %d topics)",
                    len(pack.nuggets), len(pack.topics))

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase
        logger.info("workshop phase -> %s", phase)

    def set_broadcast(self, broadcast: Broadcast) -> None:
        with self._lock:
            self._broadcast = broadcast
            self._broadcast_at = time.time()
        logger.info("broadcast (%s): %s", broadcast.level, broadcast.message[:80])


content_service = ContentService()
