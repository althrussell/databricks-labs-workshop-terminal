# Claude Code on Databricks — Workshop Edition

Welcome! This workshop environment comes pre-configured with AI coding agents,
a full library of Databricks skills, and an authenticated Databricks CLI.

## Skills

Your skills library is loaded from
[databricks-solutions/ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
(always the latest version) plus development-workflow skills from
[obra/superpowers](https://github.com/obra/superpowers).

### Databricks skills (highlights)

| Category | Skills |
|----------|--------|
| AI & Agents | databricks-agent-bricks, databricks-genie, databricks-mlflow-evaluation, databricks-model-serving, databricks-vector-search |
| Analytics | databricks-aibi-dashboards, databricks-dbsql, databricks-metric-views, databricks-unity-catalog |
| Data Engineering | databricks-spark-declarative-pipelines, databricks-jobs, databricks-synthetic-data-gen, databricks-zerobus-ingest |
| Development | databricks-bundles, databricks-apps-python, databricks-python-sdk, databricks-config, databricks-lakebase-provisioned |
| Reference | databricks-docs, databricks-ai-functions |

### Development workflow skills

brainstorming, writing-plans, executing-plans, test-driven-development,
systematic-debugging, verification-before-completion, subagent-driven-development,
dispatching-parallel-agents, requesting/receiving-code-review, using-git-worktrees.

## Databricks CLI

The Databricks CLI is pre-configured with workshop credentials. Test it:

```bash
databricks current-user me
```

Databricks authenticates with the token in `~/.databrickscfg` (rotated
automatically). If you hit auth trouble, remove `DATABRICKS_CLIENT_ID` /
`DATABRICKS_CLIENT_SECRET` from the environment and retry — credentials must
come from `~/.databrickscfg` only.

Common commands:

```bash
databricks workspace list /Workspace/Users/
databricks jobs list
databricks clusters list
```

## Two identities — build as the workshop, read data as YOU

There are two databricks CLI profiles, and they are NOT interchangeable:

- **`DEFAULT` (the workshop service principal)** — the identity for everything
  you *build, deploy, and provision*: apps, jobs, pipelines, Lakebase, model
  setup, workspace sync. It is reliable for long, unattended, and idle runs.
  Plain `databricks ...` uses it. Keep using it for all creation work.
- **`me` (you, the signed-in attendee)** — your *personal* identity. Use it
  whenever you need to show or query **the data this attendee actually has
  access to** — it respects their Unity Catalog grants, row filters, and column
  masks. The service principal sees different catalogs than the attendee, so
  listing catalogs as `DEFAULT` is misleading.

**To list or query the catalogs, schemas, tables, and data the attendee can
see, always use the `databricks-me` helper** (a thin wrapper around
`databricks --profile me ...` that auto-recovers an expired token):

```bash
databricks-me current-user me      # confirms it runs as the attendee, not the SP
databricks-me catalogs list        # the attendee's catalogs
databricks-me schemas list <catalog>
databricks-me tables list <catalog> <schema>
```

Caveat: the `me` identity comes from the live browser tab. If the tab has been
idle or closed for more than ~1 hour and `databricks-me` reports an expired
session, ask the attendee to refresh the workshop tab, then retry. (Building
and deploying are unaffected — they run as `DEFAULT`.)

## Where to create things — always inside `$WORKSHOP_CATALOG`

So that everything you build is automatically usable by the attendee:

- **Unity Catalog objects (schemas, tables, volumes, functions): create them
  inside `$WORKSHOP_CATALOG`** (use `$WORKSHOP_SCHEMA` when set). The attendee
  has inherited `ALL PRIVILEGES` on that catalog, so anything you create there
  is instantly usable by them and visible via `databricks-me`. Never create a
  brand-new top-level catalog — objects there would not be usable by the
  attendee (and you typically can't create one anyway).
- **Non-UC resources (apps, jobs, Lakebase/Postgres instances, pipelines,
  serving endpoints):** these are auto-shared to the attendee within ~one
  reconcile interval. For *instant* access after a build, run `workshop-grant-me`
  — it grants the attendee `CAN_MANAGE` on what you just created.

## Project setup

Before starting any new project:

1. **Always create the project with `workshop-init-project` first:**
   ```bash
   cd "$(workshop-init-project my-project)"
   ```
   This makes `~/projects/my-project`, runs `git init`, and commits the
   workshop's AppKit project memory as both `CLAUDE.md` and `AGENTS.md` so the
   rules travel with the repo into every agent and sub-agent (including
   Omnigent's workers running in isolated worktrees). The command prints the
   project path, so the `cd "$(...)"` lands you inside it.
2. **Why a helper?** Every git commit automatically syncs your work to the
   Databricks Workspace at
   `/Workspace/Users/{your-email}/projects/{project-name}/` — so your workshop
   work survives after this environment is torn down. The committed
   `CLAUDE.md`/`AGENTS.md` also guarantee the AppKit baseline is followed no
   matter which agent or harness picks up the work.
3. **Then start building** — commit early and often.

## How to work with users — clarify, recommend, confirm

Apply this to every request (it matters most for people building their first
Databricks project):

- **Do not rush to build.** For any app or resource request, first state your
  **recommended** approach and the key choices, then confirm before
  scaffolding, provisioning, or deploying anything. Plan, then build.
- **Lead with your recommendation.** When you ask a question, give your
  recommended option first (with a one-line "why"), then list alternatives.
  Never present a bare list of options with no guidance.
- **Clarify which Databricks resources are actually needed** before creating
  any — app type, persistence (Lakebase) vs analytics (SQL warehouse),
  catalog/schema, model/serving endpoint. Don't provision resources the
  project doesn't need.
- **End every build with the payoff:** give the user the live URL (app,
  dashboard, job run) and a short, plain-language recap of what you built.

## Building apps — always use AppKit

AppKit is the required baseline for every app. For this workshop, **every app,
dashboard, tool, or UI you build MUST use AppKit** (React + Vite + TypeScript)
following the `databricks-apps-python` skill — scaffold it with
`databricks apps init` and apply the CoDA UX defaults from the skill's
`7-appkit-ux.md`. This applies no matter which agent you are (Claude, Codex, or
Omnigent).

Do **not** reach for a Python framework (Streamlit / Dash / Gradio / Flask /
FastAPI / Reflex). The only exception is when an attendee **explicitly and
insistently** asks for a specific Python framework — in that case confirm
that's really what they want, then proceed. Otherwise it is always AppKit.

If the app needs to **save data**, provision Lakebase non-interactively
following the `databricks-lakebase-provisioned` skill — never tell the user to
click resources together in the Databricks UI. Apps with no saved state skip
Lakebase.

## After a build completes — always offer Promote

When you have **successfully deployed or completed any build** (app, pipeline,
dashboard, job, or other Databricks resource), always make this offer exactly
once before ending your response:

> "Your build is live! Want me to generate handoff documentation —
> architecture spec, security review, Jira stories, test cases, and a
> build prompt — and upload it to your Databricks Volume? Just say yes."

If the attendee says yes, use the **`promote`** skill. Claude Code users can
also type `/promote` at any time to generate docs manually.

For Codex and Omnigent: follow the same promote steps inline (generate each
doc as markdown, write to `/tmp/promote/<doc>.md`, upload with
`databricks files upload ... /Volumes/$WORKSHOP_CATALOG/$WORKSHOP_SCHEMA/promote/<email>/<timestamp>/<doc>.md`).

## Things to remember

- Never move or upload a `.git` folder when syncing or importing to the
  Databricks Workspace.
- Serverless compute first: new jobs, pipelines, and SQL should default to
  serverless unless there's a reason not to.
- Everything you create belongs in Unity Catalog at `catalog.schema.object` —
  use `$WORKSHOP_CATALOG` (and `$WORKSHOP_SCHEMA` when set), the catalog the
  workshop assigned you, so it stays usable by you (see "Where to create").
