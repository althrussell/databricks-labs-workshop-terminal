# Project memory — Databricks Workshop

<!-- workshop-project-memory -->

This file is committed to the repo so the rule below travels with the project
into every agent and harness. Claude reads `CLAUDE.md`; Codex reads `AGENTS.md`;
Omnigent's sub-agents read whichever applies — including the Codex worker, which
runs in an isolated `CODEX_HOME` + git worktree and therefore only ever sees a
*committed, project-level* `AGENTS.md`. That is why this content is duplicated
into both files and committed on the first commit.

## Building apps

AppKit (Node.js + TypeScript + React) is the default via `databricks-apps`.
Respect an explicit framework request; `databricks-app-apx` and
`databricks-apps-python` cover alternatives. Scaffold AppKit through
`workshop-init-project <name> --appkit` (additional flags after `--`) so policy
and skills are committed at the project root.

Use **`impeccable` as the single UX authority** for visible interfaces.
AppKit/APX guidance owns components, scaffolds and APIs; the shared workshop
contract owns pacing and product context. Do not impose a common visual shell.

Use `databricks-lakebase` for shared or database-backed saved data, provisioned
non-interactively. Browser storage is for a labeled single-browser demo.
Custom agent backends use `workshop-agent-bricks-cli` when enabled; scaffold
with `workshop-init-project <name> --agentbricks` and deploy with `agentbricks
deploy`. A separate frontend follows the same UX policy and app deployment tool.

<!-- workshop-contract-slot -->

## Deployment — typecheck, deploy, share the preview

Use these deployment steps alongside the contract's practical task checks:

1. **Typecheck and build** (`npx tsc --noEmit`, then the build). Confirm AppKit
   API signatures with
   `npx @databricks/appkit docs <section>` before writing against them, and
   never write `as unknown as <T>`.
2. **Deploy with the `deploy_databricks_app` Workshop MCP tool**, passing this
   project's absolute directory (resolve it with `pwd`) and target. Relative
   MCP project paths are rejected. It uses `databricks apps deploy`, polls
   `databricks apps get`, waits for the exact deployment and app compute, and
   returns the live URL. Do not substitute bare `databricks bundle deploy` or
   arbitrary `sleep`/log-tail loops. If MCP is unavailable, run
   `workshop-app-deploy --project "$PWD" --target <target>`. A timeout on the
   deploy command is not a failed deploy; retrying the tool resumes it.
3. **Hand over the URL** and keep improving against it.

<!-- discovery-anchor -->

If something breaks, read the actual error and fix what it names. No root-cause
ceremony, no test-first ritual.

## Documents — only when they ask

Never generate a document unprompted — no architecture spec, security review,
Jira stories, test cases, or build prompt unless the user asks. Do not pitch
documentation after a build. When they do ask, write only the requested document
inside their project.

Keep `README.md` current instead: a short purpose line, the live URL once it
exists, and the `<!-- workshop-brief:v1 -->` continuity section required by the
workshop contract above. Preserve attendee-authored notes.

## Take-home source

For a take-home or code-download request, use **`workshop-export`**. Export all
`~/projects` by default or the named project with `--project`. It creates a ZIP
in `workshop_exports` under the assigned `WORKSHOP_CATALOG`/`WORKSHOP_SCHEMA`,
verifies the uploaded copy and prints the exact path and download steps. Show
those steps on screen and remind the attendee to download before teardown.
Do not describe the temporary Volume or Workspace sync as their durable copy.
