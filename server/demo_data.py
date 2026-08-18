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
"""

from __future__ import annotations

import logging
import threading
import time

from . import config, credentials

logger = logging.getLogger(__name__)

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


def verify(tables: list[str]) -> bool:
    """Whether every ``schema.table`` in ``tables`` exists in the demo catalog.

    All or nothing on purpose. An idea card that names four tables needs four
    tables; showing it because three of them exist just moves the failure from
    the wizard into the terminal, where it costs the attendee their time instead
    of ours.
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
