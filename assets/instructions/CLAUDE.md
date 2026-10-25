# Claude Code on Databricks — Workshop Edition

<!-- terminal-safety-slot -->

`ls ~/.claude/skills` is the authoritative skill list; older Databricks skill
names were renamed or merged, so check it rather than guessing.

## Databricks CLI and identities

Plain `databricks ...` is the workshop service principal (`DEFAULT` profile):
use it to build, deploy and provision (apps, jobs, pipelines, Lakebase). Use
`databricks-me ...` to read what the **attendee** can see: it runs as them and
respects their grants, row filters and masks (`databricks-me catalogs list`).
If it reports an expired session, ask them to return to the workshop tab, then
retry. Credentials come from `~/.databrickscfg` only. `jq` is not installed;
parse JSON with `python3 -c`.

Python that creates things must name its profile; inside Omnigent a bare
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
when set); the attendee has inherited `ALL PRIVILEGES` there. Never create a
new top-level catalog. Apps, jobs, Lakebase, pipelines and serving endpoints
are shared with the attendee automatically; `workshop-grant-me` grants
`CAN_MANAGE` immediately. Prefer serverless compute.

## Project setup

Start every new project with the helper, which scaffolds into the project root
and commits the workshop memory as both `CLAUDE.md` and `AGENTS.md` (so it
reaches every agent, including Omnigent's isolated worktree workers):

```bash
cd "$(workshop-init-project my-app --apx)"           # add APX flags after --
cd "$(workshop-init-project my-agent --agentbricks)" # custom agent backend
```

Drop the scaffold flag for another framework or a notes repo.
Never run `databricks apps init` yourself: it always creates a subdirectory
named after the app, and moving it out replaces the project's `CLAUDE.md`. `workshop-init-project <name> --json` reports
preparation status; repair a failure before delegating. Rerunning it refreshes
only workshop-managed memory. Commits sync to the Workspace so a terminal
restart or a redeploy can't lose it; that copy is **not** a take-home.

<!-- workshop-contract-slot -->

## Saved data and agents

Use `databricks-lakebase` (non-interactive) for shared saved data. Custom agent
backends use `workshop-agent-bricks-cli` and `agentbricks deploy`; their UI
deploys with the workshop app tool.

## Deployment — typecheck, deploy, share the preview

`deploy_databricks_app` (MCP) takes the absolute project directory (`pwd`) and
target: `dev` for a standard APX scaffold, `default` for AppKit, otherwise the
target in `databricks.yml`. It runs `databricks apps deploy`, polls
`databricks apps get` for the exact deployment, returns only when it `SUCCEEDED`
and the app is `RUNNING`/`ACTIVE`, and requests the attendee's access. Never use a
hand-built `--source-code-path` or a bare `databricks bundle deploy`, which
leaves the app stopped with no URL. Without MCP run
`workshop-app-deploy --project "$PWD" --target <target>`. Run `apx dev check`
and `apx build` first (the framework's checks otherwise); check AppKit
signatures with `npx @databricks/appkit docs <section>` and never write
`as unknown as <T>`. A first deploy takes minutes.
A timeout on the deploy command is not a failed deploy; the next call resumes it. Never print credentials while diagnosing.

<!-- discovery-anchor -->

If something breaks, read the actual error and fix what it names.

## Documents and take-home

Write documents only when asked, inside the project. For “take my work home”
use the `workshop-export` skill (`--project <path>` for one project) and show
its Volume link and the Catalog → … → workshop_exports → code →
File options → Download file steps. "It's committed and synced" is not the same as "it's saved"
on their computer: everything here is deleted after the workshop, so remind
them to download before teardown and never claim they have. For a Git remote
they own: `git remote add origin <their-repo-url>`, `git push -u origin main`;
never bake a token into the remote URL or a file.
