"""Optional collaboration preferences, independent of a product's saved goal."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from . import attendee_state
from .users import User

PREFERENCES = ("", "guided", "concise", "technical")


@dataclass
class Profile:
    schema_version: int = 1
    revision: int = 0
    help_preference: str = ""
    source: str = "default"
    legacy_persona: str = ""

    def to_json(self) -> dict:
        return asdict(self)


class ProfileConflict(ValueError):
    def __init__(self, profile: Profile):
        super().__init__("Your help preference changed in another tab. Reload it before saving.")
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
            or raw.get("legacy_persona", "") not in {"", "technical", "business"}):
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
        raise ProfileReadError("Your help preference could not load. Retry before changing it.") from exc


def read(user: User) -> Profile:
    return from_json(attendee_state.read_document(user, "profile", default=Profile().to_json(),
        local_read=lambda: read_local(user).to_json(), path=path(user)))


def save(user: User, preference: str, *, expected_revision: int | None) -> Profile:
    if preference not in PREFERENCES:
        raise ValueError("Unknown help preference")
    def update(raw):
        current = from_json(raw)
        if expected_revision is not None and current.revision != expected_revision:
            raise ProfileConflict(current)
        updated = Profile(revision=current.revision, help_preference=preference,
                          source="attendee",
                          legacy_persona=current.legacy_persona)
        if updated.to_json() != current.to_json():
            updated.revision += 1
        return updated.to_json()

    return from_json(attendee_state.update_document(user, "profile", default=Profile().to_json(),
        local_read=lambda: read_local(user).to_json(), path=path(user), update=update))
