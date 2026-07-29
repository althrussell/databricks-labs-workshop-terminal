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

### `POST /api/admin/broadcast`
```json
{ "message": "Labs close in 10 minutes — commit your work!", "level": "warning", "ttl_s": 600 }
```
Shows a banner on every connected attendee screen for `ttl_s` seconds.
Levels: `info`, `success`, `warning`.

### `GET /api/admin/presence`
Per-attendee status: online (active in the last 60 s), first/last seen,
credential health, open sessions (agent, created, last activity), and per
attendee `obo` (OBO/`me`-profile freshness — see below). The top level also
carries `credential` and `entitlements` status blocks.

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
  "last_reconcile": 1750000000, "last_error": null, "interval": 300 }
```

`ok: false` with a `last_error` means a grant is failing (commonly the app SP
lacks permission on `WORKSHOP_CATALOG`, or the catalog name is wrong). The
reconciler also emits an `entitlements.health` event to Control Tower (same
envelope as `credential.health`) so a drifted/missing grant alerts ahead of the
event.

### App callback endpoints (not admin-gated)

Two small endpoints back the in-terminal helper scripts; both accept an
`{"email": "<attendee>"}` body so a PTY helper (no proxy identity) can call
them, and both no-op cleanly when the feature is disabled:

- `POST /api/obo/refresh` — force-writes the freshest captured OBO token to the
  `me` profile and nudges the tab (the `databricks-me` 401 self-heal path).
- `POST /api/entitlements/reconcile` — runs an immediate entitlement reconcile
  for the attendee (the `workshop-grant-me` path), returning the per-resource
  grant summary.

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
{phase, started_at, session_count}. Code stats are cached ~5 minutes, so
periodic polling is cheap. Control Tower persists snapshots into its
Lakebase for reporting that survives workspace teardown.

### `GET /api/admin/omnigent-host-readiness`
Admin/SP-authenticated, token-free readiness used by Control Tower alongside
stats collection. It returns the exact local supervisor `status`, `connected`,
and `expected_host_id`; `host_id` and `last_seen_at` appear only after a fresh
attendee-owned bearer verifies `GET /v1/hosts/{expected_host_id}` as `online`.
`last_seen_at` is the UTC timestamp when Workshop Terminal completed that
successful verification; upstream v0.7.0's host response has no last-seen field.
Network/auth/offline/mismatch results remain disconnected and never expose the
bearer.

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

## Deploy-time configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `WORKSHOP_PAT` | *(unset)* | Emergency-only vended workspace credential. Normal mode uses direct `app_identity_oauth`; a static PAT is always reported `degraded` |
| `WORKSHOP_APP_SP_ID` | *(unset; required for `/readyz`)* | Numeric SCIM `service_principal_id` returned by app create/get. Control Tower patches the uploaded `app.yaml` after app creation and before deploy; SCIM `/Me` must match this ID together with `DATABRICKS_CLIENT_ID` in `userName` when `applicationId` is absent |
| `ADMIN_GROUP` | `platform_admins` | Group that grants operator/admin access |
| `LAB_COACH` | `true` | Append lab-coach instructions to attendee agent memory |
| `TOPIC_DETECTION` | `true` | Terminal keyword spotting for contextual insights |
| `SKILLS_REPO` | github databricks/databricks-agent-skills | Skills source; event use is constrained by the reviewed artifact manifest |
| `SKILLS_REF` | empty in `app.yaml` | Exact reviewed tag/SHA for the skills overlay; must match the manifest ref, commit, and content SHA-256 |
| `ARTIFACT_MANIFEST_PATH` | empty | Optional mirror override for the repo-owned contract in `assets/artifacts/manifest.json`; may redirect `source` only, and a version/checksum override is rejected |
| `CLAUDE_CODE_VERSION` | `2.1.216` in `app.yaml` | Exact reviewed Claude Code CLI release candidate |
| `CODEX_CLI_VERSION` | `0.144.6` in `app.yaml` | Exact reviewed Codex CLI release candidate |
| `OMNIGENT_VERSION` | `0.7.0` in `app.yaml` | Exact reviewed Omnigent release candidate, matched to the dedicated App protocol |
| `DATABRICKS_CLI_VERSION` | `1.8.0` in `app.yaml` | Exact reviewed Databricks CLI release input |
| `DEEPWIKI_MCP_URL` / `EXA_MCP_URL` | public endpoints | MCP servers for attendee agents (empty string disables) |
| `ACCESS_GROUP` | *(unset)* | Optional group restricting attendee access |
| `WORKSHOP_ATTENDEE_EMAIL` | *(unset; required for `/readyz`)* | Control-Tower-injected email assigned to this one app instance. A different attendee receives HTTP 403 / WebSocket 4403 unless `ALLOW_SHARED_TOPOLOGY=true`. Admin service-principal routes remain group-authorized and independent of this binding |
| `WORKSHOP_PHASE` | `intro` | Phase on (re)start |
| `CONTENT_PACK_PATH` | *(unset)* | Alternate pack file inside the deployed source |
| `BRAND_NAME` / `BRAND_LOGO_URL` / `BRAND_PRIMARY_COLOR` / `EVENT_NAME` | *(unset)* | Cobranding |
| `DATABRICKS_GATEWAY_HOST` | auto-probed | AI Gateway override for CLI model traffic |
| `ANTHROPIC_MODEL` / `CODEX_MODEL` | *(unset)* | Required event release pins for the CLI model endpoints; `/readyz` stays red until both are explicit |
| `MAX_SESSIONS_PER_USER` / `MAX_SESSIONS_GLOBAL` | 3 / 3 | Terminal caps; global must not exceed per-user for the one-attendee topology |
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
