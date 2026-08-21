"""What demo data actually exists, and how to describe it to an agent.

Control Tower seeds a shared read-only catalog with one schema per industry
(``workshop_demo.automotive_mobility``, ``workshop_demo.retail``, ...) so that an
attendee who asks for a warranty dashboard gets a warranty dashboard rather than
twenty minutes of an agent inventing fixtures.

Everything here is built around one rule: **never claim data we have not seen.**
The catalog is seeded by a human running a notebook, per metastore, and a region
that was missed looks exactly like a region that was done — right up until an
attendee clicks an idea card and the agent finds nothing. So this module lists
what is really there and every caller filters against it. An unseeded deployment
then degrades to the behaviour Workshop Terminal had before demo data existed,
which is a missing feature rather than a broken promise.

Reads are cached. The catalog changes when an operator reseeds between events,
never during one, so a long TTL costs nothing and a per-request round trip to
Unity Catalog on the wizard's critical path would cost a great deal.

One thing here is deliberately *not* built on that rule: ``KNOWN_INDUSTRIES``.
The industries a room can be told it is in are a property of the seed notebook,
not of this deployment's luck with it, and gating the picker on live inventory
meant an unset catalog removed the question entirely — the attendee was not told
demo data was missing, they were told their industry did not exist. The list is
shipped, always offered, and callers badge what is really seeded on top of it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from . import config, credentials

logger = logging.getLogger(__name__)

# Anything that is not a letter or digit separates words in an industry name,
# so "Financial Services", "financial-services" and "financial services!" all
# land on the same slug.
_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")

_SEED_MANIFEST_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "content", "demo_seed_manifest.json")
)

# Long, because the underlying data is only allowed to change between events.
_TTL_SECONDS = 900
# Short, because an unreachable catalog is usually a transient permission or
# warehouse blip, and being wrong in the pessimistic direction hides real data.
_FAILURE_TTL_SECONDS = 60

# An automotive schema has ~20 tables. Listing all of them in the agent's
# instructions crowds out everything else in the file and buys nothing: the agent
# only needs enough to know what is here and that DESCRIBE will tell it the rest.
_MANIFEST_TABLE_CAP = 14

_lock = threading.Lock()
_cache: dict[str, set[str]] | None = None
_cache_at = 0.0
_cache_ok = False


def _load_seed_manifest() -> tuple[dict[str, frozenset[str]], dict[str, str]]:
    """The shipped contract with Control Tower's seed notebook.

    Read once at import. A missing or malformed file is loud but not fatal:
    the wizard falls back to whatever is actually seeded, which is the old
    behaviour, and a deployment that lost the asset should not fail to boot
    over a list of industry names.
    """
    try:
        with open(_SEED_MANIFEST_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        schemas = {
            str(name): frozenset(str(t) for t in tables or ())
            for name, tables in (raw.get("schemas") or {}).items()
        }
        labels = {str(k): str(v) for k, v in (raw.get("labels") or {}).items()}
        return schemas, labels
    except Exception as exc:  # noqa: BLE001 — an absent asset must not stop boot
        logger.error(
            "demo seed manifest unreadable at %s: %s — the wizard will offer only "
            "industries this deployment has actually seeded",
            _SEED_MANIFEST_PATH,
            exc,
        )
        return {}, {}


SEED_MANIFEST, _SEED_LABELS = _load_seed_manifest()

# Every industry the seed notebook creates, whether or not this deployment got
# it. The wizard offers all of these; ``industries()`` says which are real.
KNOWN_INDUSTRIES: tuple[str, ...] = tuple(sorted(SEED_MANIFEST))


def industry_label(industry: str) -> str:
    """Human name for a schema slug, falling back to a readable form of it."""
    slug = (industry or "").strip()
    if not slug:
        return ""
    return _SEED_LABELS.get(slug) or slug.replace("_", " ").title()


def catalog() -> str:
    return config.workshop_demo_catalog().strip()


def enabled() -> bool:
    """Whether this deployment has demo data configured at all.

    Only says a name was set. Whether anything is behind that name is a separate
    question, answered by ``inventory``.
    """
    return bool(catalog())


def _load() -> dict[str, set[str]]:
    """schema -> table names, straight from Unity Catalog. Raises on failure."""
    client = credentials.workspace_client()
    if client is None:
        # Local dev, or before app identity is initialized. Not an error.
        raise RuntimeError("no workspace client")

    cat = catalog()
    found: dict[str, set[str]] = {}
    # One call for the whole catalog rather than one per schema. Summaries carry
    # the full name, which is all the filtering and manifests need; anything
    # wanting column detail should DESCRIBE the table at the point of use.
    for summary in client.tables.list_summaries(catalog_name=cat, schema_name_pattern="*"):
        full = getattr(summary, "full_name", None)
        if not full:
            continue
        parts = full.split(".")
        if len(parts) != 3:
            continue
        _, schema, table = parts
        if schema.startswith("_"):
            # _meta holds the seed manifest: provenance for operators, not
            # something an attendee should be pointed at.
            continue
        found.setdefault(schema, set()).add(table)
    return found


def inventory(*, refresh: bool = False) -> dict[str, set[str]]:
    """schema -> table names for the demo catalog. Empty when unavailable.

    Never raises. Every caller is on a page-render path, and a wizard that fails
    to open because Unity Catalog was briefly slow is a worse outcome than a
    wizard that shows generic ideas.
    """
    global _cache, _cache_at, _cache_ok

    if not enabled():
        return {}

    with _lock:
        age = time.time() - _cache_at
        ttl = _TTL_SECONDS if _cache_ok else _FAILURE_TTL_SECONDS
        if _cache is not None and not refresh and age < ttl:
            return _cache

    try:
        found = _load()
        ok = True
    except Exception as e:
        # Debug, not warning: in local dev there is no workspace client and this
        # is the expected path every time, so anything louder trains people to
        # ignore it.
        logger.debug("demo data catalog %s unavailable: %s", catalog(), e)
        found, ok = {}, False

    with _lock:
        _cache, _cache_at, _cache_ok = found, time.time(), ok
    if ok:
        logger.info(
            "demo data catalog %s: %d schemas, %d tables",
            catalog(),
            len(found),
            sum(len(v) for v in found.values()),
        )
    return found


def industries() -> list[str]:
    """Industries with data actually seeded, alphabetically."""
    return sorted(inventory())


def offered_industries() -> list[str]:
    """Every industry worth offering, seeded or not.

    The shipped list first, then anything this deployment has seeded that the
    manifest does not know about — a notebook that added a schema ahead of this
    release should not have it hidden by a stale asset.
    """
    extra = sorted(set(inventory()) - set(KNOWN_INDUSTRIES))
    return list(KNOWN_INDUSTRIES) + extra


def has_industry(industry: str) -> bool:
    """Whether this name resolves to a schema that is actually seeded."""
    if not industry:
        return False
    resolved = normalize_industry(industry)
    return bool(resolved) and resolved in inventory()


def normalize_industry(value: str) -> str:
    """Map a human or slug industry name onto a seeded schema, or empty.

    Discovery records and overlay text have historically mixed ``financial
    services`` with ``financial_services``. The catalog key is the schema name;
    anything that does not resolve to one is treated as unset so we never
    advertise a schema that is not there.
    """
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    slug = raw.replace(" ", "_").replace("-", "_")
    if not enabled():
        # No catalog in this deployment: keep the slug so a typed industry still
        # reaches discovery, but do not invent a schema we have not seen.
        return slug
    inv = inventory()
    if slug in inv:
        return slug
    compact = slug.replace("_", "")
    for schema in inv:
        if schema.replace("_", "") == compact:
            return schema
    return ""


def industry_slug(value: str) -> str:
    """A stable slug for any industry, seeded or not. Empty only for empty input.

    ``normalize_industry`` answers "which seeded schema is this?", and correctly
    answers nothing when there is none. That is the wrong question for the brief,
    the discovery record and the agent overlay, all of which want to carry what
    the attendee actually said: an industry no notebook seeded is still the
    industry they work in, and dropping it lost the single most useful field on
    the record for every attendee who typed their own.

    Prefers a seeded schema, then a known one, then the slug itself.
    """
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    slug = _SLUG_SEPARATORS.sub("_", raw).strip("_")
    if not slug:
        return ""
    seeded = normalize_industry(slug)
    if seeded:
        return seeded
    compact = slug.replace("_", "")
    for known in KNOWN_INDUSTRIES:
        if known.replace("_", "") == compact:
            return known
    return slug


def readable() -> bool:
    """Whether we have actually read this catalog, as opposed to failed to.

    ``enabled`` says a name was configured. This says the read against that name
    succeeded. The gap between them is a permission error, a cold warehouse or a
    Unity Catalog blip — moments where the honest answer is "we do not know what
    is here", which is not the same answer as "nothing is here" and must not be
    treated as one.

    Answered from the read's own outcome rather than from whether it returned
    anything, because a catalog that exists and is empty is a successful read.
    Conflating the two put ``_buildable`` on the unreadable branch — where it
    stops filtering, on the grounds that it cannot check — for a catalog it had
    checked perfectly well and found nothing in, so cards naming tables that
    demonstrably do not exist were offered without the ``data_ready`` badge that
    would have warned anyone.
    """
    if not enabled():
        return False
    inventory()  # populates the cache, and with it the flag below
    with _lock:
        return _cache_ok


def verify(tables: list[str]) -> bool:
    """Whether every ``schema.table`` in ``tables`` exists in the demo catalog.

    All or nothing on purpose. An idea card that names four tables needs four
    tables; showing it because three of them exist just moves the failure from
    the wizard into the terminal, where it costs the attendee their time instead
    of ours.

    An unreadable catalog answers False for everything. Callers deciding whether
    to *show* a card must check :func:`readable` first — see ``wizard._buildable``.
    """
    if not tables:
        return True  # needs no demo data, so nothing can be missing
    inv = inventory()
    if not inv:
        return False
    for ref in tables:
        schema, _, table = ref.partition(".")
        if not table or table not in inv.get(schema, ()):
            return False
    return True


def data_ready(tables: list[str]) -> bool:
    """Whether this card's demo tables are known to exist, for the badge.

    Deliberately pessimistic where ``verify`` is permissive: a card naming no
    tables gets no badge, because "Data ready" on a card that uses no data is a
    claim about nothing. And an unreadable catalog gets no badge either — the
    badge is a promise, and we cannot keep one we cannot check.
    """
    return bool(tables) and verify(tables)


def _rank(table: str) -> tuple[int, str]:
    """Sort key putting the tables worth mentioning first.

    The 360s are the point of the automotive schema: they are the joined,
    ready-to-query views, and an agent that finds ``vehicle360`` will not
    hand-assemble the same thing out of six base tables. Everything else is
    alphabetical.
    """
    if table.endswith("360"):
        return (0, table)
    if table.startswith("gold_"):
        return (1, table)
    return (2, table)


def manifest(industry: str = "") -> str:
    """Markdown describing available demo data, for the agent's instructions.

    With an industry, that schema in full (capped). Without one — the attendee
    skipped the wizard, or named an industry nobody seeded — a one-line-per-schema
    summary instead, so the agent still knows the catalog exists and can look for
    itself rather than assuming there is nothing here.

    Returns an empty string when there is nothing to describe, which callers
    treat as "say nothing about demo data at all".
    """
    inv = inventory()
    if not inv:
        return ""
    cat = catalog()

    def _tables_for(schema: str) -> list[str]:
        return sorted(inv[schema], key=_rank)

    industry = normalize_industry(industry)
    if industry and industry in inv:
        tables = _tables_for(industry)
        shown, hidden = tables[:_MANIFEST_TABLE_CAP], len(tables) - _MANIFEST_TABLE_CAP
        lines = [f"Tables in `{cat}.{industry}`:", ""]
        lines += [f"- `{cat}.{industry}.{t}`" for t in shown]
        if hidden > 0:
            lines.append(
                f"- ...and {hidden} more — `SHOW TABLES IN {cat}.{industry}` for the rest."
            )
        lines += [
            "",
            f"Run `DESCRIBE TABLE EXTENDED` on any of these before using it: every "
            f"table and column carries a comment explaining what it holds.",
        ]
        return "\n".join(lines)

    lines = [f"Schemas in `{cat}` (one per industry):", ""]
    for schema in sorted(inv):
        tables = _tables_for(schema)
        preview = ", ".join(f"`{t}`" for t in tables[:4])
        more = f", +{len(tables) - 4} more" if len(tables) > 4 else ""
        lines.append(f"- `{schema}` — {preview}{more}")
    lines += [
        "",
        f"`SHOW TABLES IN {cat}.<schema>` to see a schema in full.",
    ]
    return "\n".join(lines)


def reset_cache() -> None:
    """Drop the cache. For tests and the admin reload path."""
    global _cache, _cache_at, _cache_ok
    with _lock:
        _cache, _cache_at, _cache_ok = None, 0.0, False
