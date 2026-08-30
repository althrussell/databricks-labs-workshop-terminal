# Unity AI Gateway governance contract

Workshop Terminal calls only the approved Databricks-hosted model services in
`system.ai`. Control Tower does not create per-event or per-terminal model
services.

Control Tower applies `control_tower_workshop_allowed=true` to every approved
model service. Event budgets match that model-service tag together with the
event workspace IDs. Request tags remain diagnostic only and do not affect
budget matching.

The enforcement layers are deliberately separate:

1. Control Tower removes broad `system.ai` access and grants `EXECUTE` only on
   approved models to the event's lab-user and Workshop Terminal service-
   principal groups.
2. Control Tower pushes the same revisioned model pool to each Workshop
   Terminal. WT uses it immediately for server inference and rewrites future
   Claude, Codex, and Omnigent session configuration without restarting an
   active agent.
3. An account-level Unity AI Gateway budget is scoped to the event workspace
   IDs. Its per-user threshold uses **Block usage**, so one attendee exhausting
   their allowance does not block other attendees.

Budget matching does not depend on request tags. WT still sends
`Databricks-Ai-Gateway-Request-Tags` containing `workshop_run_id`,
`workshop_unit_id`, `agent`, and `wt_release` for diagnostics and usage
attribution.

## Live model policy

CT pushes its complete WT service-principal policy to authenticated endpoint
`PUT /api/admin/model-policy`:

```json
{
  "revision": 12,
  "pool": [
    {
      "service_name": "system.ai.gpt-oss-120b",
      "enabled": true,
      "capabilities": ["chat"],
      "principal_classes": ["lab_user", "wt_sp"],
      "limit_profile": {}
    }
  ],
  "denied_models": ["system.ai.outside-pool"],
  "restart_processes": false
}
```

Revisions are monotonic. Replaying identical content at the current revision is
successful and idempotent; a stale revision or different content at the same
revision returns 409. Invalid or overlapping names return 422. The snapshot is
atomically persisted under `DATA_ROOT`. An applied revision is authoritative,
including an empty pool: WT never falls back to workspace discovery when CT has
intentionally allowed no models.

For CT-managed deployments, `WORKSHOP_MODEL_POLICY_REQUIRED=true` rejects agent
launches until the first policy revision is durably applied. This closes the
deployment window before exact model grants and WT routing have converged.

The response reports the exact policy evidence CT verifies:

```json
{
  "revision": 12,
  "applied": true,
  "changed": true,
  "verified": true,
  "positive_checks": ["system.ai.gpt-oss-120b"],
  "negative_checks": ["system.ai.outside-pool"],
  "processes_restarted": false
}
```

## Budget and emergency control

The budget is owned by Control Tower/account administration, not WT. It uses
the Unity AI Gateway resource type, the exact event workspace IDs, a monthly
per-user threshold, and **Block usage**. Enforcement is near-real-time and
approximate; active requests are not interrupted.

`POST /api/admin/agent-controls` accepts `enabled`, an audit `reason`, and
optional `terminate_active`. Disabling is linearized with session creation: a
concurrent launch is either refused or found and terminated. This emergency
control does not replace the Gateway budget or Unity Catalog permissions.

Gateway 429s distinguish temporary rate limits from exhausted budget allowance.
Generated Codex and Omnigent configuration limits automatic retries so clients
do not fight an enforced Gateway boundary.
