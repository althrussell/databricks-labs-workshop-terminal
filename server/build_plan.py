"""Per-build plan approval: the attendee's yes before a build is scaffolded.

The record lives in the attendee's home, not in the project, because the gate
has to answer before the project exists (``workshop-init-project`` is what
creates it). ``assets/bin/workshop-workflow`` reads and writes the same file
with the same schema; keep the two in step.

    ~/.config/workshop/build-plan.json
    {"version": 1, "build": "<name>",
     "plan_put_at": "...", "plan_summary": "...",
     "plan_approved_at": "...", "plan_approved_source": "attendee|claude-plan-mode"}

A new build (WT's New build, or a new project) rewrites the file for that build
with no plan and no approval, so nothing carries over from the build before.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

#: The Claude Code PostToolUse hook (matcher ``ExitPlanMode``) runs this exact
#: command. It records the attendee's approval when Claude leaves plan mode.
PLAN_APPROVAL_COMMAND = "workshop-workflow plan --approved --source claude-plan-mode"

#: The refusal every scaffolding command gives before approval (sweep contract §2).
PLAN_GUIDANCE = (
    "Put the plan to the attendee and wait for their yes; then run "
    "`workshop-workflow plan --approved`."
)

SCHEMA_VERSION = 1
PLAN_SOURCES = ("attendee", "claude-plan-mode")


def state_path(home: str | os.PathLike) -> Path:
    return Path(home) / ".config" / "workshop" / "build-plan.json"


def read(home: str | os.PathLike) -> dict:
    """The stored plan record, or ``{}`` when there is none or it cannot be read."""
    try:
        value = json.loads(state_path(home).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get("version") != SCHEMA_VERSION:
        return {}
    return value


def _write(home: str | os.PathLike, value: dict) -> None:
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".build-plan-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reset(home: str | os.PathLike, build: str) -> None:
    """Start ``build`` with no plan and no approval (a new build)."""
    _write(home, {"version": SCHEMA_VERSION, "build": build})


def is_scaffolded(project_dir: str | os.PathLike) -> bool:
    """A build whose scaffold already ran has passed the gate (or predates it).

    ``preparation.json`` is local status and is not synced; ``project.json`` is
    committed by the helper, so a build recovered from its Workspace copy keeps
    the marker after an Apps restart wipes the attendee's home approval file.
    """
    workshop = Path(project_dir) / ".workshop"
    return (workshop / "preparation.json").is_file() or (workshop / "project.json").is_file()


def is_approved(home: str | os.PathLike, build: str, project_dir: str | os.PathLike | None = None) -> bool:
    """Whether ``build`` may be scaffolded or launched in its approved mode.

    Approved means a recorded attendee (or Claude plan-mode) approval for this
    build, or a build already scaffolded: a folder that exists was past the gate,
    and re-running the helper on it must not refuse.
    """
    record = read(home)
    if record.get("build") == build and record.get("plan_approved_at"):
        return True
    return project_dir is not None and is_scaffolded(project_dir)


def approved_for_active(home: str | os.PathLike, active: str, projects_root: str | os.PathLike) -> bool:
    """The launch decision: no active build means nothing has been approved yet."""
    if not active:
        return False
    return is_approved(home, active, Path(projects_root) / active)


def put(home: str | os.PathLike, build: str, summary: str, when: str) -> dict:
    record = read(home)
    if record.get("build") != build:
        record = {"version": SCHEMA_VERSION, "build": build}
    record.update(plan_put_at=when, plan_summary=summary)
    _write(home, record)
    return record


def approve(home: str | os.PathLike, build: str, source: str, when: str) -> dict:
    if source not in PLAN_SOURCES:
        raise ValueError(f"source must be one of {', '.join(PLAN_SOURCES)}")
    record = read(home)
    if record.get("build") != build:
        record = {"version": SCHEMA_VERSION, "build": build}
    record.update(plan_approved_at=when, plan_approved_source=source)
    _write(home, record)
    return record
