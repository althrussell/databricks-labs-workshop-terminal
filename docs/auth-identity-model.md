# Auth and identity model

Which identity does each part of Workshop Terminal act as, what that identity is
allowed to do, and where the current wiring falls short of the intent.

Everything below marked *measured* was observed against the live
`apj-aws-lab-df03-03` deployment (workspace `7474655046390143`, attendee
`labuser+3@awsbricks.com`) on 2026-08-10, not inferred from the source. The
method is recorded per section so each claim can be re-checked.

Read the four measured ceilings first, because they close off most of the
designs anyone reaches for: the OBO scope vocabulary is 17 scopes with no write
scope in it, the attendee cannot mint a PAT, an admin cannot mint one for them,
and the Apps proxy rejects PATs outright.

## Topology: two apps, one host

Three deployables are involved, and it is easy to mistake them for two:

| Deployable | Role | Service principal |
|---|---|---|
| `wt` | Attendee UI, PTYs, per-user HOMEs, credential files | `7abffd35-9777-494a-90d5-abb349cc1a52` |
| `omni` | Omnigent control plane: durable stores, session registry, web UI | `420c4f89-85b2-40e3-acac-70a26a9eb5de` |
| `omnigent host` | Runs *inside the `wt` container*, registers to `omni`, owns the PTYs and working dirs | none — runs as the attendee's HOME |

This is mode 3 in [omnigent-modes.md](omnigent-modes.md). The control plane holds
no compute; every session executes on the host process living in the WT
container.

**Sessions are shared across both surfaces.** *Measured*: `omni` reports exactly
one host for the attendee (`workshop-labuser-3-4aae51d3`,
`host_id 9cd8df6bce3a87faab36a177b2aa7e99`, online), and every session returned
by `GET /v1/sessions` is bound to that `host_id`. A session started from the
Omni web UI and one started from WT's `omnigent` terminal are the same object,
executing in the same process, against the same HOME.

The practical consequence: anything true of the host process environment is true
of both surfaces. A defect there is not a "WT bug" or an "Omni bug" — it is one
bug with two front doors, and one fix.

## Which identity acts where

*Measured* by running the CLIs inside a live attendee bash session:

| Context | Resolves as |
|---|---|
| AI Gateway / model calls | `wt` app service principal |
| `databricks …` in a WT bash session | `wt` app service principal |
| `databricks-me …` in a WT bash session | `labuser+3@awsbricks.com` |
| `databricks …` inside an Omnigent terminal | `wt` app service principal — via the CLI wrapper, see defect 1 |
| `databricks-me …` inside an Omnigent terminal | `labuser+3@awsbricks.com` |
| Runner / harness **SDK** calls on the Omnigent plane | attendee OBO, always — never the SP |

The last row is the invariant the whole arrangement is built to preserve, and it
is why parity was restored with a wrapper rather than a config merge.

The service-principal default is deliberate, not drift.
`cli_config.configure_databricks_cli` writes the SP's rotating credential to
`[DEFAULT]` and names it "the agent's build/deploy/provision identity";
`users.shell_env` pins `DATABRICKS_CONFIG_PROFILE=DEFAULT`. The attendee identity
is opt-in through the `databricks-me` wrapper, whose own header states that
"Building and deploying still use the default service-principal profile".

### Credential inventory

| Location | Holds | Renewable without the browser tab | Attendee can read it |
|---|---|---|---|
| `~/.databrickscfg` `[DEFAULT]` | `wt` app SP token | yes, server-side | **yes** |
| `~/.databrickscfg` `[me]` | attendee OBO | no | yes (their own) |
| `~/.omnigent/auth_tokens.json` | attendee OBO, mirrored for the host | no | yes (their own) |
| gateway token file | `wt` app SP token | yes, server-side | yes |

The OBO entries are the fragile ones. The app holds no refresh token — the proxy
forwards an access token only — so freshness is pulled from a live browser tab,
which is why a closed or idle tab eventually kills Omnigent sessions.

## What each identity is permitted to do

### The attendee OBO is read-only

*Measured* via `databricks apps get`:

| App | Configured `user_api_scopes` | Effective |
|---|---|---|
| `wt` | `catalog.catalogs:read`, `catalog.schemas:read`, `catalog.tables:read`, `sql` | the above plus `iam.access-control:read`, `iam.current-user:read` |
| `omni` | none | `iam.access-control:read`, `iam.current-user:read` |

So the OBO identity can read catalog metadata and run SQL, and nothing else.
`databricks-me jobs list` fails because `jobs` is not in scope. **Pointing agent
build/deploy actions at the OBO identity cannot work as configured** — it is a
read credential by design, and `databricks-me` is correspondingly a read tool.

### …and widening the scopes cannot fix that

The obvious next move — grant the app every scope there is — was tried and does
not exist as an option. *Measured* against the scope picker in the app's user
authorization UI, the entire vocabulary is **17 scopes**:

```
sql                      sql:restricted-query     sql.statement-execution
sql.warehouses:read      postgres                 catalog.catalogs:read
catalog.schemas:read     catalog.tables:read      catalog.connections
files                    ai-gateway               model-serving
vector-search            genie                    workspace.workspace
mcp.external             mcp.functions
```

There is no `jobs`, `clusters`, `pipelines`, `permissions`, `serving-endpoints`
or `apps` scope, and `all-apis` is rejected outright. **The read-only ceiling on
the OBO is permanent, not a configuration gap.** Every design that depends on
the attendee's OBO creating something is therefore dead on arrival, and this is
the fact that forced App-SP-plus-reconciler as the shipping answer.

### The attendee cannot mint their own PAT either

The next fallback — have the attendee's own browser session mint a PAT and use
that for CLI writes — is also closed. *Measured* by calling
`POST /api/2.0/token/create` with the attendee OBO:

```
Provided OAuth token does not have required scopes: authentication
```

`authentication` is not in the 17-scope vocabulary above, so no app
configuration can supply it. Admins cannot mint one on the attendee's behalf
either: `create-obo-token` issues tokens for service principals only.

Note the difference from the PAT results in the next section. Those were minted
by an interactive login as `labuser+3`, which proves what a PAT *can do* once it
exists. What is unavailable is a way for the app to obtain one without a human
pasting it in.

### The attendee's own PAT has full developer rights

*Measured* by attempting real creates with a PAT minted as `labuser+3`. Every
resource created was read back to confirm the recorded owner, then deleted.

| Resource | Result |
|---|---|
| UC schema | created, `owner = labuser+3@awsbricks.com` |
| Job | created, `creator_user_name = labuser+3@awsbricks.com` |
| Secret scope | created |
| Pipeline, App, Serving endpoint, Lakebase instance | permitted (rejected on payload, not permission) |
| SQL warehouse | **403 denied** |

The warehouse denial is an entitlement gap rather than a PAT limitation: the
attendee holds no direct entitlements and the `users` group carries only
`workspace-access` and `databricks-sql-access`, with no cluster-creation right.

PAT issuance itself required a one-time workspace change: token usage is
governed by the ACL at `/api/2.0/permissions/authorization/tokens`, not by a SCIM
entitlement. Granting `CAN_USE` to the `users` group unblocked it. Requested
lifetimes of 1 hour, 7 days and 30 days were all granted without clamping.

## Databricks Apps proxy behaviour

Established by spike, and load-bearing for any redesign:

| Question | Answer |
|---|---|
| Does the proxy accept a PAT as `Authorization: Bearer`? | **No.** `wt` returns 401, `omni` 302s to `/oidc/`, while the same PAT is valid against the workspace API |
| Does it accept a user OAuth (U2M) token? | Yes — 200 on both apps |
| Does it accept a service principal OAuth (M2M) token? | Yes — 200, once that SP holds `CAN_USE` on the app |
| Can a client spoof `X-Forwarded-Email`? | No — the proxy overwrites it with the authenticated principal, in any header casing |
| Do custom headers survive to the app? | **Yes** — anything outside the `X-Forwarded-*` family arrives untouched |
| Are SP token scopes enforced? | Yes — a narrow scope returns 403 on out-of-scope APIs |
| Does app invocation depend on token scope? | **No** — it is authorised by the principal's `CAN_USE`, so even an unrelated scope can invoke the app |

For an SP caller the proxy injects `x-forwarded-email` (the SP's client id),
`x-forwarded-preferred-username` (its display name) and `x-forwarded-user`
(numeric id @ workspace id).

The last two rows together mean a service principal can hold a credential scoped
to almost nothing (`iam.current-user:read`) and still call the Omnigent control
plane — useful, because such a token is near-worthless if it leaks.

### Spike residue: the WT-SP `CAN_USE` grant on `omni`

Proving the row above required granting the `wt` app SP `CAN_USE` on `omni`.
**Decision: keep the grant. It is currently unused by product code** — WT's only
outbound call to the control plane (`omnigent_remote`, the host-readiness probe)
authenticates with the attendee's mirrored OBO, not the SP.

Kept rather than revoked because it is additive, it costs a workspace-admin
round trip to restore, and both of the designs that would make it load-bearing
need it: the post-event runner-to-control-plane split, and an operator-side host
probe that still answers when the attendee credential is stale — today that
probe cannot distinguish "the host is down" from "the tab is closed", which is
the same class of blind spot the rest of this work removed.

Nothing may *assume* the grant is present. Anything that starts depending on it
must check and fail loudly, because it is a manual grant on one workspace and
not part of any provisioning path.

The throwaway `hdrprobe` app used for the spike and its workspace source
directory have been deleted.

## Known defects

### 1. The Databricks CLI inside Omnigent terminals — fixed twice

`build_host_launch` stripped the Databricks environment variables and pointed
`DATABRICKS_CONFIG_FILE` at an *empty* config with profile
`workshop-omnigent-no-credentials`. Native terminals inherit the host process
environment (`OSEnvSpec(type="caller_process")`), so they inherited that too.

*Measured*: the live host process carried exactly those values, and under them
the default profile failed with "has no workshop-omnigent-no-credentials profile
configured" while `databricks-me` failed with "has no me profile configured" —
the `[me]` profile exists in `~/.databrickscfg`, but the CLI was reading the
empty file, and the wrapper sets only `--profile`, never the config path. Per the
topology section this affected the Omni web UI identically.

The empty file was load-bearing, so the fix could not simply point at
`~/.databrickscfg`. The runner's token factory falls back to Databricks SDK auth
whenever the mirrored OIDC token is missing or stale, and **there is no way to
disable that fallback** — so any credential resolvable from this file is one the
runner can silently assume. Pointing it at `~/.databrickscfg` would have made
stale-OBO sessions quietly become service-principal-owned, which is worse than
failing loudly.

The fix keeps the separate file but makes it *attendee-only* rather than empty:
`~/.config/workshop/omnigent-databrickscfg` carries the `[me]` profile and
nothing else, mirrored on every OBO capture, with no `[DEFAULT]` section for
configparser inheritance to bleed through. The service principal stays
unreachable from the Omnigent plane, the agent's CLI resolves an identity, and
the SDK fallback becomes a resilience path — it now lands on the attendee, the
identity the runner should be using anyway, instead of returning no token at all.

**Second fix: parity for the CLI.** The above resolved an identity but the wrong
one for building — the attendee OBO reads and cannot create, so the agent's
first `databricks … create` returned 403 instead of "no credentials". Better,
but still a broken workshop.

The fix is a `databricks` wrapper in `~/.local/bin`, ahead of the shared symlink
on PATH (`server/users.py::_write_databricks_cli_wrapper`). When — and only
when — `DATABRICKS_CONFIG_FILE` holds exactly the Omnigent-plane path, it
redirects to `~/.databrickscfg` and clears the profile pin, so a CLI invocation
inside Omnigent resolves the App SP exactly as the same command does in a WT
shell. An attendee who sets the variable themselves is left alone, and
`databricks-me` is unaffected because it passes `--profile me` explicitly.

The narrowness is the design. The wrapper only intercepts argv, so the runner's
own SDK calls still read the attendee-only config and still cannot become the
service principal when the mirror goes stale — which was the entire reason the
separate file exists. The trade is that code building an SDK client directly
inside a harness still resolves the attendee and still cannot create; the agent
instructions therefore point at the CLI for anything that writes.

### 2. Ownership depends on after-the-fact reconciliation

Because the agent builds as the service principal, resources are created owned by
the SP and handed over afterwards by `server/entitlements.py`. Its coverage is
partial:

| Resource type | Handed off | Ownership transferred |
|---|---|---|
| Jobs | yes | yes |
| Pipelines, warehouses | only when absent from the boot baseline | yes |
| Serving endpoints, apps, database instances, database projects | yes | no — permissions only |
| Lakeview dashboards | **no** | no |

Dashboards are excluded because the Lakeview list/get responses expose no
authoritative creator. Pipelines and warehouses need the baseline because they
report no creation timestamp. Anything outside these eight specs — model registry
entries, vector search indexes, Genie spaces, experiments, Git folders, UC
objects outside the workshop catalog — is not covered at all.

### 3. An app SP credential sits in the attendee's home directory

`[DEFAULT]` in `~/.databrickscfg` is a working `wt` app SP credential the
attendee can simply read.

Process isolation itself holds up. *Measured*: every process runs as a single uid
(`app`), but a scan of all readable `/proc/*/environ` found **zero** exposing
`DATABRICKS_CLIENT_SECRET` or `WORKSHOP_PAT` — the deny-by-default `shell_env`
introduced in `38028b4` is doing its job. The config file is the remaining
exposure, and it is by design rather than by accident.

## Attribution: answering "who created this?"

Two identities act on the workspace, and the split is invisible after the fact —
a deployed app looks the same whichever principal deployed it. Neither PTY
scrollback nor error logs can settle it: transcripts stay on the box by design,
and a command that succeeded logged nothing. Two records exist instead.

**Live, per attendee: the `identity.resolved` event.** At session start
`server/identity.py` runs `databricks current-user me` and
`databricks-me current-user me` in both plane environments and emits what each
one answered:

```json
{
  "planes": {
    "workshop_terminal": {"databricks": "<app-id>", "databricks-me": "labuser+3@…"},
    "omnigent":          {"databricks": "<app-id>", "databricks-me": "labuser+3@…"}
  }
}
```

Identical rows across planes is the healthy state. A divergence — most likely
`databricks` resolving to the attendee inside Omnigent — means the agent has lost
its ability to create and will start returning 403s, and it says so before an
attendee reports it. The same snapshot is available from the admin diagnostics
endpoint while the instance is alive.

**After the fact: the workspace audit log.** It records the acting principal for
every API call and survives the instance being torn down, which the events above
do not. Service principals appear as their application ID rather than an email:

```sql
SELECT
  event_time,
  user_identity.email        AS acting_principal,
  service_name,
  action_name,
  request_params
FROM system.access.audit
WHERE workspace_id = :workspace_id
  AND event_date >= current_date() - INTERVAL 7 DAYS
  AND action_name RLIKE '(?i)^(create|deploy|update)'
ORDER BY event_time DESC
```

Narrow it to one resource with `request_params` (for example
`request_params.name = 'my-app'`), or to one identity by putting the app's
application ID in `acting_principal`. The `system.access` schema has to be
enabled on the metastore for the workshop account; enabling it is a one-line
prerequisite and it is worth doing before an event rather than during the
post-mortem of one.

## What ships, and why it is not the original intent

The intent was "app SP for AI Gateway, the attendee's own identity for agent CLI
actions". The first half holds. The second is **not achievable** with anything
currently reachable: the OBO cannot create (17-scope ceiling), the attendee
cannot mint a PAT (`authentication` scope does not exist), an admin cannot mint
one for them (`create-obo-token` is service-principal-only), and the Apps proxy
rejects PATs on the runner hop anyway. Each of those was measured, not assumed.

So the shipping split is:

| Plane | Credential | Why |
|---|---|---|
| AI Gateway / model calls | `wt` app SP | already correct, server-refreshed, independent of any browser tab |
| Agent CLI (build, deploy, provision) | `wt` app SP, via the CLI wrapper | the only identity in reach that can create; ownership is repaired afterwards by the reconciler |
| Governance-faithful reads (`databricks-me`) | attendee OBO | read-only is the right shape here |
| Omnigent runner → control plane and its SDK calls | attendee OBO | keeps the runner off the SP even when the mirror is stale |

The cost is that the entitlements reconciler is load-bearing rather than a
safety net: it is what makes an SP-created resource usable by the attendee who
asked for it. `server/entitlements.py` coverage is therefore a correctness
requirement, not a nicety, and `entitlements.health` is emitted on failure so a
silent reconciler failure surfaces before an attendee discovers it.

Putting attendees on the App SP's gateway credential was considered and
rejected for a different reason: it converts a single-attendee failure into a
fleet-wide one.

## The durable credential, post-event

One route survives everything above: a **custom OAuth U2M app integration**
registered by an account admin with `all-apis` and `offline_access`, where WT
hosts the redirect and holds the refresh token server-side. That is the only
path to a full-rights, attendee-identity credential with no tab dependency. It
needs its own spike — whether the account permits custom app integrations, the
refresh-token lifetime and rotation behaviour, and whether consent can be
pre-approved for a group — and it is explicitly out of scope for event week.

A manual PAT entry box is the fallback shape: opt-in, validated at entry,
health-checked, degrading to the SP rather than hard-failing. It gives correct
ownership at creation with no reconciler, but it costs the zero-friction start
and it does not help Omnigent or Pi at all, because the proxy rejects PATs on
the runner hop.

The decision, what it retires, and the three questions that gate the spike are
recorded in [ADR 0002](adr/0002-attendee-credential.md). Two designs are retired
there rather than deleted quietly, because both are the natural thing to reach
for once and will be proposed again: a **wrapper service principal** auth
provider, and a **credential broker** vending attendee-scoped credentials.
Neither survives the scope ceiling — both assume an attendee-shaped write
credential exists to wrap or vend, and none does.

Until one of those lands, the tab dependency is structural: no refresh token
means freshness can only be pulled from a live tab. What Phase 2 changed is that
running out is now gated, explained and self-recovering instead of a mysterious
mid-lab failure — see [verification-gate.md](verification-gate.md) for the
evidence and [operator-runbook.md](operator-runbook.md) for what to do about it.

## Open questions

- Whether attendees need SQL warehouse creation, and if so whether to grant the
  `users` group a cluster-creation entitlement.
- Whether `[DEFAULT]` holding a working App SP credential in the
  attendee-readable home directory is acceptable now that the agent's creates
  depend on it.
- Whether the account permits custom OAuth app integrations at all, which
  decides whether the U2M route above is real.
