# Control Tower implementation instructions

What **Control Tower (CT)** must implement to provision, configure, and operate
the Workshop Terminal app reliably at event scale — including the one grant that
makes credentials rotate (without it, attendee agent sessions return `503`).

This is the integration contract. For the live steering API (phase/broadcast/
content), see [`admin-api.md`](./admin-api.md).

---

## 0. TL;DR — the non-negotiables

| # | CT must... | Why | Failure mode if skipped |
|---|---|---|---|
| 1 | Deploy **one app instance per attendee**, single worker | PTY fds + credential + git identity are instance-wide and not uid-isolated | Attendees share one identity; security model broken |
| 2 | Grant the **app's own service principal `token CAN_USE`** at provision time | App mints 15-min rotating tokens from its auto-refreshed OAuth identity | `token/create` → 403, credential `degraded`, `POST /api/sessions` → **503** |
| 3 | Make the app SP a member of **`ADMIN_GROUP`** (default `platform_admins`) | SCIM group resolution + operator gating | Operator panel/admin API denied |
| 4 | Set **`SESSION_STATE_PATH`** to a data-volume path | Terminals survive restart as relaunchable ghosts | Blank screen + client reconnect loop after any restart |
| 5 | **Pin `AI_DEV_KIT_REF`** to a reviewed tag/SHA per event | Skills are fetched at every boot from a branch tip otherwise | Attendees run un-reviewed skills mid-event |
| 6 | Ingest the **`credential.health`** event (set `CONTROL_TOWER_INGEST_URL`/`_TOKEN`, `WORKSHOP_RUN_ID`) | Catch a bad grant hours/days ahead, not at T-0 | First sign of trouble is an attendee 503 on event day |
| 7 | *(OBO opt-in)* Enable **user authorization** + set the app's **`user_api_scopes`** (`catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql`; no `unity-catalog` scope exists), admin-grant consent, restart, set `ENABLE_OBO=true` | Agent can read data **as the attendee** (governance-faithful UC), not as the SP | `databricks --profile me` empty/401; attendee sees the SP's catalogs, not theirs |
| 8 | *(OBO opt-in)* Provision a **per-attendee catalog** (labuser OWNER + `ALL PRIVILEGES`, app SP `CREATE`/`USE`), pass it as **`WORKSHOP_CATALOG`**, set `ENABLE_ENTITLEMENTS=true` | Everything the SP builds is usable by the labuser (UC by inheritance, non-UC by sweep) | Labuser can't `SELECT`/manage what the agent created |

Items 2–3 are the ones most commonly missed and caused the live `503`s we just
debugged on labs. Items 7–8 are the OBO dual-profile feature — opt-in per event;
skip them and the app behaves exactly as before (SP-only identity).

---

## 1. Provisioning order (per attendee)

The app's service principal **does not exist until the app is created**, so the
`CAN_USE` grant must come *after* create and *before* (or shortly after) start:

```
1. Create the per-attendee workspace            (existing CT flow)
2. Create the app  ─────────────────────────────► returns service_principal_client_id
3. Grant that SP `token CAN_USE`   ◄── STEP 2 above (the critical one)
4. Add that SP to ADMIN_GROUP                     (SCIM / group membership)
5. Sync source + set env overrides + deploy/start
6. Verify credential.state == "rotating"          (acceptance gate, §5)
7. Teardown at event end (revoke, delete)          (§6)
```

Rotation **self-heals within ~30s** once the grant lands — if the app was already
started, no redeploy is needed; the background probe picks it up within one
~5-minute cycle. Grant-before-start just makes the first boot clean.

*OBO dual-profile (opt-in):* additionally enable user authorization + set
`user_api_scopes` on the app resource (admin-consent, then restart), provision
the per-attendee catalog, and set `ENABLE_OBO`/`ENABLE_ENTITLEMENTS`/
`WORKSHOP_CATALOG`. See §8–9.

---

## 2. The critical grant — app SP `token CAN_USE`

Databricks Apps OBO scopes deliberately exclude the Token API, so the app cannot
mint per-user PATs from forwarded tokens. Instead it mints rotating tokens **from
its own service-principal OAuth identity** — but only if that SP holds workspace
token-create (`CAN_USE`).

Read the app's SP client id from the create/get response field
`service_principal_client_id` (an application/client UUID, e.g.
`9945abef-8d09-45e0-b85a-7b1b05b6c6ef`), then grant **additively** (PATCH — do
not overwrite the existing `admins → CAN_MANAGE`).

**REST (PATCH = additive):**

```http
PATCH /api/2.0/permissions/authorization/tokens
Authorization: Bearer <CT workspace-admin token>
Content-Type: application/json

{
  "access_control_list": [
    { "service_principal_name": "<APP_SP_CLIENT_ID>", "permission_level": "CAN_USE" }
  ]
}
```

> Use `service_principal_name` = the SP's **application/client id**, not its
> numeric id or display name.

**CLI equivalent:**

```bash
databricks permissions update authorization tokens --profile <WS> --json '{
  "access_control_list": [
    { "service_principal_name": "<APP_SP_CLIENT_ID>", "permission_level": "CAN_USE" }
  ]
}'
```

**Python SDK equivalent:**

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.settings import (
    TokenAccessControlRequest, TokenPermissionLevel,
)

w = WorkspaceClient(profile="<WS>")  # workspace admin
w.token_management.update_token_permissions(
    access_control_list=[
        TokenAccessControlRequest(
            service_principal_name="<APP_SP_CLIENT_ID>",
            permission_level=TokenPermissionLevel.CAN_USE,
        )
    ]
)
```

The caller (CT's deployer SP) must be a workspace admin (or hold `CAN_MANAGE` on
token authorization) to set this.

### Emergency-only fallback: `WORKSHOP_PAT`

If a usable app identity is genuinely unavailable, CT may vend a workspace token
as the `WORKSHOP_PAT` env var. This is a **static credential with no rotation
behind it** — the app reports it `degraded` and alerts. If used, provision the
PAT with a lifetime well beyond deploy-to-event-end and **revoke at teardown**.
Prefer the SP grant for anything large or long-lived.

---

## 3. Identity & permissions the app SP needs

| Grant | Scope | Purpose |
|---|---|---|
| `token CAN_USE` | workspace token authorization | mint rotating attendee tokens (§2) |
| Member of `ADMIN_GROUP` (`platform_admins`) | workspace group | SCIM group resolution + operator/admin access |
| SCIM read (`/Me`, `/Users`) | workspace | resolve attendee group membership for operator gating |

The CT **deployer SP** (the one calling the admin API) additionally needs
`CAN_USE` on the app itself and membership in `ADMIN_GROUP` — see
[`admin-api.md` §Authentication](./admin-api.md#authentication--authorization).

---

## 4. Deploy-time env overrides

All env is read at runtime; CT `env_overrides` (or in-place `app.yaml` edits)
take effect on restart with **no rebuild**. Set these per instance:

| Var | Set to | Why |
|---|---|---|
| `SESSION_STATE_PATH` | `/app/python/source_code/data/sessions.json` | restart recovery (item 4) |
| `AI_DEV_KIT_REF` | a reviewed **tag or commit SHA** | freeze skills per event (item 5) |
| `WORKSHOP_RUN_ID` | CT's run id for this attendee | event attribution on ingested events |
| `DATABRICKS_WORKSPACE_ID` | the workspace id | event attribution |
| `CONTROL_TOWER_INGEST_URL` | CT ingest base URL | enable real-time event push (§5) |
| `CONTROL_TOWER_INGEST_TOKEN` | shared `X-Ingest-Token` | auth for ingest |
| `ADMIN_GROUP` | `platform_admins` (or your group) | operator gating |
| `EVENT_NAME` / `BRAND_*` | per-event branding | cobranding |
| `ANTHROPIC_MODEL` | a READY Databricks Claude serving endpoint, e.g. `databricks-claude-opus-4-8` | default Claude model for this app instance |
| `CODEX_MODEL` | a READY Databricks OpenAI-compatible serving endpoint, e.g. `databricks-gpt-5-5` | default Codex model for this app instance |
| `WORKSHOP_PAT` | **leave empty** | use the SP grant, not a static PAT |
| `ENABLE_OBO` | `true` *(opt-in)* | persist the attendee OBO token to the `me` profile (§8) — also needs user authorization + `user_api_scopes` on the app resource |
| `ENABLE_ENTITLEMENTS` | `true` *(opt-in)* | run the labuser-usability reconciler (§9) |
| `WORKSHOP_CATALOG` | per-attendee catalog name | the catalog the agent creates UC objects in; the reconciler verifies the labuser's `ALL PRIVILEGES` on it (§9) |
| `WORKSHOP_SCHEMA` | *(optional)* | default schema within `WORKSHOP_CATALOG` |

Model defaults can vary by event or attendee because CT applies overrides to
each app instance independently. For example, set
`ANTHROPIC_MODEL=databricks-claude-sonnet-5` for a standard workshop and
`ANTHROPIC_MODEL=databricks-claude-opus-4-8` for a premium workshop. The value
is a Databricks serving endpoint name, not an Anthropic public API model id.
At attendee bootstrap the app prefers the requested READY endpoint, then
degrades through its built-in model chain if endpoint discovery reports it
unavailable. Leaving the override empty preserves the app defaults.

The CT simulator accepts the same settings:

```bash
python scripts/deploy_ct_sim.py \
  --anthropic-model databricks-claude-opus-4-8 \
  --codex-model databricks-gpt-5-5
```

Leave `MAX_SESSIONS_*`, `SESSION_IDLE_TIMEOUT_SECONDS` at defaults unless the
event needs otherwise. Full table in [`admin-api.md`](./admin-api.md#deploy-time-configuration-env-vars).

---

## 5. Credential-health ingestion (early warning)

When `CONTROL_TOWER_INGEST_URL` + `CONTROL_TOWER_INGEST_TOKEN` + `WORKSHOP_RUN_ID`
are set, the app pushes events to CT. A background probe verifies the credential
end-to-end every ~5 minutes — **through idle windows** — and emits
`credential.health` on every state transition (and re-emits on a cadence while
unhealthy). This is how CT catches a missing grant *before* the event.

**Endpoint the app calls (CT must implement):**

```http
POST {CONTROL_TOWER_INGEST_URL}/api/ingest/events
X-Ingest-Token: <CONTROL_TOWER_INGEST_TOKEN>
Content-Type: application/json
```

**Event envelope:**

```json
{
  "schema_version": 1,
  "run_id": "<WORKSHOP_RUN_ID>",
  "workspace_id": "<DATABRICKS_WORKSPACE_ID>",
  "attendee": "system",
  "type": "credential.health",
  "occurred_at": "2026-06-24T03:39:34Z",
  "payload": { "state": "rotating", "error": null, "source": "app-identity" },
  "idempotency_key": "..."
}
```

- Respond **2xx** to acknowledge; non-2xx keeps the event buffered and retried
  (the buffer is bounded, drop-oldest — never blocks attendees).
- De-dupe on `idempotency_key`.
- `payload.state` ∈ `rotating` (healthy) · `degraded` (static credential, no
  rotation) · `unhealthy` (rejected/expired or nothing configured) · `unknown`.
- **Alert when `state != "rotating"`.** A `degraded`/`unhealthy` instance days
  before the event almost always means the §2 grant is missing.

Other emitted types (`session.started`, and — when OBO entitlements are on —
`entitlements.health`, payload `{ok, error, catalog}`) follow the same
envelope. CT's poll-based harvest (`GET /api/admin/stats`) remains the
reconciliation path if ingest is disabled.

---

## 6. Acceptance gate (verify before handing to the attendee)

Per instance, confirm rotation is green. Either read the app config as an
admin-group caller:

```bash
curl -s https://<app-url>/api/config \
  -H "Authorization: Bearer <admin token>" | jq .credential
# expect: { "state": "rotating", "healthy": true, "degraded": false, "source": "app-identity", ... }
```

...or check the app logs for:

```
server.credentials INFO credential healthy — rotating short-lived tokens
```

If you instead see `credential degraded: cannot mint short-lived tokens (403)`,
the §2 grant is missing or not yet applied — apply it; rotation recovers within
~30s with no redeploy.

---

## 7. Teardown

- Delete the per-attendee app + workspace per existing CT flow.
- If a `WORKSHOP_PAT` was vended (emergency path only), **revoke it** explicitly.
- App-SP rotating tokens are short-lived (15 min) and auto-expire; no manual
  revoke needed, but deleting the app removes the SP and its grants.

---

## 8. OBO dual-profile — read data as the attendee *(opt-in)*

By default the agent authenticates as the app **service principal** for
everything, so `databricks catalogs list` shows the *SP's* grants, never the
signed-in attendee's. The OBO dual-profile feature adds a second CLI profile
(`me`) backed by the attendee's forwarded **on-behalf-of-user token**, so the
agent can read exactly the Unity Catalog the attendee is governed by
(`databricks --profile me ...`, via the `databricks-me` helper). The SP
`[DEFAULT]` profile is unchanged and still powers all build/deploy/provision
work (reliable for long, idle, cross-1h runs — OBO is tab-bound and can't be).

**Scopes live on the app resource, not in `app.yaml`.** Per attendee workspace:

1. **Enable user authorization** for Apps (workspace admin: *Settings →
   Development → Apps*; default "All APIs").
2. **Declare the app's `user_api_scopes`** — baseline
   **`catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql`**:
   the granular `catalog.*:read` scopes let the agent list the attendee's
   catalogs/schemas/tables (UC metadata), and `sql` queries the attendee's
   UC-governed data via a warehouse. There is **no `unity-catalog` scope** — use
   the granular `catalog.*:read` ones. UI: the app's *User authorization → Add
   scope*; IaC/API: the app resource `user_api_scopes` field. Keep it
   least-privilege — do **not** add `apps`/`jobs`/`clusters`, the SP handles
   those. Other valid scopes (add only if the workshop needs them):
   `genie`, `postgres`, `model-serving`, `files`, `ai-gateway`,
   `vector-search`, `workspace.workspace`, `catalog.connections`,
   `mcp.external`, `mcp.functions`.
   ⚠️ Invalid scope strings (e.g. `unity-catalog`) are rejected at app
   create/update time: *"The specified scope … is not a valid scope."*
3. **Admin-grant consent** on the attendee's behalf for a zero-touch UX;
   otherwise each attendee sees a one-time consent screen on first use.
4. **Restart the app** after any scope change (scopes only take effect on
   restart and re-trigger consent). Note `databricks apps update` does a full
   replacement — re-apply `user_api_scopes` on every deploy or they get wiped.
5. Set **`ENABLE_OBO=true`** (and
   `OBO_SCOPES=catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql`
   to match, for health reporting only).

This is **additive** to the SP grants in §2–3 (which keep powering `[DEFAULT]`
and model traffic). Verify per instance: `GET /api/config` → `obo.fresh == true`
while a tab is open, and `databricks --profile me current-user me` returns the
attendee email (not the SP). `obo.present == false` with a tab open ⇒ scopes or
consent missing.

The labuser identity used everywhere (UC grants, presence) is the attendee
email from `X-Forwarded-Email`.

## 9. Namespace provisioning + labuser usability *(opt-in)*

Because the SP (not the labuser) creates resources, the labuser would have no
access to them. Provision a per-attendee catalog so usability is automatic:

- **Create a per-attendee catalog** with the **labuser as `OWNER`** and
  **`ALL PRIVILEGES` granted to the labuser**, and the **app SP granted
  `CREATE` + `USE`**. UC privileges inherit downward, so this single grant makes
  every schema/table/volume the SP later creates inside the catalog instantly
  usable by the labuser — and visible via the `me`/OBO profile.
- **Pass the catalog name as `WORKSHOP_CATALOG`** (optional `WORKSHOP_SCHEMA`).
  The agent is instructed to create UC objects only inside it.
- Set **`ENABLE_ENTITLEMENTS=true`**. The app then, on a cadence and on demand
  (`workshop-grant-me`): re-verifies/re-applies the labuser's `ALL PRIVILEGES`
  on `WORKSHOP_CATALOG` (defense-in-depth even though CT set it up), and sweeps
  **non-UC** resources (apps, jobs, pipelines, database instances, serving
  endpoints), granting the labuser `CAN_MANAGE` (these don't inherit). All calls
  run as the app SP, are idempotent, and emit `entitlements.health` on failure.

Grant payloads the app issues (the app SP needs permission to make these — i.e.
it must own/manage the catalog and the swept resources):

```http
PATCH /api/2.1/unity-catalog/permissions/catalog/<WORKSHOP_CATALOG>
{ "changes": [ { "principal": "<attendee-email>", "add": ["ALL_PRIVILEGES"] } ] }

PATCH /api/2.0/permissions/{jobs|pipelines|serving-endpoints|apps|database-instances}/<id>
{ "access_control_list": [ { "user_name": "<attendee-email>", "permission_level": "CAN_MANAGE" } ] }
```

Verify: have the agent create a schema+table in `$WORKSHOP_CATALOG` and an
app+Lakebase instance (as `DEFAULT`/SP), then confirm `databricks --profile me`
can `SELECT` the table and the labuser has `CAN_MANAGE` on the app/instance
(immediately after `workshop-grant-me`, and after a sweep with no inline call).
`GET /api/admin/presence` → `entitlements.ok == true`.

## Appendix — quick reference

- App SP client id: `service_principal_client_id` from `apps create` / `apps get`.
- Critical grant: `PATCH /api/2.0/permissions/authorization/tokens` →
  `{service_principal_name: <client_id>, permission_level: CAN_USE}` (additive).
- Health states: `rotating` (good) · `degraded` · `unhealthy` · `unknown`.
- Recovery after grant: ~30s, no redeploy.
- Ingest endpoint: `POST {INGEST_URL}/api/ingest/events`, header
  `X-Ingest-Token`, ack with 2xx, de-dupe on `idempotency_key`.
