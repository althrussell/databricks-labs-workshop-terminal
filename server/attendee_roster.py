"""What the roster knows about a seat's owner: the WT side of the industry handover.

Control Tower classifies each roster entry's industry from its company, domain
and job title, then pushes the result here (``POST /api/admin/attendee-profile``).
The agent uses it to confirm the attendee's industry in the first meeting
question, and ``workshop-workflow show`` prints the same facts.

Rules, each because of how the data is used:

1. **The attendee's own answer always wins.** A push writes only this file. It
   never touches the saved profile, so a choice made with ``workshop-workflow
   prefs --industry`` survives every later push.
2. **Revisions are monotonic.** A push whose revision is not newer than the
   stored one changes nothing, so a replayed or late push is harmless. Control
   Tower bumps the revision on every change, including an operator edit.
3. **Stored per owner.** The file lives in the owner's own home, beside the
   attendee's names, so an instance with one attendee never mixes two owners.

The file is ``<home>/.workshop/roster-profile.json``, and only ever holds the
one record for that home's owner.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import attendee_state, config
from .users import email_slug

logger = logging.getLogger(__name__)

RELATIVE = os.path.join(".workshop", "roster-profile.json")
#: Below this, the suggestion is not offered as a confirmation: the meeting lists
#: every seeded industry instead.
CONFIDENCE_FLOOR = 0.7
SOURCES = ("llm", "operator", "none")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TEXT_CHARS = 200


class RosterProfileIn(BaseModel):
    """The body Control Tower sends. Extra keys are ignored, not an error."""

    model_config = ConfigDict(extra="ignore")

    email: str = Field(min_length=3, max_length=254)
    full_name: str = Field(default="", max_length=_TEXT_CHARS)
    company: str = Field(default="", max_length=_TEXT_CHARS)
    job_title: str = Field(default="", max_length=_TEXT_CHARS)
    suggested_industry: str = Field(default="", max_length=64)
    industry_confidence: float = Field(ge=0.0, le=1.0)
    industry_source: Literal["llm", "operator", "none"]
    revision: int = Field(ge=0, le=2**53)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if "@" not in cleaned or any(ch.isspace() for ch in cleaned):
            raise ValueError("email must be an address")
        return cleaned

    @field_validator("full_name", "company", "job_title")
    @classmethod
    def _text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("suggested_industry")
    @classmethod
    def _industry(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned and not _SLUG.match(cleaned):
            raise ValueError("suggested_industry must be a schema name")
        return cleaned


def path_for(email: str) -> str:
    """The owner's roster file, in the same home the attendee's names live in."""
    return os.path.join(config.users_root(), email_slug(email.strip().lower()), RELATIVE)


def _usable(slug: str) -> bool:
    """Only a schema the deployment offers is ever suggested; anything else would send the agent to a schema that may not exist."""
    if not slug:
        return False
    from . import demo_data

    return slug in demo_data.offered_industries()


def _record(profile: RosterProfileIn, received_at: str) -> dict[str, Any]:
    """The push as received. ``offered`` records whether it was usable when it arrived.

    The industry is kept even when this deployment did not offer it at that
    moment (the catalog can be cold, or past its timeout), so a later push of the
    same revision can correct it. Reads check the offered list again.
    """
    from . import demo_data

    offered = _usable(profile.suggested_industry)
    return {
        "email": profile.email,
        "full_name": profile.full_name,
        "company": profile.company,
        "job_title": profile.job_title,
        "suggested_industry": profile.suggested_industry,
        "offered": offered,
        "industry_label": demo_data.industry_label(profile.suggested_industry) if offered else "",
        "industry_confidence": profile.industry_confidence,
        "industry_source": profile.industry_source,
        "revision": profile.revision,
        "received_at": received_at,
    }


def store(profile: RosterProfileIn) -> tuple[dict[str, Any], bool]:
    """Keep a newer push, or re-evaluate a same-revision push that was not usable.

    A push older than the stored revision changes nothing. A same-revision push
    changes nothing either, unless the stored industry was not offered when it
    arrived and this push's industry is offered now. Returns the record now held
    and whether this push changed it.
    """
    path = path_for(profile.email)
    with attendee_state.locked(path):
        try:
            current = attendee_state.read_json(path)
        except (OSError, ValueError):
            # An unreadable file is ours to replace; the push carries the truth.
            current = None
        if isinstance(current, dict) and current.get("email") == profile.email:
            stored_revision = int(current.get("revision", -1))
            if profile.revision < stored_revision:
                return current, False
            if profile.revision == stored_revision:
                same_industry = current.get("suggested_industry") == profile.suggested_industry
                if current.get("offered") or same_industry and not profile.suggested_industry:
                    return current, False
        now = datetime.now(timezone.utc).isoformat()
        record = _record(profile, now)
        if isinstance(current, dict) and current.get("email") == profile.email and record == {
                **current, "received_at": record["received_at"]}:
            return current, False
        attendee_state.write_json(path, record)
        return record, True


def read(email: str) -> dict[str, Any] | None:
    """The stored record for this owner, or None when nothing was pushed."""
    if not email:
        return None
    try:
        raw = attendee_state.read_json(path_for(email))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("email") != email.strip().lower():
        return None
    return raw


def suggestion(email: str) -> dict[str, Any] | None:
    """The industry to confirm first, when CT is confident enough, else None.

    The offered list is checked now, not at push time: a schema that arrived
    after the push is offered, and one that has gone is not.
    """
    record = read(email)
    slug = str((record or {}).get("suggested_industry") or "")
    if not record or not _usable(slug):
        return None
    if float(record.get("industry_confidence") or 0.0) < CONFIDENCE_FLOOR:
        return None
    from . import demo_data

    return {**record, "offered": True, "industry_label": demo_data.industry_label(slug)}
