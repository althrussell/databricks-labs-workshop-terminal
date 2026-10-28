"""Edge summary of one attendee's session (contract C6, tier 3).

The harvest in ``artifacts.py`` produces the raw residue; this module turns it
into the few sentences an account team can act on and pushes them to Control
Tower over the existing ingest path. Doing the reduction here rather than in
Control Tower is not an optimisation — the inputs are destroyed by teardown
(``drop_catalog`` takes the promote docs, ``delete_workspace`` takes the repos),
so an instance that shuts down without summarising has nothing left to summarise.

**The summary rolls; it does not wait for a phase.** It regenerates off the
harvest Control Tower already makes, so a run whose operator never flips the
phase — the normal case, since most operators don't know the control exists —
still reaches an account team with something. Wrap and teardown remain triggers,
but they are now *forcing* moments rather than the only ones.

Two gates keep that affordable, and both matter:

- an **interval** floor (``INSIGHT_SUMMARY_MIN_INTERVAL_MINUTES``), checked
  before the home is walked;
- a **material fingerprint**, checked after the harvest and before the model
  call, so an attendee who has done nothing since the last summary costs a
  directory walk rather than a model call.

*Wrap* still gets the model. *The final teardown harvest* is the backstop and
gets keyword extraction only — teardown can run hours later via scheduled
cleanup, is explicitly best-effort, and an LLM call there would fail silently
into nothing. The two are distinguished on the wire by ``generator``, because an
extraction summary is keyword-thin and must never be read as a finding.

Each emission carries a **revision**, and it is part of the idempotency key.
Without it Control Tower would de-dupe every regeneration after the first
against the key it already held, and the rolling summary would be a rolling
summary that only ever arrived once. An LLM summary is still never downgraded by
a later extraction pass; an extraction summary *can* be upgraded once the model
becomes reachable.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone

from . import artifacts, config, models
from .capture_journal import CaptureHeldError
from .closeout_guard import CaptureClosedError, admitted_writer, check_open
from .discovery import SESSION_INTENTS
from .users import User

logger = logging.getLogger("workshop.insight_summary")

_MODEL_TIMEOUT = 45
_MAX_TOKENS = 1400

MAX_HEADLINE_CHARS = 240
MAX_TEXT_CHARS = 1200
MAX_USE_CASES = 5
MAX_LIST_ITEMS = 10

_PROMPT = """You are summarising one attendee's hands-on session at a Databricks \
workshop, for the account team that covers their company.

You are given the attendee's own prompts to their coding agent, the agent's plan \
items, files and commands it touched, error first-lines it hit, and the titles of \
documents it wrote. You are NOT given the conversation.

Return ONLY a JSON object:

{
  "headline": "one sentence an account executive can read in a pipeline review",
  "what_they_built": "two or three sentences, concrete",
  "session_intent": "business_problem | evaluation | learning | fun",
  "use_cases": [{"title": "...", "summary": "...", "products": ["..."],
                 "evidence": "the prompt or artifact this came from"}],
  "blockers": ["what stopped them, in their terms"],
  "products": ["Databricks products they actually touched"]
}

Rules:
- Ground every claim in the material below. If it is not there, leave the field \
empty rather than inferring it.
- Never invent a company, a customer name, an industry, a timeline, or a budget.
- Distinguish what the attendee asked for from what the agent did on its own.
- An attendee who built nothing but hit a wall is a valuable record: say what the \
wall was. Do not describe a session as successful when the errors say otherwise.
- In "blockers", include ONLY actual technical, permission, authentication, or \
tooling failures that interrupted building or deploying during the session. \
NEVER put prototype scope disclaimers, synthetic/sample data notes (e.g. \
"Workshop data is synthetic", "Pole locations are synthetic"), or unconfigured \
external customer production systems into "blockers".
- Do not flatter. "Explored Lakeflow ingestion and hit a permissions wall" beats \
"showed strong engagement with the platform".

Classify "session_intent" from what they actually built:
- "business_problem" — they described a problem their own team has.
- "evaluation" — they compared Databricks with a tool they already run.
- "learning" — skilling up, with no specific project behind it.
- "fun" — a game, a toy, or a demo built for the enjoyment of building it.
Say "fun" when it was fun. A Space Invaders session classified as a business \
problem sends someone to chase a lead that was never there, and that costs more \
than an empty field. If the material genuinely does not say, leave it empty.
"""


@dataclass
class _Stamp:
    """What was last emitted for one attendee, and when it was last attempted."""

    generator: str = ""
    # Material fingerprint of the last *emitted* summary. Compared after the
    # harvest to decide whether there is anything new to say.
    fingerprint: str = ""
    # How many summaries have been emitted for this attendee. The next emission
    # is ``revision + 1``.
    revision: int = 0
    # Monotonic clock at the last *attempt*, not the last emission. Resetting it
    # on an unchanged pass is what stops an idle attendee being re-harvested on
    # every poll for the rest of the workshop.
    attempted_at: float = 0.0


class _Stamps:
    """Which attendees have been summarised, how, when, and against what material.

    Successful revisions, fingerprints and generators share the retained capture
    transaction with their exact envelopes. The interval and in-flight guards are
    process-local; monotonic clock readings cannot be reused after restart.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamps: dict[str, _Stamp] = {}
        self._in_flight: set[str] = set()
        self._journal = None

    @staticmethod
    def _validate(raw):
        if not isinstance(raw, dict):
            raise CaptureHeldError("retained summary state is invalid")
        for email, stamp in raw.items():
            if (not isinstance(email, str) or not isinstance(stamp, dict)
                    or type(stamp.get("revision")) is not int or stamp["revision"] < 0
                    or stamp.get("generator") not in {"", "llm", "extraction"}
                    or not isinstance(stamp.get("fingerprint"), str)):
                raise CaptureHeldError("retained summary revision is invalid")
        return raw

    @staticmethod
    def _checked(journal, operation):
        try:
            return operation()
        except CaptureHeldError as error:
            journal.mark_held(error)
            raise

    def configure_capture(self, journal) -> None:
        def initialize(state):
            self._validate(state.setdefault("summaries", {}))
        self._checked(journal, lambda: journal.transaction(initialize))
        with self._lock:
            if self._journal is not journal:
                self._stamps = {}
            self._journal = journal
        self._sync_capture()

    def _sync_capture(self) -> None:
        if self._journal is None:
            return
        retained = self._checked(self._journal, lambda: self._validate(
            self._journal.read().get("summaries")))
        with self._lock:
            for email, raw in retained.items():
                stamp = self._stamps.setdefault(email, _Stamp())
                stamp.revision = raw["revision"]
                stamp.generator = raw["generator"]
                stamp.fingerprint = raw["fingerprint"]

    def claim(self, email: str) -> bool:
        """Take this attendee for the duration of one pass, or decline.

        The interval keeps ordinary passes from overlapping, but a forced one —
        the wrap flip, or the synchronous teardown backstop — ignores the interval
        by design and can start while a backgrounded pass is still inside its
        model call. Both would then read the same revision and emit against the
        same idempotency key, so one of the two summaries is discarded by Control
        Tower after being paid for.
        """
        with self._lock:
            if email in self._in_flight:
                return False
            self._in_flight.add(email)
            return True

    def release(self, email: str) -> None:
        with self._lock:
            self._in_flight.discard(email)

    def may_emit(self, email: str, *, generator: str) -> bool:
        """Whether this generator is allowed to write over what is stored.

        An extraction pass never overwrites a model summary; the reverse is an
        upgrade and is allowed, because a rolling summary may have settled for
        extraction while the endpoint was unreachable.

        Separate from the interval, and deliberately so: the fallback inside a
        single pass re-checks this after the model has failed, and re-checking the
        interval there would reject every fallback — the attempt was stamped
        moments earlier by the pass doing the asking.
        """
        self._sync_capture()
        with self._lock:
            stamp = self._stamps.get(email)
        if stamp is None:
            return True
        return not (stamp.generator == "llm" and generator != "llm")

    def should_run(self, email: str, *, generator: str, force: bool = False) -> bool:
        """The cheap gate, checked before the home is walked.

        ``force`` is for wrap and teardown: the interval exists to bound cost
        during a session, and at the end of one there is no later chance.
        """
        if not self.may_emit(email, generator=generator):
            return False
        self._sync_capture()
        with self._lock:
            stamp = self._stamps.get(email)
        if stamp is None or force:
            return True
        interval = config.insight_summary_min_interval_seconds()
        return (time.monotonic() - stamp.attempted_at) >= interval

    def changed(self, email: str, fingerprint: str, *, generator: str) -> bool:
        """Whether there is new material worth spending a model call on.

        An extraction-to-``llm`` upgrade counts as changed even on identical
        material: the point of the upgrade is the better prose, not new facts.
        """
        self._sync_capture()
        with self._lock:
            stamp = self._stamps.get(email)
        if stamp is None or not stamp.fingerprint:
            return True
        if stamp.generator == "extraction" and generator == "llm":
            return True
        return stamp.fingerprint != fingerprint

    def note_attempt(self, email: str) -> None:
        """Record that the home was walked, whether or not anything was emitted.

        The interval gate reads this rather than the last emission, so a session
        that has gone quiet is not re-harvested on every harvest.
        """
        with self._lock:
            stamp = self._stamps.setdefault(email, _Stamp())
            stamp.attempted_at = time.monotonic()

    def next_revision(self, email: str) -> int:
        self._sync_capture()
        with self._lock:
            stamp = self._stamps.get(email)
            return (stamp.revision if stamp else 0) + 1

    def mark(self, email: str, generator: str, fingerprint: str = "") -> None:
        with self._lock:
            stamp = self._stamps.setdefault(email, _Stamp())
            stamp.generator = generator
            stamp.fingerprint = fingerprint
            stamp.revision += 1
            stamp.attempted_at = time.monotonic()

    def generator_for(self, email: str) -> str | None:
        self._sync_capture()
        with self._lock:
            stamp = self._stamps.get(email)
            return stamp.generator or None if stamp else None

    def revision_for(self, email: str) -> int:
        self._sync_capture()
        with self._lock:
            stamp = self._stamps.get(email)
            return stamp.revision if stamp else 0

    def clear(self) -> None:
        with self._lock:
            self._journal = None
            self._stamps.clear()
            self._in_flight.clear()


stamps = _Stamps()
_worker_condition = threading.Condition()
_background_workers: set[threading.Thread] = set()
_background_outcomes: list[dict] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary_id(run_id: str, email: str) -> str:
    """Stable per attendee, so a regenerated summary supersedes rather than adds."""
    digest = hashlib.sha256(f"{run_id}:{email}".encode()).hexdigest()
    return f"sum_{digest[:16]}"


def _fingerprint(harvest: artifacts.Harvest, signal: dict | None) -> str:
    """A cheap hash of "has this session moved since we last described it".

    Fingerprints the *summariser's own input* rather than activity in general,
    because that is the question worth asking: if neither the harvest nor the
    signal handed to the model has changed, the summary it writes would say the
    same thing, so regenerating spends a model call to restate a stored answer.

    Built from counters and titles rather than the prose, so it stays stable
    across two harvests of an unchanged home. Commits already arrive as harvested
    artifacts, so code movement is covered without reaching for the stats payload.
    """
    signal = signal or {}
    parts: list[object] = [
        harvest.prompt_count,
        len(harvest.prompts),
        len(harvest.plans),
        len(harvest.tool_targets),
        len(harvest.errors),
        len(harvest.artifacts),
        sum(artifact.bytes for artifact in harvest.artifacts),
        len(harvest.documents),
        # The newest error and artifact, so a session that swaps one failure for
        # another registers as movement even at unchanged counts.
        harvest.errors[-1] if harvest.errors else "",
        harvest.artifacts[0].title if harvest.artifacts else "",
        harvest.artifacts[-1].title if harvest.artifacts else "",
        [f"{a.kind}:{a.title}" for a in harvest.artifacts],
        # The signal fields the prompt is given. Engagement crossing from
        # explorer to builder changes the summary even on identical prompts.
        signal.get("engagement", ""),
        signal.get("shipped", ""),
        sorted(signal.get("products") or []),
        sorted((signal.get("topic_hits") or {}).items()),
    ]
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode())
    return digest.hexdigest()[:16]


def _trim(text: object, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _string_list(raw: object, limit: int = MAX_LIST_ITEMS) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        cleaned = _trim(item, MAX_HEADLINE_CHARS)
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) >= limit:
            break
    return out


# --- model call ---------------------------------------------------------------


class ModelUnavailable(RuntimeError):
    """No reachable model service, or it answered with something unusable."""


def _served_models(token: str) -> dict[str, frozenset[str]]:
    from .cli_config import current_model_catalogue

    return current_model_catalogue(token)


def _pick_model(token: str) -> str:
    """The summariser's model, from the ``insight`` role in server/models.py.

    Unlike the CLI writers this raises rather than guessing when nothing in the
    chain is served, because there is a good non-LLM answer available: the
    extraction generator quotes the attendee's own words. A summary invented by
    a model we could not confirm exists is worse than that.
    """
    pinned = config.insight_summary_model()
    if pinned:
        wanted = models.service_name(pinned)
        from . import model_policy

        if (
            model_policy.direct_catalogue() is not None
            and not model_policy.direct_service_allowed(wanted, "chat")
        ):
            raise ModelUnavailable(f"{wanted} is not enabled by the current model policy")
        return wanted
    available = _served_models(token)
    for name in models.chain("insight"):
        if models.serves(available, name, "insight"):
            return models.service_name(name)
    raise ModelUnavailable("no summarisation model service is available in this workspace")


def _extract_json(text: str) -> dict:
    """Parse the model's answer, tolerating a fenced or prefixed object.

    Models wrap JSON in prose or a code fence often enough that treating it as a
    hard failure would push most sessions onto the keyword fallback.
    """
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
    except ValueError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise ModelUnavailable("model did not return JSON") from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except ValueError:
            raise ModelUnavailable("model returned unparseable JSON") from None
    if not isinstance(parsed, dict):
        raise ModelUnavailable("model returned JSON that is not an object")
    return parsed


def _rejects_temperature(resp) -> bool:
    """A 400 whose message says the model does not take ``temperature``."""
    if getattr(resp, "status_code", None) != 400:
        return False
    try:
        text = resp.text or ""
    except Exception:  # noqa: BLE001
        return False
    return "temperature" in text.lower() and "support" in text.lower()


def _ask_model(harvest: artifacts.Harvest, signal: dict | None) -> tuple[dict, str]:
    import requests

    from .credentials import CredentialError, credential_manager

    from . import model_policy
    from .cli_config import unified_chat_url
    from .gateway_errors import describe

    url = unified_chat_url()
    if not url:
        raise ModelUnavailable("no DATABRICKS_HOST configured")
    try:
        token = credential_manager.token()
    except CredentialError as exc:
        raise ModelUnavailable(f"no workshop credential: {exc}") from exc

    model = _pick_model(token)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": _material(harvest, signal)},
        ],
        "max_tokens": _MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Databricks-Ai-Gateway-Request-Tags": model_policy.request_tags("insight-summary"),
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=_MODEL_TIMEOUT)
        if _rejects_temperature(resp):
            # Claude Opus 5.x through the gateway refuses the parameter outright
            # ("does not support the temperature parameter"), so every summary
            # fell back to extraction (live qualification, 25 Oct 2026).
            body.pop("temperature")
            resp = requests.post(url, headers=headers, json=body, timeout=_MODEL_TIMEOUT)
    except requests.RequestException as exc:
        raise ModelUnavailable(f"AI Gateway unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise ModelUnavailable(describe(resp))
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ModelUnavailable(f"unexpected serving response shape: {exc}") from exc
    if isinstance(content, list):  # some endpoints return content blocks
        content = "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return _extract_json(str(content)), model


def _material(harvest: artifacts.Harvest, signal: dict | None) -> str:
    """The bounded evidence pack the model is allowed to reason over."""
    blocks: list[str] = []
    if signal:
        blocks.append(
            "Behavioural signal: engagement={}, shipped={}, topics touched={}".format(
                signal.get("engagement", "unknown"),
                signal.get("shipped", False),
                ", ".join(signal.get("products") or []) or "none",
            )
        )
    if harvest.prompts:
        blocks.append(
            "Attendee prompts (verbatim, newest last):\n"
            + "\n".join(f"- {p}" for p in harvest.prompts)
        )
    if harvest.plans:
        blocks.append(
            "Agent plan items:\n" + "\n".join(f"- {p}" for p in harvest.plans)
        )
    if harvest.tool_targets:
        blocks.append(
            "Files, commands and resources touched:\n"
            + "\n".join(f"- {t}" for t in harvest.tool_targets)
        )
    if harvest.errors:
        blocks.append(
            "Errors hit (first lines):\n" + "\n".join(f"- {e}" for e in harvest.errors)
        )
    if harvest.artifacts:
        blocks.append(
            "Documents the agent wrote:\n"
            + "\n".join(
                f"- {a.kind}: {a.title}" for a in harvest.artifacts if a.kind != "commit"
            )
        )
    commits = [a.title for a in harvest.artifacts if a.kind == "commit"]
    if commits:
        blocks.append("Commit subjects:\n" + "\n".join(f"- {c}" for c in commits))
    for title, excerpt in harvest.documents:
        blocks.append(f"--- excerpt of {title} ---\n{excerpt}")
    return "\n\n".join(blocks) or "No material was captured for this attendee."


# --- keyword extraction fallback ---------------------------------------------


def _extraction(harvest: artifacts.Harvest, signal: dict | None) -> dict:
    """A summary with no model behind it.

    Not a lesser version of the same thing: it makes no claims it cannot point
    at. The headline states the engagement band and the topic trail, the use
    cases are the attendee's own longest prompts quoted as-is, and the blockers
    are the error lines verbatim. Everything a reader might mistake for analysis
    is left out, which is why ``generator`` travels with it.
    """
    signal = signal or {}
    engagement = signal.get("engagement") or "unknown"
    products = _string_list(signal.get("products"))
    topic_phrase = ", ".join(products) if products else "no recognised product topics"
    shipped = bool(signal.get("shipped"))
    built = [a.title for a in harvest.artifacts if a.kind == "commit"]

    headline = (
        f"{engagement.title()} session: {topic_phrase}"
        + (f"; {len(built)} commits" if built else "")
        + ("; hit errors" if harvest.errors else "")
    )

    what: list[str] = []
    if harvest.plans:
        what.append("Agent plan: " + "; ".join(harvest.plans[:3]))
    if built:
        what.append("Committed: " + "; ".join(built[:3]))
    docs = [a.title for a in harvest.artifacts if a.kind != "commit"]
    if docs:
        what.append("Documents produced: " + ", ".join(docs[:5]))
    if not what and harvest.tool_targets:
        what.append("Touched: " + "; ".join(harvest.tool_targets[:5]))

    # Longest prompts first: length correlates with a stated intention rather
    # than a follow-up, and this pass has no way to judge meaning.
    candidates = sorted(harvest.prompts, key=len, reverse=True)[:MAX_USE_CASES]
    use_cases = [
        {
            "title": _trim(prompt, 120),
            "summary": "",
            "products": [],
            "evidence": "attendee prompt, quoted verbatim (no model summarisation)",
        }
        for prompt in candidates
    ]

    return {
        "headline": _trim(headline, MAX_HEADLINE_CHARS),
        "what_they_built": _trim(" ".join(what), MAX_TEXT_CHARS),
        # Deliberately absent. Intent is a judgement about why someone was
        # building, and this pass makes no judgements — a keyword rule would
        # read "game" in a prompt and file a fraud-detection session as `fun`.
        # `generator: extraction` already tells a reader why the field is empty.
        "use_cases": use_cases,
        "blockers": _string_list(harvest.errors),
        "products": products,
        "shipped": shipped,
    }


# --- assembly and emission ----------------------------------------------------


def _payload(
    raw: dict,
    harvest: artifacts.Harvest,
    *,
    run_id: str,
    email: str,
    phase: str,
    generator: str,
    model: str | None,
    revision: int = 1,
) -> dict:
    """Shape a raw summary into the contract's ``insight.summary`` payload.

    Every field is re-derived and re-bounded here rather than trusted from the
    model, so a model that ignores the schema produces a thin summary instead of
    an ingest rejection — the event is the only copy that survives teardown.
    """
    use_cases = []
    for item in raw.get("use_cases") or []:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = _trim(item.get("title"), MAX_HEADLINE_CHARS)
        if not title:
            continue
        entry = {"title": title}
        for key, limit in (("summary", MAX_TEXT_CHARS), ("evidence", MAX_TEXT_CHARS)):
            value = _trim(item.get(key), limit)
            if value:
                entry[key] = value
        products = _string_list(item.get("products"))
        if products:
            entry["products"] = products
        use_cases.append(entry)
        if len(use_cases) >= MAX_USE_CASES:
            break

    payload = {
        "summary_id": _summary_id(run_id, email),
        "generated_at": _now(),
        "generator": generator,
        "model": model,
        "phase": phase,
        # Monotonic per attendee. Control Tower must ignore a revision lower than
        # the one it holds: the retry buffer can flush out of order, and a rolling
        # summary that took the last arrival would regress to an older session.
        "revision": revision,
        # The one required content field. An empty headline would render as a
        # blank row in a brief, which reads as a data bug rather than a quiet
        # session, so the fallback text says which it is.
        "headline": _trim(raw.get("headline"), MAX_HEADLINE_CHARS)
        or "No summary could be derived from this session.",
        "prompt_count": harvest.prompt_count,
        "redactions": harvest.redactions,
    }
    for key in ("what_they_built",):
        value = _trim(raw.get(key), MAX_TEXT_CHARS)
        if value:
            payload[key] = value
    # Constrained like discovery's copy of the field: an unrecognised value is
    # dropped, so absent means "not classified" rather than "classified oddly".
    intent = _trim(raw.get("session_intent"), 32).lower()
    if intent in SESSION_INTENTS:
        payload["session_intent"] = intent
    if use_cases:
        payload["use_cases"] = use_cases
    for key in ("blockers", "products"):
        values = _string_list(raw.get(key))
        if values:
            payload[key] = values
    artifact_payload = harvest.artifact_payload()
    if artifact_payload:
        payload["artifacts"] = artifact_payload
    return payload


def summarise_user(
    user: User,
    *,
    phase: str,
    signal: dict | None = None,
    allow_llm: bool = True,
    single_attendee: bool = True,
    force: bool = False,
    emitter=None,
) -> dict | None:
    """Harvest and emit one attendee's session; storage errors remain visible.

    Returns the emitted payload, or ``None`` when there was nothing to say,
    nothing has changed since the last summary, the interval has not elapsed, or
    capture is off. ``None`` is the common outcome on a rolling schedule and is
    never an error: an attendee who opened the landing page and left has no
    session to describe, and inventing one would be worse than the gap.

    ``force`` skips the interval floor, for wrap and teardown — the floor bounds
    cost during a session, and at the end of one there is no later chance.
    """
    if not config.insight_capture_enabled():
        return None
    if emitter is None:
        from .event_emitter import event_emitter as emitter

    journal = getattr(emitter, "journal", None)
    if journal is not None:
        emitter._ensure_ready()
        if stamps._journal is not journal:
            stamps.configure_capture(journal)
    intended = "llm" if allow_llm else "extraction"
    if not stamps.should_run(user.email, generator=intended, force=force):
        return None
    # One pass per attendee at a time. A forced pass bypasses the interval, so it
    # can arrive while a backgrounded one is still waiting on the model; without
    # this both would emit at the same revision and Control Tower would keep one.
    if not stamps.claim(user.email):
        logger.debug("edge summary already in flight for %s; skipping", user.email)
        return None
    try:
        with admitted_writer(journal, "insight_summary") if journal is not None else nullcontext():
            return _summarise_claimed(
                user, phase=phase, signal=signal, allow_llm=allow_llm,
                single_attendee=single_attendee, intended=intended, emitter=emitter,
            )
    finally:
        stamps.release(user.email)
        with _worker_condition:
            _worker_condition.notify_all()


def _summarise_claimed(
    user: User,
    *,
    phase: str,
    signal: dict | None,
    allow_llm: bool,
    single_attendee: bool,
    intended: str,
    emitter,
) -> dict | None:
    """One claimed pass; model failures fall back, retained-write failures propagate."""
    try:
        harvest = artifacts.harvest_user(user, single_attendee=single_attendee)
    except Exception as exc:  # noqa: BLE001 — harvest is already fail-soft
        logger.warning("harvest failed for %s: %s", user.email, exc)
        return None
    # Recorded whatever happens next, so an attendee who has gone quiet costs one
    # directory walk per interval rather than one per harvest.
    stamps.note_attempt(user.email)
    if harvest.is_empty():
        return None

    fingerprint = _fingerprint(harvest, signal)
    if not stamps.changed(user.email, fingerprint, generator=intended):
        logger.debug("edge summary unchanged for %s; skipping", user.email)
        return None

    generator, model, raw = "extraction", None, None
    if allow_llm:
        try:
            raw, model = _ask_model(harvest, signal)
            generator = "llm"
        except ModelUnavailable as exc:
            logger.info("edge summary falling back to extraction (%s)", exc)
        except Exception as exc:  # noqa: BLE001 — a summary is never worth a 500
            logger.warning("edge summary model call failed: %s", exc)
    if raw is None:
        # The model was meant to answer and didn't. Falling back is only correct
        # while it wouldn't downgrade a model summary already sent — the interval
        # is not re-checked here, because this pass stamped its own attempt above.
        if not stamps.may_emit(user.email, generator="extraction"):
            return None
        raw = _extraction(harvest, signal)

    if getattr(emitter, "journal", None) is not None:
        template = _payload(
            raw, harvest, run_id=emitter.run_id, email=user.email, phase=phase,
            generator=generator, model=model, revision=1,
        )
        def prepare(state):
            summaries = stamps._validate(state.setdefault("summaries", {}))
            previous = summaries.get(user.email, {})
            # A concurrent process may have emitted while this model call ran.
            if previous.get("generator") == "llm" and generator != "llm":
                return None
            if (previous.get("fingerprint") == fingerprint
                    and not (previous.get("generator") == "extraction" and generator == "llm")):
                return None
            revision = previous.get("revision", 0) + 1
            payload = copy.deepcopy(template)
            payload["revision"] = revision
            summaries[user.email] = {"revision": revision, "generator": generator,
                                     "fingerprint": fingerprint}
            return payload, f"summary:{emitter.run_id}:{user.email}:{generator}:{revision}"
        accepted = stamps._checked(emitter.journal, lambda: emitter.emit_transaction(
            "insight.summary", user.email, prepare))
        if not accepted:
            return None
        stamps._sync_capture()
        payload = emitter.journal.read()["insight"]["events"][str(accepted)]["payload"]
        revision = payload["revision"]
    else:
        revision = stamps.next_revision(user.email)
        payload = _payload(
            raw, harvest, run_id=emitter.run_id, email=user.email, phase=phase,
            generator=generator, model=model, revision=revision,
        )
        emitter.emit(
            "insight.summary", user.email, payload,
            idempotency_key=f"summary:{emitter.run_id}:{user.email}:{generator}:{revision}",
        )
        stamps.mark(user.email, generator, fingerprint)
    logger.info(
        "edge summary emitted for %s (generator=%s, revision=%d, prompts=%d, "
        "artifacts=%d)",
        user.email, generator, revision, harvest.prompt_count,
        len(harvest.artifacts),
    )
    return payload


def summarise_all(
    users: list[User], *, phase: str, allow_llm: bool, force: bool = False,
    emitter=None,
) -> int:
    """Summarise every attendee on this instance. Returns how many were emitted.

    The behavioural signal is gathered once per user and handed to the summariser
    so the narrative and the counters describe the same session — deriving
    engagement twice is how the two ends of a brief come to disagree.

    On the rolling schedule most calls emit nothing, because most attendees have
    not moved since the last pass. Zero is the expected return, not a failure.
    """
    from . import stats

    if not config.insight_capture_enabled():
        return 0
    users = list(users)
    single = len(users) <= 1
    resources = stats._workspace_resources() if users else {}
    emitted = 0
    for user in users:
        try:
            signal = stats.gather_user(
                user, fresh=False, resources=resources
            ).get("signal")
        except Exception as exc:  # noqa: BLE001
            logger.warning("signal unavailable for %s: %s", user.email, exc)
            signal = None
        if summarise_user(
            user, phase=phase, signal=signal, allow_llm=allow_llm,
            single_attendee=single, force=force, emitter=emitter,
        ):
            emitted += 1
    return emitted


def summarise_in_background(
    users: list[User], *, phase: str, force: bool = False
) -> threading.Thread | None:
    """Run summarisation off the request thread.

    Both callers need this. ``POST /api/admin/phase`` is a foreground operator
    action in front of a live room, and ``GET /api/admin/stats`` is Control
    Tower's harvest, whose latency decides how long a fleet-wide poll takes.
    Neither can afford a model call per attendee inline.

    Returns ``None`` when there is nothing to do, so a caller can report whether
    a pass was even started without waiting for it to finish.
    """
    if not config.insight_capture_enabled():
        return None
    from .event_emitter import event_emitter

    event_emitter._ensure_ready()
    try:
        check_open(event_emitter.journal)
    except CaptureClosedError:
        return None
    snapshot = list(users)
    if not snapshot:
        return None
    # Cheap pre-check on the caller's thread: if no attendee is due, don't pay for
    # a thread per harvest for the whole workshop.
    if not force and not any(
        stamps.should_run(user.email, generator="llm") for user in snapshot
    ):
        return None

    # Reserve before starting the thread: closing can race the scheduler, but
    # cannot pass a thread that has not yet entered its body as already drained.
    try:
        ticket = admitted_writer(event_emitter.journal, "insight_summary", detached=True)
    except CaptureClosedError:
        return None

    def run() -> None:
        outcome = {"writer": "insight_summary", "outcome": "completed"}
        try:
            with ticket:
                summarise_all(snapshot, phase=phase, allow_llm=True, force=force,
                              emitter=event_emitter)
        except Exception as exc:  # noqa: BLE001
            logger.warning("summarisation failed: %s", exc)
            outcome = {"writer": "insight_summary", "outcome": "failed",
                       "error": type(exc).__name__}
        finally:
            with _worker_condition:
                _background_workers.discard(threading.current_thread())
                _background_outcomes.append(outcome)
                del _background_outcomes[:-128]
                _worker_condition.notify_all()

    thread = threading.Thread(target=run, name="insight-summary", daemon=True)
    with _worker_condition:
        _background_workers.add(thread)
    try:
        thread.start()
    except Exception:
        with _worker_condition:
            _background_workers.discard(thread)
        # Complete the reserved ticket with the actual failure, preserving its
        # failed outcome rather than leaking an apparently running reservation.
        with ticket:
            raise
    return thread


def drain_for_closeout(timeout: float) -> dict:
    """Wait for this process's summaries; durable interrupted tickets stay held.

    The coordinator also checks the journal's writer inventory. An empty local
    thread set after process replacement is not evidence that its previous
    durable reservations completed.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout < 0):
        raise ValueError("summary drain timeout must be nonnegative")
    deadline = time.monotonic() + timeout
    with _worker_condition:
        while True:
            with stamps._lock:
                claimed = bool(stamps._in_flight)
            if not _background_workers and not claimed:
                return {"complete": True, "issues": [],
                        "outcomes": copy.deepcopy(_background_outcomes)}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"complete": False, "issues": ["summary_drain_timeout"],
                        "outcomes": copy.deepcopy(_background_outcomes)}
            _worker_condition.wait(timeout=remaining)


__all__ = [
    "ModelUnavailable",
    "stamps",
    "summarise_all",
    "summarise_in_background",
    "summarise_user",
    "drain_for_closeout",
]
