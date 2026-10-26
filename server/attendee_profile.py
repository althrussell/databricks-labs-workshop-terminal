"""Optional collaboration preferences, independent of a product's saved goal.

Four independent dimensions (W2), each optional and changeable at any time:

- **working approach** with the coding agent: ``co_build`` (build with me),
  ``learn`` (teach me while we build) or ``lead`` (let me drive). Unset
  defaults gently to building together;
- **explanation detail**: ``adaptive``, ``brief`` or ``detailed``;
- **engagement path** default: ``rapid`` or ``guided``. A project's own
  recorded path (``.workshop/design-workflow.json``) takes precedence;
- **background**, the starting point they told us: ``domain`` (business or
  domain side), ``data`` (data and SQL), ``developer`` or ``exploring``. It is
  only ever what they said, never inferred from role, jargon or message length.

They are additive fields on schema version 1, so an older release reading a
newer profile keeps working. ``help_preference`` is the legacy single choice;
it is migrated conservatively on read and never proves a working approach or
consent to a guided interview. ``choice_sources`` records who set each field.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

from . import attendee_state
from .users import User

PREFERENCES = ("", "guided", "concise", "technical")
APPROACHES = ("", "co_build", "learn", "lead")
DETAILS = ("", "adaptive", "brief", "detailed")
ENGAGEMENT_PATHS = ("", "rapid", "guided")
BACKGROUNDS = ("", "domain", "data", "developer", "exploring")
CHOICE_SOURCES = ("ui", "chat", "wizard", "migrated")
FIELDS = {"working_approach": APPROACHES, "explanation_detail": DETAILS, "engagement_path": ENGAGEMENT_PATHS,
          "background": BACKGROUNDS}


@dataclass
class Profile:
    schema_version: int = 1
    revision: int = 0
    help_preference: str = ""
    source: str = "default"
    legacy_persona: str = ""
    working_approach: str = ""
    explanation_detail: str = ""
    engagement_path: str = ""
    background: str = ""
    choice_sources: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)

    def effective(self) -> dict:
        """What the instructions should say, with legacy values migrated.

        ``concise`` keeps brevity. ``technical`` keeps technical discussion as
        vocabulary, not a detail level. ``guided`` keeps plain-language help;
        none of them sets a working approach or a guided product interview.
        """
        detail = self.explanation_detail
        sources = dict(self.choice_sources)
        if not detail and self.help_preference == "concise":
            detail, sources["explanation_detail"] = "brief", "migrated"
        return {
            "working_approach": self.working_approach or "co_build",
            "working_approach_stated": bool(self.working_approach),
            "explanation_detail": detail or "adaptive",
            "engagement_path": self.engagement_path,
            "background": self.background,
            "background_stated": bool(self.background),
            "technical_vocabulary": self.help_preference == "technical",
            "plain_language": self.help_preference == "guided",
            "sources": sources,
        }


class ProfileConflict(ValueError):
    def __init__(self, profile: Profile):
        super().__init__("Your preferences changed in another tab. Reload them before saving.")
        self.profile = profile


class ProfileReadError(RuntimeError):
    pass


def path(user: User) -> str:
    return os.path.join(user.home, ".workshop", "profile.json")


def from_json(raw: dict) -> Profile:
    if (not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1
            or raw.get("help_preference") not in PREFERENCES
            or type(raw.get("revision")) is not int or raw["revision"] < 0
            or raw.get("source", "default") not in {"default", "legacy_persona", "attendee"}
            or raw.get("legacy_persona", "") not in {"", "technical", "business"}
            or any(raw.get(name, "") not in allowed for name, allowed in FIELDS.items())
            or not isinstance(raw.get("choice_sources", {}), dict)
            or any(value not in CHOICE_SOURCES for value in raw.get("choice_sources", {}).values())):
        raise ValueError("invalid saved profile")
    return Profile(**{key: raw[key] for key in Profile.__dataclass_fields__ if key in raw})


def read_local(user: User) -> Profile:
    try:
        raw = attendee_state.read_json(path(user))
        if raw is not None:
            return from_json(raw)
        try:
            with open(os.path.join(user.home, ".workshop", "persona"), encoding="utf-8") as handle:
                legacy = handle.read().strip()
        except FileNotFoundError:
            legacy = ""
        # Business was seeded automatically. It cannot establish a choice or
        # expertise. Preserve an explicit technical style as a legacy preference.
        return Profile(help_preference="technical" if legacy == "technical" else "",
                       source="legacy_persona" if legacy == "technical" else "default",
                       legacy_persona=legacy if legacy in {"technical", "business"} else "")
    except (OSError, ValueError, TypeError) as exc:
        raise ProfileReadError("Your preferences could not load. Retry before changing them.") from exc


def read(user: User) -> Profile:
    return from_json(attendee_state.read_document(user, "profile", default=Profile().to_json(),
        local_read=lambda: read_local(user).to_json(), path=path(user)))


def save(user: User, preference: str | None = None, *, expected_revision: int | None,
         changes: dict[str, str] | None = None, choice_source: str = "ui") -> Profile:
    """Apply ``changes`` (and/or the legacy ``preference``), revision-checked.

    Only the named fields change; the goal, the brief and every other
    preference are left alone.
    """
    changes = dict(changes or {})
    if preference is not None and preference not in PREFERENCES:
        raise ValueError("Unknown help preference")
    for name, value in changes.items():
        if name not in FIELDS or value not in FIELDS[name]:
            raise ValueError(f"Unknown {name.replace('_', ' ')}")
    if choice_source not in CHOICE_SOURCES:
        raise ValueError("Unknown preference source")

    def update(raw):
        current = from_json(raw)
        if expected_revision is not None and current.revision != expected_revision:
            raise ProfileConflict(current)
        updated = from_json(current.to_json())
        if preference is not None:
            updated.help_preference, updated.source = preference, "attendee"
        for name, value in changes.items():
            setattr(updated, name, value)
            if value:
                updated.choice_sources = {**updated.choice_sources, name: choice_source}
            else:
                updated.choice_sources = {k: v for k, v in updated.choice_sources.items() if k != name}
        if updated.to_json() != current.to_json():
            updated.revision += 1
        return updated.to_json()

    return from_json(attendee_state.update_document(user, "profile", default=Profile().to_json(),
        local_read=lambda: read_local(user).to_json(), path=path(user), update=update))
