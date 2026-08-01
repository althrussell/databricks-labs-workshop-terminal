# Databricks Workshop Terminal

A purpose-built, one-attendee-per-instance Databricks App for AI-coding-agent
training events. Each attendee opens one URL and gets **one-click Claude Code / Codex
terminals** with their Databricks credentials wired up automatically — plus a
steerable right-hand pane of curated Databricks insights that operators drive
live during the workshop.

Built to be deployed (and torn down) as workshop infrastructure by
[databricks-labs-control-tower](https://github.com/althrussell/databricks-labs-control-tower).

## Architecture

Control Tower deploys the Workshop Terminal into each attendee's isolated
workspace and pushes phase/broadcast updates live. The app distributes its
short-lived, auto-refreshing app-SP OAuth bearer to attendee CLIs, serves
config-driven content packs and an AI Dev Kit, and exposes a group-gated
operator admin panel.

![Workshop Terminal architecture](docs/images/architecture.png)

## What attendees get

- **Launch buttons** for Claude Code, Codex, and a plain terminal — the agent
  catalog is config-driven (`content/agents.json`), extensible per event.
- **Zero-touch auth**: Databricks Apps injects auto-refreshing app-SP OAuth
  credentials; the server creates one explicit `oauth-m2m` SDK client, removes
  duplicate client-secret environment variables, makes the production Linux
  process non-dumpable, and binds each bearer to the injected app client ID
  through `GET /api/2.0/current-user/me` (`applicationId` or
  `application_id`). `userName` is diagnostic only and never proves app
  identity; a username-only response continues to SCIM
  `/Me.applicationId` fallback. A 200 response without an exact authoritative
  identity match is never sufficient. Safe endpoint statuses and observed
  identity IDs are exposed in credential health; bearer values and response
  bodies are not. This is a process boundary—not secret
  erasure from the SDK object's memory. No token ACL or PAT mint is required,
  and no attendee creates a PAT. A vended `WORKSHOP_PAT` is an
  emergency-only fallback (reported `degraded` since it doesn't rotate).
  Attendees never see a token screen.
- **Session isolation**: per-user HOME directories, sessions strictly bound to
  their owner, secrets stripped from terminal env (deny-by-default). Note this is
  HOME/PTY-level isolation, not credential isolation — the vended credential and
  git identity are shared instance-wide. The supported topology is **one
  disposable workspace (and instance) per attendee**. Control Tower must inject
  `WORKSHOP_ATTENDEE_EMAIL`; a different attendee receives a clear 403/4403.
  Running multiple attendees on one instance is unsupported unless you set
  `ALLOW_SHARED_TOPOLOGY=true` to acknowledge the shared-credential caveat.
- **Resilient terminals**: PTYs survive page refreshes and wifi blips —
  reconnect and your scrollback replays.
- **Insight nuggets**: a collapsible pane of docs, best practices, and
  marketing-grade product cards that follows the workshop phase, reacts to
  activity signals, and **watches the terminal for topics** — mention
  Lakebase and a "spotted in your session" Lakebase card appears with the
  value prop and docs link. Only topic flags are recorded, never terminal
  content (`TOPIC_DETECTION=false` to disable).
- **A coached first run**: attendees say how they want things explained before
  they launch, so the coach adapts to technical vs business from its very first
  reply instead of spending a turn asking; anything they build gets a real
  design pass they never have to think about; event-pinned [databricks-agent-skills](https://github.com/databricks/databricks-agent-skills)
  skills are installed only from the reviewed artifact manifest, TDD subagents
  are pre-installed, and every
  git commit auto-syncs to the attendee's Workspace home so a restart or
  redeploy can't lose their work. That sync is not a take-home — teardown
  deletes the workspace too — so the wrap guidance tells attendees to push to a
  remote they own or download what matters while the event is still live.

## What operators get

- An in-app **Operator panel** (members of the `platform_admins` workspace
  group): live presence, phase control, broadcast banners.
- A remote **admin API** (same group, works for service principals too) for
  Control Tower or `scripts/push_content.py` — push content packs, set the
  phase, broadcast. See [docs/admin-api.md](docs/admin-api.md).
- **Cobranding** via env vars (`BRAND_NAME`, `BRAND_LOGO_URL`,
  `BRAND_PRIMARY_COLOR`, `EVENT_NAME`) for single-customer enablements.

## Architecture

- **Backend**: FastAPI + stdlib `pty`, single uvicorn worker (PTY fds and
  session state are process-local by design). WebSockets for terminal I/O
  (`/ws/sessions/{id}`) and app events (`/ws/events`).
- **Frontend**: React + TypeScript + Vite + xterm.js. The production build is
  **committed to `static/`** because Control Tower deploys the repo
  as-cloned with no build step.
- **Persistent metadata, not content**: no Lakebase or database is required *of
  this app*. Session lifecycle metadata can persist on the app volume for restart
  ghosts; terminal output is never persisted. Content/phase live in memory and
  reset to the deployed pack on restart.
  **Amended** for workshop insight capture: when an operator enables
  `WORKSHOP_INSIGHT_CAPTURE` (default off), attendee-authored discovery answers
  and a behavioural signal rollup are pushed to Control Tower, so *some* attendee
  content now leaves the instance. Raw terminal output still never does, the app
  still owns no database, and teardown is still `apps.delete`. See
  [docs/adr/0001-workshop-insight-capture.md](docs/adr/0001-workshop-insight-capture.md).
- **Security**: workspace-group based. Identity from the Apps proxy headers;
  operator access requires `ADMIN_GROUP` (default `platform_admins`)
  membership resolved via SCIM — using the caller's bearer token for service
  principals, or the vended credential to look attendees up by email.
  Optional `ACCESS_GROUP` restricts attendees.
- **Agent egress & autonomy** (P1-21): the agent runs autonomously
  (`WORKSHOP_AUTO_MODE`, no approval prompts) with a live workspace credential.
  This is acceptable **only** under the one-workspace-per-attendee topology
  (see the isolation note above), where the blast radius is a single disposable
  workspace. To limit the indirect prompt-injection surface, **public MCP
  servers (DeepWiki, Exa) are off by default** — opt in per event with
  `ENABLE_PUBLIC_MCP=true` — and the Databricks skills overlay is **pinned** via
  `SKILLS_REF`; event readiness requires the same reviewed ref, commit, and
  content SHA-256 that `assets/artifacts/manifest.json` declares.

## Deploying

`app.yaml` starts Uvicorn with exactly one worker and no explicit host or port
arguments. In the observed failed deployment, list-form
`${DATABRICKS_APP_PORT}` reached Uvicorn literally and was rejected as an
invalid integer. This app deliberately relies instead on the documented
auto-injected `UVICORN_HOST=0.0.0.0` and `UVICORN_PORT`.

### Prerequisite: the attendee CLI credential

The normal credential is the short-lived app-SP OAuth bearer returned by the
Databricks SDK from the platform-managed Databricks Apps identity. The app
reacquires and validates it through idle windows and fans out only fresh,
changed bearers. There is no token ACL prerequisite, PAT lifetime cap, or app
client secret in attendee shell env; production Linux startup fails unless
same-UID `/proc`/ptrace access is blocked with a non-dumpable server process.
As an emergency-only fallback the deployer may inject a vended
`WORKSHOP_PAT` (a static, non-rotating token reported `degraded`). Databricks
Apps OBO remains separate and powers attendee-governed reads through `[me]`;
the app OAuth bearer powers reliable builds through `[DEFAULT]`. See
[docs/admin-api.md](docs/admin-api.md) for the full contract and
credential-health alerting. Without either credential the app
serves plain terminals and shows a clear banner, but agent CLIs can't
authenticate.

For events, set `WORKSHOP_ATTENDEE_EMAIL` to the identity assigned to this
instance. It is a hint, not a prerequisite: an instance with no binding claims
the first non-admin identity to connect and persists it, since Control Tower
provisions one disposable workspace per attendee. Every other identity is still
refused, and `/readyz` reports where the binding came from. Also set
`SESSION_STATE_PATH` so terminals
survive a restart as metadata-only relaunchable ghosts; raw terminal output
remains in memory and is never journaled (see the env table in
[docs/admin-api.md](docs/admin-api.md)).

Nothing needs to be staged for the toolchain. The reviewed contract ships in the
image at `assets/artifacts/manifest.json`, pinning the source and SHA-256 of Node
(linux-x64 and linux-arm64), tmux, the Claude installer/binary, the separate
Codex npm launcher/native packages, the Databricks CLI installer/archive, the uv
binary, the Python 3.12 archive, the Omnigent hashed lock, and the skills
commit/content. Downloads are verified before execution or extraction, and
persistent reuse requires both the reviewed artifact checksum and the installed
binary checksum. `ARTIFACT_MANIFEST_PATH` is an optional override for mirrored
events that may redirect `source` only — see
[docs/artifact-manifest.md](docs/artifact-manifest.md).

### Via Control Tower

Add a `WorkshopApp` row pointing at this repo (`git_url`, branch `main`).
Configure per-event behaviour through `env_overrides` — every variable in
[docs/admin-api.md](docs/admin-api.md#deploy-time-configuration-env-vars) is
read at runtime.

`scripts/deploy_ct_sim.py` reuses an existing dedicated catalog only when CT
supplies exact expected owner, creator, type, isolation mode, and storage-root
provenance. It verifies that metadata before mutation and reads back owner plus
attendee/app-SP grants afterward. Post-deploy acceptance also requires an
attendee OAuth token (the deployer token is reused only when identities match)
to call `/api/config`, reconcile entitlements, and prove `/readyz` green.

### Dev smoke test

```bash
pip install databricks-sdk
export DATABRICKS_CONFIG_PROFILE=my-dev-workspace
python scripts/deploy_dev.py
```

Then: launch Claude → run `databricks current-user me` in the terminal →
confirm it's *you*; open a second browser profile as another user and confirm
they can't see your tabs.

## Local development

```bash
make install        # venv + pip + npm
make dev-backend    # uvicorn on :8000 with fake identity (dev@example.com, platform_admins)
make dev-frontend   # Vite on :5173, proxies /api and /ws
make test           # pytest: isolation, authz, caps, triggers, replay
```

Before committing UI changes: `make build-frontend` and commit `static/`.

## Repo map

```
app.yaml                  Databricks Apps entrypoint (single worker!)
server/                   FastAPI backend
  auth.py                 identity + group authz (SCIM /Me, cached)
  users.py                per-user HOME/env isolation
  credentials.py          vended-credential bootstrap + 10-min rotation
  cli_config.py           claude/codex/databricks CLI config writers
  sessions.py             PTY lifecycle, ownership, scrollback, reaper
  ws.py                   terminal + events websockets
  content.py              nugget packs, phases, triggers, broadcasts
  admin.py                operator/steering API
  bootstrap/              boot-time CLI installers (pinned, idempotent)
frontend/                 React + TS source (Vite)
static/                   committed production build — do not hand-edit
content/                  default nugget pack + agent catalog
scripts/                  push_content.py (steering), deploy_dev.py
docs/admin-api.md         Control Tower integration contract
```
