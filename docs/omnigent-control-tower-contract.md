# Omnigent Control Tower contract

Status: **control plane and attendee-owned remote-host integration implemented**.

This is the cross-repository contract for a dedicated Omnigent Databricks App.
It does not implement Control Tower orchestration or add a Workshop Terminal UI
link.

## Confirmed upstream behavior

A read-only spike used `gh` against `omnigent-ai/omnigent` main at
`b08eb50f2ebe242cc64d5e289bc8d209567c5126`. The following are source-confirmed:

- The published `omnigent==0.10.0` wheel contains the upstream React UI. Its
  release commit is `40755dd8dddb07e1eb6e4055d1d9936e184ceb9b`; the wheel SHA-256
  is recorded in `deploy/omnigent-app/upstream-lock.json`.
- Omnigent 0.10.0 requires Python `>=3.12`. The App source keeps `>=3.12,<3.13`
  so Databricks Apps stays on 3.12 uv mode, and omits `requirements.txt` so the
  platform selects uv mode from `pyproject.toml` and `uv.lock` instead of its
  Python 3.11 pip mode.
- Upstream's Databricks wrapper still constructs the same stores and
  `create_app(...)` kwargs. 0.10.0 added an optional `feature_flags` argument;
  when omitted, `create_app` reads `OMNIGENT_FEATURES`. We leave that unset so
  attendees do not get a cost page or web-driven harness installer. We track
  the release wrapper deliberately, not main.
- The remote foreground host command is
  `omnigent host --server <OMNIGENT_APP_URL> --non-interactive`.
  `--non-interactive` suppresses browser login; it does not create credentials.
- The terminal/REPL command is `omnigent polly --server <OMNIGENT_APP_URL> -c`
  when the attendee already has a joinable session, and the same command without
  `-c` when they do not. Either targets the remote control plane and does not
  start a second local Omnigent server.

  Both name the agent, because only a named agent uploads the local agent
  bundle and so puts the launch in the local-runner/remote-server topology.
  The agent-less `omnigent run --server <url>` is a thin client: it can join a
  session the server already holds, but it cannot create one, and measured on a
  live deployment the first prompt that needs a new session fails inside the TUI
  with `Sessions API fresh session creation requires a local agent bundle`.
  Any attendee whose second launch follows a conversation whose runner is gone
  reaches that, which is why this App does not use the form.
  `-c` is what preserves the property the attach existed for — it continues the
  most recent conversation for the agent, so a conversation opened in the App's
  UI is the conversation the terminal shows. It is conditional only because it
  needs something to continue, which a fresh control plane (every attendee's
  first launch) does not have, so the helper probes first (see below). `polly`
  is the bundled orchestrator a bare `omnigent` launches for a Claude
  credential, so the create path matches the terminal's pre-App behavior.
- `omnigent login <OMNIGENT_APP_URL>` detects Databricks Apps and runs
  `databricks auth login --host <workspace>`, an interactive browser flow.
- Host and runner connections refresh Databricks SDK credentials when ambient
  credentials or a stored login pointer are available.
- `GET /health` returns `{"status":"ok"}` after startup. `GET /api/version`
  returns the installed server version.

## Authentication and ownership

Every authenticated Workshop Terminal request carries the attendee's Databricks
Apps OBO bearer in `X-Forwarded-Access-Token`. When `OMNIGENT_APP_URL` is set,
Workshop Terminal mirrors that bearer into the attendee's isolated
`~/.omnigent/auth_tokens.json`, keyed by the normalized App URL, using
Omnigent 0.10.0's `{token,user_id,expires_at}` record shape. The attendee email
is lowercased and JWT `exp` supplies `expires_at`. Capture ordering uses JWT
`iat` then `exp`; a late older request, even if still valid, cannot replace the
newer mirrored bearer.

The mirror is an atomic read/merge/write under the attendee lock (0600,
same-directory temporary file, fsync, `os.replace`) and preserves records for
other servers. Omnigent calls `load_token(server_url)` on every authentication
attempt, so token rotation does not restart the foreground host. An expired
record is not considered usable; a later authenticated browser request writes
the fresh OBO token and wakes the supervisor.

The host therefore belongs to the attendee represented by the bearer. App
service-principal credentials are forbidden in host argv/environment because
they would register an SP-owned host invisible to the attendee. No Control
Tower static bootstrap secret is required, issued, returned, or revoked.
Credential fallback is explicitly prevented: remote host and TUI processes
unset Databricks token/client/host variables and point
`DATABRICKS_CONFIG_FILE` plus `DATABRICKS_CONFIG_PROFILE` at an isolated empty
per-attendee config. They can still read `auth_tokens.json`, but cannot fall
through to the attendee shell's app-SP-backed `[DEFAULT]` profile after OBO
expiry. The local bare-Omnigent branch does not change its credential context.

Workshop Terminal supervises one foreground process per attendee using an
argument array (no shell):
`omnigent host --server <OMNIGENT_APP_URL> --non-interactive`. HOME, cwd, tmux
paths, and token file are attendee-isolated; stdout/stderr go to `/dev/null` and
the deny-by-default environment contains no OBO bearer or Databricks app
client/token variables. Exits use capped full-jitter exponential backoff.

Workshop Terminal supplies a stable, non-secret host identity only to the
supervised host process:

`host_id = SHA256("databricks-workshop-terminal/omnigent-host-id/v1\0" ||
normalized_app_url || "\0" || normalized_attendee_email)[:32]`

The result is exactly 32 lowercase hex characters. The App URL has normalized
scheme/host/default port/trailing slash, and the attendee email is trimmed and
lowercased. This domain-separated construction is deterministic across
Workshop Terminal HOME loss and App restarts, while differing across
attendee/server pairs; it does not use or store a secret. The display name is
`workshop-<existing attendee slug>`, capped at Omnigent's 64-character host
name limit while preserving the slug's email hash. It exposes no email domain
or bearer.

The supervisor passes these values together as `OMNIGENT_HOST_ID` and
`OMNIGENT_HOST_NAME`. They are not injected into attendee PTYs or the TUI.
`OMNIGENT_HOST_TOKEN` must not be set: this is not managed-sandbox auth.
The attendee OBO bearer remains the sole authentication and ownership proof.

The same identity is persisted as the `host` section of the attendee's
`~/.omnigent/config.yaml`, which is where the CLI reads the host to launch a
runner on. Both agree, so a terminal command reuses the supervised host's
daemon record for the URL rather than starting a rival daemon. Left unpinned the
CLI invents and persists a uuid, then waits out its timeout for a daemon nobody
runs while the attendee's real host is online beside it.

The generated `workshop-omnigent` TUI helper is authoritative. With the URL and
no arguments it executes `omnigent polly --server "$OMNIGENT_APP_URL" -c` when
the attendee has a joinable session and the same command without `-c` when they
do not; either way no second local server starts. Any argument
suppresses the probe: a first positional that does not look like a flag is taken
as the agent to create a session with, so a model-set variant can have its own
catalog card. With an empty URL it executes bare `omnigent`, preserving the
existing local behavior exactly.

The verdict comes from the generated `workshop-omnigent-live-sessions` probe,
which reads the attendee's own mirrored bearer and asks the App
`GET /v1/sessions`, reporting success when any returned session is neither
archived nor `failed`. It asks as the attendee because the App scopes sessions to
the bearer's owner. `failed` is excluded because restarting Workshop Terminal
fails every session it hosted a runner for, so after any redeploy the attendee's
whole history reads `failed` and a picker over it would offer only dead
conversations; the exclusion is a denylist so an unrecognized status still counts
as joinable.
Every failure — no mirrored token, an expired one, an unreachable App, or a
missing probe — is reported as "nothing to join", so the card degrades to
creating a session rather than to an error. Deciding by lookup rather than by
running the attach and treating a non-zero exit as an empty control plane is
deliberate: that exit code cannot distinguish a fresh control plane from an
attendee quitting a session they had deliberately joined, and would fork a new
session on every such exit.

Remote startup fails closed unless Omnigent is enabled and the effective CLI
install is exactly protocol-compatible 0.10.0. Local mode retains install-spec
override support.

Remote startup also enforces the security topology: exactly one attendee may
use a Workshop Terminal instance/workspace. Startup rejects
`ALLOW_SHARED_TOPOLOGY=true` and rejects session caps where
`MAX_SESSIONS_GLOBAL > MAX_SESSIONS_PER_USER`, with instructions to deploy one
instance per attendee. Those static checks are defense in depth, not identity
enforcement: equal caps still allow different principals to use the instance
sequentially. At runtime, `WORKSHOP_ATTENDEE_EMAIL` is the durable deterministic
owner. Remote startup requires a valid normalized configured email, and Control
Tower sets it from the unit's exact attendee identity. Every normal HTTP,
WebSocket, and OBO-helper path checks it before OBO capture, HOME bootstrap,
credential/config writes, or host startup. A different caller receives HTTP
403. Ownership therefore survives local App filesystem loss and compute
replacement; no local marker is a source of truth. `/api/omnigent-host`
exposes only sanitized lifecycle/readiness fields, never the owner identity or
bearer. Operator-only routes retain their existing `ADMIN_GROUP` authorization
and do not pass through the attendee dependency. This combined behavior is the enforced invariant
because per-attendee HOMEs share one Unix uid. With `OMNIGENT_APP_URL` empty,
the binding is disabled and existing local Omnigent multi-attendee behavior is
unchanged.

## Resource contract

Control Tower creates one isolated resource set per attendee:

- one Lakebase project/branch/default database, never shared with another
  Omnigent app;
- one UC Volume for agent bundles and executor snapshots; and
- one dedicated Databricks App sourced from `deploy/omnigent-app`.

The App resource must bind:

- key `postgres`: Lakebase branch/database with `CAN_CONNECT_AND_CREATE`;
- key `artifact_volume`: UC Volume with `WRITE_VOLUME`, plus parent
  `USE_CATALOG` and `USE_SCHEMA`.

After the App service principal exists, Control Tower grants it Lakebase
database connect/create and schema/table migration privileges. The first app
start may fail before those grants; that is a provisioning phase, not readiness.

### Smart routing (Auto · smart routing)

Omnigent 0.10.0 has two routing backends and the App configures both.

The external one is AI Gateway `POST …/ai-gateway/routing/v1/routes:select`,
called as the App service principal with ambient Apps OAuth. Labs
(`DATABRICKS_CONFIG_PROFILE=labs`, 2026-08-11) authenticated successfully but
returned HTTP 404 `ENDPOINT_NOT_FOUND` / "routing/v1/routes:select is not
enabled for this account." That is an **account-level product flag**, not a
missing App-SP grant: no Control Tower privilege change unblocks it.

The local one is the built-in judge, and it is what makes Auto usable anyway.
0.10.0 latches the external client off after its first `ENDPOINT_NOT_FOUND`, so
the cost is one request per App process and the judge answers everything after
it. `GET /v1/info` drops the external routing source once latched, so the UI
never offers a router that cannot answer. The judge also serves any pane whose
harness is not gateway-backed, which the external router cannot do because its
picks are gateway catalog ids that pane cannot reach.

Control Tower obligations:

- `WORKSHOP_SMART_ROUTING=true` (shipped default).
- Ensure the Omnigent App SP can query exactly the serving endpoint named by
  `WORKSHOP_ROUTING_JUDGE_MODEL` (default `system.ai.gpt-5-6-luna`). The
  default needs no action: pay-per-token foundation models are queryable by any
  principal with workspace access, and expose no endpoint ID to grant on.
  Pointing the setting at a custom endpoint does require `CAN_QUERY`.
- Do not grant the Omnigent App SP general model-serving. Harness inference
  runs on the attendee host under the Workshop Terminal gateway token, and
  candidate models still come from that host's runner catalog.

If the account later enables routing, no code or config change is needed: the
external client stops latching off and takes precedence on gateway-backed
panes.

Workshop Polly economy/balanced/frontier template agents are removed; stock
`polly` is the default agent.

### Shared-session approvals (upstream, 0.10.0)

Posting a session event — including an `approval` verdict — requires
`LEVEL_EDIT`, not `LEVEL_OWNER`. Any principal granted edit on a shared session
can therefore resolve that session's approval prompts. This reverts a
previously tightened check and is upstream behavior, not a Workshop Terminal
choice.

It does not currently expose attendees to each other: each attendee gets their
own App, host, and sessions, the App never enables public sharing, and the
`header` auth provider scopes every request to the proxy-authenticated
identity. Treat it as a constraint on any future change that would share one
session across attendees — that change would also hand every editor the
approval gate.

## Environment ownership

Databricks Apps runtime owns `DATABRICKS_APP_PORT`, `PGHOST`, `PGPORT`,
`PGDATABASE`, `PGUSER`, workspace OAuth variables, and token refresh.

The App resource bindings own:

- `AP_LAKEBASE_ENDPOINT` from `valueFrom: postgres`;
- `AP_ARTIFACT_VOLUME_PATH` from `valueFrom: artifact_volume`.

This repository owns `OMNIGENT_AUTH_PROVIDER=header`. Header auth is valid only
behind the Databricks Apps proxy, which strips caller-provided identity headers
and injects `X-Forwarded-Email`. Do not expose the process through alternate
ingress.

Control Tower returns `OMNIGENT_APP_URL` and non-secret
`WORKSHOP_ATTENDEE_EMAIL` to the Workshop Terminal deployment.
The URL is the only Omnigent value exposed by `/api/config`.
The same handoff sets `ALLOW_SHARED_TOPOLOGY=false` and session caps with
`MAX_SESSIONS_GLOBAL <= MAX_SESSIONS_PER_USER`; otherwise Workshop Terminal
refuses remote startup.
Control Tower must not return Lakebase credentials, app OAuth client secrets,
access tokens, or volume credentials. User authorization must be enabled on the
Workshop Terminal App so the proxy forwards the attendee OBO bearer.

## Provisioning sequence

1. Create the isolated Lakebase project, branch, endpoint, and default database.
2. Create the UC schema and Volume and grant parent UC privileges.
3. Create the Databricks App so its service principal exists.
4. Bind `postgres` and `artifact_volume`; grant the App service principal.
5. Deploy pinned source from `deploy/omnigent-app`.
6. On App startup, write, read back, and delete a unique probe file inside
   `AP_ARTIFACT_VOLUME_PATH` as the App SP, through the Files API — Databricks
   Apps does not mount Unity Catalog volumes, so the path is unreachable from the
   filesystem. Startup fails if the write is not permitted.
7. Poll App deployment state, authenticated `GET /health`, and
   `GET /api/version`; require version `0.10.0`. Successful health proves the
   Volume startup invariant.
8. Record the App URL and resource/deployment identifiers.
9. Deploy Workshop Terminal with the normalized App URL, exact attendee email,
   and enforced
   single-attendee topology values. Its first authenticated attendee request
   creates the HOME, mirrors the OBO token, derives the expected stable host ID,
   and starts one supervised `omnigent host` process for that attendee.
10. Return the Workshop supervisor state verbatim: `waiting_for_token`,
   `starting`, `running`, `backoff`, `error`, or `stopped`. Report `connected`
   only from control-plane host verification, never from local PID state.

Control Tower may parallelize independent Lakebase and Volume creation, but
steps 3–10 retain the dependency order above.

## Health and readiness

App health is true only when deployment is running, authenticated
`GET /health` returns HTTP 200 with `{"status":"ok"}`, and
`GET /api/version` is `0.10.0`. Resource readiness additionally requires the
Lakebase endpoint active and an App-SP write probe to the bound Volume.

Workshop readiness is stricter:

`ready = app.ready && resources.lakebase_ready &&
resources.artifact_volume_ready && remote_host.connected`

The readiness schema maps the supervisor lifecycle without aliases:

- `waiting_for_token`: no fresh mirrored attendee token is available;
- `starting`: a spawn is in progress;
- `running`: the local supervised process is alive;
- `backoff`: the process exited or failed to spawn and retry delay is active;
- `error`: configuration or executable readiness prevents progress; and
- `stopped`: shutdown completed.

All six local states require `connected=false`; `running` is not proof of
control-plane connectivity. `error` and `stopped` map to top-level `failed` and
require a structured failure. `connected` is the only external verification
state. Control Tower may calculate `expected_host_id` from its normalized App
URL and attendee identity, but may set `connected=true`, `host_id`, and
`last_seen_at` only after the dedicated Omnigent control plane verifies the
attendee-owned host and the observed `host_id` equals `expected_host_id`.
Workshop Terminal performs that verification only while its local supervisor is
running and a fresh attendee-owned token mirror exists, using authenticated
`GET /v1/hosts/{expected_host_id}` with a short timeout. Upstream v0.10.0 does
not return a last-seen field, so `last_seen_at` is the UTC timestamp when
Workshop Terminal completed the successful exact-host online verification.
Network/auth failures produce `connected=false` while retaining the honest
local lifecycle state and omit both `host_id` and `last_seen_at`.
Control Tower calls the admin-authenticated readiness endpoint during normal
run/unit stats collection and final pre-teardown collection. It validates the
unit email, expected host ID, lifecycle state, and connected fields, then
reconciles persisted readiness in both directions. Malformed or mismatched
reports, and failures to fetch the admin readiness endpoint, invalidate prior
verification: Control Tower persists `connected=false`, clears `host_id` and
`last_seen_at`, sets both persisted remote statuses to `error`, and records the
retryable `CONTROL_TOWER_HOST_READINESS_UNAVAILABLE` failure. This identifies a
collector verification failure rather than a Workshop Terminal supervisor
error. Collection remains fail-soft, and the next valid online report restores
`ready` and clears the failure.
The schema rejects `ready=true` unless App health, both resources, and verified
remote connectivity are all true; it also rejects a connected host without a
non-empty verified `host_id` and Workshop Terminal verification timestamp.

A closed browser tab cannot refresh OBO tokens. An already-active WebSocket can
remain active, but once reconnection/authentication needs a fresh token the host
waits without a busy loop. Reopening the Workshop Terminal (or making another
authenticated browser request) refreshes the token file and wakes the host.

## Retries and rollback

Provisioning operations are keyed by attendee/run identity and must be
idempotent. Retry transient create, grant, deploy, and health failures with
bounded full-jitter exponential backoff. Expired/missing attendee auth maps to
`waiting_for_token`, not a retryable process failure.

A failed required Omnigent deployment fails the attendee deployment. An
optional deployment may continue only if Control Tower records the degradation
and omits `OMNIGENT_APP_URL`.

Rollback pins both the Workshop Terminal source revision and Omnigent package
version. Re-deploy a previously reviewed revision rather than changing the
dependency at runtime. Database migrations may not be downgradable; test
forward compatibility against a disposable Lakebase branch before rollback.

## Teardown

Delete in dependency order:

1. stop the Workshop Terminal supervisor (TERM process groups, bounded wait,
   KILL/reap if needed) and remove attendee-local mirrored token state;
2. stop/delete Workshop Terminal references to the dedicated App;
3. stop/delete the Omnigent App and wait for termination;
4. revoke App permissions and remove the UC Volume/schema if event-owned;
5. delete the Lakebase branch/project; and
6. remove persisted deployment/resource correlation rows.

Contract version 1.6 issues no static host credential. Teardown uses the same
deterministically derived host ID for correlation and repeated cleanup;
not-found is success, but permission failures are surfaced.

## Control Tower return value

Control Tower returns the versioned object in
`docs/examples/omnigent-control-tower-payload.json`:

- dedicated App URL, app name, deployment ID, expected server version;
- deterministic expected host ID and its versioned derivation;
- source repository, source revision with an explicit
  `source_ref_immutable` flag, and source subdirectory. The flag is false
  when the revision is a branch rather than a commit, which happens where the
  workspace receives the app source as a plain directory and exposes no git
  head to read back;
- Lakebase/Volume identifiers required for status correlation and teardown;
- non-secret environment values;
- remote-host state and exact verified commands; and
- the non-secret OBO mirror mechanism (`static_secret_required=false`).

The JSON schemas in `tests/fixtures` are normative. Additive or breaking field
changes require schema, example, consumer, and contract-version updates.
