# Databricks Skills Source

The `databricks-*` skills in this directory are vendored verbatim from
[databricks-agent-skills](https://github.com/databricks/databricks-agent-skills)
`skills/`. They are the **offline fallback** only: at boot the terminal clones
the same repository at the reviewed ref and overlays it, so a healthy instance
runs the fetched copy and reports `source: network` or `prewarmed`. Do not
hand-edit them — refresh via the `refresh-databricks-skills` skill instead.

| Field | Value |
|-------|-------|
| Upstream repo | https://github.com/databricks/databricks-agent-skills |
| Source path | `skills/` |
| Pinned tag | `v0.2.10` |
| Pinned commit | `3d79814688e28765db0153eddcf6ec74972c75b4` |
| Content SHA-256 | `43b1fde1da1c892bd023eed0fd8c677c82ba68bc727b5d39d49d6b3e43de9104` |
| Synced on | 2026-07-29 |
| Synced by | refresh-databricks-skills |

The tag, commit, and content digest above must equal the
`databricks_agent_skills` entry in `assets/artifacts/manifest.json`. A test
asserts it, so the fallback can never drift to a different skills version than
the one boot installs.

## Not vendored from upstream (preserved on refresh)

- `databricks-app-apx` — Control Tower / apx-specific, fork-only
- `workshop-design-studio` — the visual baseline and copy-ready AppKit patterns
  for anything an attendee builds with a UI. Maintained here, not upstream; see
  its `NOTICE.md` for attribution.
- `promote` — handoff document generation, on explicit request only
- `refresh-databricks-skills` — the refresh skill itself

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
canonical `databricks-apps` and `databricks-app-design` skills, while
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
