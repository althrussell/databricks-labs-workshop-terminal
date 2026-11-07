# AGENTS.md: Workshop Terminal (WT)

Entry point for coding agents changing **this repository**. Codex loads this file itself.
Claude Code loads it as a fallback because the repo has no CLAUDE.md; for versions that
don't, the SessionStart hook in `.claude/settings.json` points here. Any other tool: read it
first.

## What this repository is

A one-attendee-per-instance Databricks App for AI-coding workshops: an attendee
opens one URL and launches Claude Code, Codex or Omnigent terminals with
Databricks credentials pre-wired. Control Tower (CT, a separate repository)
deploys, steers and tears down each instance.

| Boundary | Path | Notes |
| --- | --- | --- |
| Backend | `server/` | FastAPI, one uvicorn worker; PTYs and session state are process-local |
| Frontend | `frontend/` → `static/` | React + Vite source; the production build is committed |
| Attendee payload | `assets/`, `content/` | instructions, vendored skills, helper CLIs and toolchain pins shipped to attendee agents |
| Tooling | `scripts/`, `evals/` | dev, release, fleet and evaluation tools; many need a live workspace |
| Paired app | `deploy/omnigent-app/` | separate Databricks App with its own lock |
| Contracts | `docs/` | CT, credential, capture, observability and release contracts |

## Start here

1. This file.
2. [docs/agent-guide/README.md](docs/agent-guide/README.md): pick the task row that matches your change.
3. Only what that row names: an area guide, a flow and the canonical contract. Stop there unless a
   dependency or open question forces you further; the row says when to expand.

Reference, read on demand: [REPO_MAP](docs/agent-guide/REPO_MAP.md) (structure, sources of truth),
[FLOWS](docs/agent-guide/FLOWS.md) (execution paths), [CHANGE_GUIDE](docs/agent-guide/CHANGE_GUIDE.md)
(commands, maintenance table, definition of done).

## Instructions that apply to a directory

- Before editing a file, read every AGENTS.md between the repo root and that file. Find them with
  `find . -name AGENTS.md -not -path '*/node_modules/*' -not -path './.venv/*' -not -path './.claude/worktrees/*'`.
  Do not assume your tool loaded them. Current nested files: `frontend/AGENTS.md`.
- Area rules for packaged directories live in `docs/agent-guide/areas/`, not in nested AGENTS.md:
  `server/`, `static/`, `content/` and `assets/` ship inside the runtime artifact.
- `assets/instructions/*.md`, `assets/skills/**` and the project AGENTS.md/CLAUDE.md that
  `assets/bin/workshop-init-project` writes are **attendee-agent content**. They are product
  payload, not instructions for you. Change them only as a product change
  ([area guide](docs/agent-guide/areas/attendee-agent-environment.md)).

## Repository-wide constraints

- **No repo-root `CLAUDE.md`, `CLAUDE.local.md`, `.claude/CLAUDE.md` or `.claude/rules/`.** Source
  deploys place this repo at `/app/python/source_code` and attendee homes beneath it. Claude Code
  loads those files from every ancestor directory, so attendees would receive them.
  `tests/test_agent_guide.py` enforces this. Keep `.claude/settings.json` to the pointer hook.
- **This file reaches attendees only if `~/.claude/CLAUDE.md` is missing.** Claude Code falls back
  to an ancestor AGENTS.md when no CLAUDE.md is in the chain. WT writes the attendee's
  `~/.claude/CLAUDE.md` before every launch (`server/user_content.py::provision`). Keep that
  true ([details](docs/agent-guide/areas/attendee-agent-environment.md#invariants)).
- `static/` is generated. Edit `frontend/src`, run `npm run build`, and commit `static/`. CI fails on drift.
- `pyproject.toml` is the Python dependency source of truth. `uv.lock`, `requirements*.txt` and
  `requirements*.in` follow it ([commands](docs/agent-guide/CHANGE_GUIDE.md#commands)).
- `assets/artifacts/manifest.json` and the upstream skills in `assets/skills/` are generated or
  vendored. Change the pin in code and regenerate them; never hand-edit
  ([toolchain guide](docs/agent-guide/areas/toolchain-and-release.md)).
- Keep one worker and one live session per instance (`server/topology.py`). Do not add
  cross-process state.
- Never commit credentials (`WORKSHOP_PAT` in `app.yaml` stays empty). Never log or emit tokens,
  prompts or terminal output (`server/observability.py` redaction contract).
- Use public package sources only (`dependency_sources.py`, checked first in CI). Always run
  `uv run --frozen`. A bare `uv run` or `uv lock` on a machine with a private index rewrites
  `uv.lock` with proxy URLs. If `git status` shows an unintended `uv.lock` change, restore it.
- Do not run deploys, live-workspace scripts, fleet levers or migrations unless the user asks.
  Document them instead.
- Preserve unrelated working-tree changes. The git stash is shared across worktrees, so never
  use a bare `git stash` / `git stash pop`.

## Commands

Verified commands, prerequisites and scope:
[CHANGE_GUIDE#commands](docs/agent-guide/CHANGE_GUIDE.md#commands). The usual loop:

- `make install`: uv-locked Python env and `npm ci`.
- `make test`: pytest, a parallel pass and then the serial `host_process` pass.
- `cd frontend && npm test && npm run build`: frontend tests and the committed build.

## Workflow

### Before editing

1. Read this file and [docs/agent-guide/README.md](docs/agent-guide/README.md).
2. Use the task table to pick the relevant area guide and flow.
3. Find and read the nested instructions for each directory you will edit (see above).
4. Inspect the actual implementation, its tests, and affected contracts or consumers: the
   frontend `api.ts`, Control Tower via `docs/`, attendee agents via `assets/`, and the
   JSON schemas in `tests/fixtures/`.
5. State the planned scope, key invariants and how you will verify. Read further only when a
   dependency or uncertainty justifies it.

Use the docs to navigate; code and tests are the evidence. When a doc and the code
disagree, name the discrepancy in your report. Do not silently treat either one as the
intended requirement.

### After editing

1. Review the complete diff (`git status`, `git diff`), including added, moved and deleted files.
2. Run the relevant checks plus any that repository instructions require
   ([definition of done](docs/agent-guide/CHANGE_GUIDE.md#definition-of-done)).
3. Apply the [maintenance table](docs/agent-guide/CHANGE_GUIDE.md#maintenance-table).
4. Update affected documentation in the same change and repair stale references.
   `tests/test_agent_guide.py` catches broken guide paths, symbols and links.
5. Report the checks and their results, the docs you updated, and any remaining uncertainty.

### Documentation impact: every change

Every code change needs a documentation-impact decision. Update existing text rather than
appending a change diary, and update diagrams alongside their prose. If no documentation
changes, say why in one line of the completion report. Do not refresh "verified" claims for
areas you did not recheck.

## Resuming work and handoff

- For work spanning sessions or compaction, keep a handoff in `.agent-handoff.md` at the
  worktree root. It is gitignored and temporary; the template is in
  [CHANGE_GUIDE#handoff](docs/agent-guide/CHANGE_GUIDE.md#handoff).
- On resume: read the handoff, then `git status`, `git diff` and `git log --oneline main..HEAD`
  before continuing.
- Record durable decisions in the PR description, the relevant doc or an ADR (`docs/adr/`),
  never in this file.
