# Databricks Skills Source

Databricks platform skills are pinned from
[databricks-agent-skills](https://github.com/databricks/databricks-agent-skills).
Bootstrap verifies the raw clone's commit and content digest, then applies the
same deterministic workshop projection used by the refresh script. The
projection retires `databricks-app-design` and routes its two callers to
Impeccable; all other platform/API content remains upstream. The delivered
fallback and runtime have their own effective digest in the reviewed manifest.
Do not hand-edit platform skills; regenerate through the refresh scripts.

| Field | Value |
|-------|-------|
| Upstream repo | https://github.com/databricks/databricks-agent-skills |
| Source path | `skills/` |
| Pinned tag | `v0.2.28` |
| Pinned commit | `ba45d10df7413de14c32937bbd584aeee17d22a2` |
| Content SHA-256 | `0ddf4467590698fd17b98ef7035ad4bee90e2a082a34467489e48c53da10d98a` |
| Synced on | 2026-10-10 |
| Synced by | refresh-databricks-skills |

The tag, commit and raw digest above equal the `databricks_agent_skills`
manifest entry. `effective_content_sha256` verifies the delivered projection.

## Additional delivered skills

- `impeccable` — the single upstream UX skill from
  [pbakaus/impeccable](https://github.com/pbakaus/impeccable), Apache-2.0;
  license in `assets/artifacts/IMPECCABLE-LICENSE`. WT uses the supported
  `impeccable install --providers=claude,codex --scope=project --no-hooks`
  installer, with its supported `IMPECCABLE_BUNDLE_PATH` override for the reviewed
  bundle. Launcher **4.1.0**, engine **0.1.14**, compiled skill **4.5.2** are
  independently pinned in the artifact manifest. The text fallback is installer
  output; the native engine is downloaded/verified at bootstrap and stays out
  of project commits. Refresh/check with `scripts/refresh_impeccable.py`.
- `workshop-agent-bricks-cli` — custom-agent CLI workflow for the WT identity,
  project scaffold and resource handoff.
- `databricks-app-apx` — APX scaffold/API guidance, fork-only.
- `promote` — handoff document generation, on explicit request only.
- `refresh-databricks-skills` — the refresh workflow itself.

One small context/pacing policy lives in
`assets/instructions/workshop_contract.md` and reaches home and project
instructions. It governs the short workshop interaction while upstream
Impeccable owns UX craft. AppKit/APX are implementation guides, not a shared
visual template. `workshop-design-studio` and `databricks-app-design` are retired
from packaged/runtime trees and managed home/project copies, including warm
installs; the projection prevents refreshes from resurrecting their routes.

## Removed on purpose

The development-workflow set (`using-superpowers`, `brainstorming`,
`writing-plans`, `executing-plans`, `test-driven-development`,
`subagent-driven-development`, `requesting-code-review`,
`receiving-code-review`, `finishing-a-development-branch`,
`using-git-worktrees`, `dispatching-parallel-agents`,
`verification-before-completion`, `systematic-debugging`, `writing-skills`) and
the `bdd-*` skills were deleted from this repository.

They exist to slow a build down deliberately — plan first, fail a test first,
review before merge. That is the opposite of what a workshop attendee needs in
the minutes they have to get an idea to a live URL, and agents auto-invoked them
mid-build. They are also absent from `FORK_ONLY` in
`scripts/refresh_vendored_skills.py`, so a refresh deletes any copy that
reappears.

## Retired skill names

These were vendored from the deprecated `ai-dev-kit` and no longer exist
upstream. A guard test fails if any of them reappears, because an agent told to
use a retired name silently gets no skill at all:

| Retired | Canonical replacement |
|---------|-----------------------|
| `databricks-bundles` | `databricks-dabs` |
| `databricks-config` | `databricks-core` |
| `databricks-genie` | `databricks-data-discovery` |
| `databricks-lakebase-autoscale` | `databricks-lakebase` |
| `databricks-lakebase-provisioned` | `databricks-lakebase` |
| `databricks-spark-declarative-pipelines` | `databricks-pipelines` |
| `spark-python-data-source` | (dropped upstream) |

This fork also used to carry a local `databricks-apps-python` variant with a
`7-appkit-ux.md` chapter and an `examples/appkit-ux/` directory. Both are
retired: AppKit is Node/TypeScript/React and its guidance now lives in the
canonical `databricks-apps` skill and upstream `impeccable`, while
`databricks-apps-python` is vendored verbatim as the Python-backend
alternative.

## How to check freshness

The `Skills freshness` workflow (`.github/workflows/skills-freshness.yml`) does
this weekly: it fails if this directory drifts from the reviewed ref or if any
artifact checksum stops matching upstream, and opens one issue per newer upstream
release. To check by hand:

```bash
gh release view --repo databricks/databricks-agent-skills --json tagName --jq .tagName
python3 scripts/refresh_vendored_skills.py --check
```

If either reports a difference, run the `refresh-databricks-skills` skill: it
re-vendors this directory *and* regenerates the manifest entry, so both move
together.
