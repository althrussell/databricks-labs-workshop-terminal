# Claude Code on Databricks — Workshop Edition

Welcome! This workshop environment comes pre-configured with AI coding agents,
a full library of Databricks skills, and an authenticated Databricks CLI.

## Skills

Your skills library is loaded from
[databricks/databricks-agent-skills](https://github.com/databricks/databricks-agent-skills)
at the workshop's reviewed release, plus a small set of workshop-specific
skills maintained here.

### Databricks skills (highlights)

| Category | Skills |
|----------|--------|
| Apps | databricks-apps (AppKit), impeccable, databricks-app-apx, databricks-lakebase |
| AI & Agents | workshop-agent-bricks-cli (custom agents), databricks-agent-bricks (managed assistants/supervisors), databricks-mlflow-evaluation, databricks-model-serving, databricks-vector-search |
| Analytics | databricks-aibi-dashboards, databricks-dbsql, databricks-metric-views, databricks-unity-catalog, databricks-data-discovery |
| Data Engineering | databricks-pipelines, databricks-jobs, databricks-dabs, databricks-synthetic-data-gen, databricks-zerobus-ingest |
| Development | databricks-core, databricks-python-sdk, databricks-apps-python |
| Reference | databricks-docs, databricks-ai-functions |

Use these names exactly, and treat `ls ~/.claude/skills` as the authoritative
list. Several skill names from older Databricks skill kits were renamed or
merged; asking for one of those gets you nothing at all, silently. If a name you
half-remember isn't in that directory, check the directory rather than guessing.

Use the workshop interaction contract below for pacing and readiness.
Upstream workflow advice does not add a production planning or testing ritual
to a short workshop.

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

`jq` is **not installed**. Parse JSON with `python3 -c` instead — reaching for
`jq` out of habit costs a round trip on `command not found`:

```bash
databricks apps get <app-name> -o json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])'
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

**Python that creates things has to name its profile.** Inside the Omnigent
harnesses the environment points the Databricks SDK at an attendee-scoped
config, so a bare `WorkspaceClient()` can read and cannot build — you get a 403
on the create, not a clear auth error. Say which identity you want:

```python
import os
from databricks.sdk import WorkspaceClient

cfg = os.path.expanduser("~/.databrickscfg")
build = WorkspaceClient(profile="DEFAULT", config_file=cfg)  # provisions, deploys
mine = WorkspaceClient(profile="me", config_file=cfg)        # the attendee's own view
```

The CLI needs none of this: `databricks ...` and `databricks-me ...` already
resolve to the right identity wherever you run them, so prefer shelling out for
one-off creates and keep the SDK for real code.

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

1. **Create the project and scaffold it in one command:**
   ```bash
   cd "$(workshop-init-project my-app --appkit)"
   ```
   Pass AppKit flags through after `--`:
   ```bash
   cd "$(workshop-init-project my-app --appkit -- --features analytics)"
   ```
   For a custom agent backend, use `--agentbricks` and the
   `workshop-agent-bricks-cli` skill. Drop the scaffold flag for another framework,
   a script or notes repo, then use the matching scaffold/API guidance.

   This makes `~/projects/my-app`, scaffolds AppKit **into that directory**,
   runs `git init`, and commits the workshop's project memory as both
   `CLAUDE.md` and `AGENTS.md` so the rules travel with the repo into every
   agent and sub-agent (including Omnigent's workers running in isolated
   worktrees). The command prints the project path, so `cd "$(...)"` lands you
   inside it.

   Read preparation warnings: a usable directory does not prove scaffolding or
   the memory commit succeeded. `.workshop/preparation.json` records each result;
   `workshop-init-project <name> --json` returns structured status and a nonzero
   exit when incomplete. Repair the reported failure before delegating to an
   isolated worktree. Rerun the helper to refresh only workshop-managed memory;
   keep attendee notes and the project's README brief current.

   **Never run `databricks apps init` yourself.** It always creates a
   subdirectory named after the app, and it refuses to write into a directory
   that already exists — so scaffolding by hand leaves you with
   `my-app/my-app` and no way out except `mv my-app/* .`, which silently
   replaces the project's `CLAUDE.md` with the scaffold's generic one and
   takes the workshop's rules with it. The helper passes `--output-dir` so the
   scaffold lands in the project root directly, and it never overwrites a file
   it did not write. This overrides the `databricks-apps` skill's scaffolding
   step.
2. **Why a helper?** Every git commit automatically syncs your work to the
   Databricks Workspace at
   `/Workspace/Users/{your-email}/projects/{project-name}/`, so a terminal
   restart or a redeploy can't lose it. That copy is **not** a take-home: this
   whole workspace is deleted after the workshop, and the Workspace folder goes
   with it. To keep anything, push the repo to a git remote you own or download
   it to your own machine before you finish — see the wrap section below. The
   committed `CLAUDE.md`/`AGENTS.md` also carry the workshop contract and skills into
   followed no matter which agent or harness picks up the work.
3. **Then start building** — commit early and often. Keep the generated
   `README.md` current: a short purpose line, the live URL once you have one,
   and the `<!-- workshop-brief:v1 -->` continuity section required by the
   workshop contract below. Preserve attendee-authored notes.

<!-- workshop-contract-slot -->

## Building apps

AppKit (Node.js + TypeScript + React) is the default, using `databricks-apps`
and `workshop-init-project <name> --appkit`. Respect an explicit framework
request, using the matching platform skill (including `databricks-app-apx` or
`databricks-apps-python`). Choose the implementation for the task; no routine
framework questionnaire or insistence on a different stack.

**`impeccable` is the single UX authority for anything with an interface.**
AppKit/APX skills own scaffolds, components and platform APIs. Product context
and short workshop pacing follow the shared contract below; do not layer a
second visual recipe over Impeccable.

Use `databricks-lakebase` for shared or database-backed saved data. Provision it
non-interactively. A labeled single-browser demo may use browser storage;
never describe it as shared staff data. No saved state means no database needed.
Custom agent backends use `workshop-agent-bricks-cli` and its Python
`DurableAgentServer` runtime when enabled; an accompanying UI follows the same
UX guidance. Deploy the backend with `agentbricks deploy` and its separate UI
with the workshop app tool.

A plain read-only dashboard normally fits `databricks-aibi-dashboards`. Recommend
it with a reason; an explicit app request or interactive workflow uses an app.

### Deployment — typecheck, deploy, share the preview

Use these deployment steps alongside the contract's practical task checks:

1. **Typecheck and build** (`npx tsc --noEmit`, then the app's build). This
   prevents a failed-deploy loop. Before writing code
   against an AppKit API, check the real signature with
   `npx @databricks/appkit docs <section>` — invented shapes fail `tsc`. Never
   write `as unknown as <T>`.
2. **Deploy with the `deploy_databricks_app` Workshop MCP tool.** Pass the
   absolute project directory (resolve it with `pwd`) and target (`default`
   unless the project says otherwise). Relative MCP project paths are rejected.
   It runs the supported `databricks apps deploy -t <target>` pipeline, polls
   `databricks apps get` and the exact deployment internally, and returns only
   after the deployment is `SUCCEEDED` and the app is `RUNNING`/`ACTIVE`.
   It also requests the attendee entitlement handoff. Use this tool — not a
   hand-built `--source-code-path`, not arbitrary `sleep`/log-tail loops, and
   not a bare `databricks bundle deploy`, which uploads code but leaves the app
   stopped with no URL. If MCP is unavailable, use the identical local command:
   `workshop-app-deploy --project "$PWD" --target <target>`.

   A first deploy starts cold app compute and takes minutes. A timeout on the deploy command is not a failed deploy; the next tool call resumes the
   accepted deployment rather than submitting another. Repeated starting states
   are normal. Never print credentials while diagnosing deployment.
3. **Give the attendee the URL** and keep improving against it.

<!-- discovery-anchor -->

If something breaks, read the actual error before changing code. Fix the thing
the error names — no root-cause ceremony, no test-first ritual.

## Documents — only when they ask

**Never generate a document unprompted.** No architecture spec, security
review, Jira stories, test cases, or build prompt unless the attendee asks for
them. Do not pitch documentation after a build — a working app followed by a
sales pitch for paperwork is not the payoff they came for.

When they *do* ask — "write me an architecture doc", `/promote`, or by tapping
the suggestion card in the workshop UI — use the **`promote`** skill and give
them the full pack.

## When the workshop wraps up — get their work into their hands

When the attendee signals they're wrapping up ("that's me done", "we're out of
time", "summarise what we did"), or the workshop moves to its wrap phase, the
priority is **the take-home path**, not generating documents.

Never describe an unfinished session as complete, and never invent a deployment
that didn't happen.

**Tell them how to actually keep it.** Nothing here is a take-home. The
Workspace sync lives in a workspace that is deleted with the workshop — so
"it's committed and synced" is not the same as "it's saved". Say that once,
plainly, and give them the two routes that work:

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
  workshop is still running. Point them at the source they'd hate to retype.

Say it once and act on their answer. This is the last moment it's possible,
but a nag at the end of a good day is still a nag. If they also want handoff
documentation, they will ask — or tap the card the workshop UI already shows
them.

For Codex and Omnigent, when documents *are* requested: follow the same promote
steps inline (generate each doc as markdown, write to `~/promote/<doc>.md`,
upload with
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
