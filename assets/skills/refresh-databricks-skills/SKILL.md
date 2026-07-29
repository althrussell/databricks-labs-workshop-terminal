---
name: refresh-databricks-skills
description: Use when Databricks skills need updating, the user asks to refresh or sync skills from upstream, or the pinned skills release looks older than the newest databricks-agent-skills release
---

# Refresh Databricks Skills

## Overview

Moves the workshop to a newer reviewed release of
[databricks-agent-skills](https://github.com/databricks/databricks-agent-skills).

Three things must move together, or boot fails closed:

1. `SKILLS_REF` in `server/bootstrap/install.py` — the ref the terminal clones.
2. The `databricks_agent_skills` entry in `assets/artifacts/manifest.json` — the
   reviewed commit and content digest boot verifies the clone against.
3. `assets/skills/` — the vendored offline fallback, which must be the same
   content so a network failure degrades the *source*, not the skill set.

Bumping only the ref makes every install fail with "skills content differs from
reviewed manifest". Bumping only the manifest makes the fallback stale.

## When to Use

- A newer upstream release exists and the event should pick it up
- A new upstream skill is needed (e.g. a new product surface)
- The `Skills freshness` workflow opened a bump issue or its fallback-drift job
  is failing

That workflow runs weekly: it verifies the committed fallback still matches the
reviewed ref, re-derives every artifact checksum from upstream, and opens one
issue per newer upstream release. Pull requests cannot catch this drift on their
own, because upstream moves without touching this repo.

## Process

1. **Find the newest release** and confirm it is a real release, not a tip:

   ```bash
   gh release view --repo databricks/databricks-agent-skills \
     --json tagName,publishedAt --jq '.tagName + " " + .publishedAt'
   ```

2. **Read its changelog** and confirm nothing an attendee depends on was
   renamed or dropped. A rename is not free: an agent told to use a retired
   skill name silently gets no skill. Record any rename in the retired-names
   table in `assets/skills/SKILLS_SOURCE.md`.

3. **Bump the ref** — `SKILLS_REF` in `server/bootstrap/install.py`. Use the
   release tag, never a branch.

4. **Regenerate the manifest entry** (resolves the tag to a commit and computes
   the content digest exactly the way boot does):

   ```bash
   python3 scripts/build_artifact_manifest.py --write
   ```

5. **Re-vendor the fallback** from the commit the manifest now pins. This
   verifies the clone's digest before touching anything, and preserves the
   fork-only skills:

   ```bash
   python3 scripts/refresh_vendored_skills.py --write
   ```

6. **Update `assets/skills/SKILLS_SOURCE.md`** — tag, commit, content digest,
   sync date, and any retired names from step 2.

7. **Update the mandate layer** if a skill an instruction file names by hand was
   renamed: `assets/instructions/CLAUDE.md`,
   `assets/instructions/project_memory.md`,
   `assets/instructions/lab_coach.md`, `assets/skills/promote/build-prompt.md`.

8. **Verify**:

   ```bash
   python3 scripts/build_artifact_manifest.py --check
   python3 scripts/refresh_vendored_skills.py --check
   pytest
   ```

   The two scripts re-check against upstream (they need network). `pytest` is
   offline: it asserts the manifest, the vendored fallback, and
   `SKILLS_SOURCE.md` agree with each other, and that nothing this fork owns —
   instruction files, fork-only skills, server code — still names a retired
   skill.

## Common Mistakes

- **Bumping the ref without the manifest.** Boot verifies the clone's commit and
  content digest against the manifest and refuses a mismatch.
- **Re-vendoring by hand.** Use the script: it verifies the clone against the
  reviewed digest first and knows which skills are fork-only.
- **Pinning a branch.** `main` is a moving target; readiness rejects it.
- **Leaving a renamed skill in the instructions.** The mandate files name skills
  in prose, and a stale name reads as a working instruction to the agent.
