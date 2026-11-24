# Handover: state of the Workshop Terminal at 24 Nov 2026

For the team taking over the Workshop Terminal (WT) and Control Tower (CT). This is a dated
snapshot. `AGENTS.md` and [`docs/agent-guide/`](agent-guide/README.md) are the living references;
when this file and the code disagree, the code wins.

## Where things stand

| Item | State |
| --- | --- |
| Latest release | `v2026.11.24.1`, tagged on `main` at `4ce4fa3`. Adds the closeout fix (#175): a failed read-only request no longer blocks the seal. |
| Fleet | CT's fleet default is `v2026.11.24.1`, so new workshops get it and existing ones keep their pin. No rehearsal has run on it yet. After the next event, check CT's closeout for `accepted_writer_failed`. |
| GitHub Actions | **Disabled** (the owner's cost choice at handover). CI, the tag release and the weekly skills-freshness check do not run until they are re-enabled. |
| Open pull requests | None. |

## Releasing while Actions are off

Releases are built only by `.github/workflows/release.yml` when a `v*` tag is pushed. The workflow:

- requires a green `ci.yml` run for the tagged commit, either a push on `main` or a pull request whose head tree matches;
- builds the PEX twice and compares the two builds;
- smoke-tests it in a clean Linux container;
- publishes `workshop-terminal.pex` and `release-manifest.json`.

Never publish a release built anywhere else.

1. Re-enable Actions in the repository settings.
2. Open a pull request with an empty commit on top of `main`. CI then runs on `main`'s exact tree. Wait for it to pass.
3. Tag `main` as `vYYYY.MM.DD.N` and push the tag. Then close the pull request without merging.
4. Disable Actions again if cost matters.
5. In CT, refresh, stage and verify the release, then promote it to fleet default between events. See CT's `docs/workshop-terminal-releases.md`.

## Open work

- **#177:** audit the remaining mutating `/api` routes for capture-writer classification.
- **#178:** `workshop-browser` should wait for a slow answer before its screenshot.
- The roadmap: [`docs/ROADMAP.md`](ROADMAP.md), refreshed at handover.

## What is specific to the previous owner

- **Repository location.** CT finds releases through `CONTROL_TOWER_WORKSHOP_RELEASE_REPOSITORY`, which defaults to this repository's current GitHub owner. A move needs that setting changed in CT.
- **Branches kept on purpose.** These hold work that is not on `main`:
  - `feature/phone-tab-shell`, the previous owner's working branch;
  - `spike/omni-codex-compaction`;
  - `fix/attach-image-import-re` (its PR was closed);
  - `fix/f027-app-model-access`, which has commits after its PR merged.

## Where to start

| Task | Start here |
| --- | --- |
| Understand or change the code | `AGENTS.md`, then [`docs/agent-guide/README.md`](agent-guide/README.md) |
| Deploy one instance by hand | [README: Deploying](../README.md#deploying). Control Tower normally deploys WT. |
| Run an event floor | [`docs/operator-runbook.md`](operator-runbook.md) |
| The CT contracts (insight, closeout, admin API) | [`docs/workshop-insight-contract.md`](workshop-insight-contract.md), [`docs/wt02-sealed-workshop-closeout.md`](wt02-sealed-workshop-closeout.md), [`docs/admin-api.md`](admin-api.md) |
