---
name: workshop
description: How this Workshop Terminal works - the build and attendee Databricks identities, where to create things, starting a project, deploying an app, the workflow helper, the optional browser check and taking code home. Read it when you set up, deploy, export or hit a workshop-specific error.
---

# Workshop Terminal mechanics

## Two Databricks identities

Plain `databricks …` is the workshop service principal (`DEFAULT` profile). Use
it to build, deploy and provision apps, jobs, pipelines and Lakebase.
`databricks-me …` runs as the **attendee** and respects their grants, row
filters and masks (`databricks-me catalogs list`). If it reports an expired
session, ask them to return to the workshop tab, then retry. Credentials come
from `~/.databrickscfg` only. `jq` is not installed; parse JSON with
`python3 -c`.

Python that creates things must name its profile. Inside Omnigent a bare
`WorkspaceClient()` is attendee-scoped and gets a 403 on create:

```python
import os
from databricks.sdk import WorkspaceClient

cfg = os.path.expanduser("~/.databrickscfg")
build = WorkspaceClient(profile="DEFAULT", config_file=cfg)  # provisions, deploys
mine = WorkspaceClient(profile="me", config_file=cfg)        # the attendee's view
```

## Where to create things

Create Unity Catalog objects inside `$WORKSHOP_CATALOG` (and `$WORKSHOP_SCHEMA`
when set); the attendee has `ALL PRIVILEGES` there. Never create a new
top-level catalog. Apps, jobs, Lakebase, pipelines and serving endpoints are
shared with the attendee automatically; `workshop-grant-me` grants `CAN_MANAGE`
immediately. Copy shared demo data into an owned table before changing it.
Metastore `system.*` tables (`system.billing`, `system.access`, `system.compute`,
`system.lakeflow`, `system.ai_gateway`, `system.serving`) are restricted for
multi-tenant isolation: for FinOps or observability apps, use
`$WORKSHOP_DEMO_CATALOG` or synthetic tables in `$WORKSHOP_CATALOG` instead of
probing `system.*`. On views, use `DESCRIBE TABLE` rather than `DESCRIBE DETAIL`.
Workshop compute is complimentary during the session; if an attendee asks about
cost, explain that their workshop seat is free and give standard production DBU
sizing guidance.

## Starting a project

```bash
cd "$(workshop-init-project my-app --apx)"           # add APX flags after --
cd "$(workshop-init-project my-agent --agentbricks)" # custom agent backend
```

Drop the scaffold flag for another framework or a notes repo.
Never run `databricks apps init` yourself: it creates a subdirectory named after the app,
and moving it out replaces the project's `CLAUDE.md`.
`workshop-init-project <name> --json` reports preparation status; fix a
failure before delegating. Rerunning it refreshes only workshop-managed memory.
Commits sync to the Workspace, so a terminal
restart or a redeploy can't lose it. That copy is **not** a take-home.

## Deploying

`deploy_databricks_app` (MCP) takes the absolute project directory (`pwd`) and
a target: `dev` for a standard APX scaffold, `default` for AppKit, otherwise
the target in `databricks.yml`. It runs `databricks apps deploy`, polls
`databricks apps get` for the exact deployment, returns only when it `SUCCEEDED` and the app is
`RUNNING`/`ACTIVE`, and requests the attendee's access. Without MCP, run
`workshop-app-deploy --project "$PWD" --target <target>`. Never use a hand-built
`--source-code-path` or a bare `databricks bundle deploy`: that leaves the app
stopped with no URL.

Read a deployed app's logs with `databricks apps logs <app>`; the apx MCP logs
tool cannot authenticate on a seat.

Any control that writes (approve, save, assign, submit) disables while its request
is in flight and after success, and the server write is an upsert on a natural key,
so a repeat click cannot duplicate a row (F-018, 31 Oct).

A Lakebase app binds the resource in `databricks.yml`, and its `app.yml` must read
`LAKEBASE_ENDPOINT` with `valueFrom` set to that resource's name. `workshop-app-deploy`
refuses to deploy until it does, and names what is missing.

Run `apx dev check` and `apx build` first (otherwise the framework's own
checks). For AppKit, check signatures with `npx @databricks/appkit docs
<section>` and never write `as unknown as <T>`. A first deploy takes minutes. A
timeout on the deploy command is not a failed deploy; the next call resumes it.
Custom agent backends use `workshop-agent-bricks-cli` and `agentbricks deploy`;
their UI deploys with the workshop app tool. Test-run a plain app only on
`$DATABRICKS_APP_PORT`. An apx app runs on the port `apx dev start` prints
(`Dev server started at http://localhost:<port>`, 9000 for the first app). Do
not edit `~/.apx/registry.toml` or set `APX_DEV_SERVER_PORT` to move it.

## Seeing the app (when `workshop-browser` is enabled)

When `WORKSHOP_BROWSER_ENABLED=false` (default), `workshop-browser` is not
installed: do one fast Impeccable pass, deploy, and let the attendee test the
live URL in their browser. When enabled for the event
(`WORKSHOP_BROWSER_ENABLED=true`), `workshop-browser inspect <url>` (add
`--deployed` for the live app, `--do click:"Save"` for an action) captures
**desktop-only** screenshots into `.workshop/evidence/` so you can review and
iterate on the rendered page. Open each screenshot with your image tool before
describing it. Never render phone/mobile viewports, install Playwright
browsers, or run `databricks apps validate`.

## The workflow helper

- `workshop-workflow show`: the record, the next step and open requests.
- `workshop-workflow requests` and `workshop-workflow done <id> "what you did"`:
  the attendee's requests from the Your build panel.
- `workshop-workflow path rapid|guided --by attendee`, and `workshop-workflow
  direction "…" --confirmed-by attendee` once they confirm a guided direction.
- `workshop-workflow decide "<choice>" --by attendee` records a build choice the attendee made; `show` lists it as settled for every agent after them. `--by agent` records your own default, which later agents ask about.
- `workshop-workflow phase build --evidence preview=<url>` records the first preview and moves the stage to build.
- `workshop-workflow plan --put "<summary>"` records the plan you put to them; `plan --approved --source attendee` records their yes. `workshop-init-project` refuses until the second command has run.
- `workshop-workflow prefs [--approach co_build|learn|lead] [--detail
  adaptive|brief|detailed] [--default-path rapid|guided] [--background
  domain|data|developer|exploring] [--industry <industry>]` saves a lasting preference. Mention it once
  the helper confirms.
- `workshop-workflow check` validates the record before you hand over.

Keep the README's `<!-- workshop-brief:v1 -->` section to a few bullets
(request, first version, facts, labelled assumptions, checks), and commit it
with the record before delegating so worktree workers receive both.

## Documents and take-home

Write documents only when asked, inside the project. For "take my work home",
tell the attendee they can click **Download my code** in the Workshop Terminal
header to download the ZIP directly, or use the `workshop-export` skill
(`--project <path>` for one project). Show its
Volume link and the Catalog → … → workshop_exports → code →
File options → Download file steps. "It's committed and synced" is not the same as "it's saved"
on their computer: everything here is deleted after the workshop, so remind them
to download before teardown, and never claim they have. For a Git remote they
own: `git remote add origin <their-repo-url>`, then `git push -u origin main`;
never bake a token into the remote URL or a file.
