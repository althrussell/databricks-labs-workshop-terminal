# Admin / steering API contract

This is the integration surface for **Control Tower** (and on-site operators)
to steer a live workshop. All endpoints live under `/api/admin/*` on the
deployed app URL.

> **Provisioning the app?** See
> [`control-tower-implementation.md`](./control-tower-implementation.md) for the
> per-attendee setup contract — especially the app-SP `token CAN_USE` grant that
> makes credentials rotate (without it, agent sessions return `503`).

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

Databricks Apps OBO scopes deliberately exclude the Token API (verified
June 2026: no token-related value exists in the `user_api_scopes` registry),
so the app cannot mint per-user PATs from forwarded tokens. There are two ways
to supply the credential; **the first is strongly preferred for any large or
long-lived event** (e.g. deployed days before, idle, then used on the day):

### Preferred: grant the app service principal token `CAN_USE`

At provision time, grant the app's own service principal token-create
(`CAN_USE`) in the workspace. The app then mints **15-minute rotating tokens
from its service-principal OAuth identity** — which the platform auto-refreshes
— and fans them out to attendee CLIs. There is **no static secret in `app.yaml`
and nothing to expire across an idle window**, and no attendee ever creates a
PAT. Rotation self-heals within ~30s once the grant lands (no redeploy needed).
Attendees act as the per-instance service principal (acceptable under the
one-workspace-per-attendee topology).

### Emergency-only: vended `WORKSHOP_PAT`

For environments without a usable app identity, Control Tower may vend a
workspace token as `WORKSHOP_PAT` (via `env_overrides` / in-place `app.yaml`
edit, or an app secret resource with `valueFrom`).

- A vended PAT is a **static credential that expires with no rotation behind
  it** — a time bomb for events deployed ahead of time. If the PAT can itself
  call `/api/2.0/token/create`, the app will chain rotating tokens off it; if
  not, it serves the PAT directly and reports the credential **`degraded`**.
- Provision the PAT with a lifetime comfortably exceeding deploy-to-event-end,
  and watch the credential health (below). Control Tower should revoke it at
  teardown.
- If `WORKSHOP_PAT` is missing **and** the app identity can't mint, the app
  still serves bash terminals and shows a clear banner; agent CLI launches
  return 503.

### Credential health and alerting

A background probe verifies the credential end-to-end on a ~5-minute cadence —
through idle windows too — and classifies it as `rotating` (healthy),
`degraded` (a static credential is being served, no rotation), or `unhealthy`
(the credential was rejected/expired or nothing is configured). The state is
exposed in `credential` on `GET /api/config` and `GET /api/admin/presence`,
surfaced as an operator banner, and emitted to Control Tower as a
`credential.health` event so a misconfigured grant or an expiring credential is
caught hours/days before the event, not at first use on the day.

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

### `GET /api/admin/stats`
Harvest endpoint for Control Tower's event-impact capture: per-attendee
build stats (`email`, `minutes_building`, `agent_sessions`,
`terminal_sessions`, `topics`, `code` {projects, commits, files, lines}),
one instance-level workspace resource census, and `instance`
{phase, started_at, session_count}. Code stats are cached ~5 minutes, so
periodic polling is cheap. Control Tower persists snapshots into its
Lakebase for reporting that survives workspace teardown.

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
| `WORKSHOP_PAT` | *(unset)* | Emergency-only vended workspace credential. Prefer granting the app SP token `CAN_USE` so it mints rotating tokens from its OAuth identity (no expiry clock); a static PAT is reported `degraded` |
| `ADMIN_GROUP` | `platform_admins` | Group that grants operator/admin access |
| `LAB_COACH` | `true` | Append lab-coach instructions to attendee agent memory |
| `TOPIC_DETECTION` | `true` | Terminal keyword spotting for contextual insights |
| `AI_DEV_KIT_REPO` | github databricks-solutions/ai-dev-kit | Skills source fetched latest at every boot |
| `AI_DEV_KIT_REF` | `main` | Git ref (tag/branch/SHA) for the skills overlay. **Pin a reviewed tag/SHA per event** so attendees run a known skills version (incl. the AppKit-default app skill) instead of the branch tip at boot |
| `DEEPWIKI_MCP_URL` / `EXA_MCP_URL` | public endpoints | MCP servers for attendee agents (empty string disables) |
| `ACCESS_GROUP` | *(unset)* | Optional group restricting attendee access |
| `WORKSHOP_PHASE` | `intro` | Phase on (re)start |
| `CONTENT_PACK_PATH` | *(unset)* | Alternate pack file inside the deployed source |
| `BRAND_NAME` / `BRAND_LOGO_URL` / `BRAND_PRIMARY_COLOR` / `EVENT_NAME` | *(unset)* | Cobranding |
| `DATABRICKS_GATEWAY_HOST` | auto-probed | AI Gateway override for CLI model traffic |
| `ANTHROPIC_MODEL` / `CODEX_MODEL` | sensible pins | Default models for the CLIs |
| `MAX_SESSIONS_PER_USER` / `MAX_SESSIONS_GLOBAL` | 3 / 30 | Terminal caps |
| `SESSION_IDLE_TIMEOUT_SECONDS` | 3600 | Idle PTY reap |
| `SESSION_STATE_PATH` | *(unset)* | Journal path so terminals survive a restart as relaunchable "ended on restart" ghosts. **Recommended for real events** |
| `ENABLE_OBO` | `false` | Persist each attendee's forwarded OBO token to a `me` CLI profile so the agent can read data **as the attendee** (`databricks-me`). Requires user authorization + `user_api_scopes` on the app resource (set by CT, not here) |
| `OBO_PROFILE_NAME` | `me` | CLI profile name backed by the OBO token |
| `OBO_SCOPES` | `catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql` | Doc/health hint; must match the scopes CT set on the app resource (the app cannot set its own scopes). No `unity-catalog` scope exists — use the granular `catalog.*:read` scopes (list UC metadata) + `sql` (query data) |
| `ENABLE_ENTITLEMENTS` | `false` | Run the SP-driven reconciler that grants the labuser access to SP-created resources (UC catalog grant + non-UC `CAN_MANAGE`) |
| `WORKSHOP_CATALOG` | *(unset)* | Per-attendee catalog the agent creates UC objects in; the reconciler verifies/re-applies the labuser's `ALL PRIVILEGES` on it. Also surfaced to the attendee shell |
| `WORKSHOP_SCHEMA` | *(unset)* | Optional default schema within `WORKSHOP_CATALOG` |
| `ENTITLEMENT_RECONCILE_INTERVAL` | `300` | Seconds between reconcile sweeps |
| `ENTITLEMENT_TRANSFER_OWNERSHIP` | `false` | Also transfer SP-created UC catalog ownership to the labuser (grant-based usability is the default) |

All env is read at runtime — Control Tower `env_overrides` and in-place
`app.yaml` edits take effect on restart with no rebuild.
