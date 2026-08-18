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
from typing import Any

from . import config, content, demo_data, models, wizard

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 4.0
_MAX_TOKENS = 1800
_ALLOWED_SHAPES = frozenset({"dashboard", "app", "pipeline", "ai", "ml"})
_FEW_SHOT = 3
_TABLE_CAP = 24


class ModelUnavailable(RuntimeError):
    """No reachable serving endpoint, or it answered with something unusable."""


def suggest(text: str, industry: str = "") -> dict[str, Any]:
    """Industry + six buildable cards, from the model or the selector.

    ``industry`` is the chip the attendee currently has. A seeded inference
    may replace it; an unseeded one is ignored. Every card that reaches the
    response has passed ``demo_data.verify``.
    """
    kept = demo_data.normalize_industry(industry) if industry else ""
    fallback = _fallback(kept)
    if not config.llm_wizard_enabled():
        return fallback
    try:
        raw, _model = _ask_model(text, kept)
    except ModelUnavailable as exc:
        logger.info("wizard llm falling back to selector: %s", exc)
        return fallback

    inferred = demo_data.normalize_industry(str(raw.get("industry") or ""))
    if inferred and (not demo_data.enabled() or demo_data.has_industry(inferred)):
        resolved = inferred
    else:
        resolved = kept

    verified = _verified_ideas(raw.get("ideas"), resolved)
    padded = _pad(verified, resolved)
    return {
        "industry": resolved,
        "ideas": [i.model_dump() for i in padded],
        "source": "llm" if verified else "selector",
    }


def _fallback(industry: str) -> dict[str, Any]:
    ideas = wizard.select_ideas(industry)
    return {
        "industry": industry,
        "ideas": [i.model_dump() for i in ideas],
        "source": "selector",
    }


def _verified_ideas(raw: Any, industry: str) -> list[content.WizardIdea]:
    if not isinstance(raw, list):
        return []
    out: list[content.WizardIdea] = []
    seen: set[str] = set()
    for item in raw:
        idea = _coerce_idea(item, industry)
        if idea is None or idea.id in seen:
            continue
        seen.add(idea.id)
        out.append(idea)
        if len(out) >= wizard.IDEA_COUNT:
            break
    return out


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
    prompt = str(raw.get("prompt") or "").strip()[:2000]
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
    ideas: list[content.WizardIdea], industry: str
) -> list[content.WizardIdea]:
    if len(ideas) >= wizard.IDEA_COUNT:
        return ideas[: wizard.IDEA_COUNT]
    picked = {i.id for i in ideas}
    labels = {i.label.lower() for i in ideas}
    for extra in wizard.select_ideas(industry, limit=12):
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
    return (
        "You write idea cards for a Databricks workshop opening wizard.\n"
        "Return JSON only, no prose, of the form "
        '{"industry": "slug", "ideas": [ ...6 cards... ]}.\n\n'
        "Rules:\n"
        "- Infer industry as a schema slug from the attendee sentence when you can.\n"
        "- If you cannot tell, keep the chip you were given (empty means none).\n"
        "- Never invent a schema that is not in the inventory below.\n"
        "- Every demo_tables value must be schema.table from that inventory.\n"
        "- All tables on one card share one schema.\n"
        "- Spread shapes across dashboard, app, pipeline, ai, ml. No fun cards.\n"
        "- Prompts must name the tables they will use so the agent can start.\n"
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


def _ready_endpoints(token: str) -> set[str]:
    from .cli_config import _discover_serving_endpoints

    return _discover_serving_endpoints(token)


def _pick_model(token: str) -> str:
    pin = config.workshop_wizard_model()
    if pin:
        return pin
    available = _ready_endpoints(token)
    for name in models.wizard_chain():
        if name in available:
            return name
    raise ModelUnavailable("no wizard endpoint is READY in this workspace")


def _ask_model(text: str, industry: str) -> tuple[dict, str]:
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
                    {"role": "user", "content": _prompt(text, industry)},
                ],
                "max_tokens": _MAX_TOKENS,
                "temperature": 0.3,
            },
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ModelUnavailable(f"serving endpoint unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise ModelUnavailable(f"serving endpoint returned {resp.status_code}")
    try:
        content_out = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ModelUnavailable(f"unexpected serving response shape: {exc}") from exc
    if isinstance(content_out, list):
        content_out = "".join(
            block.get("text", "") for block in content_out if isinstance(block, dict)
        )
    return _extract_json(str(content_out)), model


__all__ = ["ModelUnavailable", "suggest"]
