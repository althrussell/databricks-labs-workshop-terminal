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
mirror serve/bypass, App deploy outcomes, entitlement reconciliation and rate
limiting, readiness state changes, OBO/Omnigent health, attendee-visible error
codes, and uncaught async task failures. `app.deploy` carries only bounded
duration, outcome, reason, attempts, resume state, and MCP/CLI source.

The supervisor (see [release-artifact.md](release-artifact.md)) has no
telemetry of its own; the server it starts records its events:

- `server.restarted`: the previous server's exit code or signal, how long it
  ran (`operation.duration_s`), and restarts in the last 10 minutes
  (`operation.attempts`).
- `server.kill_blocked`: a stop signal from inside the app was ignored, with the
  signal, `event.reason_code` (`descendant` or `vanished`) and the attempts
  since the last report. At most one record per signal every 5 seconds.

Neither is in Control Tower's collection buffer.

Metric names are stable:

- `workshop.session.launches`
- `workshop.session.refusals`
- `workshop.session.active` (bounded to 0 or 1)
- `workshop.agent.exits`
- `workshop.bootstrap.duration`
- `workshop.app.deploy.duration`
- `workshop.entitlement.rate_limits`
- `workshop.entitlement.http_429`
- `workshop.entitlement.requests`
- `workshop.entitlement.backoff.duration`
- `workshop.entitlement.cache.accesses` (`cache.result=hit|miss`)
- `workshop.entitlement.convergence.latency`
- `workshop.entitlement.reconcile.duration`
- `workshop.mirror.coverage`
- `workshop.readiness.latency`

Every readiness probe updates the latency metric. The structured readiness
event is emitted only when state changes or once per minute while unchanged,
which avoids multiplying Zerobus ingestion cost under fleet polling.

### Journey and agent activity events (W6)

Counts and coarse states only: never prompts, arguments, tool output,
keystrokes or reasoning. All are buffered for Control Tower and logged to
`wt_otel_*` through the same `telemetry.emit` path as `journey.status`.

| Event | When | Payload |
|---|---|---|
| `journey.status` | the seat's facilitator signal changes | `{phase, path, status, outcome: "observed"}` |
| `journey.phase` | the workflow moves to another phase | `{from, to, path, seconds}` (time spent in `from`) |
| `journey.outcome` | the attendee (or a facilitator) records their account at feedback | `{goal_explained, decisions_shaped, main_task, second_change, knows_next_step, confidence, by}` |
| `agent.activity` | the seat's agent activity counts change (on CT's stats harvest) | `{prompts, tool_calls: {tool: n}, tool_errors: {reason: n}, errors}` |

`tool_calls` names tools (`Bash`, `apply_patch`, MCP tools as `mcp:<server>`);
`tool_errors` uses coarse reasons (`permission_denied`, `not_found`,
`timeout`, `rate_limited`, `server_error`, `syntax_or_type`,
`command_failed`, `other`). The stats row carries the same `agent_activity`,
and `errors` now adds the agents' failed tool results to failed launches.
`minutes_building` is each seat's own span from first visit to latest
activity.

The agents' own OpenTelemetry exporters stay off. Handing attendee processes the
app's collector endpoint would give them a write path into the central
telemetry tables, and their logs include tool parameters. WT derives the same
counts from the transcripts it already reads.

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
