# Claude Code on Databricks — Workshop Edition

Welcome! This workshop environment comes pre-configured with AI coding agents,
a full library of Databricks skills, and an authenticated Databricks CLI.

## Skills

Your skills library is loaded from
[databricks/databricks-agent-skills](https://github.com/databricks/databricks-agent-skills)
at the workshop's reviewed release, plus development-workflow skills from
[obra/superpowers](https://github.com/obra/superpowers).

### Databricks skills (highlights)

| Category | Skills |
|----------|--------|
| Apps | databricks-apps (AppKit), databricks-app-design, databricks-lakebase |
| AI & Agents | databricks-agent-bricks, databricks-mlflow-evaluation, databricks-model-serving, databricks-vector-search |
| Analytics | databricks-aibi-dashboards, databricks-dbsql, databricks-metric-views, databricks-unity-catalog, databricks-data-discovery |
| Data Engineering | databricks-pipelines, databricks-jobs, databricks-dabs, databricks-synthetic-data-gen, databricks-zerobus-ingest |
| Development | databricks-core, databricks-python-sdk, databricks-apps-python |
| Reference | databricks-docs, databricks-ai-functions |

Use these names exactly, and treat `ls ~/.claude/skills` as the authoritative
list. Several skill names from older Databricks skill kits were renamed or
merged; asking for one of those gets you nothing at all, silently. If a name you
half-remember isn't in that directory, check the directory rather than guessing.

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
session, ask the attendee to return to the workshop tab; focus/visibility
automatically forwards a fresh token, then retry. No OBO refresh is possible
while the browser sends no request. (Building and deploying are unaffected —
they run as `DEFAULT`.)

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
   `/Workspace/Users/{your-email}/projects/{project-name}/`, so a terminal
   restart or a redeploy can't lose it. That copy is **not** a take-home: this
   whole workspace is deleted after the workshop, and the Workspace folder goes
   with it. To keep anything, push the repo to a git remote you own or download
   it to your own machine before you finish — see the wrap section below. The
   committed `CLAUDE.md`/`AGENTS.md` also guarantee the AppKit baseline is
   followed no matter which agent or harness picks up the work.
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
dashboard, tool, or UI you build MUST use AppKit** (Node.js + TypeScript +
React) via the **`databricks-apps`** skill — scaffold with `databricks apps
manifest` then `databricks apps init --features <plugins>`. This applies no
matter which agent you are (Claude, Codex, or Omnigent).

Two more skills are not optional:

- **`databricks-app-design`** — required whenever the app displays *any* data:
  a KPI or overview page, a report, a chart, a table, query results, or a
  Genie/chat assistant. It decides layout, charts, semantic color, the
  loading/empty/error states, and how to show AI-result provenance, and maps
  each to real AppKit components.
- **`databricks-lakebase`** — required when the app needs to **save data**.
  Provision it non-interactively; never tell the attendee to click resources
  together in the Databricks UI. Apps with no saved state skip Lakebase.

Do **not** reach for a Python framework (Streamlit / Dash / Gradio / Flask /
FastAPI / Reflex), and do not use `databricks-apps-python` by default — it is
the Python-backend alternative, for when an attendee **explicitly and
insistently** asks for one. In that case confirm that's really what they want,
then proceed. Otherwise it is always AppKit.

A plain "build me a dashboard" with no app-specific need is a managed AI/BI
dashboard (`databricks-aibi-dashboards`), not an app. Offer both and let the
attendee choose rather than defaulting to an app.

### Before you call an AppKit build done

An app that deploys but throws on first click is not finished. Run the AppKit
validation gate and make it pass **before** you tell the attendee it's live:

1. **Update `tests/smoke.spec.ts` selectors first.** The template asserts the
   "Minimal Databricks App" heading and "hello world" text, which your app no
   longer has, so validation fails until you point them at your real UI. Use
   Playwright locators only — `getByRole`, `getByText`, `getByPlaceholder`,
   `getByLabel`. There is no `getByLabelText` in Playwright; it throws at
   runtime. Keep asserted queries small (`LIMIT` or an aggregate) — a result set
   over 1 MB aborts the analytics event and every assertion then fails.
2. **Run `databricks apps validate`.** This is the gate: it runs `appkit lint`
   (`no-double-type-assertion` — never `as unknown as <T>`), `tsc --noEmit`, and
   the smoke test. Before writing code against an AppKit API, check the real
   signature with `npx @databricks/appkit docs <section>`; invented shapes fail
   `tsc`.
3. **Fix and re-run until it passes.** Do not report success, and do not offer
   Promote, on a build whose validation is red — say what failed instead.

## After a build completes — always offer Promote

When you have **successfully deployed or completed any build** (app, pipeline,
dashboard, job, or other Databricks resource), always make this offer exactly
once before ending your response:

> "Your build is live! Want me to generate handoff documentation —
> architecture spec, security review, Jira stories, test cases, and a
> build prompt — and upload it to your Databricks Volume? Just say yes."

If the attendee says yes, use the **`promote`** skill. Claude Code users can
also type `/promote` at any time to generate docs manually.

## When the workshop wraps up — run Promote regardless

When the attendee signals they're wrapping up ("that's me done", "we're out of
time", "summarise what we did"), or the workshop moves to its wrap phase, **run
promote without asking, whether or not anything shipped.** State it in one line
and do it — don't turn it into a question.

This environment is deleted after the workshop. The wrap moment is the last
chance to write anything down, and a "yes/no" there is how a session ends with
nothing to take home. It applies just as much to a session that never got a
build working: document the architecture they were heading towards and name the
wall they hit. That document is more useful than the one for a finished demo,
because it is the one somebody can act on afterwards.

Never describe an unfinished session as complete, and never invent a deployment
that didn't happen.

**Then tell them how to actually keep it.** Nothing here is a take-home. The
promote docs live in a Volume that is dropped with the catalog, and the
Workspace sync lives in a workspace that is deleted with it — so "it's committed
and synced" is not the same as "it's saved". Say that once, plainly, and give
them the two routes that work:

- **Push to a git remote they own** (best — it takes the history with it). They
  create an empty repo on their own account, then:
  ```bash
  git remote add origin <their-repo-url>
  git push -u origin main
  ```
  Git will prompt for a credential. Have them paste their own token at the
  prompt, and never bake it into the remote URL or a file — that would leave it
  committed on a machine they don't control.
- **Download the files they care about** from the Databricks Workspace file
  browser at `/Workspace/Users/{their-email}/projects/{project}/`, while the
  workshop is still running. Point them at the promote docs first, then the
  source they'd hate to retype.

Offer it once and act on their answer. This is the last moment it's possible,
but a nag at the end of a good day is still a nag.

For Codex and Omnigent: follow the same promote steps inline (generate each
doc as markdown, write to `~/promote/<doc>.md`, upload with
`databricks files upload ... /Volumes/$WORKSHOP_CATALOG/$WORKSHOP_SCHEMA/promote/<email>/<timestamp>/<doc>.md`).
Use `~/promote`, not `/tmp/promote` — `/tmp` is shared across attendees on the
container and cleared on restart.

## Things to remember

- Never move or upload a `.git` folder when syncing or importing to the
  Databricks Workspace.
- Serverless compute first: new jobs, pipelines, and SQL should default to
  serverless unless there's a reason not to.
- Everything you create belongs in Unity Catalog at `catalog.schema.object` —
  use `$WORKSHOP_CATALOG` (and `$WORKSHOP_SCHEMA` when set), the catalog the
  workshop assigned you, so it stays usable by you (see "Where to create").
