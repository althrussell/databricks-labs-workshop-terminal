# Control Tower implementation instructions

What **Control Tower (CT)** must implement to provision, configure, and operate
the Workshop Terminal app reliably at event scale, including the post-deploy
proof that Databricks Apps supplied healthy direct app-identity OAuth.

This is the integration contract. For the live steering API (phase/broadcast/
content), see [`admin-api.md`](./admin-api.md).

---

## 0. TL;DR — the non-negotiables

| # | CT must... | Why | Failure mode if skipped |
|---|---|---|---|
| 1 | Deploy **one app instance per attendee**, single worker | PTY fds + credential + git identity are instance-wide and not uid-isolated | Attendees share one identity; security model broken |
| 2 | Require healthy direct app-SP OAuth after deploy (`source=app_identity_oauth`, recent `state=rotating`) | Databricks Apps injects auto-refreshing OAuth; no token ACL/PAT mint is needed | `/readyz` stays red and coding-agent launches return **503** |
| 3 | Make the app SP a member of **`ADMIN_GROUP`** (default `platform_admins`) | SCIM group resolution + operator gating | Operator panel/admin API denied |
| 4 | Set **`SESSION_STATE_PATH`** to a data-volume path | Terminals survive restart as relaunchable ghosts | Blank screen + client reconnect loop after any restart |
| 5 | Nothing. Leave **`SKILLS_REF`** unset | The reviewed tag, commit, and content digest ship in the repo's own `assets/artifacts/manifest.json` | Setting a ref that the manifest does not pin fails the skills install closed |
| 6 | Collect events on every harvest (`GET /api/admin/insight-events`) and act on **`credential.health`** | Catch a bad grant hours/days ahead, not at T-0. The Apps proxy blocks a token-only push, so collection is the delivery path | First sign of trouble is an attendee 503 on event day |
| 7 | *(OBO opt-in)* Enable **user authorization** + set the app's **`user_api_scopes`** (`catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql`; no `unity-catalog` scope exists), admin-grant consent, restart, set `ENABLE_OBO=true` | Agent can read data **as the attendee** (governance-faithful UC), not as the SP | `databricks --profile me` empty/401; attendee sees the SP's catalogs, not theirs |
| 8 | *(OBO opt-in)* Reuse a matching dedicated catalog when FEVM already provisioned it; otherwise create it. Ensure labuser OWNER + `ALL PRIVILEGES` + `MANAGE`; app SP catalog-scoped `MANAGE`, `USE_CATALOG`, `CREATE_SCHEMA`; pass **`WORKSHOP_CATALOG`** and set `ENABLE_ENTITLEMENTS=true` | Works without `CREATE CATALOG` entitlement while preserving exact scoped grants | Reconciliation cannot verify/reapply grants, or the labuser/app SP cannot use created objects |
| 9 | Gate admission on **`GET /readyz` returning 200** | The deep gate proves topology, credentials, installers, persistence, catalog, entitlements, OBO, and release pins together | A process can be live at `/healthz` while unsafe or incomplete for attendees |
| 10 | Inject **`WORKSHOP_ATTENDEE_EMAIL`** with the attendee assigned to this instance | Binds every attendee HTTP/WebSocket request to the Control Tower assignment | A second attendee is denied with 403/4403 instead of sharing instance-wide credentials |
| 11 | After app create/get, patch uploaded `app.yaml` with numeric **`WORKSHOP_APP_SP_ID=service_principal_id`** before every new or existing-app deploy | Live SCIM `/Me` identifies the app with `userName=<client UUID>` plus numeric `id`, but may omit `applicationId` | `/readyz` stays red because app-SP OAuth cannot be authoritatively bound |

Items 2–3 are the credential/admin prerequisites most likely to block launches.
Items 7–8 are the OBO dual-profile feature — opt-in per event;
skip them and the app behaves exactly as before (SP-only identity), but the
event-readiness gate remains red. **Control Tower is implemented externally to
this repository**; in this phase its integration must validate two independently
provisioned attendee instances end to end before releasing a fleet.

---

## 1. Provisioning order (per attendee)

The app's service principal **does not exist until the app is created**.
Databricks Apps then injects and refreshes its OAuth credentials:

```
1. Create or get the per-attendee workspace and app
   └─ app create/get returns service_principal_client_id and numeric service_principal_id
2. Patch the already-uploaded app.yaml with WORKSHOP_APP_SP_ID=service_principal_id,
   then configure groups, OBO scopes, and every reviewed release pin
3. Detect/reuse an existing dedicated catalog; create only when absent
4. Ensure attendee OWNER + ALL PRIVILEGES + MANAGE and app-SP scoped grants
5. Keep WORKSHOP_PAT empty (unless explicitly accepting degraded emergency mode)
6. Sync source, deploy, and start the app (one Uvicorn worker; documented
   auto-injected `UVICORN_HOST`/`UVICORN_PORT`, with no explicit host/port args)
7. Verify direct OAuth through the admin credential status, then poll GET /readyz
   until HTTP 200; HTTP 503 blocks attendee admission
8. Validate the same contract on a second independent instance
9. Teardown at event end (revoke and delete)
```

OAuth acquisition and validation repeat every ~5 minutes and on demand after
idle. Live agents re-read the shared bearer file every four minutes.

In the observed failed deployment, list-form `${DATABRICKS_APP_PORT}` reached
Uvicorn literally and was rejected as an invalid integer. This app therefore
keeps only `--workers 1` in `app.yaml` and deliberately relies on the documented
auto-injected `UVICORN_HOST=0.0.0.0` and `UVICORN_PORT`.

*OBO dual-profile (opt-in):* additionally enable user authorization + set
`user_api_scopes` on the app resource (admin-consent, then restart), provision
the per-attendee catalog, and set `ENABLE_OBO`/`ENABLE_ENTITLEMENTS`/
`WORKSHOP_CATALOG`. See §8–9.

### Machine-enforced readiness contract

`GET /healthz` is liveness only: it returns `200 {"status":"ok"}` whenever the
web process can answer. It must not be used to admit attendees.

`GET /readyz` is the release gate. It returns JSON with top-level `status`
(`ready` or `not_ready`), boolean `ready`, and a `checks` object. Every check
contains boolean `ok`, `state` (`green` or `red`), a non-secret `detail`, and
check-specific metadata. It returns HTTP 200 only when every hard check is
green; otherwise it returns the complete report with HTTP 503.

The hard checks are:

1. `topology`: shared topology is off and
   `MAX_SESSIONS_GLOBAL <= MAX_SESSIONS_PER_USER`.
2. `attendee_identity`: a valid attendee identity is bound to the instance. The
   check reports the binding `source`: `control-tower` when Control Tower
   injected `WORKSHOP_ATTENDEE_EMAIL`, `self-bound` when the instance bound
   itself to its first non-operator caller, or `unbound`. Attendee routes
   enforce an exact normalized-email match against the binding regardless of
   source; admin service principals continue to use `ADMIN_GROUP` authorization.
   Injecting `WORKSHOP_ATTENDEE_EMAIL` remains strongly preferred, because
   self-binding cannot distinguish the intended attendee from whoever opens the
   app first.
3. `credentials`: state is `rotating`, source is `app_identity_oauth`,
   `WORKSHOP_PAT` is absent, and successful OAuth validation occurred within the
   bounded freshness window. JWT expiry is tracked when inspectable; a stale
   validation or static fallback is not release-ready.
4. `app_sp_binding`: `WORKSHOP_APP_SP_ID` is numeric and the latest safe OAuth
   diagnostic proves the same numeric SCIM ID with the expected app client UUID.
   A missing, non-numeric, mismatched, or unverified ID is not release-ready.
5. `installers`: Node, Claude, Codex, Databricks CLI, skills, and—when
   enabled—tmux and Omnigent have completed installation. The release manifest
   reports each enabled CLI's expected and observed version, and readiness
   requires an exact match.
6. `session_state`: `SESSION_STATE_PATH` is a file destination whose parent
   supports mode-0600 SessionMetadataStore-style atomic write/read. The journal
   contains restart metadata only—never terminal output. The probe uses and
   removes a disposable sibling; it never mutates the real journal.
7. `catalog`: `WORKSHOP_CATALOG` matches a recent successful read-after-patch
   proof that the attendee is owner and has `ALL_PRIVILEGES`.
8. `entitlements`: reconciliation is enabled and healthy, has recent
   attendee/catalog proof, and either its background thread is alive or a
   fresh on-demand reconcile succeeded. Empty-attendee runs fail closed.
9. `obo`: OBO is enabled, the configured hint includes the required scopes,
   and a trusted proxy-forwarded attendee token has actually exposed
   `catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql` in its
   JWT `scope`/`scp` claim. The profile must be present and fresh, and that scope
   observation must be no older than 300 seconds. Before a token is observed—or
   after it expires/goes stale—HTTP remains 503.
10. `release_pins`: `SKILLS_REF` is not a branch tip; exact Claude Code,
   Codex CLI, Databricks CLI, and Node version pins are present, plus Omnigent
   and Pi CLI when Omnigent is enabled; and each of those pins equals the version
   bootstrap actually installed. Both halves are needed — raising a pin does not
   reinstall anything, so a pin alone would report a version no attendee is
   running. Bootstrap also resolves the fetched `SKILLS_REF`
   to a commit, checksums installed content, and records source `network` or
   `prewarmed`. A vendored fallback keeps the app usable after a network failure
   but never claims the configured ref was installed and keeps readiness red.

   Model names are not release pins. `WORKSHOP_MODEL_PROFILE` names an event's
   cost posture and role chains resolve it against the endpoints a workspace
   actually serves, so `ANTHROPIC_MODEL` and `CODEX_MODEL` are optional
   overrides. Requiring one would copy a chain head into every deployment to go
   stale there. The soft `model_profile` check reports the active profile and
   any pins that are set.

Three **soft** checks ride alongside them, each carrying `"soft": true` and each
excluded from the `ready` verdict. CT should surface a soft `red` to the
operator, but must never treat any of them as an admission failure.

`model_gateway` reports how the AI Gateway resolved. Amber means no gateway, or
one in a shape Omnigent does not route through: every CLI then falls back to
`<host>/serving-endpoints`, which serves every model an attendee needs, so this
is never a reason to fail an instance. What the fallback costs is governance —
gateway policy, usage tracking and rate limits — which is why it is reported at
all. Setting `DATABRICKS_GATEWAY_HOST` to `https://<workspace-host>/ai-gateway`
turns it green.

`model_profile` reports the active `WORKSHOP_MODEL_PROFILE` and any model pins
that are set. Amber means the deployment asked for a profile name this release
does not know, so the default is in force — the one case where what an operator
asked for and what they got differ.

`insight_capture`
reports what this
instance collects (`requested`) versus what it can deliver (`effective`), with
the same pair mirrored as `expected`/`actual`/`match` under
`release_manifest.insight_capture`. Its purpose is provenance: "was insight
capture on for this run" is a question asked long after the event, by someone
reading an Account Manager Brief.

It also reports `delivery` (always `pull` for a CT-deployed instance),
`collections`, `pending` and `dropped`. Since delivery is by collection, a path
always exists and configuration cannot prove the feature works — so the check is
`amber` while capture is on but nothing has collected yet, `green` once CT has
collected at least once, and `red` only if `dropped` is non-zero, meaning the
buffer overflowed and events were lost for good. See
[§14](#14-workshop-insight-capture-opt-in-per-event).

The report and release manifest never include token or secret values. CT should poll with bounded
backoff during installer/reconciler startup, fail the instance on persistent
503, and retain the per-check report as release evidence. For this phase,
exercise the full create-to-ready sequence on **two instances**; a single green
instance does not validate the external Control Tower implementation.

---

## 2. The credential contract — direct app-identity OAuth

Live discovery showed token authorization endpoints return
`tokens tokens does not exist` even where `enableTokensConfig=true`.
Token ACLs and a 15-minute workspace-PAT cap are therefore not prerequisites.

Databricks Apps injects app-SP OAuth client credentials and refreshes them.
The app calls its explicit OAuth-M2M client's `config.authenticate()`, validates
the returned bearer first with the lower-privilege
`GET /api/2.0/current-user/me` first. An exact `applicationId` or
`application_id == DATABRICKS_CLIENT_ID` is authoritative only when the same
payload's numeric `id == WORKSHOP_APP_SP_ID`. When the application-ID field is
absent or the endpoint is unavailable, SCIM `/Me` may instead prove the
conjunction `userName == DATABRICKS_CLIENT_ID` **and**
`id == WORKSHOP_APP_SP_ID`, where the expected SP ID must be numeric.
`userName` alone is never accepted. Missing, non-numeric, or mismatched expected
or observed IDs are rejected. Credential status retains only safe expected and
observed IDs, endpoint statuses, and allowlisted identity fields—never the
bearer or response body. The app tracks JWT `exp` when
inspectable and distributes only fresh/changed bearers. The app client secret
never enters attendee-readable env, shell, or files.

### Emergency-only fallback: `WORKSHOP_PAT`

If a usable app identity is genuinely unavailable, CT may vend a workspace token
as the `WORKSHOP_PAT` env var. This is a **static credential with no rotation
behind it** — the app reports it `degraded` and alerts. If used, provision the
PAT with a lifetime well beyond deploy-to-event-end and **revoke at teardown**.
It is never considered rotating and is never used to create another PAT.

---

## 3. Identity & permissions the app SP needs

| Grant | Scope | Purpose |
|---|---|---|
| `MANAGE`, `USE_CATALOG`, `CREATE_SCHEMA` | only the attendee's dedicated catalog | GET/PATCH its grants and create workshop schemas; add narrower object-create privileges only when the content requires them |
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
| `SESSION_STATE_PATH` | `/app/python/source_code/data/sessions.json` | metadata-only restart recovery (item 4); raw terminal output remains in memory |
| `WORKSHOP_ATTENDEE_EMAIL` | the attendee email assigned to this instance | fail-closed attendee identity binding; required by `/readyz` |
| `WORKSHOP_APP_SP_ID` | numeric `service_principal_id` from app create/get | authoritative SCIM `/Me` app binding; patch the uploaded `app.yaml` after app creation and before deploy |
| `SKILLS_REF` | leave unset | the reviewed tag ships in `assets/artifacts/manifest.json` (item 5) |
| `CLAUDE_CODE_VERSION` | `2.1.216` release candidate | reviewed Claude Code CLI release |
| `CODEX_CLI_VERSION` | `0.144.6` release candidate | reviewed Codex CLI release |
| `OMNIGENT_VERSION` | `0.7.0` release candidate | reviewed Omnigent release matched to the dedicated App protocol |
| `DATABRICKS_CLI_VERSION` | reviewed exact version | make the Databricks CLI input explicit |
| `ANTHROPIC_MODEL` / `CODEX_MODEL` | reviewed endpoint names | prevent model drift between instances |
| `WORKSHOP_RUN_ID` | CT's run id for this attendee | event attribution on ingested events |
| `DATABRICKS_WORKSPACE_ID` | the workspace id | event attribution |
| `CONTROL_TOWER_INGEST_URL` | *(optional)* CT ingest base URL | enable the additive push path (§5); unnecessary for delivery, which is by collection |
| `CONTROL_TOWER_INGEST_TOKEN` | *(optional)* shared `X-Ingest-Token` | auth for the push path only |
| `ADMIN_GROUP` | `platform_admins` (or your group) | operator gating |
| `EVENT_NAME` / `BRAND_*` | per-event branding | cobranding |
| `WORKSHOP_PAT` | **leave empty** | use direct app-identity OAuth, not a static PAT |
| `ENABLE_OBO` | `true` *(opt-in)* | persist the attendee OBO token to the `me` profile (§8) — also needs user authorization + `user_api_scopes` on the app resource |
| `ENABLE_ENTITLEMENTS` | `true` *(opt-in)* | run the labuser-usability reconciler (§9) |
| `WORKSHOP_CATALOG` | per-attendee catalog name | the catalog the agent creates UC objects in; the reconciler verifies the labuser's `ALL PRIVILEGES` on it (§9) |
| `WORKSHOP_SCHEMA` | *(optional)* | default schema within `WORKSHOP_CATALOG` |
| `WORKSHOP_INSIGHT_CAPTURE` | `true` *(CT fleet default; per-run opt-out)* | turn on workshop insight capture (§14). This is the one switch that sends attendee-authored content to CT, so a run whose registration terms don't cover it must opt out — see §14 |
| `DISCOVERY_ENABLED` | `false` *(optional, within capture)* | drop the conversational discovery tier while keeping the derived behavioural signal |
| `WORKSHOP_TOOLCHAIN_MIRROR_PATH` | *(optional)* `/Volumes/<catalog>/<schema>/<volume>` | serve the pinned toolchain from a staged UC Volume instead of the public internet (§15). Empty = download from source, which needs no volume and is the default |
| `WORKSHOP_TOOLCHAIN_MIRROR_STRICT` | `false` | fail an artifact the mirror cannot serve instead of falling back to the internet. Air-gapped events only |

Model defaults can vary by event or attendee because CT applies overrides to
each app instance independently. For example, set
`ANTHROPIC_MODEL=databricks-claude-sonnet-5` for a standard workshop and
`ANTHROPIC_MODEL=databricks-claude-opus-4-8` for a premium workshop. The value
is a Databricks serving endpoint name, not an Anthropic public API model id.
At attendee bootstrap the app prefers the requested READY endpoint, then
degrades through its built-in model chain if endpoint discovery reports it
unavailable. Leaving the override empty preserves the app defaults.

Keep `MAX_SESSIONS_GLOBAL <= MAX_SESSIONS_PER_USER` and
`ALLOW_SHARED_TOPOLOGY=false`; the release contract is one attendee per app
instance. Full table in
[`admin-api.md`](./admin-api.md#deploy-time-configuration-env-vars).

---

## 5. Credential-health ingestion (early warning)

A background probe verifies the credential end-to-end every ~5 minutes —
**through idle windows** — and emits `credential.health` on every state
transition (and re-emits on a cadence while unhealthy). This is how CT catches a
missing grant *before* the event.

**These events are buffered and collected, like every other event.** The app
always buffers regardless of configuration; CT reads the buffer on its harvest
via `GET /api/admin/insight-events` (see
[§14](#14-workshop-insight-capture-opt-in-per-event)). This is the delivery path
to rely on.

**The push path below requires a Databricks identity on the request.** Setting
`CONTROL_TOWER_INGEST_URL` + `CONTROL_TOWER_INGEST_TOKEN` + `WORKSHOP_RUN_ID`
starts a background flusher, but if CT is itself a Databricks App its proxy
rejects a request carrying only `X-Ingest-Token` before it reaches CT's code. It
works for a CT reachable without the Apps proxy, or for a caller that also
presents an OAuth bearer for a principal granted `CAN USE` on the CT app. Push
is therefore optional and additive; the envelope below is the canonical one
either way.

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
# expect: { "state": "rotating", "healthy": true, "degraded": false, "source": "app_identity_oauth", ... }
```

...or check the app logs for:

```
server.credentials INFO credential healthy — rotating short-lived tokens
```

If you instead see `credential degraded`, the explicit emergency
`WORKSHOP_PAT` fallback is active. If OAuth validation is stale or rejected,
the state is `unhealthy`; inspect Databricks Apps app-identity authentication.

---

## 7. Teardown

- Delete the per-attendee app + workspace per existing CT flow.
- If a `WORKSHOP_PAT` was vended (emergency path only), **revoke it** explicitly.
- App-SP OAuth access bearers expire naturally; no token-delete call is needed.
  Deleting the app removes the SP and its scoped grants.

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
consent missing. OBO cannot refresh while there is no browser request, because
Apps exposes no refresh token to the app. Recovery is automatic when the
attendee returns to the tab: focus, visible-state, or socket reconnect forces an
authenticated config request through the Apps proxy and captures its fresh
forwarded user token; no manual page refresh is required.

The labuser identity used everywhere (UC grants, presence) is the attendee
email from `X-Forwarded-Email`.

## 9. Namespace provisioning + labuser usability *(opt-in)*

Because the SP (not the labuser) creates resources, the labuser would have no
access to them. Provision a per-attendee catalog so usability is automatic:

- **Create a per-attendee catalog** where the **attendee remains OWNER +
  `ALL PRIVILEGES`** and grant the **app SP `MANAGE`, `USE_CATALOG`, and
  `CREATE_SCHEMA` only on that attendee's dedicated catalog**. Add narrower
  catalog/object creation privileges only when the workshop content needs them.
  `MANAGE` is required because the reconciler performs GET/PATCH on that
  catalog's grants. Do not grant ownership or account/metastore-wide privileges
  to the app SP. UC privileges inherit downward, so the attendee grant makes
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

Grant payloads the app issues (the app SP uses catalog-scoped `MANAGE`; it does
not need ownership or metastore-wide `MANAGE`):

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

## 10. Prewarm and two-instance verification contract

Prewarm each app once against its persistent data volume before event doors. CT
stages nothing: the reviewed contract ships in the terminal image at
`assets/artifacts/manifest.json`, pinning a source and SHA-256 for every
artifact, including the separate Codex npm launcher and platform-native package
tarballs plus the expected installed native executable checksum, and for Omnigent
the uv archive, the Python 3.12 archive, and the in-repo fully pinned transitive
lock with hashes. Reuse verifies the complete installed venv and runtime, not
only their launchers. Omnigent runs with `UV_PYTHON_DOWNLOADS=never` and
`--require-hashes`, so no unpinned wheel can enter the venv.
`ARTIFACT_MANIFEST_PATH` is optional and may redirect `source` only; a manifest
that tries to change a version or checksum is rejected, and `/readyz` reports
whether the default or an override is in force.

Bootstrap records each step's `source` (`prewarmed`, `cache`, or `staged`),
start/completion timestamps, duration, and expected/actual
version/ref/checksum in attendee-scoped `GET /api/setup-status` and
admin-bearer-compatible `GET /api/admin/setup-status`. The skills overlay is
reused only when its persistent stamp binds the configured repository + pinned
ref to a resolved commit and both the checkout and installed skill content
match the recorded checksum. A missing/tampered stamp, checkout, or installed
overlay forces a fresh reviewed refresh. Vendored fallback remains visible as
`vendored_fallback` and keeps `/readyz` closed.

`GET /api/admin/prewarm-status` independently re-verifies persistent on-disk
binaries, exact versions, separate Codex launcher/native stamps, tmux checksum
stamp, Omnigent uv/Python/lock/binary stamp, and skills
ref/resolved-commit/content checksum. It does not trust the current process's
installer state or the initial `network`/`prewarmed` source. Shared-prefix
mutation is serialized by a cross-process file lock; skills are assembled in a
complete staging tree and swapped as a unit, and stamps use unique same-volume
temporary files plus atomic rename.

Control Tower remains responsible for app authentication, provisioning,
grants, and deployment. The repository's helper is read-only and does not
create or modify infrastructure:

```bash
export DATABRICKS_TOKEN='<external CT bearer>'
python scripts/ct_verify.py \
  --app-url https://app-a.example \
  --app-url https://app-b.example \
  --timeout 300
```

Alternatively pass `--manifest inventory.json`. Exactly two apps are required:

```json
{
  "apps": [
    {"name": "canary-a", "url": "https://app-a.example", "workspace_host": "https://workspace-a.example", "token_env": "APP_A_ADMIN_TOKEN", "attendee_token_env": "APP_A_ATTENDEE_TOKEN"},
    {"name": "canary-b", "url": "https://app-b.example", "workspace_host": "https://workspace-b.example", "token_env": "APP_B_ADMIN_TOKEN", "attendee_token_env": "APP_B_ATTENDEE_TOKEN"}
  ]
}
```

The helper waits for `/readyz`, `/api/admin/setup-status`, and
`/api/admin/prewarm-status`; requires reusable on-disk proof; and compares the
complete canonical release and prewarm manifests across both instances,
including identical resolved skills commits and checksums. Initial
`network` versus restart `prewarmed` provenance does not create drift. It prints
one stable JSON object and exits `0` only when both apps are ready and identical
(`1` not ready/timeout/mismatch, `2` invalid input). It never prints bearer
tokens.

Inventory names and normalized URLs must be unique. URLs require HTTPS and may
not contain userinfo, query strings, or fragments. Local development may opt in
to `http://localhost`/loopback only with `--allow-local-http`.

## 11. Operational health ingestion

When CT ingest is enabled, the app periodically emits `operational.health`
through the same bounded, nonblocking `event_emitter` path. The payload includes
bootstrap duration/error counts; HTTP `409`/`429`/`503` counts; current/total
WebSocket attachments; terminal and EventHub subscriber overflow/current
metrics; live PTY count; Linux RSS when available; credential state/freshness;
and entitlement handoff failure count.
The payload selects fields explicitly and contains no tokens or detailed error
text. CT should alert on rising bootstrap errors, `503`s, overflows, stale
credentials, or handoff failures and use `/api/admin/stats` as reconciliation.
The emitter flusher and operational reporter retain their thread handles and
are signalled and briefly joined during app shutdown.

## 12. Fleet operations, restart recovery, and kill switch

Use an inventory in the format above. All operations have bounded timeouts,
emit deterministic JSON, and support `--dry-run`:

```bash
# Readiness/setup/phase/launch-control rollup
python scripts/ct_fleet.py --inventory inventory.json status

# Pause or resume new LLM-agent launches fleet-wide (existing PTYs continue)
python scripts/ct_fleet.py --inventory inventory.json --dry-run pause
python scripts/ct_fleet.py --inventory inventory.json pause
python scripts/ct_fleet.py --inventory inventory.json resume

# After restart, restore content first and then the active phase
python scripts/ct_fleet.py --inventory inventory.json repush \
  --content-pack event_pack.json --phase build
```

The fleet kill switch is `POST /api/admin/agent-controls` with
`{"enabled": false}`. It pauses new paid-agent launches only; bash and already
running sessions remain available. Resume with `{"enabled": true}`.

Restarted apps retain phase only in process memory, so CT must re-push the
content pack and then phase after readiness returns. Session metadata on the
persistent `SESSION_STATE_PATH` surfaces prior terminals as restart ghosts; CT
must not treat those as live PTYs.

## 13. External teardown

Before deletion, capture a read-only final report:

```bash
python scripts/ct_fleet.py --inventory inventory.json teardown-report
```

The helper only reads presence, state, and launch-control status and retains a
bounded summary: presence/session counts, phase, credential/entitlement state,
and agent controls. It never retains attendee identities, raw payloads, tokens,
or detailed errors, and never deletes the app, workspace, catalog, or
credentials. External CT must then
perform the §7 teardown, record per-resource success/failure, retry bounded
transient failures, revoke any emergency `WORKSHOP_PAT`, and retain the final
report with the event run. The app must never delete itself.

## 14. Workshop insight capture *(opt-in, per event)*

Off unless CT sets `WORKSHOP_INSIGHT_CAPTURE=true` on the instance. The full
contract — event shapes, schema, redaction, roster CSV, brief structure — is in
[`workshop-insight-contract.md`](./workshop-insight-contract.md); this section is
the operator checklist.

Every other ingest event answers an operational question. These answer a
commercial one: what is this attendee trying to build, on what stack, and what is
stopping them. That is why WT defaults the switch off: a terminal that captures
has to have somewhere for the buffer to go, and standalone it does not.

Where the decision is made changed once in practice. Leaving it per-instance
meant the first CT-provisioned workshop captured nothing at all, because nothing
set the flag. CT now decides for the fleet it deploys
(`CONTROL_TOWER_WORKSHOP_INSIGHT_CAPTURE`) and a single run opts out through its
app `env_overrides`, which is the form the consent decision below should take:
one deliberate act by whoever owns the deployment, not a flag each operator has
to remember for each run.

**Before turning it on:**

1. Confirm the event's registration terms cover capture. WT shows no consent
   prompt — attendees are mid-lab and a modal there is coercive, not informed. If
   the terms don't cover it, opt that run out
   (`env_overrides: {"WORKSHOP_INSIGHT_CAPTURE": "false"}` on its terminal app),
   which wins over the fleet default in both the deployment env and `app.yaml`.
2. Import the roster (`POST /api/labs/{run_id}/roster` on CT). WT only ever knows
   the pooled `labuserNNN@` identity, so without a roster the events land in
   Lakebase attributable to nobody and there is no company to brief on. Import
   before the event, not after: CT assigns pooled identity to roster entry as
   instances are bound.
3. Nothing else. Capture needs **no delivery configuration** — see below.

**How CT receives it: by collecting, not by being pushed to.**

```http
GET {app_url}/api/admin/insight-events?after={seq}&stream={stream_id}
```

The Terminal buffers every event and hands the buffer over on this admin
endpoint, authenticated exactly like `/api/admin/stats`. It does **not** post to
CT, because it cannot: Databricks Apps sit behind a proxy that requires a
Databricks identity on every request, so a `POST` carrying only `X-Ingest-Token`
is rejected before it reaches CT's code. Collection reuses the workspace-scoped
credential CT already holds for the harvest, so no new grant, token or egress
path is needed on either side.

| Type | When | Content |
|---|---|---|
| `workshop.signal` | with each stats poll | derived counters + engagement band; no attendee text |
| `discovery.record` | as the agent elicits it | attendee-described use case, stack, blockers, confidence |
| `insight.summary` | wrap-phase transition, teardown backstop | summary over harvested artifact *metadata* |

**What CT must do with a collected batch:**

1. **Collect after the stats call, not before** — on the `?final=true` pass the
   Terminal generates its wrap summaries *while answering it*, so collecting first
   leaves the most valuable events behind on the last visit the app ever gets.
2. **Stamp identity from the polled unit.** A collected envelope has an empty
   `run_id` and often no `workspace_id`: a deployed Terminal is never told which
   run it belongs to. An envelope that names an `attendee` wins over the unit's
   pooled email, because one instance can serve several.
3. **Namespace a runless idempotency key with the run.** The Terminal's keys embed
   `{run_id}`, which is empty here, so they collide across runs — the same pooled
   `labuser001@` at the next event would be discarded as a duplicate of this one.
4. **Replay the cursor.** `after` is CT's high-water mark and doubles as an
   acknowledgement; the Terminal discards events at or below it. Present the
   `stream_id` that came back with the batch — sequence numbers restart with the
   process, and a cursor replayed across that boundary is refused and reset rather
   than used to discard a fresh buffer unread. Losing the cursor is safe (ingest is
   idempotent); getting it wrong silently is not.
5. **Watch `dropped`.** Non-zero means the Terminal's 5000-event buffer overflowed
   and events that existed are gone. No later collection recovers them; the fix is
   to collect more often.

`/readyz` reports capture state as a soft check, and the release manifest records
it, so after the event CT can prove whether capture was on for a given run rather
than inferring it from whether rows arrived. Because a delivery path now always
exists, the check reports whether anything has *actually collected* — that is the
only fact configuration cannot establish.

**Two things CT must do for the wrap summary to exist at all:**

1. **Drive the `wrap` phase transition** (`POST /api/admin/phase`). This is the
   primary summarisation trigger, and it is the only one that gets a model call —
   the app is warm and the attendee is present. An event that never flips to `wrap`
   gets the thin extraction fallback at best.
2. **Call the final harvest with `?final=true`** before deleting the app
   (`GET /api/admin/stats?final=true`). Beyond the usual snapshot this runs the
   model-free backstop for anyone `wrap` missed and *flushes the emitter buffer
   synchronously*. Without it, whatever was buffered in the last 15 seconds — quite
   possibly the whole summary set — dies with the container. The response reports
   `instance.summaries_emitted` and `instance.events_flushed`.

An `insight.summary` can legitimately arrive twice for one attendee: once from the
extraction fallback and once from a model pass, distinguished by the `generator`
field and by the last segment of the idempotency key. They share a `summary_id`.
**CT must prefer `llm` over `extraction`**, not last-write-wins, or the teardown
backstop will overwrite the better summary the wrap transition already produced.

Attendees can see their own discovery records in the terminal and remove any of
them. A removal arrives as a further `discovery.record` revision with the content
blank and `redacted_by_attendee: true`. **CT must honour it** — exclude the record
from every brief and export. The terminal cannot reach into Lakebase, so a
withdrawal it delivered but CT ignored is a consent failure on CT's side.

**What never leaves the instance:** raw terminal output, scrollback, file
contents, tokens. The harvester takes titles, paths and first-lines only, and a
redaction pass strips secret-shaped strings from discovery text before it is
buffered. Teardown is still `apps.delete` — WT holds no durable insight store, so
anything not pushed before deletion is gone. That is why the summary is generated
at the **wrap phase**, with the teardown harvest only as a backstop.

## 15. Toolchain mirror *(opt-in, per event)*

A fresh app instance installs ~430 MiB of pinned toolchain from the public
internet before `/readyz` goes green. On event morning many instances do that at
once against the same few hosts. Staging the pinned artifacts into one UC Volume
moves that to workspace-local storage.

**Optional and off by default.** With `WORKSHOP_TOOLCHAIN_MIRROR_PATH` empty, a
terminal boots exactly as it always has. Nothing below is required to run an
event.

### What CT owns

WT owns the artifact contract and ships the stager; CT owns provisioning,
authorisation and orchestration.

1. **Provision once per workspace:** a volume (any catalog/schema) and a reader
   group holding `READ_VOLUME` on it plus `USE_CATALOG` / `USE_SCHEMA` on the
   parents.

   Issuing those grants requires `MANAGE` on each securable, which CT holds only
   where it is the owner. It owns a volume and schema it created itself, so those
   two are self-service; it will never own a shared parent catalog such as `main`,
   so `USE_CATALOG` there is a one-time admin grant. Make it to the *group*, not
   to CT — the group is stable, so the grant is made once and never revisited,
   whereas granting CT `MANAGE` on the catalog hands a provisioning service
   authority over every schema in it. If the volume was created by hand before CT
   was pointed at it, transfer the schema and volume to CT's service principal, or
   the remaining two grants need an admin every time as well.
2. **Stage before deploying:** run `scripts/ct_mirror.py stage --volume ...` from
   the WT checkout being deployed, as the deployer SP.
3. **Add the app SP to the reader group**, then deploy. Do not grant the SP
   directly — it is minted seconds before deploy and the bootstrap thread starts
   almost immediately after the container comes up, so an ungropagated per-SP
   grant is indistinguishable from a missing one.
4. **Patch `WORKSHOP_TOOLCHAIN_MIRROR_PATH`** into the uploaded `app.yaml`
   alongside `WORKSHOP_APP_SP_ID`.

Ordering matters: staging and the grant must both be settled before deploy, or
the app misses, silently downloads from the internet, and the only symptom is a
slow boot.

`scripts/deploy_ct_sim.py --toolchain-mirror ... --toolchain-mirror-group ...`
performs this whole sequence and is the reference implementation.

### Operator buttons

Both call `scripts/ct_mirror.py`, which prints one JSON object and exits `0`
success / `1` drift or failure / `2` invalid input.

| Button | Command | Notes |
|---|---|---|
| **Validate** | `ct_mirror.py verify --volume ... --reader-group ...` | Confirms the volume can serve this release and the group can read it. ~6s; downloads no artifact bytes. Render `status`, `missing`, `corrupt`, `grants`, `staged_from_commit`. |
| **Force resync** | `ct_mirror.py resync --volume ... [--prune]` | Re-fetches and rewrites every blob. For a blob suspected corrupt, or after a WT release bumped pins. |

`verify` reports `staged_from_commit` against `expected_from_commit`, so a
failure reads as "three artifacts behind, staged from `abc123`" rather than a
bare pass/fail. `index_complete: false` means a previous stage was partial.
Artifacts named in `unsized` are present but had no recorded size to check
against — they pass, because boot re-hashes everything regardless, but a green
Validate should not be read as more than it is.

Validate fails on a missing reader grant even when every blob is staged, and
says so rather than reporting a blob count: staging and authorisation fail
independently, and only the grant is symptomless until event morning, when every
attendee quietly falls back to the internet. A grant fault is not fixed by the
staging buttons, so it must not be reported as something staging could fix.

### Pre-flight before a workshop

Three checks, three different questions:

1. `ct_mirror.py verify` — is the volume right?
2. A canary app's `/api/admin/setup-status` — is one app using it?
   `toolchain_mirror.served` counts volume hits, `from_network` names misses.
3. `ct_verify.py --require-mirror` — is the *fleet* using it?

Layer 3 is not redundant. Group membership propagates per principal, so 1 and 2
can both pass while a late-minted app SP misses a grant every earlier app has.
A bypassed mirror is invisible otherwise: the app is healthy, every checksum
matches, the bytes are identical. `ct_verify.py` reports `mirror_bypassed`
distinctly from `not_ready` because the remedy is a resync or a grant, not a
redeploy.

### Safety

Blobs are keyed by the manifest's own sha256, and the checksum gate at boot is
identical on the mirror and internet paths. So a release that bumps a pin simply
misses on a volume nobody re-staged — no stale-file case, no need to coordinate
WT releases with a re-stage — and a corrupt or tampered blob costs a fallback
rather than a bad install. Full detail in
[`artifact-manifest.md`](./artifact-manifest.md#toolchain-mirror).

---

## Appendix — quick reference

- App SP client id: `service_principal_client_id` from `apps create` / `apps get`.
- App SP numeric SCIM id: `service_principal_id` from `apps create` / `apps get`;
  patch it into uploaded `app.yaml` as `WORKSHOP_APP_SP_ID` before deploy.
- Critical grant: `PATCH /api/2.0/permissions/authorization/tokens` →
  `{service_principal_name: <client_id>, permission_level: CAN_USE}` (additive).
- Health states: `rotating` (good) · `degraded` · `unhealthy` · `unknown`.
- Recovery after grant: ~30s, no redeploy.
- Event delivery: `GET {app_url}/api/admin/insight-events?after={seq}&stream={id}`
  on the harvest, after the stats call. De-dupe on `idempotency_key`; stamp
  `run_id`/`workspace_id` from the polled unit and namespace a runless key with
  the run. Push (`POST {INGEST_URL}/api/ingest/events`, `X-Ingest-Token`) is
  additive and blocked by the Apps proxy for a deployed terminal.
