# Claude Code on Databricks — Workshop Edition

<!-- terminal-safety-slot -->

<!-- workshop-teammate-slot -->

<!-- workshop-contract-slot -->

## Databricks essentials

- `databricks …` (`DEFAULT` profile) builds, deploys and provisions;
  `databricks-me …` (`me` profile) reads as the attendee. In Python:
  `WorkspaceClient(profile="DEFAULT")` builds, `profile="me"` reads. Run
  `workshop-grant-me` for instant access.
- Create Unity Catalog objects in `$WORKSHOP_CATALOG` (`$WORKSHOP_SCHEMA` when
  set), never a new catalog. Clone `$WORKSHOP_DEMO_CATALOG` tables with `DEEP
  CLONE` into `$WORKSHOP_CATALOG` to write. Prefer serverless.
- Official APX (React + FastAPI) is the default (`workshop-init-project <name>
  --apx`, `apx`; AppKit via `databricks-apps`). Use Streamlit only if asked.
  Shared saved data uses `databricks-lakebase`. For take-home code, use
  `workshop-export`; `~/.claude/skills` has `workshop`.

## Deployment — typecheck, deploy, share the preview

Run `apx dev check` and `apx build`, then deploy only with
`workshop-app-deploy` (`deploy_databricks_app` MCP), never `databricks bundle
deploy`. Share the URL as a first preview. A deploy timeout resumes when rerun.

<!-- discovery-anchor -->

If something breaks, read the error and fix what it names. Never print credentials.
