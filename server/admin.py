"""Operator/steering API — gated by ADMIN_GROUP membership (platform_admins).

Control Tower (or scripts/push_content.py, or the in-app operator panel)
drives the live workshop through these endpoints. See docs/admin-api.md
for the contract.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import agents
from . import attendee as attendee_binding
from . import config, help as help_module, obo, readiness, spend, user_content
from .auth import require_admin
from .content import Broadcast, ContentPack, content_service
from .credentials import credential_manager
from .entitlements import entitlement_manager
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


class PhaseBody(BaseModel):
    phase: str


@router.get("/state")
def admin_state():
    from . import wizard_llm

    pack = content_service.pack
    return {
        "phase": content_service.phase,
        "phases": pack.phases,
        "nugget_count": len(pack.nuggets),
        "broadcast": (b.model_dump() if (b := content_service.active_broadcast()) else None),
        "started_at": content_service.started_at,
        # An override lives in memory only, so a restart reverts it. Reporting
        # the effective model here is what stops that revert being invisible to
        # the operator who applied it.
        "wizard_model": wizard_llm.effective_model(),
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


@router.get("/agent-controls")
def agent_controls():
    """P1-16: kill-switch state + per-attendee LLM-agent spend metering.

    Lets an operator see who's consuming agent sessions and pause new launches
    fleet-wide if spend runs hot. Bash sessions are free and excluded.
    """
    return {
        "agents_enabled": spend.agents_enabled(),
        "max_agent_launches_per_user": config.max_agent_launches_per_user(),
        "attendees": sorted(
            (spend.metering(u) for u in user_manager.all()),
            key=lambda m: m["agent_launches"],
            reverse=True,
        ),
    }


@router.post("/agent-controls")
def set_agent_controls(body: AgentControlBody):
    """Operator kill-switch: pause (``enabled=false``) or resume new LLM-agent
    launches across the whole instance, effective immediately."""
    spend.set_kill_switch(killed=not body.enabled)
    return {"agents_enabled": spend.agents_enabled()}


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
    content_service.set_phase(body.phase)
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
    through_seq: int = 0


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

    return {"status": "ok", **help_module.ack_outbox(body.through_seq)}


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
    from . import stats

    users = user_manager.all()
    payload = stats.gather_all(users)
    payload["instance"] = {
        "phase": content_service.phase,
        "started_at": content_service.started_at,
        "session_count": session_manager.count_all(),
        "final": final,
    }
    # C6: push the derived behavioural signal on the same cadence CT polls at, so
    # the signal and the snapshot CT stores alongside it describe the same moment.
    # No-op unless WORKSHOP_INSIGHT_CAPTURE is on; buffered, so it can't slow the
    # harvest or fail it.
    stats.emit_signals(payload)
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
            # The periodic flusher runs every 15s and this container has seconds
            # left, so drain here or lose everything still buffered — including the
            # summaries emitted on the line above.
            payload["instance"]["events_flushed"] = emitter_module.flush_now()
        except Exception as exc:  # noqa: BLE001
            logger.warning("final-harvest summary failed: %s", exc)
            payload["instance"]["summary_error"] = str(exc)[:200]
    # Reported last so it accounts for the signals and summaries emitted above.
    # On the ``final=true`` pass this is what Control Tower still owes itself a
    # GET /api/admin/insight-events for: everything left here dies with the app.
    from .event_emitter import event_emitter as buffered

    payload["instance"]["events_pending"] = buffered.pending()
    payload["instance"]["events_dropped"] = buffered.dropped
    payload["instance"]["event_stream_id"] = buffered.stream_id
    return payload


@router.get("/insight-events")
def collect_insight_events(after: int = 0, stream: str = "", limit: int = 0):
    """Hand buffered attendee events to Control Tower (contract C3b, pull).

    This is the delivery path for insight capture. The push alternative — POSTing
    to CT's ingest endpoint with a shared token — cannot work through the
    Databricks Apps proxy, which requires a Databricks identity on every request;
    collection reuses the authenticated call CT already makes to this router.

    ``after`` is the collector's cursor and doubles as an acknowledgement: events
    at or below it are discarded, so a collector that keeps up keeps the buffer
    flat. It is only honoured when ``stream`` matches this process's stream id,
    because sequence numbers restart with the process and a cursor replayed across
    that boundary would discard a fresh buffer unread.
    """
    from .event_emitter import DEFAULT_COLLECT_LIMIT, event_emitter

    return event_emitter.collect(
        after=max(0, after),
        stream=stream,
        limit=limit if limit > 0 else DEFAULT_COLLECT_LIMIT,
    )


@router.get("/omnigent-host-readiness")
def omnigent_host_readiness():
    """Token-free verified host readiness for Control Tower reconciliation."""
    return remote_host_manager.readiness(attendee_binding.resolved_email())


@router.get("/diagnostics")
def diagnostics(limit: int = 50):
    """Everything needed to answer "why did that attendee see an error?".

    The rule this serves: an operator must be able to see any error an attendee
    can see, with more detail, without the attendee's browser and without
    shelling into the box. Classified errors come from the collector's journal,
    which survives an app restart; readiness and identity come from live state.
    """
    from . import identity
    from .log_collector import log_collector

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


@router.get("/presence")
def presence():
    now = time.time()
    users = []
    for user in user_manager.all():
        sessions = session_manager.list_for(user.email)
        users.append({
            "email": user.email,
            "online": (now - user.last_seen) < 60 if user.last_seen else False,
            "last_seen": user.last_seen,
            "first_seen": user.first_seen,
            "cli_ready": bool(user.cli_ready),
            "obo": obo.obo_manager.status(user.email),
            "workspace_sync": user_content.workspace_sync_status(user),
            "sessions": [s.to_dict() for s in sessions],
        })
    return {
        "users": sorted(users, key=lambda u: u["email"]),
        "session_count": session_manager.count_all(),
        "credential": credential_manager.status(),
        "entitlements": entitlement_manager.status(),
        **help_module.presence_fields(),
    }
