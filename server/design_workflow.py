"""The project workflow record (W2), shared with the ``workshop-workflow`` helper.

The helper is the single implementation: it has to run inside isolated
worktrees that cannot import this package, so the server loads the same file
rather than keeping a second copy of the rules.
"""
from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import os
from pathlib import Path

_HELPER = os.path.join(os.path.dirname(__file__), "..", "assets", "bin", "workshop-workflow")


def _load():
    loader = SourceFileLoader("workshop_workflow", os.path.normpath(_HELPER))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


workflow = _load()
PHASES = workflow.PHASES
PATHS = workflow.PATHS
RECORD = workflow.RECORD
WorkflowError = workflow.WorkflowError


def read(project: str | Path) -> dict:
    return workflow.load(Path(project))


def check(record: dict, project: str | Path | None = None) -> list[str]:
    return workflow.check(record, Path(project) if project is not None else None)


def verification(record: dict, project: str | Path | None = None) -> dict:
    return workflow.verification(record, Path(project) if project is not None else None)


def summary(record: dict) -> str:
    return workflow.summary(record)


__all__ = ["PATHS", "PHASES", "RECORD", "WorkflowError", "check", "read", "summary", "verification", "workflow"]
