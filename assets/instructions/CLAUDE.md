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
| Apps | databricks-apps (AppKit), workshop-design-studio, databricks-app-design, databricks-lakebase |
| AI & Agents | databricks-agent-bricks, databricks-mlflow-evaluation, databricks-model-serving, databricks-vector-search |
| Analytics | databricks-aibi-dashboards, databricks-dbsql, databricks-metric-views, databricks-unity-catalog, databricks-data-discovery |
| Data Engineering | databricks-pipelines, databricks-jobs, databricks-dabs, databricks-synthetic-data-gen, databricks-zerobus-ingest |
| Development | databricks-core, databricks-python-sdk, databricks-apps-python |
| Reference | databricks-docs, databricks-ai-functions |

Use these names exactly, and treat `ls ~/.claude/skills` as the authoritative
list. Several skill names from older Databricks skill kits were renamed or
merged; asking for one of those gets you nothing at all, silently. If a name you
half-remember isn't in that directory, check the directory rather than guessing.

There is no process/workflow skill layer in this workshop — no planning,
test-first, code-review, or verification ritual to invoke before building.
Build the thing, deploy it, show it to them.

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
   Drop `--appkit` for something that isn't an app (a script, a notes repo).

   This makes `~/projects/my-app`, scaffolds AppKit **into that directory**,
   runs `git init`, and commits the workshop's project memory as both
   `CLAUDE.md` and `AGENTS.md` so the rules travel with the repo into every
   agent and sub-agent (including Omnigent's workers running in isolated
   worktrees). The command prints the project path, so `cd "$(...)"` lands you
   inside it.

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
   committed `CLAUDE.md`/`AGENTS.md` also guarantee the AppKit baseline is
   followed no matter which agent or harness picks up the work.
3. **Then start building** — commit early and often. Keep the generated
   `README.md` current: one line on what the app is for, and the live URL once
   you have one. It is the attendee's take-home reminder and it costs a
   sentence.

## Tempo — get something on their screen fast

The attendee came to see their idea become real. Everything below serves the
shortest path from what they asked for to a URL they can open.

- **Show something early.** Build a thin but real version, deploy it, and give
  them the URL as soon as it renders. Then keep improving it. Never disappear
  into a long build with nothing on screen — a 25-minute silent stretch is a
  failure even if the result is good.
- **Iterate against the live URL.** After the first deploy, every enhancement
  is: change it, redeploy, tell them what to look at.
- **At most one or two questions**, and only when the answer changes what you
  build. Otherwise pick the sensible default, build it, and say what you chose.
  A trivial or self-contained ask — a page, a script, a query, a game — gets
  zero questions. Just build it.
- **Confirm only when it costs something.** State a one-line plan and get a yes
  before provisioning real resources (Lakebase, warehouses, serving endpoints)
  or when the request is genuinely ambiguous. Never for a self-contained build.
- **Short todo lists.** Scaffold, build, deploy, share. If a todo is not on the
  path to something the attendee can see, it does not belong on the list. Name
  todos by outcome ("build the fraud-scoring page"), never by step number.
- **Scaffold minimally.** Only the AppKit features and plugins the app actually
  needs. No Lakebase, SQL, or Genie wiring for an app that has no data.
- **Lead with your recommendation.** When you do ask, give your recommended
  option first with a one-line "why", then alternatives. Never a bare list.
- **Never announce process.** No design systems, no gates, no methodology.
  Describe what their product does.
- **End every build with the payoff:** the live URL (clickable) and a short,
  plain-language recap of what it does.
- **Keep noticing.** Fewer questions does not mean less listening. Record what
  they already told you about their stack, their problem, and what is blocking
  them, as it comes up — never as a wrap-up interview.

## Building apps — always use AppKit

AppKit is the required baseline for every app. For this workshop, **every app,
dashboard, tool, or UI you build MUST use AppKit** (Node.js + TypeScript +
React) via the **`databricks-apps`** skill — scaffolded for you by
`workshop-init-project --appkit` (see Project setup above; do not call
`databricks apps init` directly). This applies no matter which agent you are
(Claude, Codex, or Omnigent).

Three more skills are not optional:

- **`workshop-design-studio`** — required for **anything with a visible
  interface**, every time. It carries the visual baseline and a library of
  ready-made AppKit patterns, so what the attendee leaves with looks
  deliberately designed rather than like a framework starter with the colours
  changed. Start from its patterns instead of inventing layout from scratch —
  it is both faster and better.
- **`databricks-app-design`** — required whenever the app displays *any* data:
  a KPI or overview page, a report, a chart, a table, query results, or a
  Genie/chat assistant. It decides chart choice, semantic color, and how to
  show AI-result provenance, mapped to real AppKit components.
- **`databricks-lakebase`** — required when the app needs to **save data**.
  Provision it non-interactively; never tell the attendee to click resources
  together in the Databricks UI. Apps with no saved state skip Lakebase.

**Where they overlap, the split is by surface.** `databricks-apps` owns
scaffolding, APIs, and deployment. **Inside a data surface** — charts, KPIs,
tables, query results, Genie answers — `databricks-app-design` owns the
decisions, and on any chart-vocabulary conflict it wins outright.
**Everywhere else in the app** — page composition, navigation, brand,
typography, spacing, imagery, motion, empty-state character —
`workshop-design-studio` owns it. An app with no data surface (a game, a
landing page, a toy) uses the design studio only; `databricks-app-design` does
not apply to it.

### Design happens silently

Attendees are not designers and most are not engineers — they must never be
asked to make a design decision, and never told the machinery exists.

- **Never ask a design question.** No palette, layout, or creative-direction
  choices. Infer from what they asked for and decide the rest yourself.
- **Never narrate the process.** Do not mention design systems, baselines,
  critique, or the skill by name. Describe what their product now *does*, not
  how it was designed.
- The exception: if they raise branding or design themselves, or hand you a
  brand kit, engage with them properly. Then it is their topic, not yours.

The platform is not the brand — do not impose Databricks colours or console
chrome on an attendee's app unless they ask for it.

### The visual baseline — non-negotiable, applied while you build

Every interface you build clears this bar. It costs nothing at build time
because you apply it as you write the components, not as a pass afterwards.
`workshop-design-studio` carries ready-made AppKit patterns for the app shell,
first-run state, KPI row, chart card, table, empty/loading/error states, and
forms — start from those.

- **Type does the hierarchy.** A real scale with a genuinely large display size
  for the primary heading. Never a page where everything is 14-16px.
- **Space generously and consistently.** Use one spacing rhythm throughout.
  Cramped default padding is the single clearest tell of an untouched template.
- **One accent colour, used for meaning** — the primary action, the live value,
  the thing that changed. Colour as decoration is worse than no colour.
- **Give the page a focal point.** Something should be obviously the most
  important thing on screen. If everything competes equally, nothing reads.
- **Real states, always.** Anything asynchronous gets loading, empty, and error
  states. An empty state with character is a moment attendees remember.
- **Considered surfaces.** Deliberate background, border, and elevation
  choices — not stock cards on stock grey.
- **Motion on state change**, brief and purposeful, and honour reduced motion.
- **Accessible by construction:** text contrast at least 4.5:1, visible focus
  states on every interactive element, alt text on meaningful images, and
  layouts that survive a narrow window. Apply these as you write the markup —
  there is no gate that will catch them later.
- **One memorable moment per app.** A considered hero, a satisfying transition,
  a chart that reads instantly. One is enough.

**After the first deploy, take one look at your own work** before you move on:
is there a clear focal point, is the type scale doing real work, is spacing
consistent, does the accent mean something, do the states exist, is contrast
and focus right? Fix what is cheap, then tell the attendee what changed in
product terms. That is one pass, in your head, against the live URL — not a
script, not a browser run, not a document.

Do **not** reach for a Python framework (Streamlit / Dash / Gradio / Flask /
FastAPI / Reflex), and do not use `databricks-apps-python` by default — it is
the Python-backend alternative, for when an attendee **explicitly and
insistently** asks for one. In that case confirm that's really what they want,
then proceed. Otherwise it is always AppKit.

A plain "build me a dashboard" with no app-specific need is a managed AI/BI
dashboard (`databricks-aibi-dashboards`), not an app. Offer both and let the
attendee choose rather than defaulting to an app.

### The ship gate — typecheck, deploy, open the URL

**Ship when the app is deployed and the URL loads.** That is the whole gate:

1. **Typecheck and build** (`npx tsc --noEmit`, then the app's build). Seconds,
   no browser, and it prevents a failed-deploy loop. Before writing code
   against an AppKit API, check the real signature with
   `npx @databricks/appkit docs <section>` — invented shapes fail `tsc`. Never
   write `as unknown as <T>`.
2. **Deploy with `databricks apps deploy -t <target>`** (`default` unless the
   project says otherwise), then open the URL once to confirm it responds.
   Use that command — not a hand-built `--source-code-path`, and not a bare
   `databricks bundle deploy`, which uploads the code but leaves the app
   stopped with no URL.

   A first deploy starts cold app compute and takes minutes, well past a
   foreground command timeout, so run it in the background and poll:
   ```bash
   databricks apps get <app-name> -o json \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["compute_status"]["state"])'
   ```
   Wait for `ACTIVE`. A timeout on the deploy command is not a failed deploy;
   check the state before assuming anything is wrong or retrying. Repeated
   `App is starting...` lines are the normal shape of a cold start, not a hang.
3. **Give the attendee the URL** and keep improving against it.

<!-- discovery-anchor -->

**Do not** run `databricks apps validate`, install Playwright browsers, or
write or update `tests/smoke.spec.ts` — unless the attendee asks for tests, or
a deploy has already failed and you are debugging it. Never install Chromium
as a condition of calling something done. Never put "update smoke tests",
"run validate", or "run the design gate" on a todo list.

This overrides the `databricks-apps` skill's instruction to always update the
smoke test before validating. In this workshop, the live URL is the gate.

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
