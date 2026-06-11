# Admin / steering API contract

This is the integration surface for **Control Tower** (and on-site operators)
to steer a live workshop. All endpoints live under `/api/admin/*` on the
deployed app URL.

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

## Prerequisite: user authorization on the App resource

The app uses Databricks Apps **user authorization** to mint per-attendee PATs.
Scopes are a property of the App resource, **not** `app.yaml` — the creator
must pass them at create time:

```python
from databricks.sdk.service.apps import App
w.apps.create_and_wait(App(name=name, user_api_scopes=["all-apis"]))
```

This is the one-line Control Tower change required for zero-touch CLI auth.
If it's missing the app still serves bash terminals and shows a clear banner;
agent CLI launches return 503.

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
`idle_10m`, `elapsed_gt_<minutes>`. Empty `triggers` = always shown; empty
`phases` = all phases. Nuggets are coarse-signal only — the app never
inspects terminal content.

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
PAT health, open sessions (agent, created, last activity).

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
| `ADMIN_GROUP` | `platform_admins` | Group that grants operator/admin access |
| `ACCESS_GROUP` | *(unset)* | Optional group restricting attendee access |
| `WORKSHOP_PHASE` | `intro` | Phase on (re)start |
| `CONTENT_PACK_PATH` | *(unset)* | Alternate pack file inside the deployed source |
| `BRAND_NAME` / `BRAND_LOGO_URL` / `BRAND_PRIMARY_COLOR` / `EVENT_NAME` | *(unset)* | Cobranding |
| `DATABRICKS_GATEWAY_HOST` | auto-probed | AI Gateway override for CLI model traffic |
| `ANTHROPIC_MODEL` / `CODEX_MODEL` | sensible pins | Default models for the CLIs |
| `MAX_SESSIONS_PER_USER` / `MAX_SESSIONS_GLOBAL` | 3 / 30 | Terminal caps |
| `SESSION_IDLE_TIMEOUT_SECONDS` | 3600 | Idle PTY reap |

All env is read at runtime — Control Tower `env_overrides` and in-place
`app.yaml` edits take effect on restart with no rebuild.
