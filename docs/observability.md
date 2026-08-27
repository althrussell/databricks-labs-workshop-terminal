# Workshop Terminal observability

Workshop Terminal uses [Databricks Apps telemetry](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/observability)
to export logs, traces, and metrics through the OTLP collector supplied by the
Apps runtime. Control Tower configures the App resource destination; WT never
constructs a collector URL and never receives collector credentials.

The shared destination contract is:

| Signal | Unity Catalog table |
|---|---|
| Logs | `main.wt_central.wt_otel_logs` |
| Metrics | `main.wt_central.wt_otel_metrics` |
| Traces | `main.wt_central.wt_otel_spans` |

Telemetry is inert when Databricks has not injected both
`OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_PROTOCOL`. In that state
`server.otel_bootstrap` starts uvicorn without auto-instrumentation. This keeps
the WT release compatible with events where the Public Preview is disabled.

## Cross-repository identity contract

Control Tower injects these non-secret values before deployment:

| Environment variable | OTel resource attribute |
|---|---|
| `WORKSHOP_RUN_ID` | `workshop.run_id` |
| `WORKSHOP_UNIT_ID` | `workshop.unit_id` |
| `EVENT_NAME` | `workshop.event_name` |
| `WORKSHOP_RELEASE_SHA` | `workshop.release_sha` |
| `DATABRICKS_WORKSPACE_ID` | `databricks.workspace.id` |
| `OTEL_SERVICE_NAME` / `DATABRICKS_APP_NAME` | `service.name` |

The bootstrap merges these with the Databricks-provided `workspace.id` and
`app.name`; it never replaces platform attributes. `/readyz` reports whether
the collector configuration and every required identity attribute are present,
but never returns the endpoint or any credential. Missing telemetry is a soft
readiness failure because fleet preflight owns the deployment gate and an
export outage must not take an attendee's terminal down.

## Event and metric contract

Structured records use `schema_version=1`, `event.name`, fixed reason codes,
and a trace ID. Relevant records also contain `agent.id`, `session.id`,
`operation.duration_ms`, outcome, process exit code or signal, and source.
Attendee email is deliberately retained only in WT's authenticated Control
Tower collection buffer and is not copied into the shared OTel record.

Operational events cover session start/refusal/switch/exit, bootstrap phases,
mirror serve/bypass, entitlement reconciliation and rate limiting, readiness
state changes, OBO/Omnigent health, attendee-visible error codes, and uncaught
async task failures.

Metric names are stable:

- `workshop.session.launches`
- `workshop.session.refusals`
- `workshop.session.active` (bounded to 0 or 1)
- `workshop.agent.exits`
- `workshop.bootstrap.duration`
- `workshop.entitlement.rate_limits`
- `workshop.entitlement.reconcile.duration`
- `workshop.mirror.coverage`
- `workshop.readiness.latency`

Every readiness probe updates the latency metric. The structured readiness
event is emitted only when state changes or once per minute while unchanged,
which avoids multiplying Zerobus ingestion cost under fleet polling.

## Privacy and failure behavior

A process-wide logging filter removes email addresses, authorization headers,
bearer/JWT/Databricks tokens, client secrets, API keys, passwords, prompts,
terminal input/output, and full configuration-file content. Structured OTel
events use an allowlist and omit free-form `detail`, `error`, and `raw_code`
fields. Attributes are bounded to 256 characters and fixed lists to 20 values.
FastAPI auto-instrumentation excludes the wizard and certificate routes because
their URL query can contain attendee-authored text; fixed WT operational events
still cover those workflows without copying that content.

The Databricks-documented batch processors perform network export outside the
request/session path. Instrument recording is fail-soft. Exporter absence,
latency, or failure can degrade observability but cannot refuse or terminate an
attendee session.
