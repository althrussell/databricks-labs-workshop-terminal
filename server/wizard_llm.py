"""LLM-backed idea cards for the opening wizard.

The modal is still a form: three beats, Skip, Escape, ``seen``. This module
only fills the idea grid and, when the sentence is enough to tell, moves the
industry chip. Discovery records are never written here — ``wizard.save`` is
the only path that produces a ``confidence: high`` row, and only for what the
attendee confirmed on Next.

A failed, timed-out, or flagged-off call returns the deterministic selector
unchanged. The textarea must never wait on a model that is not answering.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from . import config, content, demo_data, models, wizard

logger = logging.getLogger(__name__)

# 4s against 1800 tokens of JSON was not a budget, it was a guarantee of
# failure: every request timed out, every attendee got the static selector, and
# because the fallback is silent nobody could see it happening. The fix is on
# both sides — ask for less (four cards, short prompts) and wait longer for it.
_TIMEOUT_SECONDS = 12.0
_MAX_TOKENS = 1100
# The model writes four; the selector pads to six. Two fewer cards is roughly a
# third off the output, and the padding is drawn from the verified catalogue so
# the grid is no thinner for it.
_LLM_CARD_COUNT = 4
# The per-card prompt is the whole output budget in one field. 2000 characters
# is ~500 tokens a card, and the agent does not need an essay — it needs the
# tables named and a first move.
_MAX_PROMPT_CHARS = 500
_ALLOWED_SHAPES = frozenset({"dashboard", "app", "pipeline", "ai", "ml"})
_FEW_SHOT = 3
_TABLE_CAP = 24

# The workspace's model catalogue changes between releases, not between
# keystrokes. This was being re-discovered on every debounce, adding a round
# trip to a request that was already timing out.
_DISCOVERY_TTL_SECONDS = 600
# Short, because an empty result means the call failed and a workspace that has
# just been fixed should not wait ten minutes to be believed.
_DISCOVERY_FAILURE_TTL_SECONDS = 60

_discovery_lock = threading.Lock()
_discovery: dict[str, frozenset[str]] | None = None
_discovery_at = 0.0
_discovery_ok = False

# An operator's mid-workshop swap, ahead of the deployed pin. Deliberately in
# memory only: the deployed value is written to both the deployment env and
# ``app.yaml`` precisely so a console redeploy cannot silently revert it, and a
# persisted override would undo that — the process would come back from a crash
# already disagreeing with the thing an operator can read. The cost is that a
# restart drops the swap, which is why ``effective_model`` reports whether one
# is active rather than leaving the revert invisible.
_override_lock = threading.Lock()
_model_override = ""

# Models that answered 400 to a JSON schema. Remembered so the retry is paid
# once per process rather than on every request.
_structured_unsupported: set[str] = set()

# Asking for JSON in the prompt and scanning the reply for braces is a
# best-effort parse of a best-effort instruction: a model that opens with
# "Here are six ideas:" or wraps the object in prose costs a whole request, and
# the failure is silent because the fallback selector looks like a result. The
# gateway will enforce the shape instead where the model supports it.
_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "wizard_ideas",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "industry": {"type": "string"},
                "ideas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "outcome": {"type": "string"},
                            "prompt": {"type": "string"},
                            "shape": {
                                "type": "string",
                                "enum": sorted(_ALLOWED_SHAPES),
                            },
                            "intents": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "products": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "technical": {"type": "boolean"},
                            "demo_tables": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        # strict mode requires every property listed; the
                        # coercer below already tolerates empty values.
                        "required": [
                            "id",
                            "label",
                            "outcome",
                            "prompt",
                            "shape",
                            "intents",
                            "products",
                            "technical",
                            "demo_tables",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["industry", "ideas"],
            "additionalProperties": False,
        },
    },
}


class ModelUnavailable(RuntimeError):
    """No reachable model service, or it answered with something unusable."""


def suggest(
    text: str, industry: str = "", *, industry_locked: bool = False
) -> dict[str, Any]:
    """Industry + six buildable cards, from the model or the selector.

    ``industry`` is the chip the attendee currently has. A seeded inference
    may replace it unless ``industry_locked`` — a chip they confirmed is not
    the model's to move. An unseeded inference is ignored. Every card that
    reaches the response has passed ``demo_data.verify``.
    """
    kept = demo_data.industry_slug(industry) if industry else ""
    fallback = _fallback(kept, query=text)
    if not text.strip() or not config.llm_wizard_enabled():
        return fallback
    try:
        raw, model = _ask_model(text, kept)
    except ModelUnavailable as exc:
        logger.info("wizard llm falling back to selector: %s", exc)
        return fallback

    inferred = demo_data.industry_slug(str(raw.get("industry") or ""))
    if industry_locked:
        resolved = kept
    elif inferred and (not demo_data.enabled() or demo_data.has_industry(inferred)):
        resolved = inferred
    else:
        resolved = kept

    verified, offered = _verified_ideas(raw.get("ideas"), resolved)
    dropped = offered - len(verified)
    if dropped:
        # The one number that makes a model swap an argument about evidence
        # rather than taste: a model that writes six cards naming tables that do
        # not exist and a model that writes four good ones look identical from
        # the grid, because the selector silently pads both back to six.
        logger.info(
            "wizard llm: %s offered %d cards, %d dropped (industry=%s)",
            model,
            offered,
            dropped,
            resolved or "none",
        )
    padded = _pad(verified, resolved, query=text)
    return {
        "industry": resolved,
        "ideas": [wizard.idea_payload(i) for i in padded],
        "source": "llm" if verified else "selector",
        "model": model,
        "offered": offered,
        "dropped": dropped,
    }


def _fallback(industry: str, query: str = "") -> dict[str, Any]:
    ideas = wizard.select_ideas(industry, query=query)
    return {
        "industry": industry,
        "ideas": [wizard.idea_payload(i) for i in ideas],
        "source": "selector",
        "model": "",
        "offered": 0,
        "dropped": 0,
    }


def _verified_ideas(raw: Any, industry: str) -> tuple[list[content.WizardIdea], int]:
    """Cards that survived validation, and how many the model offered."""
    if not isinstance(raw, list):
        return [], 0
    out: list[content.WizardIdea] = []
    seen: set[str] = set()
    offered = 0
    for item in raw:
        offered += 1
        idea = _coerce_idea(item, industry)
        if idea is None or idea.id in seen:
            continue
        seen.add(idea.id)
        out.append(idea)
        if len(out) >= _LLM_CARD_COUNT:
            break
    return out, offered


def _coerce_idea(raw: Any, industry: str) -> content.WizardIdea | None:
    if not isinstance(raw, dict):
        return None
    shape = str(raw.get("shape") or "dashboard").strip().lower()
    if shape not in _ALLOWED_SHAPES:
        return None
    tables = [
        str(t).strip()
        for t in (raw.get("demo_tables") or [])
        if str(t).strip()
    ]
    if not demo_data.verify(tables):
        return None
    schemas = {ref.partition(".")[0] for ref in tables}
    if len(schemas) > 1:
        return None
    if industry and schemas and industry not in schemas:
        return None
    label = str(raw.get("label") or "").strip()[:80]
    outcome = str(raw.get("outcome") or "").strip()[:200]
    prompt = str(raw.get("prompt") or "").strip()[:_MAX_PROMPT_CHARS]
    if not (label and outcome and prompt):
        return None
    idea_id = re.sub(r"[^a-z0-9-]+", "-", str(raw.get("id") or label).lower())
    idea_id = idea_id.strip("-")[:48] or "llm-idea"
    tagged = [industry] if industry else list(schemas)
    intents = [
        str(v).strip()
        for v in (raw.get("intents") or ["business_problem"])
        if str(v).strip() in wizard.INTENTS
    ] or ["business_problem"]
    return content.WizardIdea(
        id=idea_id,
        label=label,
        outcome=outcome,
        prompt=prompt,
        industries=tagged,
        intents=intents,
        products=[str(v).strip() for v in (raw.get("products") or []) if str(v).strip()][:8],
        shape=shape,
        technical=bool(raw.get("technical")),
        demo_tables=tables,
    )


def _pad(
    ideas: list[content.WizardIdea], industry: str, query: str = ""
) -> list[content.WizardIdea]:
    if len(ideas) >= wizard.IDEA_COUNT:
        return ideas[: wizard.IDEA_COUNT]
    picked = {i.id for i in ideas}
    labels = {i.label.lower() for i in ideas}
    for extra in wizard.select_ideas(industry, limit=12, query=query):
        if extra.id in picked or extra.label.lower() in labels:
            continue
        ideas.append(extra)
        picked.add(extra.id)
        labels.add(extra.label.lower())
        if len(ideas) >= wizard.IDEA_COUNT:
            break
    return ideas[: wizard.IDEA_COUNT]


def _inventory_lines(industry: str) -> str:
    inv = demo_data.inventory()
    if not inv:
        return "(no demo catalog in this deployment)"
    schemas = [industry] if industry and industry in inv else sorted(inv)
    lines: list[str] = []
    for schema in schemas:
        tables = sorted(inv[schema])[:_TABLE_CAP]
        extra = len(inv[schema]) - len(tables)
        shown = ", ".join(tables)
        if extra > 0:
            shown += f", +{extra} more"
        lines.append(f"- {schema}: {shown}")
    return "\n".join(lines)


def _few_shot(industry: str) -> str:
    ideas = [
        i
        for i in content.content_service.ideas()
        if (industry and industry in i.industries) or (not industry and not i.industries)
    ][:_FEW_SHOT]
    if not ideas:
        ideas = [i for i in content.content_service.ideas() if not i.industries][:_FEW_SHOT]
    blobs = []
    for idea in ideas:
        blobs.append(
            json.dumps(
                {
                    "id": idea.id,
                    "label": idea.label,
                    "outcome": idea.outcome,
                    "prompt": idea.prompt,
                    "shape": idea.shape,
                    "industries": idea.industries,
                    "intents": idea.intents,
                    "demo_tables": idea.demo_tables,
                }
            )
        )
    return "\n".join(blobs) if blobs else "(none)"


def _prompt(text: str, industry: str) -> str:
    locked = (
        "The attendee has confirmed this industry chip. Do not infer a "
        "different industry. Keep it.\n"
        if industry
        else ""
    )
    return (
        "You write idea cards for a Databricks workshop opening wizard.\n"
        "Return JSON only, no prose, of the form "
        f'{{"industry": "slug", "ideas": [ ...{_LLM_CARD_COUNT} cards... ]}}.\n\n'
        "Rules:\n"
        f"- {locked}"
        "- If the industry chip is empty, infer a schema slug from the sentence "
        "when you can; otherwise leave it empty.\n"
        "- Never invent a schema that is not in the inventory below.\n"
        "- If the sentence can be built from the listed tables, every "
        "demo_tables value must be schema.table from that inventory.\n"
        "- If the sentence cannot be built from those tables, return cards with "
        "empty demo_tables. That is net-new: the agent will generate data. "
        "Still tag the card with the confirmed industry.\n"
        "- All tables on one card share one schema.\n"
        "- Spread shapes across dashboard, app, pipeline, ai, ml. No fun cards.\n"
        "- Prompts must name the tables they will use when demo_tables is not "
        "empty, "
        f"and must be under {_MAX_PROMPT_CHARS} characters. Two or three "
        "sentences, not a specification.\n"
        "- Cards must be buildable in a two-hour workshop.\n\n"
        f"Attendee sentence:\n{text or '(they have not typed anything yet)'}\n\n"
        f"Current industry chip: {industry or '(none)'}\n\n"
        f"Seeded tables:\n{_inventory_lines(industry)}\n\n"
        f"Example cards (style only, not the only allowed outputs):\n{_few_shot(industry)}\n"
    )


def _extract_json(text: str) -> dict:
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


def _served_models(token: str) -> dict[str, frozenset[str]]:
    """The workspace's model catalogue, cached. Empty means discovery failed."""
    global _discovery, _discovery_at, _discovery_ok

    from . import model_policy

    governed = model_policy.direct_catalogue()
    if governed is not None:
        return governed

    with _discovery_lock:
        ttl = _DISCOVERY_TTL_SECONDS if _discovery_ok else _DISCOVERY_FAILURE_TTL_SECONDS
        if _discovery is not None and time.time() - _discovery_at < ttl:
            return _discovery

    from .cli_config import discover_model_services

    found = discover_model_services(token)
    with _discovery_lock:
        _discovery, _discovery_at, _discovery_ok = found, time.time(), bool(found)
    return found


def reset_discovery_cache() -> None:
    """Drop the cached catalogue. For tests and the admin reload path."""
    global _discovery, _discovery_at, _discovery_ok
    with _discovery_lock:
        _discovery, _discovery_at, _discovery_ok = None, 0.0, False


class UnknownModel(ValueError):
    """A swap named something this workspace demonstrably does not serve."""


def model_override() -> str:
    """The live override, or empty when the deployed value is in force."""
    with _override_lock:
        return _model_override


def set_model_override(name: str) -> str:
    """Swap the wizard's model for this process. Empty clears it.

    Validated against discovery *when discovery succeeds*, and accepted when it
    does not — the same rule as everywhere else in this module, because an empty
    catalogue means the discovery call failed rather than that the workspace
    serves nothing. Refusing a swap on a discovery blip would be refusing the
    one action an operator has left at the moment the room is already unhappy.
    """
    wanted = models.service_name(name.strip()) if name.strip() else ""
    if wanted:
        from .credentials import CredentialError, credential_manager

        try:
            available = _served_models(credential_manager.token())
        except CredentialError:
            available = {}
        from . import model_policy

        policy_active = model_policy.direct_catalogue() is not None
        if (available or policy_active) and not models.serves(
            available, wanted, "wizard"
        ):
            raise UnknownModel(
                f"{wanted} is not served on the wizard's wire in this workspace"
            )
    global _model_override
    with _override_lock:
        _model_override = wanted
    logger.info("wizard model override %s", wanted or "cleared")
    return wanted


def effective_model() -> dict[str, Any]:
    """What the wizard will ask next, and why — for ``/api/admin/state``.

    An ephemeral override that a restart silently reverted is worse than no
    override at all: the operator believes the room is on the model they picked.
    So the reported value carries its own provenance rather than a bare string.
    """
    override = model_override()
    pin = models.service_name(config.workshop_wizard_model().strip())
    if override:
        source = "override"
    elif pin:
        source = "deployed"
    else:
        source = "chain"
    return {
        "model": override or pin or "",
        "source": source,
        "override": override,
        "deployed": pin,
        "chain": list(models.wizard_chain()),
        "llm_enabled": config.llm_wizard_enabled(),
    }


def _pick_model(token: str) -> str:
    override = model_override()
    if override:
        return override
    pin = config.workshop_wizard_model()
    if pin:
        wanted = models.service_name(pin)
        from . import model_policy

        if (
            model_policy.direct_catalogue() is not None
            and not model_policy.direct_service_allowed(wanted, "chat")
        ):
            raise ModelUnavailable(f"{wanted} is not enabled by the current model policy")
        return wanted
    chain = models.wizard_chain()
    available = _served_models(token)
    from . import model_policy

    if not available and model_policy.direct_catalogue() is None:
        # Empty is documented in ``discover_model_services`` as *the call
        # failed*, not *the workspace serves nothing*. Reading it the second way
        # took the entire idea grid down for a blip on an unrelated API, and did
        # it silently, because the fallback to the static selector looks
        # identical to a model that simply had no better ideas.
        logger.info("model discovery unavailable; using the head of the wizard chain")
        return chain[0]
    for name in chain:
        if models.serves(available, name, "wizard"):
            return name
    raise ModelUnavailable("no wizard model service is available in this workspace")


def _ask_model(text: str, industry: str) -> tuple[dict, str]:
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
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": _prompt(text, industry)},
        ],
        "max_tokens": _MAX_TOKENS,
        "temperature": 0.3,
    }
    structured = model not in _structured_unsupported
    if structured:
        payload["response_format"] = _RESPONSE_FORMAT

    def post() -> Any:
        try:
            return requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Databricks-Ai-Gateway-Request-Tags": model_policy.request_tags(
                        "wizard"
                    ),
                },
                json=payload,
                timeout=_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ModelUnavailable(f"AI Gateway unreachable: {exc}") from exc

    resp = post()
    if resp.status_code == 400 and structured:
        # Not every served model takes a JSON schema, and the gateway says so
        # with a 400 rather than by degrading. Remember it per model so the
        # retry is paid once rather than on every keystroke, and fall back to
        # asking for JSON in the prompt — which is what this did before, brace
        # scan and all.
        logger.info("%s rejected response_format; retrying without it", model)
        _structured_unsupported.add(model)
        payload.pop("response_format", None)
        resp = post()
    if resp.status_code != 200:
        raise ModelUnavailable(describe(resp))
    try:
        content_out = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ModelUnavailable(f"unexpected gateway response shape: {exc}") from exc
    if isinstance(content_out, list):
        content_out = "".join(
            block.get("text", "") for block in content_out if isinstance(block, dict)
        )
    return _extract_json(str(content_out)), model


__all__ = ["ModelUnavailable", "reset_discovery_cache", "suggest"]
