"""Fresh, model-free final outputs using the existing capture payload policy.

The prepared payloads are retained before emission. A process replacement or a
lost reply reuses those exact observations, rather than changing timestamps or
turning one logical closeout into another summary revision.
"""

from __future__ import annotations

import copy
import hashlib
import os

from . import artifacts, config, insight_summary, stats
from .capture_journal import CaptureHeldError, canonical_json
from .content import content_service
from .users import user_manager

_RESOURCE_KINDS = {"jobs", "pipelines", "apps", "dashboards"}


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _checkpoint(state: dict, capture_id: str) -> dict | None:
    checkpoints = state.setdefault("closeout_outputs", {})
    if not isinstance(checkpoints, dict):
        raise CaptureHeldError("retained final outputs are invalid")
    saved = checkpoints.get(capture_id)
    if saved is None:
        return None
    if (not isinstance(saved, dict)
            or set(saved) != {"schema_version", "prepared", "prepared_sha256", "accepted"}
            or type(saved["schema_version"]) is not int or saved["schema_version"] != 1
            or not isinstance(saved["prepared"], dict)
            or set(saved["prepared"]) != {"stats", "summaries", "emissions"}
            or not isinstance(saved["prepared"]["stats"], dict)
            or not isinstance(saved["prepared"]["summaries"], dict)
            or not isinstance(saved["prepared"]["emissions"], list)
            or saved["prepared_sha256"] != _digest(saved["prepared"])
            or not isinstance(saved["accepted"], dict)):
        raise CaptureHeldError("retained final outputs are invalid")
    for key, sequence in saved["accepted"].items():
        if not isinstance(key, str) or type(sequence) is not int or sequence < 1:
            raise CaptureHeldError("retained final output receipts are invalid")
    return saved


def _prepare(emitter, capture_id: str) -> tuple[dict, list[str]]:
    # This runtime's durable source is one assigned attendee. Operator visits
    # may register extra live users, but cannot widen its final capture scope.
    owner = user_manager.peek(emitter.journal.binding["owner"])
    if owner is None:
        return {"stats": {"schema_version": stats.STATS_SCHEMA_VERSION, "users": [],
                           "resources": {}, "resource_observation": {},
                           "instance": {"phase": content_service.phase, "final": True,
                                        "final_capture_id": capture_id}},
                "summaries": {}, "emissions": []}, ["final_expected_owner_missing"]
    users = [owner]
    payload = stats.gather_all(users, fresh=True)
    payload["instance"] = {"phase": content_service.phase, "final": True,
                           "final_capture_id": capture_id}
    issues: set[str] = set()
    observations = payload.get("resource_observation") or {}
    if any(observations.get(kind, {}).get("source") != "live" for kind in _RESOURCE_KINDS):
        issues.add("final_resource_observation_unavailable")
    rows = {row["email"]: row for row in payload.get("users", [])}
    summaries, emissions = {}, []
    for user in users:
        row = rows[user.email]
        observation = row.get("capture_observation") or {}
        if observation.get("journal") != "ready":
            issues.add("final_capture_observation_unavailable")
        active = bool(user.first_seen or user.sessions_launched or user._capture_recovered)
        if active and observation.get("code", {}).get("source") != "local":
            issues.add("final_code_observation_unavailable")
        if active and not os.path.isdir(user.home):
            issues.add("final_artifact_home_unavailable")
        harvest = artifacts.harvest_user(user, single_attendee=len(users) <= 1)
        issues.update(harvest.issues)
        if harvest.is_empty():
            summaries[user.email] = {"status": "no_material"}
            continue
        template = insight_summary._payload(
            insight_summary._extraction(harvest, row.get("signal")), harvest,
            run_id=emitter.run_id, email=user.email, phase=content_service.phase,
            generator="extraction", model=None, revision=1,
        )
        template["final_capture_id"] = capture_id
        summary = {"status": "prepared", "fingerprint": insight_summary._fingerprint(
            harvest, row.get("signal")), "payload": template}
        summaries[user.email] = summary
        emissions.append({"kind": "summary", "attendee": user.email,
                          "payload": template, "fingerprint": summary["fingerprint"],
                          "key": f"summary:closeout:{capture_id}:{user.email}"})
    for attendee, signal, _key in stats.signal_events(payload, emitter.run_id):
        signal["final_capture_id"] = capture_id
        emissions.append({"kind": "signal", "attendee": attendee, "payload": signal,
                          "key": f"signal:closeout:{capture_id}:{attendee}"})
    return {"stats": payload, "summaries": summaries, "emissions": emissions}, sorted(issues)


def _emit_one(emitter, capture_id: str, emission: dict) -> int:
    kind, attendee, key = emission["kind"], emission["attendee"], emission["key"]

    def prepare(state):
        saved = _checkpoint(state, capture_id)
        if saved is None:
            raise CaptureHeldError("prepared final outputs are missing")
        accepted = saved["accepted"].get(key)
        if accepted is not None:
            original = state["insight"]["events"].get(str(accepted), {})
            if (original.get("idempotency_key") != key
                    or original.get("attendee") != attendee):
                raise CaptureHeldError("final output receipt has no matching event")
            return original["payload"], key
        payload = copy.deepcopy(emission["payload"])
        if kind == "summary":
            summaries = insight_summary.stamps._validate(state.setdefault("summaries", {}))
            previous = summaries.get(attendee, {})
            payload["revision"] = previous.get("revision", 0) + 1
            previous_events = [
                (int(sequence), event) for sequence, event in state["insight"]["events"].items()
                if event.get("type") == "insight.summary"
                and event.get("attendee") == attendee
                and event.get("payload", {}).get("generator") == "llm"
            ]
            if previous_events:
                sequence, narrative = max(previous_events, key=lambda item: item[0])
                payload["prior_narrative"] = {
                    "sequence": sequence, "revision": narrative["payload"]["revision"],
                    "generator": narrative["payload"]["generator"],
                    "model": narrative["payload"].get("model"),
                    "raw_entry_sha256": _digest({"seq": sequence, "event": narrative}),
                }
            summaries[attendee] = {"revision": payload["revision"], "generator": "extraction",
                                   "fingerprint": emission["fingerprint"]}
        else:
            retained = stats._retained_stats(state)
            previous = retained["signals"].get(attendee, {})
            stats_payload = saved["prepared"]["stats"]
            row = next(row for row in stats_payload["users"] if row["email"] == attendee)
            stats._save_observation(state, row, stats_payload["resources"],
                                    stats_payload["resource_observation"],
                                    max((item["observed_at"] for item in
                                         stats_payload["resource_observation"].values()), default=0))
            retained["signals"][attendee] = {
                "revision": previous.get("revision", 0) + 1,
                "fingerprint": stats._fingerprint(payload), "key": key,
                "checkpoint_token": f"closeout:{capture_id}:{attendee}",
            }
        # EventEmitter commits recovery metadata and the envelope together.
        saved["accepted"][key] = state["insight"]["next_seq"] + 1
        return payload, key

    return emitter.emit_transaction("insight.summary" if kind == "summary" else "workshop.signal",
                                    attendee, prepare)


def finalize(emitter, capture_id: str) -> dict:
    """Prepare once and durably emit the current-policy model-free final state.

    The coordinator supplies the privileged finalizer scope and has already
    drained required writers. Storage errors propagate so no caller can mistake
    an interrupted finalization for complete output.
    """
    if not isinstance(capture_id, str) or not capture_id.strip():
        raise ValueError("a final capture identifier is required")
    emitter._ensure_ready()
    if not config.insight_capture_enabled():
        return {"complete": False, "issues": ["capture_disabled"], "outputs": {}}
    saved = _checkpoint(emitter.journal.read(), capture_id)
    if saved is None:
        prepared, issues = _prepare(emitter, capture_id)
        if issues:
            return {"complete": False, "issues": issues, "outputs": {"stats": prepared["stats"]}}

        def retain(state):
            existing = _checkpoint(state, capture_id)
            if existing is None:
                state["closeout_outputs"][capture_id] = {
                    "schema_version": 1, "prepared": prepared,
                    "prepared_sha256": _digest(prepared), "accepted": {},
                }

        emitter.journal.transaction(retain)
        saved = _checkpoint(emitter.journal.read(), capture_id)
    for emission in saved["prepared"]["emissions"]:
        _emit_one(emitter, capture_id, emission)
    saved = _checkpoint(emitter.journal.read(), capture_id)
    events = emitter.journal.read()["insight"]["events"]
    facts = copy.deepcopy(saved["prepared"]["summaries"])
    for emission in saved["prepared"]["emissions"]:
        if emission["kind"] == "summary":
            sequence = saved["accepted"][emission["key"]]
            facts[emission["attendee"]] = {
                "status": "captured", "sequence": sequence,
                "payload": copy.deepcopy(events[str(sequence)]["payload"]),
            }
    return {"complete": True, "issues": [], "outputs": {
        "stats": copy.deepcopy(saved["prepared"]["stats"]),
        "summary_facts": facts,
        "event_sequences": copy.deepcopy(saved["accepted"]),
        "prepared_sha256": saved["prepared_sha256"],
    }}
