# Admin / steering API contract

This is the integration surface for **Control Tower** (and on-site operators)
to steer a live workshop. All endpoints live under `/api/admin/*` on the
deployed app URL.

> **Provisioning the app?** See
> [`control-tower-implementation.md`](./control-tower-implementation.md) for the
> per-attendee setup contract and post-deploy direct-OAuth health gate.

## Authentication & authorization

Authorization is **workspace-group based** — there are no email allowlists.

- The caller must be a member of the group named by the `ADMIN_GROUP` env var
  (default **`platform_admins`**).
- Browser users are identified by the Databricks Apps proxy headers; their
  forwarded OBO token resolves group membership via SCIM `/Me`.
- Service principals (e.g. Control Tower's deployer SP) call the app URL
  directly with `Authorization: Bearer <oauth token>` and must:
  1. have `CAN_USE` on the app, and
  2. be a member of `ADMIN_GROUP` in the workspace.

Group membership is cached for 5 minutes.

## Prerequisite: the attendee CLI credential

Databricks Apps injects platform-managed app-SP OAuth credentials. The SDK's
explicit `WorkspaceClient(host=..., client_id=..., client_secret=...,
auth_type="oauth-m2m")` returns the current short-lived bearer. The server
removes client-secret env aliases after constructing that singleton and, on
production Linux, sets `PR_SET_DUMPABLE=0` before installers or PTYs start.
This protects the same-UID process boundary; it does not claim the SDK object
contains no secret in memory. OBO remains the attendee-governed `[me]` profile.

### Normal: direct app-identity OAuth

No token ACL or Token API call is required. The manager reacquires OAuth every
five minutes, tracks JWT `exp` when inspectable, and first validates
`GET /api/2.0/current-user/me`. The returned `applicationId`,
or `application_id` must exactly equal the injected `DATABRICKS_CLIENT_ID`;
HTTP 200 alone is not accepted. `userName` is retained only as safe diagnostic
context and never proves app identity. If that lower-privilege endpoint lacks
an authoritative application-ID field, SCIM `/Me` is attempted as a fallback
and its `applicationId` must match exactly. An authoritative mismatch is
rejected rather than overridden by the fallback. The manager then fans out
only fresh, changed bearers. A first request after an idle validation window
reacquires on demand. There is **no static secret in `app.yaml`**, credential
files contain only access bearers, and no attendee creates a PAT. `/readyz`
fails closed if environment scrubbing or production Linux non-dumpable
hardening did not succeed.
Attendees act as the per-instance service principal (acceptable under the
one-workspace-per-attendee topology).

### Remote Omnigent topology enforcement

When `OMNIGENT_APP_URL` is configured, one attendee per Workshop Terminal
instance is mandatory, not advisory. Startup fails if
`ALLOW_SHARED_TOPOLOGY=true` or if
`MAX_SESSIONS_GLOBAL > MAX_SESSIONS_PER_USER`, because attendee OBO mirrors
live in separate HOMEs under the same Unix uid. Deploy one instance/workspace
per attendee and use single-attendee session caps. This enforcement does not
apply when `OMNIGENT_APP_URL` is empty, preserving local Omnigent behavior.

### Emergency-only: vended `WORKSHOP_PAT`

For environments without a usable app identity, Control Tower may vend a
workspace token as `WORKSHOP_PAT` (via `env_overrides` / in-place `app.yaml`
edit, or an app secret resource with `valueFrom`).

- A vended PAT is a **static credential that expires with no rotation behind
  it** — a time bomb for events deployed ahead of time. It is served directly,
  never used to mint another token, and always reports **`degraded`**.
- Provision the PAT with a lifetime comfortably exceeding deploy-to-event-end,
  and watch the credential health (below). Control Tower should revoke it at
  teardown.
- If `WORKSHOP_PAT` is missing **and** the app identity can't authenticate, the app
  still serves bash terminals and shows a clear banner; agent CLI launches
  return 503.

### Credential health and alerting

A background probe verifies the credential end-to-end on a ~5-minute cadence —
through idle windows too — and classifies source `app_identity_oauth` as
`rotating` only while validation is recent,
`degraded` (a static credential is being served, no rotation), or `unhealthy`
(the credential was rejected/expired or nothing is configured). The state is
exposed in `credential` on `GET /api/config` and `GET /api/admin/presence`,
surfaced as an operator banner, and emitted to Control Tower as a
`credential.health` event so broken OAuth or an expiring fallback is
caught hours/days before the event, not at first use on the day.
The `validation_diagnostic` status field records the validation result, each
endpoint's HTTP status, and only allowlisted observed identity fields/IDs
(`applicationId`, `application_id`, `userName`, `id`). It never stores the
bearer or response body.

The app identity (or vended PAT) is also the app's SCIM credential for
resolving attendee group membership (operator gating), so it needs SCIM read
access.

## Endpoints

### `GET /api/admin/state`
Current phase, available phases, nugget count, active broadcast, start time.

### `POST /api/admin/content-pack`
Replace the live content pack (nuggets + shell config). Body must validate
against the pack schema (`server/content.py`). In-memory until the next
restart; the deployed default pack (`content/default_pack.json`, or the file
at `CONTENT_PACK_PATH`) is reloaded on restart.

```json
{
  "version": 1,
  "phases": ["intro", "setup", "build", "wrap"],
  "shell": {
    "links": [{ "label": "Customer Academy", "url": "https://...", "icon": "graduation-cap" }],
    "features": { "nuggets_pane": true, "operator_panel": true }
  },
  "nuggets": [
    {
      "id": "unique-id",
      "title": "Card title",
      "markdown": "Body — markdown supported.",
      "link": { "url": "https://...", "label": "Read the docs" },
      "tags": ["unity-catalog"],
      "phases": ["build"],
      "triggers": ["claude_active"],
      "weight": 5,
      "pinned": false
    }
  ]
}
```

Trigger vocabulary: `always`, `claude_active`, `codex_active`, `bash_active`,
`idle_10m`, `elapsed_gt_<minutes>`, and `topic:<name>`. Empty `triggers` =
always shown; empty `phases` = all phases.

**Ideation prompts:** the optional pack-level `prompts` map
(`{"all": [...], "build": [{"label": "...", "prompt": "..."}]}`) renders
clickable chips on Home and feeds the per-phase ideas; clicking types the
prompt into the attendee's agent session **unsent**. Nuggets may also carry a
`prompt` field — the card gains a "type it into my terminal" action. The
shell config gains `workspace_links` (`{label, path, icon, description}`)
rendered as deep-link tiles into the attendee's workspace.

**Topic detection:** the optional pack-level `topics` map
(`{"lakebase": ["lakebase", "postgres", ...]}`) drives contextual insights —
when a keyword appears in an attendee's terminal output, nuggets with the
matching `topic:<name>` trigger surface immediately with a "spotted in your
session" treatment and rank first for 15 minutes. Only topic *names* are
recorded per user; terminal content is never stored or transmitted. Disable
with `TOPIC_DETECTION=false`.

### `POST /api/admin/phase`
```json
{ "phase": "build" }
```
422 if the phase isn't defined by the live pack. All connected attendees
refresh their nugget pane immediately (`/ws/events` push).

### `POST /api/admin/wizard-model`
```json
{ "model": "system.ai.gpt-oss-20b" }
```
Swaps the model behind the onboarding wizard's idea grid for this instance,
immediately. An empty `model` clears the swap. A bare name is qualified the same
way a pin is, so an operator can send what the dropdown shows.

Precedence is **override → deployed pin (`WORKSHOP_WIZARD_MODEL`) → the
terminal's own chain**. 422 when the workspace demonstrably does not serve the
model on the wizard's wire; accepted when discovery itself fails, because an
empty discovery result means the call failed rather than that the workspace
serves nothing — refusing there would withdraw the operator's only remaining
action at the moment the room is already unhappy.

**The override is ephemeral by decision.** Nothing is written to the state dir,
so a restart reverts to the deployed value. That keeps `app.yaml` the thing that
is always true after a restart, at the cost of the operator re-applying — which
is why `GET /api/admin/state` reports `wizard_model` with its provenance
(`source: "override" | "deployed" | "chain"`) rather than a bare string. The
wizard's own suggest response carries the model that actually answered, so a
swap can be confirmed rather than assumed.

### `PUT /api/admin/model-policy`

Control Tower's authenticated, monotonic live-sync seam. It applies the complete
capability-filtered WT-SP pool plus denied models, persists the revision
atomically, refreshes direct inference and future-session configuration, and
never restarts an existing process. See
[`governed-ai-gateway.md`](./governed-ai-gateway.md#live-model-policy).

### `POST /api/admin/agent-controls`

```json
{
  "enabled": false,
  "terminate_active": true,
  "reason": "Gateway incident 142"
}
```

Pauses new agent launches immediately. `terminate_active` also closes the sole
active session. Repeating the same state is idempotent. `GET` returns the switch
state, audit reason, and launch activity; it never labels launch count as token,
dollar, or remaining budget.

### `POST /api/admin/broadcast`
```json
{ "message": "Labs close in 10 minutes — commit your work!", "level": "warning", "ttl_s": 600 }
```
Shows a banner on every connected attendee screen for `ttl_s` seconds.
Levels: `info`, `success`, `warning`.

Optional `clear_help: true` clears the attendee's locally raised hand (used when
Control Tower resolves a help request). Chat history is retained after resolve.
An empty `message` with `clear_help` only clears the hand without showing a banner.

### `POST /api/admin/help/message`

Control Tower fan-out: deliver one help-thread message into this unit's local
buffer and push it over `/ws/events` as `help_message`.

```json
{
  "message_id": "uuid-or-opaque-id",
  "help_request_id": "uuid",
  "sender_role": "operator",
  "sender": "operator@example.com",
  "body": "Try restarting the warehouse",
  "created_at": "2026-08-04T01:02:03Z",
  "show_banner": true
}
```

When `show_banner` is true (default) and `sender_role` is `operator`, the server
also emits a `broadcast` with `source: "help"` so attendees with the Help panel
closed see a toast. The attendee UI suppresses that banner while the panel is
open.

### Attendee help (`POST /api/help/raise` · `POST /api/help/lower` · `POST /api/help/messages` · `GET /api/help/thread`)

Authenticated attendees raise or lower a hand for operator help, post follow-up
messages, and fetch the local thread mirror. Local state updates immediately;
when `CONTROL_TOWER_URL`, `WORKSHOP_RUN_ID`, and `WORKSHOP_UNIT_ID` are set the
app also POSTs to Control Tower's `/api/help/raise|lower|messages` using the
app service-principal OAuth bearer (fail-soft when misconfigured). Optional note
on raise, max 280 characters; message bodies max 2000 characters.

That push only reaches a Control Tower in **this** workspace. Across workspaces
— the supported event topology — it is refused by the Apps proxy and delivery
falls to collection instead; see
[`help_outbox`](#help_outbox--the-delivery-path-for-what-the-attendee-writes).
The refusal is reported once per process, not once per message.

Where the push does work, both paths are live, so it carries the same
`message_id` the outbox offers for that message. Control Tower files the row
under that id and recognises the second arrival as a duplicate; without it the
attendee's sentence would appear twice in the operator's thread.

`GET /api/config` includes a `help` block
`{raised, note, raised_at, message_count, help_request_id}` for the header
control.

### `GET /api/admin/presence`
Per-attendee status: online (active in the last 60 s), first/last seen,
credential health, open sessions (agent, created, last activity), and per
attendee `obo` (OBO/`me`-profile freshness — see below). The top level also
carries `credential`, `entitlements`, and help-queue fields for Control Tower
presence reconcile:

| Field | Type | Meaning |
|---|---|---|
| `help_raised` | bool | Attendee has raised a hand |
| `help_open` | bool | Alias of `help_raised` |
| `help_note` | string \| null | Optional note (≤280 chars) |
| `help_raised_at` | number \| null | Unix timestamp when raised |
| `help_outbox` | array | Attendee messages and read receipts waiting to be collected (see below) |

#### `help_outbox` — the delivery path for what the attendee writes

The push to Control Tower described above cannot authenticate in the supported
topology. This app's OAuth bearer is minted against the attendee's own
workspace, and Control Tower sits behind the Databricks Apps proxy of the admin
workspace, which rejects a token minted elsewhere with `401` and an empty body
before Control Tower's code runs. Collection is the direction that works, so
attendee-authored help traffic waits here until Control Tower polls for it.

Each entry carries a monotonic `seq` and a `kind`:

```json
{"seq": 4, "kind": "message", "message_id": "uuid", "body": "still stuck",
 "sender_role": "attendee", "created_at": "2026-08-12T08:36:03Z"}
{"seq": 5, "kind": "seen", "message_id": "uuid"}
```

`message_id` is assigned here and is the same id Control Tower stores, so a
round that is collected but not acknowledged is applied exactly once. Raised
and lowered hands are **not** in the outbox — `help_raised` already carries
that state and Control Tower already reconciles it.

At most 20 entries are offered per response and 50 are retained; a backlog only
grows while nothing is collecting, which is also when nobody is reading it.

### `POST /api/admin/help/ack`

Control Tower confirms it has applied everything up to a sequence number, and
the terminal drops it: `{"through_seq": 5}` → `{"status": "ok",
"acked_through": 5, "pending": 0}`. Until this arrives the same entries are
offered again, because an unacknowledged message is indistinguishable from one
that never arrived.

### OBO + entitlements status (operator pre-flight)

When `ENABLE_OBO` is on, `GET /api/config` and `GET /api/admin/presence` carry
an `obo` block per attendee:

```json
{ "enabled": true, "profile": "me",
  "scopes": "catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql",
  "present": true, "fresh": true, "expires_in": 3210, "last_refresh": 1750000000 }
```

`fresh: false` (or `present: false`) means the attendee's `databricks --profile
me ...` reads won't work yet — usually the tab isn't open, consent wasn't
granted, or `user_api_scopes` is missing on the app resource. Catch it here
**before** the event.

When `ENABLE_ENTITLEMENTS` is on, an `entitlements` block reports the SP-driven
reconciler that makes SP-created resources usable by the labuser:

```json
{ "enabled": true, "catalog": "wsh_alice", "schema": null, "ok": true,
  "state": "healthy", "last_reconcile": 1750000000, "last_error": null,
  "interval": 300, "idle_interval": 1800, "idle": true,
  "next_attempt_at": 1750001200, "next_attempt_reason": "cache_expiry",
  "deferred_reason": null, "backoff_seconds": 0 }
```

`ok: false` with a `last_error` means a grant is failing (commonly the app SP
lacks permission on `WORKSHOP_CATALOG`, or the catalog name is wrong). The
reconciler also emits an `entitlements.health` event to Control Tower (same
envelope as `credential.health`) so a drifted/missing grant alerts ahead of the
event. `state: degraded_backoff` means a 429 or `RESOURCE_EXHAUSTED` response
deferred verification until `next_attempt_at`; `ok` and the handoff ledger keep
their last verified values during that bounded delay.

`POST /api/admin/entitlements/reconcile` forces a verified pass for every bound
attendee or the optional `email`. An optional `resource_type` refreshes only
that adapter (`apps`, `jobs`, `pipelines`, `serving-endpoints`,
`database-instances`, `database-projects`, `warehouses`, or `dashboards`). A
request made during platform backoff returns the deferred status without making
another Databricks API call.

### App callback endpoints (not admin-gated)

Two small endpoints back the in-terminal helper scripts; both accept an
`{"email": "<attendee>"}` body so a PTY helper (no proxy identity) can call
them, and both no-op cleanly when the feature is disabled:

- `POST /api/obo/refresh` — force-writes the freshest captured OBO token to the
  `me` profile and nudges the tab (the `databricks-me` 401 self-heal path).
- `POST /api/entitlements/reconcile` — runs an immediate entitlement reconcile
  for the attendee (the `workshop-grant-me` path), returning the per-resource
  grant summary. The optional `resource_type` body field performs a targeted
  refresh; the helper accepts the same value as its first argument.
- `POST /api/discovery` — records one agent-elicited discovery record (the
  `workshop-discovery` path, contract C6). Only `record_id` is meaningful as a
  key; every other field is optional, unknown fields are ignored, and free text
  is redacted and truncated on the way in. Returns
  `{captured, record_id, redactions, records}`. When capture is disabled it
  returns `{"captured": false, "reason": "disabled"}` with HTTP 200 — the agent
  must not retry, and must not tell the attendee something failed. Authentication
  still applies first, so an unauthenticated caller can't probe the flag.

Two attendee-authenticated (browser) routes make capture inspectable by its
subject:

- `GET /api/discovery` — the caller's own records, scoped to their identity.
- `POST /api/discovery/redact` — `{"record_id": "..."}` withdraws one of the
  caller's own records. Browser identity only: a PTY capability token confers
  nothing here, otherwise an agent could quietly erase what it captured.
  Withdrawal leaves a tombstone, so re-submitting the same `record_id` cannot
  resurrect it.

Both back a panel in the attendee's own insights pane, which appears only once
something has been captured and lists each record with a Remove control.
Consent is arranged out of band (see `WORKSHOP_INSIGHT_CAPTURE` below), so this
is where an attendee can actually see and revoke what was recorded about them.
A withdrawn record stops being sent to Control Tower, but anything already
pushed is CT's to delete — the terminal cannot reach back into Lakebase.

`GET /api/omnigent-host` is authenticated and attendee-scoped. It returns only
sanitized supervisor state (`disabled`, `waiting_for_token`, `starting`,
`running`, `backoff`, `stopped`, or `error`); `running` means process-alive, not
control-plane connected. `/api/config` exposes only the non-secret normalized
Omnigent App URL and enabled state.

### `GET /api/admin/stats`
Harvest endpoint for Control Tower's event-impact capture: per-attendee
build stats (`email`, `minutes_building`, `agent_sessions`,
`terminal_sessions`, `topics`, `code` {projects, commits, files, lines}),
one instance-level workspace resource census, and `instance`
{phase, started_at, session_count, final}. Code stats are cached ~5 minutes, so
periodic polling is cheap. Control Tower persists snapshots into its
Lakebase for reporting that survives workspace teardown.

Every response also reports `instance.events_pending`, `instance.events_dropped`
and `instance.event_stream_id` — the state of the buffer that
`GET /api/admin/insight-events` serves, measured *after* any summaries this call
generated.

**`?final=true`** marks Control Tower's pre-delete pass. It is the last moment
anything can be read off this instance, so beyond returning the usual payload it
also, when `WORKSHOP_INSIGHT_CAPTURE` is on:

- runs the model-free edge-summary backstop for any attendee the `wrap` transition
  didn't already cover, reporting the count as `instance.summaries_emitted`;
- drains the event emitter's buffer synchronously, reported as
  `instance.events_flushed` (a no-op unless the optional push path is configured).

Because the summaries land in the buffer as this call answers, **Control Tower must
collect after this call, not before** — `events_pending` in the response is what it
still owes itself a `GET /api/admin/insight-events` for. Everything left there dies
with the app.

Both are best-effort and never fail the response: teardown reads this payload for
the durable impact record, and losing that to a summarisation bug would cost more
than the insight it was trying to save. A failure is reported as
`instance.summary_error`. Older terminals ignore the parameter, so Control Tower
can send it unconditionally.

`schema_version` is `3`. Each per-attendee row also carries a derived `signal`
block — `engagement` (`observer`/`explorer`/`builder`), `primary_topic`,
`topic_hits`, `products`, `resource_kinds`, `shipped` — reducing the counters to
the form a post-event brief quotes. The block is **always present**, including
when `WORKSHOP_INSIGHT_CAPTURE` is off: the flag gates transmission, not
derivation, so an operator can inspect exactly what capture would send before
enabling it. The census stays instance-level and is deliberately not copied into
per-attendee rows — on a shared instance it reflects the whole cohort.

Each row also carries `discovery_records`, a **count only**. The records
themselves are collected from the event buffer, never returned through this
operator-readable poll; the count exists so CT can tell "this attendee said
nothing" from "we lost what they said" after the buffer overflowed.

When capture *is* on, polling this endpoint also queues one `workshop.signal`
event per attendee into the buffer. Emission rides the harvest rather than its
own timer so the signal and the snapshot CT stores alongside it describe the same
moment; keys are bucketed per 10 minutes so a long workshop writes a coarse time
series instead of a row per poll.

### `GET /api/admin/insight-events`

Hands Control Tower the buffered attendee events. This is the **delivery path** for
insight capture (and for the health events in
[Credential health](#credential-health-and-alerting)): a deployed app cannot post
to Control Tower, because every Databricks App sits behind a proxy requiring a
Databricks identity on the request, so a token-only `POST` is rejected before it
arrives. Collection reuses the authenticated call CT already makes to this router.

| Query | Default | Meaning |
|---|---|---|
| `after` | `0` | CT's cursor **and its acknowledgement**: events at or below it are discarded. |
| `stream` | `""` | The `stream_id` the cursor was issued under. |
| `limit` | `500` | Maximum events in one response. |

```json
{
  "schema_version": 1,
  "stream_id": "9f2c…",
  "events": [{"seq": 41, "event": { /* AttendeeEventIn envelope */ }}],
  "high_water": 41,
  "pending": 3,
  "dropped": 0,
  "cursor_reset": false,
  "delivery": "pull"
}
```

`seq` is transport state and sits *outside* the envelope, which is byte-for-byte
the one the push path posts — with one exception: `run_id` is empty, because a
deployed terminal is never told which run it belongs to. The collector stamps
identity from the unit it polled; see
[the insight contract](./workshop-insight-contract.md#transport).

`after` is honoured **only** when `stream` matches the current `stream_id`.
Sequence numbers restart with the process, so a cursor replayed across a restart
would discard a fresh buffer unread; the mismatch is reported as
`cursor_reset: true` and the full buffer is returned instead. Re-collection is
harmless — ingest de-dupes on `idempotency_key`.

`dropped` counts events evicted because the 5000-event buffer overflowed. It is
the one loss no later collection recovers, so it is also surfaced in `/readyz`.

### `GET /api/admin/omnigent-host-readiness`
Admin/SP-authenticated, token-free readiness used by Control Tower alongside
stats collection. It returns the exact local supervisor `status`, `connected`,
and `expected_host_id`; `host_id` and `last_seen_at` appear only after a fresh
attendee-owned bearer verifies `GET /v1/hosts/{expected_host_id}` as `online`.
`last_seen_at` is the UTC timestamp when Workshop Terminal completed that
successful verification; upstream v0.10.0's host response has no last-seen field.
Network/auth/offline/mismatch results remain disconnected and never expose the
bearer.

### `GET /api/admin/diagnostics`
The answer to "why did that attendee see an error?", without their browser and
without a shell on their container.

| Field | Meaning |
|---|---|
| `errors` | Classified Omnigent failures from the collector's journal, newest first. Each carries `code`, `attendee`, `source` (`runner`/`host`/`server`), `level`, `logger`, `session`, a redacted `message`, the redacted `detail` (traceback), a `fingerprint`, and `count`/`first_seen`/`last_seen` |
| `collector` | Sweep counters and whether the background collector is running |
| `readyz` | The same runtime readiness report `/readyz` returns |
| `identity` | Most recent `identity.resolved` snapshot per attendee — which principal each CLI surface resolves to on each plane |
| `hosts` | Per-attendee Omnigent host readiness |

`limit` (default 50, max 500) caps `errors`. The journal is de-duplicated by
`(attendee, session, code, traceback fingerprint)` and persisted beside the
session journal, so it survives a restart of the app — which matters, because
the failures worth reading are usually the ones that restarted something.

### `GET /api/admin/diagnostics/logs`
Redacted tails of the Omnigent **process** logs — `~/.omnigent/logs/*/*.log`,
which now includes the captured host stdout/stderr. Optional `attendee`,
`source`, and `limit_bytes` (default 64 KiB, max 256 KiB).

Never returns `auth_tokens.json` and never returns PTY scrollback: how the
machine failed is operator-visible, what the attendee typed is not.

### `POST /api/admin/diagnostics/sweep`
Run a collection pass now instead of waiting for the next tick. An operator
reading the panel has an attendee waiting.

### `GET /api/admin/omnigent-tier`
Whether the Omnigent tier is being offered, and who could actually launch it:
`enabled` (false once demoted), `remote` (whether this deployment is wired to a
remote Omnigent app at all), and an `attendees` list carrying each attendee's
sign-in freshness and host state.

### `POST /api/admin/omnigent-tier`
`{"enabled": false}` withdraws every Omnigent-backed card fleet-wide;
`{"enabled": true}` restores them. Open tabs are pushed the change immediately.

This is the rung-4 lever in [`operator-runbook.md`](./operator-runbook.md): the
Omnigent harnesses share one credential plane and fail together, so when that
plane is down the useful move is to withdraw them and leave the bare tier —
Claude and Codex — which runs on the app credential and cannot fail
the same way. Distinct from the spend kill-switch (`agent-controls`), which
pauses *every* agent and stops the workshop.

### `POST /api/admin/recover`
`{"email": "..."}` for one attendee, `{}` for everyone. Runs the same three
steps the server takes on its own — re-mirror the attendee's token, wake their
Omnigent host, ask their tab for a fresh one — and returns `recovered` plus a
per-attendee `results` array with the actions taken. The operator cooldown is
bypassed: it exists to stop a log sweep thrashing, not to refuse a human.

An attendee's own Recover button hits the unauthenticated-to-admin
`POST /api/recover`, which does the same for the caller only.

## CLI usage

`scripts/push_content.py` wraps all of the above:

```bash
export WORKSHOP_APP_URL=https://my-app-1234.aws.databricksapps.com
export DATABRICKS_TOKEN=...   # member of platform_admins

python scripts/push_content.py state
python scripts/push_content.py phase build
python scripts/push_content.py broadcast "Break ends at 2pm" --level info
python scripts/push_content.py pack ./my_event_pack.json
python scripts/push_content.py presence
```

`scripts/pull_diagnostics.py` is the diagnostics companion, and takes a file of
app URLs so a fleet can be checked in one pass:

```bash
export DATABRICKS_TOKEN=...   # member of platform_admins

python scripts/pull_diagnostics.py summary --url https://my-app-1234.aws.databricksapps.com
python scripts/pull_diagnostics.py errors  --url https://my-app-1234.aws.databricksapps.com
python scripts/pull_diagnostics.py logs    --url https://... --source runner
python scripts/pull_diagnostics.py errors  --urls ./instances.txt   # whole fleet
```

## Deploy-time configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `WORKSHOP_PAT` | *(unset)* | Emergency-only vended workspace credential. Normal mode uses direct `app_identity_oauth`; a static PAT is always reported `degraded` |
| `WORKSHOP_APP_SP_ID` | *(unset; required for `/readyz`)* | Numeric SCIM `service_principal_id` returned by app create/get. Control Tower patches the uploaded `app.yaml` after app creation and before deploy; SCIM `/Me` must match this ID together with `DATABRICKS_CLIENT_ID` in `userName` when `applicationId` is absent |
| `ADMIN_GROUP` | `platform_admins` | Group that grants operator/admin access |
| `WORKSHOP_LOG_COLLECTOR` | `true` | Background sweep of Omnigent process logs into the diagnostics journal |
| `WORKSHOP_LOG_COLLECTOR_INTERVAL_S` | `5` | Seconds between sweeps |
| `WORKSHOP_LOG_JOURNAL_CAPACITY` | `500` | Distinct classified errors retained across restarts |
| `OMNIGENT_HOST_LOG_LEVEL` | `DEBUG` | Log level for the per-attendee Omnigent host process; the records explaining a failed start are below INFO |
| `OMNIGENT_HOST_LOG_MAX_BYTES` | `2097152` | Ceiling on one attendee's captured host stdout/stderr before rotation |
| `LAB_COACH` | `true` | Append lab-coach instructions to attendee agent memory |
| `TOPIC_DETECTION` | `true` | Terminal keyword spotting for contextual insights |
| `SKILLS_REPO` | github databricks/databricks-agent-skills | Skills source; event use is constrained by the reviewed artifact manifest |
| `SKILLS_REF` | empty in `app.yaml` | Exact reviewed tag/SHA for the skills overlay; must match the manifest ref, commit, and content SHA-256 |
| `ARTIFACT_MANIFEST_PATH` | empty | Optional mirror override for the repo-owned contract in `assets/artifacts/manifest.json`; may redirect `source` only, and a version/checksum override is rejected |
| `CLAUDE_CODE_VERSION` | `2.1.237` in `app.yaml` | Exact reviewed Claude Code CLI release candidate |
| `CODEX_CLI_VERSION` | `0.148.0` in `app.yaml` | Exact reviewed Codex CLI release candidate |
| `OMNIGENT_VERSION` | `0.10.0` in `app.yaml` | Exact reviewed Omnigent release candidate, matched to the dedicated App protocol |
| `DATABRICKS_CLI_VERSION` | `1.11.0` in `app.yaml` | Exact reviewed Databricks CLI release input |
| `DEEPWIKI_MCP_URL` / `EXA_MCP_URL` | public endpoints | MCP servers for attendee agents (empty string disables) |
| `ACCESS_GROUP` | *(unset)* | Optional group restricting attendee access |
| `WORKSHOP_ATTENDEE_EMAIL` | *(unset; required for `/readyz`)* | Control-Tower-injected email assigned to this one app instance. A different attendee receives HTTP 403 / WebSocket 4403 unless `ALLOW_SHARED_TOPOLOGY=true`. Admin service-principal routes remain group-authorized and independent of this binding |
| `WORKSHOP_PHASE` | `intro` | Phase on (re)start |
| `CONTENT_PACK_PATH` | *(unset)* | Alternate pack file inside the deployed source |
| `BRAND_NAME` / `BRAND_LOGO_URL` / `BRAND_PRIMARY_COLOR` / `EVENT_NAME` | *(unset)* | Cobranding |
| `DATABRICKS_GATEWAY_HOST` | *(unset — derives `<host>/ai-gateway`)* | Override to name a dedicated Unity AI Gateway subdomain instead of the workspace-hosted one |
| `ANTHROPIC_MODEL` / `CODEX_MODEL` | *(unset)* | Optional direct `system.ai` pins; when a CT model policy is active, each pin must be in its approved capability pool |
| `WORKSHOP_MODEL_POLICY_REQUIRED` | `false` | CT sets `true` for managed workshops; agent launch waits for a synchronized model-policy revision |
| `MAX_SESSIONS_PER_USER` / `MAX_SESSIONS_GLOBAL` | 1 / 1 | Fixed one-session admission invariant |
| `ALLOW_SHARED_TOPOLOGY` | `false` | Shared use is unsupported and fails `/readyz` |
| `SESSION_IDLE_TIMEOUT_SECONDS` | 28800 | Idle PTY reap |
| `SESSION_STATE_PATH` | `/app/python/source_code/data/sessions.json` in `app.yaml` | Mode-0600 metadata-only journal for relaunchable "ended on restart" ghosts. Raw terminal output is never persisted. `/readyz` exercises atomic sibling write/read semantics without touching the real journal |
| `ENABLE_OBO` | `false` | Persist each attendee's forwarded OBO token to a `me` CLI profile so the agent can read data **as the attendee** (`databricks-me`). Requires user authorization + `user_api_scopes` on the app resource (set by CT, not here) |
| `OBO_PROFILE_NAME` | `me` | CLI profile name backed by the OBO token |
| `OBO_SCOPES` | `catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql` | Doc/health hint; must match the scopes CT set on the app resource (the app cannot set its own scopes). No `unity-catalog` scope exists — use the granular `catalog.*:read` scopes (list UC metadata) + `sql` (query data) |
| `OMNIGENT_APP_URL` | *(unset)* | Dedicated Omnigent App URL. Empty keeps local Omnigent behavior; a production value must be HTTPS. Remote mode mirrors attendee OBO independently of `ENABLE_OBO` |
| `OMNIGENT_HOST_STABLE_RUNTIME_S` | `30` | Process runtime that resets remote-host crash backoff |
| `ENABLE_ENTITLEMENTS` | `true` in `app.yaml` | Run the SP-driven reconciler that grants the labuser access to SP-created resources (UC catalog grant + non-UC `CAN_MANAGE`) |
| `WORKSHOP_CATALOG` | *(unset)* | Dedicated per-attendee catalog. Attendee remains OWNER + `ALL PRIVILEGES`; app SP receives catalog-scoped `MANAGE`, `USE_CATALOG`, `CREATE_SCHEMA` so it can create content and read/patch only this catalog's grants. Also surfaced to the attendee shell |
| `WORKSHOP_SCHEMA` | *(unset)* | Optional default schema within `WORKSHOP_CATALOG` |
| `ENTITLEMENT_RECONCILE_INTERVAL` | `300` | Seconds between reconcile sweeps |
| `ENTITLEMENT_TRANSFER_OWNERSHIP` | `false` | Also transfer SP-created UC catalog ownership to the labuser (grant-based usability is the default) |
| `WORKSHOP_INSIGHT_CAPTURE` | `false` | Master switch for workshop insight capture. **The only feature that sends attendee-authored content off the instance**, so it stays off unless the operator has arranged consent in the event's registration terms. Off = the signal rollup, discovery endpoint, CLI helper, agent instructions and wrap harvest are all inert. See [`workshop-insight-contract.md`](./workshop-insight-contract.md) |
| `CONTROL_TOWER_URL` | *(unset)* | Control Tower app base URL for app→app help raise/lower push |
| `WORKSHOP_RUN_ID` | *(unset)* | CT lab run id (required with `WORKSHOP_UNIT_ID` for help push) |
| `WORKSHOP_UNIT_ID` | *(unset)* | CT lab unit id for this attendee instance (required with `WORKSHOP_RUN_ID` for help push) |
| `WORKSHOP_RELEASE_SHA` | *(unset)* | Immutable WT release digest used as the `workshop.release_sha` OTel resource attribute |
| `OTEL_TRACES_SAMPLER` | `always_on` | Trace sampler recommended by Databricks Apps telemetry; endpoint, protocol, service name, resource attributes, and batching remain platform-injected |
| `DISCOVERY_ENABLED` | `true` *(within capture)* | Whether the agent-elicited discovery tier runs. Subordinate to `WORKSHOP_INSIGHT_CAPTURE` — `false` keeps the derived behavioural signal and drops the conversational capture, for events where the anonymous rollup is in scope but attendee narrative isn't |
| `INSIGHT_SUMMARY_MODEL` | *(unset)* | Model service for the wrap-phase edge summary. With a live CT policy, the pin must be enabled for WT chat calls |

All env is read at runtime — Control Tower `env_overrides` and in-place
`app.yaml` edits take effect on restart with no rebuild.

For event admission, use `GET /readyz`, not `GET /healthz`. Readiness returns
HTTP 503 with machine-readable per-check state until topology, the required
`WORKSHOP_ATTENDEE_EMAIL` binding, a numeric and authoritatively verified
`WORKSHOP_APP_SP_ID`, a recent
successful app-identity OAuth validation (without `WORKSHOP_PAT`), exact installed CLI
versions, atomic journal I/O, recent attendee catalog grant/owner proof, a live
or freshly successful entitlement reconciler, actually observed OBO token
scopes, and every release pin are green. A configured `OBO_SCOPES` hint alone
never satisfies readiness. The exact create-to-ready sequence and JSON contract are in
[`control-tower-implementation.md`](./control-tower-implementation.md#machine-enforced-readiness-contract).

Readiness also carries one **soft** check, `insight_capture`, marked
`"soft": true` and excluded from the `ready` verdict. It reports what this
instance will collect (`requested`: `off`, `signal`, `signal+discovery`) and
what it actually delivered (`effective`), plus the same pair as
`expected`/`actual`/`match` under `release_manifest.insight_capture` so Control
Tower can prove months later whether capture was on for a run.

It also reports `delivery` (`pull` for any Control Tower-deployed instance),
`push_configured`, `collections`, `pending` and `dropped`. Since delivery is by
collection, a path always exists and configuration alone cannot prove the feature
works — so the check is `amber` while capture is on but nothing has collected yet,
`green` once Control Tower has collected at least once, and `red` only when
`dropped` is non-zero, meaning the buffer overflowed and events were lost for
good. It never returns 503 in any of those states: insight capture serves the
sales follow-up, and no attendee should lose a workshop to it.
