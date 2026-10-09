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


def read(user: User) -> Profile:
    try:
        raw = attendee_state.read_json(path(user))
        if raw is not None:
            if (not isinstance(raw, dict) or raw.get("schema_version") != 1
                    or raw.get("help_preference") not in PREFERENCES
                    or type(raw.get("revision")) is not int or raw["revision"] < 0):
                raise ValueError("invalid saved profile")
            return Profile(**{key: raw[key] for key in Profile.__dataclass_fields__ if key in raw})
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


def save(user: User, preference: str, *, expected_revision: int | None) -> Profile:
    if preference not in PREFERENCES:
        raise ValueError("Unknown help preference")
    with attendee_state.locked(path(user)):
        current = read(user)
        if expected_revision is not None and current.revision != expected_revision:
            raise ProfileConflict(current)
        updated = Profile(revision=current.revision, help_preference=preference,
                          source="attendee",
                          legacy_persona=current.legacy_persona)
        if updated.to_json() != current.to_json():
            updated.revision += 1
        attendee_state.write_json(path(user), updated.to_json())
        return updated
