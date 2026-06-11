# Databricks Workshop Terminal

A purpose-built, multi-user Databricks App for AI-coding-agent training
events. Attendees open one URL and get **one-click Claude Code / Codex
terminals** with their Databricks credentials wired up automatically — plus a
steerable right-hand pane of curated Databricks insights that operators drive
live during the workshop.

Built to be deployed (and torn down) as workshop infrastructure by
[databricks-labs-control-tower](https://github.com/althrussell/databricks-labs-control-tower).

## What attendees get

- **Launch buttons** for Claude Code, Codex, and a plain terminal — the agent
  catalog is config-driven (`content/agents.json`), extensible per event.
- **Zero-touch auth**: Control Tower vends a workspace credential at deploy
  time (`WORKSHOP_PAT`); the app chains short-lived rotating tokens off it
  and feeds them to every CLI config. Attendees never see a token screen.
- **Fully isolated sessions**: per-user HOME directories, sessions strictly
  bound to their owner, secrets stripped from terminal env. Up to 10
  attendees per instance.
- **Resilient terminals**: PTYs survive page refreshes and wifi blips —
  reconnect and your scrollback replays.
- **Insight nuggets**: a collapsible pane of docs, best practices, and blog
  pointers that follows the workshop phase and reacts to coarse activity
  signals (which agent is running, idle time). No terminal content is ever
  inspected.

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
- **No external state**: no Lakebase, no database. Content/phase live in
  memory and reset to the deployed pack on restart; teardown is a plain
  `apps.delete`.
- **Security**: workspace-group based. Identity from the Apps proxy headers;
  operator access requires `ADMIN_GROUP` (default `platform_admins`)
  membership resolved via SCIM — using the caller's bearer token for service
  principals, or the vended credential to look attendees up by email.
  Optional `ACCESS_GROUP` restricts attendees.

## Deploying

### Prerequisite: vended credential

The deployer (Control Tower) must inject a workspace credential as the
`WORKSHOP_PAT` env var (or secret). Databricks Apps OBO scopes exclude the
Token API, so the app can't mint credentials itself — see
[docs/admin-api.md](docs/admin-api.md) for the vending contract. Without it
the app serves plain terminals and shows a clear banner, but agent CLIs
can't authenticate.

### Via Control Tower

Add a `WorkshopApp` row pointing at this repo (`git_url`, branch `main`).
Configure per-event behaviour through `env_overrides` — every variable in
[docs/admin-api.md](docs/admin-api.md#deploy-time-configuration-env-vars) is
read at runtime.

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
