"""Attach existing WT producers to the attendee's existing Workspace folder.

This is part of the app lifecycle, with no extra service or credential. The
local container directory is a cache; accepted capture lives in Workspace Files
until CT has committed and acknowledged it. The journal remains scoped to this
exact CT run/unit/workspace/owner when a pooled login is reused.
"""

from __future__ import annotations

import hashlib
import logging

from . import config
from .runtime_timing import stage as timing_stage
from .capture_journal import CaptureHeldError, CaptureJournal, WorkspaceCaptureStorage, canonical_json

logger = logging.getLogger(__name__)


def source_binding() -> dict[str, str]:
    owner = config.workshop_attendee_email()
    binding = {"run_id": config.workshop_run_id(), "unit_id": config.workshop_unit_id(),
               "workspace_id": config.workspace_id(), "owner": owner}
    if not all(binding.values()):
        raise CaptureHeldError("capture requires a configured CT source and owner")
    if not config.valid_attendee_email(owner) or any(c in owner for c in ("/", "\\", "\x00")):
        raise CaptureHeldError("capture owner cannot name a Workspace folder")
    return binding


def workspace_path(binding: dict[str, str]) -> str:
    digest = hashlib.sha256(canonical_json(binding)).hexdigest()
    return f"/Workspace/Users/{binding['owner']}/projects/.workshop-capture/{digest}"


def configure(emitter, *, client=None) -> None:
    """Install the fail-closed guard before attaching or starting producers.

    Workspace folder grants can converge just after app deployment. A failed
    startup stays visible and retries on the existing collection/request path;
    it never falls back to accepting production capture in memory.
    """
    from . import attendee_names, attendee_state, discovery, help as help_module, identity, insight_summary, model_policy, stats
    from .credentials import workspace_client
    from .users import user_manager

    help_module.require_capture_ready(emitter._ensure_ready)
    user_manager.require_capture_ready(emitter._ensure_ready)
    journal = None
    legacy_loaded = False

    @timing_stage("capture.initialize")
    def initialize():
        nonlocal journal, legacy_loaded
        binding = source_binding()
        sdk = client if client is not None else workspace_client()
        if sdk is None:
            raise CaptureHeldError("capture app identity is unavailable")
        if journal is None:
            journal = CaptureJournal(WorkspaceCaptureStorage(sdk, workspace_path(binding)), binding, {})
        emitter.configure_journal(journal)
        model_policy.store.configure_capture(journal)
        help_module.configure_capture(journal)  # Help is independent of content consent.
        user_manager.configure_capture(journal, binding["owner"])
        attendee_state.configure_capture(journal)
        stats.configure_capture(journal)
        identity.configure_capture(emitter)
        if config.insight_capture_enabled():
            if not legacy_loaded:
                discovery.discovery_store.configure_journal(discovery.journal_path())
                legacy_loaded = True
            discovery.discovery_store.configure_capture(emitter)
            insight_summary.stamps.configure_capture(journal)
            attendee_names.configure_capture(emitter)

    emitter.defer_initialization(initialize, "capture startup is pending")
    try:
        emitter._ensure_ready()
    except Exception as exc:
        logger.warning("capture initialization held (%s); requests will retry", type(exc).__name__)
