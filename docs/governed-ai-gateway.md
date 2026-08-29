# Governed AI Gateway contract

Workshop Terminal supports a fail-closed, event-scoped model-service mode owned
by Control Tower. It never changes routing on the global `system.ai` services.

## Deployment configuration

Control Tower sets the following values together:

| Variable | Value |
|---|---|
| `WORKSHOP_AI_GATEWAY_MODE` | `required` for governed events |
| `WORKSHOP_AI_GATEWAY_CONFIG_SCHEMA` | `1` |
| `WORKSHOP_CLAUDE_DRIVER_SERVICE` | fully qualified custom model service |
| `WORKSHOP_CLAUDE_OPUS_SERVICE` | fully qualified custom model service |
| `WORKSHOP_CLAUDE_SONNET_SERVICE` | fully qualified custom model service |
| `WORKSHOP_CLAUDE_HAIKU_SERVICE` | fully qualified custom model service |
| `WORKSHOP_CODEX_SERVICE` | fully qualified custom model service |

Every service must be a lowercase three-part Unity Catalog name outside
`system.ai`. Claude slots must expose `anthropic/v1/messages`; Codex must expose
`openai/v1/responses`.

Generated Claude, Codex, and Omnigent configuration uses these exact names and
the rotating app-service-principal bearer. Discovery validates the services but
cannot replace one with a discovered global service. Each wire sends
`Databricks-Ai-Gateway-Request-Tags` as a JSON object containing
`workshop_run_id`, `workshop_unit_id`, `agent`, and `wt_release`.

## Live model policy

CT pushes its complete WT-SP policy to authenticated endpoint
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
atomically persisted under `DATA_ROOT`, direct server inference changes
immediately, and configs for future sessions are rewritten. A running agent is
never restarted or silently rerouted.

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

## Admission and emergency control

In required mode, `/readyz` remains red until a policy revision is present,
stable request-tag identity is configured, and the app SP discovers every
custom service on the expected wire. There is no direct `system.ai` fallback.

`POST /api/admin/agent-controls` accepts `enabled`, an audit `reason`, and
optional `terminate_active`. Disabling is linearized with session creation: a
concurrent launch is either refused or is found and terminated. Launch counts
remain an activity metric only; Gateway QPM/TPM and allowance controls own cost.

Gateway 429s distinguish temporary rate limits (with bounded retry guidance)
from exhausted event allowance. Generated Codex/Omnigent configuration limits
automatic request retries so it cannot fight the configured Gateway boundary.
