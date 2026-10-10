---
name: databricks-app-apx
description: "Build full-stack Databricks applications using the APX framework (FastAPI + React). Only for an explicit APX request or an existing APX repository — the default for a new Databricks App is the `databricks-apps` skill (AppKit)."
---

# Databricks APX Application

Build full-stack Databricks applications using APX framework (FastAPI + React).

## Trigger Conditions

**Invoke only when**:
- The user names APX explicitly, or
- The repository is already an APX project (see the prerequisites check below)

**Do NOT invoke for a generic "build me a Databricks app" request.** The default
for a new app is **[databricks-apps](../databricks-apps/SKILL.md)** (AppKit —
Node.js + TypeScript + React), paired with
[impeccable](../impeccable/SKILL.md) for interface UX.
Load those instead unless one of the two conditions above holds.

**Do NOT invoke if user specifies**: Streamlit, Dash, Node.js, Shiny, Gradio, Flask, or other frameworks.

## Prerequisites Check

Option A)
Repository configured for use with APX.
1.. Verify APX MCP available: `mcp-cli tools | grep apx`
2. Verify shadcn MCP available: `mcp-cli tools | grep shadcn`
3. Confirm APX project (check `pyproject.toml`)

Option B)
Install APX
1. Verify uv available or prompt for install. On Mac, suggest: `brew install uv`.
2. Verify bun available or prompt for install. On Mac, suggest: 
```
brew tap oven-sh/bun
brew install bun
```
3. Verify git available or prompt for install.
4. Run APX setup commands:
```
uvx --from git+https://github.com/databricks-solutions/apx.git apx init
```


## Workflow Overview

Use only the steps needed for the requested first version. `impeccable` owns
the interface's composition, navigation and visual choices; the shared workshop
contract owns pacing, data discovery and truthful preview claims. The CRUD
examples below explain APX APIs, rather than prescribing a sidebar, table,
detail page or backend for every app. Reuse prepared data before sample rows.

## Phase 1: Initialize

```bash
# Start APX development server
mcp-cli call apx/start '{}'
mcp-cli call apx/status '{}'
```

Keep a short implementation plan when useful; no separate planning ceremony.

## Phase 2: Backend Development

### Create Pydantic Models

In `src/{app_name}/backend/models.py`:

**For API-backed CRUD, use the 3-model pattern when appropriate**:
- `EntityIn` - Input validation
- `EntityOut` - Complete output with computed fields
- `EntityListOut` - Performance-optimized summary

**See [backend-patterns.md](backend-patterns.md) for complete code templates.**

### Create API Routes

In `src/{app_name}/backend/router.py`:

**Critical requirements**:
- Always include `response_model` (enables OpenAPI generation)
- Always include `operation_id` (becomes frontend hook name)
- Use naming pattern: `listX`, `getX`, `createX`, `updateX`, `deleteX`
- Use suitable existing data, or clearly labelled samples after discovery

**See [backend-patterns.md](backend-patterns.md) for complete CRUD templates.**

### Type Check

```bash
mcp-cli call apx/dev_check '{}'
```

Fix any Python type errors reported by basedpyright.

## Phase 3: Frontend Development

**Wait 5-10 seconds** after backend changes for OpenAPI client regeneration.

### Add UI Components

```bash
# Get shadcn add command
mcp-cli call shadcn/get_add_command_for_items '{
  "items": ["@shadcn/button", "@shadcn/card", "@shadcn/table",
            "@shadcn/badge", "@shadcn/select", "@shadcn/skeleton"]
}'
```

Run the command from project root with `--yes` flag.

### Create Pages

Choose the pages and layout for the actual task with `impeccable`. For an
API-backed CRUD interface, these are implementation examples:

- A list route can live at `src/{app_name}/ui/routes/_sidebar/{entity}.tsx`
  when the app actually needs a sidebar. Choose cards, rows or a table for the
  content and screen size; include an appropriate Suspense fallback for async data.
- A detail route can live at `src/{app_name}/ui/routes/_sidebar/{entity}.$id.tsx`
  when a separate detail view serves the task. Expose relevant mutations and a
  way back; a card layout is optional.

**See [frontend-patterns.md](frontend-patterns.md) for complete page templates.**

### Update Navigation

If using the sidebar, add relevant navigation in
`src/{app_name}/ui/routes/_sidebar/route.tsx`. Do not add navigation the task
does not need.

## Phase 4: Testing

```bash
# Type check both backend and frontend
mcp-cli call apx/dev_check '{}'

# Test API endpoints
curl http://localhost:8000/api/{entities} | jq .
curl http://localhost:8000/api/{entities}/{id} | jq .

# Get frontend URL
mcp-cli call apx/get_frontend_url '{}'
```

Share the preview promptly and use the workshop contract's practical checks:
the requested action, saved state if promised and a narrow layout. Verify async
states when the app has async data. Report unavailable checks accurately;
development/release testing owns deeper coverage.

## Phase 5: Deployment & Monitoring

### Deploy to Databricks

Use DABs to deploy your APX application to Databricks. See the `databricks-asset-bundles` skill for complete deployment guidance.

### Monitor Application Logs

**Automated log checking with APX MCP:**

The APX MCP server can automatically check deployed application logs. Simply ask:
"Please check the deployed app logs for <app-name>"


The APX MCP will retrieve logs and identify issues automatically, including:
- Deployment status and errors
- Runtime exceptions and stack traces
- Both `[SYSTEM]` (deployment) and `[APP]` (application) logs
- Browser console errors (now included in APX dev logs)

**Manual log checking (reference):**

For direct CLI access:
```bash
databricks apps logs <app-name> --profile <profile-name>
```

**Key patterns to look for:**
- ✅ `Deployment successful` - App deployed correctly
- ✅ `App started successfully` - Application is running
- ❌ `Error:` - Check stack traces for issues

## Phase 6: Documentation

Maintain the compact README workshop brief and live URL. Write additional
architecture, API or code-structure documentation only when requested or useful
for a handoff; it is not a prerequisite for the attendee's first preview.

## Key Patterns

### Backend
- **3-model pattern**: Separate In, Out, and ListOut models
- **operation_id naming**: `listEntities` → `useListEntities()`
- **Type hints everywhere**: Enable validation and IDE support

### Frontend
- **Suspense hooks**: `useXSuspense(selector())`
- **Suspense boundaries**: Provide an appropriate fallback for async hooks
- **Formatters**: Currency, dates, status colors
- **Never edit**: `lib/api.ts` or `types/routeTree.gen.ts`

## Success Criteria

- [ ] Type checking passes (`apx dev check` succeeds)
- [ ] Relevant API endpoints return correct data when the app has a backend
- [ ] The attendee's useful task works, with saved state when promised
- [ ] The interface fits its task and remains usable at the requested screen size
- [ ] The preview and material demo limitations are shared promptly

## Common Issues

**Deployed app not working**: Ask to check deployed app logs (APX MCP will automatically retrieve and analyze them) or manually use `databricks apps logs <app-name>`
**Python type errors**: Use explicit casting for dict access, check Optional fields
**TypeScript errors**: Wait for OpenAPI regen, verify hook names match operation_ids
**OpenAPI not updating**: Check watcher status with `apx dev status`, restart if needed
**Components not added**: Run shadcn from project root with `--yes` flag

## Reference Materials

- **[backend-patterns.md](backend-patterns.md)** - Complete backend code templates
- **[frontend-patterns.md](frontend-patterns.md)** - Complete frontend page templates
- **[best-practices.md](best-practices.md)** - Best practices, anti-patterns, debugging

Read these files only when actively writing that type of code or debugging issues.

## Related Skills

- **[databricks-apps](../databricks-apps/SKILL.md)** - AppKit (Node/TypeScript/React), the default for a new Databricks App
- **[impeccable](../impeccable/SKILL.md)** - task-appropriate UX for every interface
- **[databricks-apps-python](../databricks-apps-python/SKILL.md)** - for Streamlit, Dash, Gradio, or Flask apps
- **[databricks-dabs](../databricks-dabs/SKILL.md)** - deploying APX apps via DABs
- **[databricks-python-sdk](../databricks-python-sdk/SKILL.md)** - backend SDK integration
- **[databricks-lakebase](../databricks-lakebase/SKILL.md)** - adding persistent PostgreSQL state to apps
