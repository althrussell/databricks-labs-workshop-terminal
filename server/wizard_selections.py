"""Server-issued selection receipts preserve the exact offered task on save."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import time
from typing import Any

from . import attendee_state, content, demo_data
from .users import User

TTL_SECONDS = 6 * 60 * 60
MAX_OFFERS = 128


class SelectionError(ValueError):
    """An obsolete, changed, expired or foreign selection needs an explicit retry."""


def _path(user: User) -> str:
    return os.path.join(user.home, ".workshop", "idea-offers.json")


def digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def offer(user: User, payload: dict[str, Any], *, source: str, model: str = "") -> dict[str, Any]:
    if source not in {"catalog", "generated"}:
        raise ValueError("unknown idea source")
    snapshot = copy.deepcopy(payload)
    legacy_id = snapshot["id"]
    content_digest = digest(snapshot)
    snapshot.update({
        "id": f"{source}:{content_digest[:16]}:{legacy_id[:48]}",
        "schema_version": 1,
        "source": source,
        "model": model if source == "generated" else "",
        "content_digest": content_digest,
    })
    snapshot["content_digest"] = digest({key: value for key, value in snapshot.items() if key != "content_digest"})
    token = secrets.token_urlsafe(24)
    now = time.time()
    key = hashlib.sha256(token.encode()).hexdigest()
    path = _path(user)
    with attendee_state.locked(path):
        stored = attendee_state.read_json(path, {})
        offers = {
            name: row for name, row in stored.items()
            if isinstance(row, dict) and row.get("expires_at", 0) > now
        }
        # Keep the most recent offers; a finite store also bounds repeated refreshes.
        offers = dict(sorted(offers.items(), key=lambda pair: pair[1]["expires_at"])[-(MAX_OFFERS - 1):])
        offers[key] = {"attendee": user.email, "expires_at": now + TTL_SECONDS, "snapshot": snapshot}
        attendee_state.write_json(path, offers)
    return snapshot | {"selection_token": token, "selection_expires_at": now + TTL_SECONDS}


def resolve(user: User, token: str, idea_id: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > 160:
        raise SelectionError("This idea needs a fresh selection. Refresh ideas and choose it again.")
    key = hashlib.sha256(token.encode()).hexdigest()
    path = _path(user)
    with attendee_state.locked(path):
        row = attendee_state.read_json(path, {}).get(key)
    if not row or row.get("attendee") != user.email or row.get("expires_at", 0) <= time.time():
        raise SelectionError("This idea selection expired. Refresh ideas and choose again; your words are kept.")
    snapshot = row.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("id") != idea_id:
        raise SelectionError("This selection does not match the offered idea. Choose it again.")
    validate_snapshot(snapshot)
    tables = snapshot.get("demo_tables", [])
    if tables and (not demo_data.data_ready(tables)
                   or set(snapshot.get("required_columns", {})) != set(tables)
                   or not demo_data.supports(snapshot["required_columns"])):
        raise SelectionError("The data available for this idea changed. Refresh ideas before continuing.")
    return copy.deepcopy(snapshot)


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    content.WizardIdea.model_validate(snapshot)
    if (snapshot.get("schema_version") != 1 or snapshot.get("source") not in {"catalog", "generated"}
            or snapshot.get("content_digest") != digest({key: value for key, value in snapshot.items() if key != "content_digest"})):
        raise SelectionError("The saved idea could not be verified. Refresh ideas and choose it again.")
