# Claude Code on Databricks — Workshop Edition

<!-- terminal-safety-slot -->

<!-- workshop-teammate-slot -->

<!-- workshop-contract-slot -->

## Databricks essentials

- `databricks …` is the workshop's build identity (`DEFAULT` profile): use it
  to build, deploy and provision. `databricks-me …` shows what the attendee can
  see. In Python, name the profile: `WorkspaceClient(profile="DEFAULT")` builds,
  `profile="me"` reads as them.
- Create Unity Catalog objects in `$WORKSHOP_CATALOG` (and `$WORKSHOP_SCHEMA`
  when set), never a new catalog. Prefer serverless compute.
- Official APX (React + FastAPI) is the default for a new custom app
  (`workshop-init-project <name> --apx`, the `apx` skill); AppKit stays
  available through `databricks-apps`. Use Streamlit only if they ask for it.
- Shared saved data uses `databricks-lakebase`. For "take my work home", use
  the `workshop-export` skill.
- Your skills are in `~/.claude/skills`; the `workshop` skill has the details.

## Deployment — typecheck, deploy, share the preview

Typecheck and build (`apx dev check`, `apx build`), then deploy only with
`workshop-app-deploy` (the `deploy_databricks_app` MCP tool), never
`databricks bundle deploy`. Share the URL as a first preview. A deploy timeout
isn't a failure: run it again and it resumes.

<!-- discovery-anchor -->

If something breaks, read the actual error and fix what it names. Never print
credentials while diagnosing.
