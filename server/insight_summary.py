"""Edge summary of one attendee's session (contract C6, tier 3).

The harvest in ``artifacts.py`` produces the raw residue; this module turns it
into the few sentences an account team can act on and pushes them to Control
Tower over the existing ingest path. Doing the reduction here rather than in
Control Tower is not an optimisation — the inputs are destroyed by teardown
(``drop_catalog`` takes the promote docs, ``delete_workspace`` takes the repos),
so an instance that shuts down without summarising has nothing left to summarise.

**Two triggers, deliberately asymmetric.**

*Wrap* is the primary: the operator drives it, the app is warm, the attendee is
still present, and the certificate moment already lives there. It gets the model
call. *The final teardown harvest* is the backstop, and it gets keyword
extraction only — teardown can run hours later via scheduled cleanup, is
explicitly best-effort, and an LLM call there would fail silently into nothing.
The two are distinguished on the wire by ``generator``, because an extraction
summary is keyword-thin and must never be read as a finding.

**Exactly once per attendee**, stamped in memory. An LLM summary is never
downgraded by a later extraction pass; an extraction summary *can* be upgraded
if the model becomes reachable, since the two carry different idempotency keys
and Control Tower prefers ``llm``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone

from . import artifacts, config
from .users import User

logger = logging.getLogger("workshop.insight_summary")

# Newest-first. Summarising a day of prompts is a small, well-bounded job, so the
# cheap tier is the correct default — spending Opus here buys nothing an account
# team would notice and competes with the attendees' own agent budget.
_MODEL_CHAIN = (
    "databricks-claude-haiku-4-5",
    "databricks-claude-sonnet-5",
    "databricks-claude-sonnet-4-6",
    "databricks-gpt-oss-120b",
    "databricks-meta-llama-3-3-70b-instruct",
)
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
- Do not flatter. "Explored Lakeflow ingestion and hit a permissions wall" beats \
"showed strong engagement with the platform".
"""


class _Stamps:
    """Which attendees have already been summarised, and how.

    In memory on purpose. A restart losing the stamp costs one duplicate
    generation whose event carries the same idempotency key, so Control Tower
    discards it — whereas a stamp persisted to the volume would be one more thing
    to reconcile at teardown for no benefit.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done: dict[str, str] = {}

    def should_run(self, email: str, *, generator: str) -> bool:
        with self._lock:
            existing = self._done.get(email)
        if existing is None:
            return True
        # An extraction pass never overwrites a model summary; the reverse is an
        # upgrade and is allowed, because wrap runs before the model may have
        # recovered and the thin summary is what we settled for.
        return existing == "extraction" and generator == "llm"

    def mark(self, email: str, generator: str) -> None:
        with self._lock:
            self._done[email] = generator

    def generator_for(self, email: str) -> str | None:
        with self._lock:
            return self._done.get(email)

    def clear(self) -> None:
        with self._lock:
            self._done.clear()


stamps = _Stamps()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary_id(run_id: str, email: str) -> str:
    """Stable per attendee, so a regenerated summary supersedes rather than adds."""
    digest = hashlib.sha256(f"{run_id}:{email}".encode()).hexdigest()
    return f"sum_{digest[:16]}"


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
    """No reachable serving endpoint, or it answered with something unusable."""


def _ready_endpoints(token: str) -> set[str]:
    from .cli_config import _discover_serving_endpoints

    return _discover_serving_endpoints(token)


def _pick_model(token: str) -> str:
    pinned = config.insight_summary_model()
    if pinned:
        return pinned
    available = _ready_endpoints(token)
    for name in _MODEL_CHAIN:
        if name in available:
            return name
    raise ModelUnavailable("no summarisation endpoint is READY in this workspace")


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


def _ask_model(harvest: artifacts.Harvest, signal: dict | None) -> tuple[dict, str]:
    import requests

    from .credentials import CredentialError, credential_manager

    host = config.databricks_host()
    if not host:
        raise ModelUnavailable("no DATABRICKS_HOST configured")
    try:
        token = credential_manager.token()
    except CredentialError as exc:
        raise ModelUnavailable(f"no workshop credential: {exc}") from exc

    model = _pick_model(token)
    try:
        resp = requests.post(
            f"{host}/serving-endpoints/{model}/invocations",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messages": [
                    {"role": "system", "content": _PROMPT},
                    {"role": "user", "content": _material(harvest, signal)},
                ],
                "max_tokens": _MAX_TOKENS,
                # Deterministic: the same session must not produce a different
                # brief on a re-run, or an account team cannot trust either copy.
                "temperature": 0.0,
            },
            timeout=_MODEL_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ModelUnavailable(f"serving endpoint unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise ModelUnavailable(f"serving endpoint returned {resp.status_code}")
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
    emitter=None,
) -> dict | None:
    """Harvest, summarise and emit one attendee's session. Never raises.

    Returns the emitted payload, or ``None`` when there was nothing to say, the
    attendee was already summarised, or capture is off. ``None`` is a normal
    outcome and not an error: an attendee who opened the landing page and left
    has no session to describe, and inventing one would be worse than the gap.
    """
    if not config.insight_capture_enabled():
        return None
    if emitter is None:
        from .event_emitter import event_emitter as emitter

    intended = "llm" if allow_llm else "extraction"
    if not stamps.should_run(user.email, generator=intended):
        return None

    try:
        harvest = artifacts.harvest_user(user, single_attendee=single_attendee)
    except Exception as exc:  # noqa: BLE001 — harvest is already fail-soft
        logger.warning("harvest failed for %s: %s", user.email, exc)
        return None
    if harvest.is_empty():
        # Stamped so the teardown backstop doesn't re-walk an empty home.
        stamps.mark(user.email, intended)
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
        if not stamps.should_run(user.email, generator="extraction"):
            return None
        raw = _extraction(harvest, signal)

    payload = _payload(
        raw, harvest,
        run_id=emitter.run_id, email=user.email, phase=phase,
        generator=generator, model=model,
    )
    emitter.emit(
        "insight.summary",
        user.email,
        payload,
        # Generator is part of the key so an extraction summary can later be
        # superseded by a model one; a retried flush of either stays a duplicate.
        idempotency_key=f"summary:{emitter.run_id}:{user.email}:{generator}",
    )
    stamps.mark(user.email, generator)
    logger.info(
        "edge summary emitted for %s (generator=%s, prompts=%d, artifacts=%d)",
        user.email, generator, harvest.prompt_count, len(harvest.artifacts),
    )
    return payload


def summarise_all(
    users: list[User], *, phase: str, allow_llm: bool, emitter=None
) -> int:
    """Summarise every attendee on this instance. Returns how many were emitted.

    The behavioural signal is gathered once per user and handed to the summariser
    so the narrative and the counters describe the same session — deriving
    engagement twice is how the two ends of a brief come to disagree.
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
            single_attendee=single, emitter=emitter,
        ):
            emitted += 1
    return emitted


def summarise_in_background(users: list[User], *, phase: str) -> threading.Thread | None:
    """Run the wrap summarisation off the request thread.

    ``POST /api/admin/phase`` is a foreground operator action in front of a live
    room, and this does a model call per attendee. Blocking the phase flip on it
    would stall the projector.
    """
    if not config.insight_capture_enabled():
        return None
    snapshot = list(users)
    if not snapshot:
        return None

    def run() -> None:
        try:
            summarise_all(snapshot, phase=phase, allow_llm=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("wrap summarisation failed: %s", exc)

    thread = threading.Thread(target=run, name="insight-summary", daemon=True)
    thread.start()
    return thread


__all__ = [
    "ModelUnavailable",
    "stamps",
    "summarise_all",
    "summarise_in_background",
    "summarise_user",
]
