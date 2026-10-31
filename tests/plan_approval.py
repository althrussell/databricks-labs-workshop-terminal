"""Test helper: the attendee has said yes to a build's plan (sweep contract §2).

Writes the same record ``workshop-workflow plan --approved`` writes, so a test
that scaffolds a build starts from the state a real attendee reaches.
"""

from __future__ import annotations

import json
from pathlib import Path


def approve_build(home: str | Path, name: str, source: str = "attendee") -> None:
    path = Path(home) / ".config" / "workshop" / "build-plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1,
        "build": name,
        "plan_approved_at": "2026-10-31T00:00:00Z",
        "plan_approved_source": source,
    }))


def approve_for_env(env: dict, name: str) -> None:
    """Approve ``name`` in the home a helper subprocess will run under."""
    approve_build(env["HOME"], name)
