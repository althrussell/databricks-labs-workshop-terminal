# Project memory — Databricks Workshop

<!-- workshop-project-memory -->

<!-- terminal-safety-slot -->

This file is committed as both `CLAUDE.md` and `AGENTS.md`, so every agent
working in this project receives it, including an Omnigent worker in an
isolated worktree. If another agent handed you a task, do that task and report
back to it. When you resume or take over, read `.workshop/project.json`,
`workshop-workflow show`, the README workshop brief and `PRODUCT.md`/`DESIGN.md`.
Keep other builds separate and preserve the attendee's framework and work.

<!-- workshop-contract-slot -->

## Deployment — typecheck, deploy, share the preview

Official APX (React + FastAPI) is the default: scaffold new apps with
`workshop-init-project <name> --apx` (AppKit through `databricks-apps`). Typecheck and
build (`apx dev check`, `apx build`), then deploy only with the
`deploy_databricks_app` MCP tool (absolute project path from `pwd`, target `dev`
for APX) or `workshop-app-deploy --project "$PWD" --target <target>`, never
`databricks bundle deploy`. Share the URL as a first preview. A deploy timeout
resumes; it isn't a failure. Use `databricks-lakebase` for shared saved data
and `workshop-export` for take-home code. If something breaks, read the error
and fix what it names.

<!-- discovery-anchor -->
