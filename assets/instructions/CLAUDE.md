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

## Things to remember

- Never move or upload a `.git` folder when syncing or importing to the
  Databricks Workspace.
- Serverless compute first: new jobs, pipelines, and SQL should default to
  serverless unless there's a reason not to.
- Everything you create belongs in Unity Catalog at `catalog.schema.object` —
  use the schema the workshop assigned you.
