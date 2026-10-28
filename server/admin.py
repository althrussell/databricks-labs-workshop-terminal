"""Operator/steering API — gated by ADMIN_GROUP membership (platform_admins).

Control Tower (or scripts/push_content.py, or the in-app operator panel)
drives the live workshop through these endpoints. See docs/admin-api.md
for the contract.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import agents
from . import attendee as attendee_binding
from . import (
    config,
    event_deadline,
    help as help_module,
    model_policy,
    obo,
    readiness,
    spend,
    user_content,
)
from .auth import require_admin
from .capture_journal import CaptureError
from .content import Broadcast, ContentPack, content_service
from .credentials import credential_manager
from .entitlements import entitlement_manager
from .event_emitter import CaptureConflictError
from .events import event_hub
from .omnigent_remote import remote_host_manager
from .sessions import session_manager
from .users import user_manager
from .bootstrap import install

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


@router.get("/setup-status")
def admin_setup_status():
    return install.status()


@router.get("/prewarm-status")
def admin_prewarm_status():
    return install.prewarm_status()


class EntitlementReconcileBody(BaseModel):
    email: str | None = None
    resource_type: str | None = None


@router.post("/entitlements/reconcile")
def reconcile_entitlements(body: EntitlementReconcileBody):
    """Force a verified pass, optionally refreshing one known resource type."""
    try:
        return entitlement_manager.reconcile(
            email=(body.email or "").strip().lower() or None,
            resource_type=body.resource_type,
            source="admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class PhaseBody(BaseModel):
    phase: str


class EventDeadlineBody(BaseModel):
    event_ends_at: int


@router.get("/state")
def admin_state():
    from . import wizard_llm

    pack = content_service.pack
    policy = model_policy.store.snapshot()
    return {
        "phase": content_service.phase,
        "capture_capabilities": {"delivery_schema_version": 2, "closeout_schema_version": 1,
                                 "sealed_closeout": True, "ct_verification_required": True},
        "phases": pack.phases,
        "nugget_count": len(pack.nuggets),
        "broadcast": (
            b.model_dump() if (b := content_service.active_broadcast()) else None
        ),
        "started_at": content_service.started_at,
        # An override lives in memory only, so a restart reverts it. Reporting
        # the effective model here is what stops that revert being invisible to
        # the operator who applied it.
        "wizard_model": wizard_llm.effective_model(),
        "model_policy": {
            "revision": policy.revision,
            "positive_checks": list(policy.allowed_services()),
            "negative_checks": list(policy.denied_models),
        },
        "event_ends_at": event_deadline.store.snapshot(),
    }


@router.put("/event-deadline")
def set_event_deadline(body: EventDeadlineBody):
    """Extend the live event deadline and read back credential durability.

    The store is monotonic and durable. Replaying the same epoch is safe and
    deliberately reruns readiness so Control Tower can retry a lost response.
    """
    try:
        event_ends_at, changed = event_deadline.store.apply(body.event_ends_at)
    except event_deadline.StaleEventDeadline as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_event_deadline",
                "current_event_ends_at": exc.current_epoch,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    report = readiness.evaluate_runtime()
    durability = dict(report.get("durability") or {})
    credential_check = dict(
        (report.get("checks") or {}).get("credential_durability") or {}
    )
    verified = (
        durability.get("event_ends_in") is not None
        and credential_check.get("ok") is True
    )
    from . import telemetry

    telemetry.emit(
        "event_deadline.applied",
        "system",
        {
            "event_ends_at": event_ends_at,
            "changed": changed,
            "verified": verified,
            "credential_state": credential_check.get("state", "unknown"),
            "outcome": "verified" if verified else "durability_failed",
        },
    )
    return {
        "event_ends_at": event_ends_at,
        "applied": True,
        "changed": changed,
        "verified": verified,
        "credential_durability": credential_check,
        "durability": durability,
    }


@router.put("/model-policy")
def set_model_policy(body: model_policy.ModelPolicyRequest):
    """Apply CT's complete WT-SP model pool without restarting an agent."""
    try:
        snapshot, changed = model_policy.store.apply(body)
    except model_policy.StalePolicy as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_model_policy",
                "current_revision": exc.current_revision,
            },
        ) from exc
    except model_policy.RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "model_policy_revision_conflict",
                "current_revision": exc.current_revision,
            },
        ) from exc

    # Drop server-side selection caches first, then rewrite only files that
    # already exist. A running process retains its start-time configuration;
    # the next session sees the new policy immediately.
    from . import cli_config, telemetry, wizard_llm

    wizard_llm.reset_discovery_cache()
    try:
        for user in user_manager.all():
            cli_config.refresh_model_configs(user)
    except Exception as exc:
        logger.warning("model policy config refresh failed", exc_info=True)
        telemetry.emit(
            "model_policy.applied",
            "system",
            {
                "outcome": "refresh_failed",
                "code": type(exc).__name__,
            },
        )
        # The durable policy is already applied. Return a retriable failure so
        # CT sends the same revision again; idempotent replay reruns the refresh.
        raise HTTPException(
            status_code=503,
            detail="model policy applied but new-session config refresh failed",
        ) from exc

    positive = list(snapshot.allowed_services())
    negative = list(snapshot.denied_models)
    telemetry.emit(
        "model_policy.applied",
        "system",
        {
            "outcome": "changed" if changed else "idempotent",
            "code": "model_policy_applied",
        },
    )
    return {
        "revision": snapshot.revision,
        "applied": True,
        "changed": changed,
        "verified": True,
        "positive_checks": positive,
        "negative_checks": negative,
        "processes_restarted": False,
    }


class WizardModelBody(BaseModel):
    model: str = ""


@router.post("/wizard-model")
def set_wizard_model(body: WizardModelBody):
    """Swap the model behind the idea grid, now, without a redeploy.

    The deployed pin is the right shape for a decision made before the room
    exists. It is the wrong shape for the failure it addresses, which is a model
    answering badly with forty people already in front of you — that needs an
    action measured in seconds, not in a redeploy.

    Ephemeral by decision: a restart reverts to the deployed value, so the thing
    written in ``app.yaml`` stays the thing that is always true after a restart.
    ``/api/admin/state`` reports which is in force.
    """
    from . import wizard_llm

    try:
        applied = wizard_llm.set_model_override(body.model)
    except wizard_llm.UnknownModel as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok", **wizard_llm.effective_model(), "applied": applied}


class AgentControlBody(BaseModel):
    enabled: bool
    terminate_active: bool = False
    reason: str = "operator control"


@router.get("/agent-controls")
def agent_controls():
    """Emergency state plus per-attendee launch activity."""
    return {
        **spend.control_state(),
        "attendees": sorted(
            (spend.metering(u) for u in user_manager.all()),
            key=lambda m: m["agent_launches"],
            reverse=True,
        ),
    }


@router.post("/agent-controls")
def set_agent_controls(body: AgentControlBody):
    """Pause/resume launches and optionally terminate the sole live session."""
    from . import telemetry

    changed = spend.set_kill_switch(
        killed=not body.enabled,
        reason=body.reason,
    )
    terminated = False
    if not body.enabled and body.terminate_active:
        active = session_manager.active()
        if active is not None:
            session_manager.terminate(active, reason="operator_disabled_agents")
            terminated = True
    telemetry.emit(
        "agent_control.changed",
        "system",
        {
            "outcome": "enabled" if body.enabled else "disabled",
            "code": "operator_control",
        },
    )
    event_hub.publish({"t": "agents_changed"})
    return {
        **spend.control_state(),
        "changed": changed,
        "terminated_active": terminated,
    }


@router.post("/content-pack")
def set_content_pack(pack: ContentPack):
    content_service.set_pack(pack)
    event_hub.publish({"t": "content_updated"})
    return {"status": "ok", "nuggets": len(pack.nuggets), "phases": pack.phases}


@router.post("/phase")
def set_phase(body: PhaseBody):
    if body.phase not in content_service.pack.phases:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown phase '{body.phase}' — pack defines {content_service.pack.phases}",
        )
    content_service.set_phase(body.phase, explicit=True)
    event_hub.publish({"t": "phase", "phase": body.phase})
    # C6 phase 4: wrap is no longer the only edge-summary trigger — the harvest
    # rolls one continuously — but it is still the best one, because the app is
    # warm and the attendee is still here to have just finished something.
    # ``force`` bypasses the interval floor: an operator flipping to wrap is
    # asking for the current picture, not the one from up to twenty minutes ago.
    # Backgrounded so a model call per attendee can't stall the phase flip, and a
    # no-op unless WORKSHOP_INSIGHT_CAPTURE is on.
    if body.phase == "wrap":
        from . import insight_summary

        insight_summary.summarise_in_background(
            user_manager.all(), phase=body.phase, force=True
        )
    return {"status": "ok", "phase": body.phase}


@router.post("/broadcast")
def broadcast(body: Broadcast):
    """Push a notice to this instance.

    Only banners are retained server-side, because only a banner needs to
    survive a page reload. A toast is a message that has been shown.
    """
    if body.clear:
        content_service.clear_broadcast()
        event_hub.publish({"t": "broadcast", **body.model_dump()})
        return {"status": "ok", "cleared": True}
    if body.surface == "banner":
        content_service.set_broadcast(body)
    event_hub.publish({"t": "broadcast", **body.model_dump()})
    msg_lower = (body.message or "").lower()
    if (
        "minutes left: the workshop ends at " in msg_lower
        and content_service.advance_to_wrap_if_default()
    ):
        event_hub.publish({"t": "phase", "phase": "wrap"})
        from . import insight_summary

        insight_summary.summarise_in_background(
            user_manager.all(), phase="wrap", force=True
        )
    return {"status": "ok"}


class HelpMessageIn(BaseModel):
    message_id: str | None = None
    help_request_id: str | None = None
    sender_role: str = "operator"
    sender: str = ""
    body: str
    created_at: str | None = None
    # Where and how long this shows. Control Tower decides; see its
    # ``notifications.py`` for the shared contract.
    surface: str = "toast"
    durability: str = "sticky"
    ttl_s: int | None = None
    request_ack: bool = True


@router.post("/help/message")
def help_message(body: HelpMessageIn):
    """Control Tower fan-out: deliver one help-thread message to this unit."""
    from . import help as help_module

    try:
        stored = help_module.ingest_operator_message(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok", "message": stored}


class HelpClearIn(BaseModel):
    help_request_id: str | None = None


@router.post("/help/clear")
def help_clear(body: HelpClearIn = HelpClearIn()):
    """Control Tower resolved the request: lower this attendee's hand.

    A first-class action. Control Tower previously achieved this by sending an
    empty broadcast with ``ttl_s=1`` and a ``clear_help`` flag, which meant the
    banner contract carried a side effect that had nothing to do with banners.
    """
    from . import help as help_module

    help_module.clear_hand()
    return {"status": "ok", "help_request_id": body.help_request_id}


class HelpAckIn(BaseModel):
    through_seq: int = Field(default=0, strict=True, ge=0)


@router.post("/help/ack")
def help_ack(body: HelpAckIn):
    """Control Tower has the attendee's messages up to ``through_seq``.

    The counterpart to the ``help_outbox`` on ``GET /api/admin/presence``.
    Until this arrives the terminal keeps offering the same events, because an
    unacknowledged message is indistinguishable from one Control Tower never
    read — and re-offering costs a few bytes a poll, while dropping it costs an
    attendee their answer.
    """
    from . import help as help_module

    try:
        return {"status": "ok", **help_module.ack_outbox(body.through_seq)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid help acknowledgement") from exc
    except CaptureError as exc:
        raise HTTPException(status_code=503, detail="Capture persistence unavailable") from exc


@router.get("/stats")
def harvest_stats(final: bool = False):
    """Harvest endpoint for Control Tower: per-attendee build stats (cached
    code stats) + one instance-level workspace census + instance meta.
    Persisted into CT's Lakebase for durable event-impact reporting.

    ``final=true`` marks Control Tower's pre-delete pass (contract C6). It is the
    last moment anything can be read off this instance, so it doubles as the
    backstop for the edge summary — extraction only, no model call, because
    teardown is best-effort and may run long after the app went cold.

    An ordinary harvest also refreshes the edge summary, in the background. That
    is what makes insight independent of the workshop phase: an operator who never
    flips to wrap — the normal case — still gets briefs that describe the session,
    rather than a run that reaches teardown having captured nothing but counters.
    """
    from . import closeout

    try:
        operation = closeout.manager().status()
    except CaptureError:
        # Preserve the existing useful stats response and explicit capture
        # fault when the retained store itself cannot currently be read.
        return _harvest_open_stats(final)
    if operation["phase"] != "OPEN":
        payload = operation["final_outputs"].get("stats")
        if payload is None:
            raise HTTPException(status_code=503, detail="Closeout final statistics are not ready")
        # Read-only compatibility path: never launch new rolling summaries or
        # revise the sealed snapshot on a legacy CT harvest after close begins.
        from .event_emitter import event_emitter

        payload.setdefault("instance", {})["capture_delivery"] = event_emitter.delivery_status()
        payload["instance"]["closeout"] = {"phase": operation["phase"],
                                            "capture_id": operation["capture_id"],
                                            "manifest_sha256": operation["manifest_sha256"]}
        return payload

    from .closeout_guard import admitted_writer
    from .closeout_guard import CaptureClosedError
    from .event_emitter import event_emitter

    # GET is also a producer here. Reserve for the entire synchronous harvest;
    # the background summary reserves its own detached ticket before dispatch.
    try:
        ticket = admitted_writer(event_emitter.journal, "admin_stats")
    except CaptureClosedError:
        raise
    except CaptureError:
        return _harvest_open_stats(final)
    with ticket:
        payload = _harvest_open_stats(final)
    if final:
        # The pre-delete harvest is the last read; retain everything first.
        event_emitter.journal.flush()
    return payload


def _harvest_open_stats(final: bool):
    from . import stats

    users = user_manager.all()
    payload = stats.gather_all(users)
    try:
        stats.emit_activity(payload)
    except Exception:  # noqa: BLE001 — telemetry never costs the harvest
        logger.debug("agent activity telemetry failed", exc_info=True)
    payload["instance"] = {
        "phase": content_service.phase,
        "started_at": content_service.started_at,
        "session_count": session_manager.count_all(),
        "final": final,
    }
    if any(row.get("capture_observation", {}).get("journal") == "unavailable"
           for row in payload.get("users", [])):
        # Derived counters still need retained checkpoints when content capture
        # is disabled, so a later no-op signal emitter cannot clear this hold.
        payload["instance"]["capture_error"] = "capture_unavailable"
    # C6: push the derived behavioural signal on the same cadence CT polls at, so
    # the signal and the snapshot CT stores alongside it describe the same moment.
    # Retained signal acceptance can be held while the counters are still
    # useful. Keep this snapshot and expose that capture needs recovery.
    try:
        stats.emit_signals(payload)
    except (CaptureError, CaptureConflictError):
        logger.warning("harvest signal capture is held", exc_info=True)
        payload["instance"]["capture_error"] = "capture_unavailable"
    if not final:
        from . import insight_summary

        # Backgrounded: this is Control Tower's fleet-wide poll, and a model call
        # per attendee on the response path would turn a fast harvest into a slow
        # one. The interval and fingerprint gates inside decide whether anything
        # actually runs, so calling this every harvest is cheap when nothing moved.
        #
        # Guarded for the same reason the final pass is: CT stores this response as
        # the durable snapshot, and insight is the optional half of it. A harvest
        # that 500s over a summary bug would cost the counters too, on every poll.
        try:
            started = insight_summary.summarise_in_background(
                users, phase=content_service.phase
            )
        except (CaptureError, CaptureConflictError):
            logger.warning("rolling summary capture is held", exc_info=True)
            started = None
            payload["instance"]["summary_error"] = "capture_unavailable"
            payload["instance"]["capture_error"] = "capture_unavailable"
        except Exception as exc:  # noqa: BLE001
            logger.warning("rolling summary pass failed to start: %s", exc)
            started = None
            payload["instance"]["summary_error"] = str(exc)[:200]
        payload["instance"]["summary_pass_started"] = started is not None
    if final:
        from . import event_emitter as emitter_module
        from . import insight_summary

        # Teardown reads this response for the durable impact record, so a broken
        # summary must cost the summary only. Insight is the optional half of this
        # payload; the stats are the half that has always been promised.
        try:
            # Synchronous: after this response Control Tower deletes the app, so a
            # background thread would be killed mid-flight. Extraction is cheap and
            # local, which is the whole reason the backstop tier is model-free.
            #
            # ``force`` bypasses the interval floor but not the fingerprint gate.
            # A session that ends minutes after its last rolling summary must still
            # get its final state recorded, while an attendee who has not moved
            # since is left alone rather than re-emitted at a higher revision.
            payload["instance"]["summaries_emitted"] = insight_summary.summarise_all(
                users, phase=content_service.phase, allow_llm=False, force=True
            )
            # Keep the optional legacy push attempt; retained events are released
            # only by the explicit authenticated pull acknowledgement.
            payload["instance"]["events_flushed"] = emitter_module.flush_now()
        except (CaptureError, CaptureConflictError):
            logger.warning("final-harvest capture is held", exc_info=True)
            payload["instance"]["summary_error"] = "capture_unavailable"
            payload["instance"]["capture_error"] = "capture_unavailable"
        except Exception as exc:  # noqa: BLE001
            logger.warning("final-harvest summary failed: %s", exc)
            payload["instance"]["summary_error"] = str(exc)[:200]
    # Reported last so it accounts for the signals and summaries emitted above.
    # Pending records still need Control Tower's committed collection and ACK;
    # retained source history also survives an app/container replacement.
    from .event_emitter import event_emitter as buffered

    delivery = buffered.delivery_status()
    payload["instance"]["capture_delivery"] = delivery
    payload["instance"]["events_pending"] = delivery.get("pending")
    payload["instance"]["events_dropped"] = delivery.get("dropped")
    payload["instance"]["event_stream_id"] = delivery.get("stream_id")
    if delivery.get("retention_ready") is False or delivery.get("retention_issues"):
        payload["instance"]["capture_error"] = "capture_unavailable"
    return payload


@router.get("/insight-events")
def collect_insight_events(after: int = 0, stream: str = "", limit: int = 0):
    """Read retained attendee events without acknowledging their prefix.

    This is the delivery path for insight capture. The push alternative — POSTing
    to CT's ingest endpoint with a shared token — cannot work through the
    Databricks Apps proxy, which requires a Databricks identity on every request;
    collection reuses the authenticated call CT already makes to this router.

    ``after`` is a read cursor in the retained source epoch. CT acknowledges only
    through the separate POST endpoint after a committed Lakebase readback.
    """
    from .event_emitter import DEFAULT_COLLECT_LIMIT, event_emitter

    try:
        # CT may commit what it reads; retain it first so a crash can never
        # reissue a delivered sequence with different content.
        event_emitter.journal.flush()
        return event_emitter.collect(
            after=max(0, after),
            stream=stream,
            limit=limit if limit > 0 else DEFAULT_COLLECT_LIMIT,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid capture cursor") from exc
    except CaptureError as exc:
        raise HTTPException(status_code=503, detail="Capture persistence unavailable") from exc


class InsightAckIn(BaseModel):
    schema_version: int = Field(strict=True, ge=2, le=2)
    stream_id: str = Field(strict=True, min_length=1, pattern=r"\S")
    through_seq: int = Field(strict=True, ge=0)


@router.post("/insight-events/ack")
def acknowledge_insight_events(body: InsightAckIn):
    """Release only the retained epoch prefix CT confirms it has committed."""
    from .event_emitter import event_emitter

    try:
        result = event_emitter.acknowledge(**body.model_dump())
        event_emitter.journal.flush()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid capture acknowledgement") from exc
    except CaptureError as exc:
        raise HTTPException(status_code=503, detail="Capture persistence unavailable") from exc


class CloseoutSourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(strict=True, min_length=1)
    unit_id: str = Field(strict=True, min_length=1)
    workspace_id: str = Field(strict=True, min_length=1)
    owner: str = Field(strict=True, min_length=1)


class CloseoutBeginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(strict=True, ge=1, le=1)
    capture_id: str = Field(strict=True, min_length=1, max_length=128,
                            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    stream_id: str = Field(strict=True, min_length=1, max_length=128)
    source: CloseoutSourceIn
    drain_timeout_seconds: int = Field(default=120, strict=True, ge=1, le=600)


def _retained(response):
    """Closeout state CT acts on is retained before it is reported."""
    from .event_emitter import event_emitter

    event_emitter.journal.flush()
    return response


@router.post("/capture-closeout/begin")
def begin_capture_closeout(body: CloseoutBeginIn):
    """Persist the input barrier; repeats resume this exact close generation."""
    from .closeout import CloseoutConflict, manager

    try:
        operation = manager()
        operation.begin(capture_id=body.capture_id, stream_id=body.stream_id,
                        source=body.source.model_dump(), drain_timeout_seconds=body.drain_timeout_seconds)
        return _retained(operation.advance(capture_id=body.capture_id))
    except CloseoutConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/capture-closeout/status")
def capture_closeout_status(capture_id: str = "", advance: bool = True):
    """Read/resume persisted closeout through the existing authenticated app."""
    from .closeout import CloseoutConflict, manager

    try:
        operation = manager()
        return _retained(operation.advance(capture_id=capture_id) if advance else operation.status(capture_id=capture_id))
    except CloseoutConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/capture-closeout/entries")
def capture_closeout_entries(capture_id: str, kind: str, after: int = 0, limit: int = 1000):
    """Replay sealed bytes even after transport ACK; GET never acknowledges."""
    from .closeout import CloseoutConflict, manager

    try:
        return _retained(manager().entries(capture_id=capture_id, kind=kind, after=after, limit=limit))
    except CloseoutConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid sealed capture cursor") from exc


@router.get("/omnigent-host-readiness")
def omnigent_host_readiness():
    """Token-free verified host readiness for Control Tower reconciliation."""
    return remote_host_manager.readiness(attendee_binding.resolved_email())


@router.get("/diagnostics")
def diagnostics(limit: int = 50, timings_only: bool = False):
    """Everything needed to answer "why did that attendee see an error?".

    The rule this serves: an operator must be able to see any error an attendee
    can see, with more detail, without the attendee's browser and without
    shelling into the box. Classified errors come from the collector's journal,
    which survives an app restart; readiness and identity come from live state.
    """
    from . import identity
    from .event_emitter import event_emitter
    from .log_collector import log_collector
    from .runtime_timing import snapshot as runtime_timing_snapshot

    if timings_only:
        # Benchmark readbacks must not execute another deep readiness proof.
        return {"runtime_timings": runtime_timing_snapshot()}

    def _hosts() -> list[dict]:
        # readiness() rejects an email that is not the bound attendee, and the
        # roster can hold an operator who opened the panel.
        bound = attendee_binding.resolved_email()
        return [
            remote_host_manager.readiness(user.email)
            for user in user_manager.all()
            if user.email == bound
        ]

    # Section by section, because the panel is the operator's only window when
    # something is already wrong: a broken section must cost its own contents and
    # nothing else. A 500 here once hid the very traceback that caused it.
    return {
        section: _section(name=section, produce=produce)
        for section, produce in (
            (
                "errors",
                lambda: log_collector.journal.recent(limit=max(1, min(limit, 500))),
            ),
            ("collector", log_collector.status),
            ("readyz", readiness.evaluate_runtime),
            ("identity", identity.all_snapshots),
            ("hosts", _hosts),
            ("capture_delivery", event_emitter.delivery_status),
            ("runtime_timings", runtime_timing_snapshot),
        )
    }


def _section(*, name: str, produce):
    try:
        return produce()
    except Exception as error:  # noqa: BLE001 — see diagnostics()
        logger.warning("diagnostics section %s failed", name, exc_info=True)
        return {"error": f"{type(error).__name__}: {error}"[:400]}


@router.get("/diagnostics/logs")
def diagnostic_logs(attendee: str = "", source: str = "", limit_bytes: int = 64 * 1024):
    """Redacted tails of the Omnigent process logs on this instance.

    Process logs only — never ``auth_tokens.json``, never PTY scrollback. The
    privacy boundary is the same one the collector works inside: how the machine
    failed is operator-visible, what the attendee typed is not.
    """
    from .diagnostics import BoundedLog
    from .log_collector import log_files

    budget = max(1024, min(limit_bytes, 256 * 1024))
    logs = []
    for user in user_manager.all():
        if attendee and user.email != attendee:
            continue
        for path in log_files(user.home):
            if source and path.parent.name != source:
                continue
            logs.append(
                {
                    "attendee": user.email,
                    "source": path.parent.name,
                    "path": str(path),
                    "size": path.stat().st_size if path.exists() else 0,
                    "tail": BoundedLog(path).tail(budget),
                }
            )
    return {"logs": logs, "limit_bytes": budget}


@router.post("/diagnostics/sweep")
def diagnostic_sweep():
    """Collect now rather than waiting for the next tick.

    An operator reading this panel has an attendee waiting; the sweep interval
    is tuned for background cost, not for someone standing at a desk.
    """
    from .log_collector import log_collector

    captured = log_collector.sweep()
    return {"captured": captured, "collector": log_collector.status()}


class OmnigentTierBody(BaseModel):
    enabled: bool


@router.get("/omnigent-tier")
def omnigent_tier():
    """Whether the Omnigent tier is being offered, and to whom it would work."""
    return {
        "enabled": not agents.omnigent_demoted(),
        "remote": config.omnigent_remote_enabled(),
        "attendees": [
            {
                "email": user.email,
                "obo": obo.obo_manager.status(user.email),
                "host": remote_host_manager.status(user.email),
            }
            for user in user_manager.all()
        ],
    }


@router.post("/omnigent-tier")
def set_omnigent_tier(body: OmnigentTierBody):
    """Demote the Omnigent tier, or restore it.

    The lever for the failure this whole plan is about: every Omnigent harness
    shares one credential plane, so when that plane is failing they fail
    together, and no amount of retrying by individual attendees helps. Demoting
    withdraws those cards fleet-wide and leaves bare Claude and Codex —
    which run on the app credential — so a room keeps working instead of
    queueing at the help desk.

    Distinct from the spend kill-switch: that pauses every agent. This one keeps
    the workshop running on the tier that cannot fail this way.
    """
    agents.set_omnigent_demoted(not body.enabled)
    # Tell every open tab immediately. The card state is read from /api/agents,
    # and an attendee staring at a card that no longer works is the situation
    # the lever exists to end.
    event_hub.publish({"t": "agents_changed"})
    return {"enabled": not agents.omnigent_demoted()}


class RecoverBody(BaseModel):
    email: str = ""


@router.post("/recover")
def recover_attendee(body: RecoverBody):
    """Run credential recovery for one attendee, or for everyone.

    The same three actions the server takes on its own when it notices a
    credential failure — re-mirror, wake the host, ask the tab for a fresh token
    — put behind an operator button so a report from the floor can be answered
    in seconds rather than by walking someone through a reload.
    """
    from . import selfheal

    targets = (
        [body.email.strip().lower()]
        if body.email.strip()
        else [user.email for user in user_manager.all()]
    )
    results = [
        {
            "email": email,
            **selfheal.self_healer.recover(email, "operator recovery", force=True),
            "obo": obo.obo_manager.status(email),
        }
        for email in targets
    ]
    return {
        "recovered": [r["email"] for r in results if r.get("credential_fresh")],
        "results": results,
    }


class _AdminExportBody(BaseModel):
    reason: str = Field(default="operator", max_length=64)


@router.post("/export")
def admin_export(body: _AdminExportBody | None = None):
    """Export every attendee's code on this terminal (CT's wrap-up lane, T−15).

    Idempotent within a few minutes: a repeat reuses the export just made. The
    first successful export's volume path and size lead the response.
    """
    from . import journey

    reason = (body.reason if body else "operator") or "operator"
    if reason == "wrap_up" and content_service.advance_to_wrap_if_default():
        event_hub.publish({"t": "phase", "phase": "wrap"})
        from . import insight_summary

        insight_summary.summarise_in_background(
            user_manager.all(), phase="wrap", force=True
        )
    exports, failed = [], []
    for user in user_manager.all():
        if not journey.has_projects(user):
            continue
        try:
            result = journey.export(user, reason=reason, reuse=True)
            exports.append({"email": user.email, "path": result.get("path"), "bytes": result.get("bytes", 0),
                            "files": result.get("files"), "reused": result.get("reused", False)})
        except Exception as error:  # noqa: BLE001 — one attendee's failure is reported, not raised
            failed.append({"email": user.email, "error": str(error)[-300:]})
    lead = exports[0] if exports else {}
    return {"path": lead.get("path"), "bytes": lead.get("bytes", 0), "exports": exports, "failed": failed}


def _journey(user) -> dict | None:
    from . import journey

    try:
        signal = journey.journey(user)
        journey.emit_if_changed(user.email, signal)
        journey.emit_outcome_if_changed(user)
        return signal
    except Exception:  # noqa: BLE001 — an unreadable record never breaks presence
        logger.debug("journey unavailable for %s", user.email, exc_info=True)
        return None


@router.get("/presence")
def presence():
    from . import network_check

    now = time.time()
    users = []
    for user in user_manager.all():
        sessions = session_manager.list_for(user.email)
        users.append(
            {
                "email": user.email,
                "online": (now - user.last_seen) < 60 if user.last_seen else False,
                "last_seen": user.last_seen,
                "first_seen": user.first_seen,
                "cli_ready": bool(user.cli_ready),
                "obo": obo.obo_manager.status(user.email),
                "workspace_sync": user_content.workspace_sync_status(user),
                "sessions": [s.to_dict() for s in sessions],
                "journey": _journey(user),
                "network_check": network_check.get_for(user.email),
            }
        )
    ranked = sorted(users, key=lambda u: u["email"])
    seat = next((u["journey"] for u in ranked if u["journey"] and u["journey"].get("phase")), None)
    return {
        "users": ranked,
        # The seat's coarse signal (W4): needs_decision, ready_to_try, blocked,
        # help_requested or working. Never prompts, keystrokes or reasoning.
        "journey": seat,
        "session_count": session_manager.count_all(),
        "credential": credential_manager.status(),
        "entitlements": entitlement_manager.status(),
        **help_module.presence_fields(),
    }
