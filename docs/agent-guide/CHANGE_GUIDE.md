# Change guide: how to change and maintain WT

How to size a change, which commands prove it, which docs it obliges you to update, and when
it is done. Routing is in [README.md](README.md); structure is in [REPO_MAP.md](REPO_MAP.md).

## Scoping a change

1. **Find the owning area.** Use the [task table](README.md#task-routing-table). Then open the
   entry point and its test file before anything else.
2. **Find callers.** Run `grep -rn '<symbol>' server frontend/src assets/bin scripts tests`.
   Most server state is a module-level singleton (`session_manager`, `user_manager`,
   `content_service`, `event_emitter`, `credential_manager`), so callers can be anywhere in
   `server/`.
3. **Find consumers across a boundary.** Ask which of these the change reaches:
   - The browser: `frontend/src/api.ts`, `frontend/src/events.ts` and the WebSocket close
     codes.
   - Control Tower (separate repo): anything under `/api/admin/*`, the capture event shapes,
     `/readyz`, the env vars CT sets, and the release manifest.
   - Attendee agents: anything written by `server/user_content.py` or `server/cli_config.py`,
     and anything in `assets/`.
   - The paired Omnigent App: `deploy/omnigent-app/`, `/api/obo/refresh`, and the host launch.
   - Operators: `docs/operator-runbook.md` and the operator panel.
4. **Check shared contracts.** Wire contracts are in `docs/` (see
   [REPO_MAP: sources of truth](REPO_MAP.md#sources-of-truth)) and in the JSON schemas under
   `tests/fixtures/`. A field rename is a contract change on both sides.
5. **Check the cross-cutting rules** each change can trip:
   - A new attendee write route passes through closeout admission
     (`server/closeout_http.py::produces_capture`) and may need a capture journal entry.
   - A new env var for PTYs must be added to `server/users.py::_SHELL_ENV_ALLOWLIST`.
   - A new event or log field must stay inside the redaction allowlist
     (`server/observability.py::safe_event_attributes`).
   - Anything process-local assumes one worker and one session (`server/topology.py`).
   - Instruction or skill text changes the attendee agent's startup context; measure it with
     `scripts/measure_startup_context.py`.
   - Frontend changes regenerate `static/`.

## Commands

Run from the repository root unless a working directory is given.

**Status key:**

- *ran ✓*: executed during the guide's inspection (2026-11-07, `c1bae5d`, macOS arm64, uv
  venv on Python 3.12.13, Node 24.15.0). CI pins Python 3.11 and Node 24.22.1.
- *ran ✗*: executed and failed, with the reason.
- *CI*: runs in `.github/workflows/`; not executed locally.
- *inspected*: read, not run.
- *live*: needs a Databricks workspace or a deployed app. Do not run without the user's go-ahead.

### Setup and the local loop

| Command | Prerequisites | Scope | Status |
| --- | --- | --- | --- |
| `uv sync --frozen` (or `make install`, which adds `cd frontend && npm ci`) | uv 0.13.5 (CI pin), network to the package index | Python env with the `dev` group | ran ✓ |
| `make dev-backend` | the env above | uvicorn :8000, `LOCAL_DEV=1`, fake admin identity, `DATA_ROOT=/tmp/workshop-terminal-dev`; skips installers and credentials | inspected |
| `make dev-frontend` | `npm ci` in `frontend/` | Vite :5173, proxies `/api` and `/ws` | inspected |

### Tests and checks

| Command | Prerequisites | Scope | Status |
| --- | --- | --- | --- |
| `uv run --frozen python -m pytest tests/ -q -m "not host_process" -n 2 --dist loadfile --timeout=120` | uv env | everything except `host_process`; LOCAL_DEV with real PTYs | ran ✓: 4292 passed, 68 skipped, 195 s |
| `uv run --frozen python -m pytest tests/ -q -m host_process --timeout=300` | uv env | real host processes and signals, serial | ran ✓: 34 passed, 11 skipped |
| `make test` | uv env | both passes above | the two parts ran ✓ |
| `uv run --frozen python -m pytest tests/test_agent_guide.py -q` | uv env | this guide's paths, symbols, links and placement rules | ran ✓ |
| `cd frontend && npm ci && npm test` | Node 24 | `frontend/src/*.test.ts` via `tsx` | ran ✓: 207 passed |
| `cd frontend && npm run build`, then `git status --porcelain -- static` | Node 24 | type-check, then rebuild `static/` | ran ✓: no drift from the committed build |
| `uv lock --check`, the two `uv export` lines in `.github/workflows/ci.yml`, then `git diff --exit-code -- pyproject.toml uv.lock requirements.txt requirements-dev.txt` | uv env | lock and export consistency | ran ✓: no diff |
| `python scripts/check_dependency_sources.py` | uv, direct access to `https://pypi.org/simple` | public-registry guard, first CI job | ran ✗ locally: connection to pypi.org refused by this machine's network. CI runs it |
| `WORKSHOP_E2E_BROWSER_TESTS=1 WORKSHOP_BROWSER_EXECUTABLE=<chrome> .venv/bin/python -m pytest tests/test_wizard_rendered.py tests/test_projects_rendered.py tests/test_workshop_lifecycle_rendered.py tests/test_generated_app_browser_rendered.py -q` | `uv pip install -r evals/generated_apps/requirements-browser.txt`, Chrome | rendered UI against local API fixtures | CI (`wizard-browser`); skipped in the local run |
| `uv run --frozen --only-group quality python scripts/check_quality_toolchain.py` | Python 3.12 | evaluator API contract, no workspace | CI |
| `uv run --frozen python scripts/measure_startup_context.py` | installed CLIs | attendee startup context size | inspected |

### Toolchain, generation and release

| Command | Prerequisites | Scope | Status |
| --- | --- | --- | --- |
| `uv run --frozen python scripts/build_artifact_manifest.py --check` / `--write` | network to the upstreams | regenerate `assets/artifacts/manifest.json` from the pins in code | CI (weekly `skills-freshness.yml`); inspected |
| `uv run --frozen python scripts/refresh_vendored_skills.py --check` / `--write` | network to GitHub | re-vendor upstream `assets/skills/` at the pinned commit | CI weekly; inspected |
| `uv run --frozen python scripts/refresh_apx.py --check` / `--write`; `scripts/refresh_impeccable.py --check` | network | APX skill bytes; Impeccable install | CI (`apx-toolchain`, `impeccable-delivery`) |
| `scripts/check_apx_toolchain.py --bootstrap`, `scripts/check_agentbricks_toolchain.py --bootstrap` | Linux | real installers and scaffolds, no cloud writes | CI |
| `make build-release` | **Linux x86_64 CPython 3.11** (refuses other hosts) | PEX and `release-manifest.json` | CI (`package`: built twice, `cmp`, offline container smoke) |
| `scripts/smoke_release_container.sh dist`, `scripts/smoke_browser_container.sh dist dist/browser-smoke` | container runtime, a built `dist/` | packaged runtime smoke | CI |

### Live environment (document; do not run unprompted)

| Command | Needs | Effect |
| --- | --- | --- |
| `uv run --frozen python scripts/deploy_dev.py` | `DATABRICKS_CONFIG_PROFILE`, clean tree | uploads the working tree **including gitignored files** and deploys a dev app; mints a 12 h PAT unless `--no-pat` |
| `uv run --frozen python scripts/deploy_ct_sim.py --dry-run` | none for `--dry-run` | the CT-like deploy plan; without `--dry-run` it is a real deploy |
| `scripts/push_content.py`, `scripts/pull_diagnostics.py`, `scripts/rehearse.py`, `scripts/soak.py watch` | app URL, admin-group token | steer or observe a live instance |
| `scripts/soak.py chaos` | as above | **mutates**: recovers attendees, toggles the Omnigent tier |
| `scripts/evaluate_generated_apps.py`, `scripts/run_generated_app_journey.py --execute` | CT and workspace access | see [docs/generated-app-evaluation-runbook.md](../generated-app-evaluation-runbook.md) |
| live identity check | a deployed instance | [docs/verification-gate.md §5](../verification-gate.md) |

### Instruction-loading probe (on a Claude Code or Codex upgrade)

The placement rules in [AGENTS.md](../../AGENTS.md) rest on measured loader behaviour
(Claude Code 2.1.318, Codex 0.166.0, 2026-11-07). Re-measure when either harness pin moves.
This costs one model call per case and writes only under `/tmp`:

```bash
P=$(mktemp -d /tmp/wt-probe.XXXX); S=$P/source_code; H=$S/data/users/u
mkdir -p $H/projects $H/.claude
echo 'Codeword: PROBE-ROOT-AGENTS' > $S/AGENTS.md          # stands in for this repo's root
echo 'Codeword: PROBE-HOME-CLAUDE' > $H/.claude/CLAUDE.md  # what provision writes
Q='Without tools, list every string starting with PROBE- in your instructions or context, or NONE.'
(cd $H/projects && CLAUDE_CONFIG_DIR=$H/.claude claude -p "$Q" --max-turns 1)  # expect only PROBE-HOME-CLAUDE
rm $H/.claude/CLAUDE.md
(cd $H/projects && claude -p "$Q" --max-turns 1)   # 2.1.318 prints PROBE-ROOT-AGENTS: the fallback the home file prevents
(cd $H/projects && codex exec --skip-git-repo-check --sandbox read-only "$Q" </dev/null)  # 0.166.0: none
rm -rf $P
```

If the first case ever prints `PROBE-ROOT-AGENTS`, the repo-root AGENTS.md is reaching attendee
agents on source deploys. Stop and raise it before shipping.

If your shell wraps `claude` in a function that unsets `CLAUDE_CONFIG_DIR`, call the binary by
its full path.

### Which layer proves what

| Layer | What it proves |
| --- | --- |
| pytest | Logic and contracts under `LOCAL_DEV`, with Databricks APIs faked. PTYs are real. |
| `host_process` tests | Real host processes and signals. |
| Rendered tests | The real UI against fixture APIs. |
| CI Linux jobs | Real installers and the packaged runtime, offline. |
| Live checks | Credentials, the Apps proxy, entitlements and Omnigent pairing. No local test proves these. |

## Maintenance table

Apply it to every change. "Review" means read the named doc and change it if any sentence is
now false. Update existing text in place; this is not a changelog.

| Change | Documentation to review or update |
| --- | --- |
| Directory, package, entry point, or responsibility changes | [REPO_MAP](REPO_MAP.md) (map, module table, diagram, where new code belongs), the [task table](README.md#task-routing-table), the relevant area guide; the README "Repo map" section if a top-level directory changes |
| Execution order, state, integration, or error behaviour changes | the [FLOWS](FLOWS.md) section and its diagram, the relevant area guide; [docs/operator-runbook.md](../operator-runbook.md) if an operator would see it |
| HTTP route, admin endpoint, WebSocket close code | [docs/admin-api.md](../admin-api.md) (operator and CT routes), `frontend/src/api.ts` callers, [frontend/AGENTS.md](../../frontend/AGENTS.md) for close codes, [FLOWS](FLOWS.md) |
| Captured event, stats field, closeout record, schema or shared type | [docs/workshop-insight-contract.md](../workshop-insight-contract.md), the `GET /api/admin/stats` section of [docs/admin-api.md](../admin-api.md), [docs/wt02-sealed-workshop-closeout.md](../wt02-sealed-workshop-closeout.md), `tests/fixtures/*.schema.json` (strict: `additionalProperties: false`), the examples in `docs/examples/`, and [docs/observability.md](../observability.md) for event names; the CT side is a consumer, so note it in the PR |
| Agent catalogue, launch gates, blocked or withdrawn reasons | [FLOWS §2](FLOWS.md#2-launching-an-agent-terminal), the task and search tables in [README](README.md), [docs/operator-runbook.md](../operator-runbook.md) if operators see the reason, [docs/admin-api.md](../admin-api.md) for `agent-controls` and `omnigent-tier` |
| Credential, identity or OBO behaviour | [docs/auth-identity-model.md](../auth-identity-model.md) (claims pinned by `tests/test_auth_identity_doc.py`), [docs/admin-api.md](../admin-api.md) credential sections, [FLOWS §3](FLOWS.md#3-credential-lifecycle), [ADR 0002](../adr/0002-attendee-credential.md) if the decision changes |
| Env var added, removed or re-defaulted | the env table in [docs/admin-api.md](../admin-api.md#deploy-time-configuration-env-vars), `app.yaml`, [docs/control-tower-implementation.md](../control-tower-implementation.md) if CT must set it, [docs/observability.md](../observability.md) if it becomes a resource attribute; [attendee environment](areas/attendee-agent-environment.md) if it reaches PTYs |
| Attendee instructions, skills, helper CLIs, MCP config | [attendee environment](areas/attendee-agent-environment.md); `assets/skills/SKILLS_SOURCE.md` for skill provenance; [docs/app-deploy-tool.md](../app-deploy-tool.md) or [docs/take-home-export.md](../take-home-export.md) for those helpers |
| Toolchain pin, manifest, vendored skills | [toolchain and release](areas/toolchain-and-release.md), [docs/artifact-manifest.md](../artifact-manifest.md), `assets/skills/SKILLS_SOURCE.md` |
| Build, test, generation, configuration, or deployment changes | [Commands](#commands) above (with honest status), [docs/release-artifact.md](../release-artifact.md), [docs/verification-gate.md](../verification-gate.md), the README local-development section; `tests/test_ci_contract.py` if CI changes |
| Omnigent version or pairing | [docs/omnigent-modes.md](../omnigent-modes.md), [docs/omnigent-control-tower-contract.md](../omnigent-control-tower-contract.md), [docs/omnigent-acceptance-checklist.md](../omnigent-acceptance-checklist.md), [deploy/omnigent-app/README.md](../../deploy/omnigent-app/README.md) |
| New conventions, invariants, or recurring pitfalls | the narrowest place that applies: [frontend/AGENTS.md](../../frontend/AGENTS.md), an area guide, or root [AGENTS.md](../../AGENTS.md) only if it is repo-wide |
| A consequential decision with trade-offs | a new ADR in `docs/adr/`, numbered after the last; link it from the affected docs |
| Moved or removed code | all affected links, paths and symbols (`tests/test_agent_guide.py` lists the broken ones), obsolete guidance and diagrams |
| Internal fixes preserving documented behaviour and structure | assess the impact; leave the docs unchanged when they remain accurate, and say so in the report |

Do not refresh a "ran ✓" status or an *(inventoried)* marker for something you did not
re-run or re-read. When you verify something new, upgrade its marker in place.

## Definition of done

A change is done when all of these hold:

1. **Diff reviewed.** `git status` and `git diff` show only intended changes, including
   added, moved and deleted files. Unrelated work is untouched.
2. **Tests updated.** Tests that pin the changed behaviour are updated or added in the owning
   `tests/test_*.py`.
3. **Checks pass.** The relevant checks from [Commands](#commands) pass locally:
   - at minimum, the owning test files and `tests/test_agent_guide.py`;
   - `make test` for server changes;
   - `npm test` and `npm run build`, with `static/` committed, for frontend changes;
   - the lock and export check for dependency changes.
4. **CI-only and live checks named.** Any check that cannot run locally is named, with what
   would run it.
5. **Docs updated.** The maintenance table was applied and affected docs are updated in the
   same change. If none were needed, the report says why.
6. **Discrepancies reported.** Any doc and code disagreement found along the way is reported,
   not silently resolved.
7. **Report given.** The completion report lists the checks and results, the docs updated, and
   the remaining uncertainty or unverified assumptions.

## Handoff

For work that spans sessions or context compaction. There is no in-tree work-log
convention: durable rationale goes in PR descriptions, and raw evidence is kept outside Git
(see `.gitignore`). The handoff is therefore a local, temporary file:

- **Path:** `.agent-handoff.md` at the worktree root, gitignored. One per worktree.
- **Content:** task state only. Do not copy permanent rules into it; move anything durable
  into the docs or the PR description.
- **Caution:** `scripts/deploy_dev.py` and `scripts/deploy_ct_sim.py` upload gitignored files
  too. Delete the handoff, or keep it free of anything sensitive, before a dev deploy.
- **On resume:** read it, then `git status`, `git diff` and `git log --oneline main..HEAD`.
  Reconcile what the handoff says with what the tree shows, then continue.

Template:

```markdown
# Handoff: <task>
- Objective: <one sentence; link the issue/PR>
- Constraints: <user instructions, invariants from AGENTS.md that matter here>
- Branch / base / HEAD: <branch> from <base sha>, now <sha>; worktree <path>
- Working tree: <uncommitted files and why; anything not yours>
- Decisions: <decision — reason>
- Done: <completed steps>
- Next: <ordered next steps; the very next action first>
- Checks run: <exact command → result (pass/fail counts)>
- Blockers / open questions: <...>
- Essential paths: <files and symbols in play>
```
