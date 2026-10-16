# WT-01 selected CT–WT qualification

This runbook qualifies durable delivery through the existing parallel CT app.
It is a selected interoperability checkpoint, not a fleet test for every PR.
Use one real attendee workspace; a second is available only for a case that
needs two sources. Preserve qualification CT's database and the Labs deployment.

## Target and preparation

The target is `control-tower-qualification` in workspace `7474655950495189`,
with database `control-tower-qualification-db`, database `databricks_postgres`
and schema `control_tower_qualification`. The app principal is
`65b976ec-7d2a-42e1-bc4d-672439c1ba48`. Read these bindings independently before
mutating anything. Both CT host workspaces, `7474655950495189` and
`7474646162386994`, are protected from attendee mutations.

The operator holds the existing `labuser+199@awsbricks.com` and
`labuser+200@awsbricks.com` logins free. No new account or pool-exclusion
framework is needed. Confirm their current assignment and the account's actual
workspace usage before building. Qualification has an aggregate two-workspace
ceiling, including creating, hibernated and deletion-pending seats.

Read the deployed qualification CT diagnostics and confirm CT-01's commit-safe
consumer and migration head. Snapshot sanitized Labs app/database identities,
qualification state and relevant account resources for comparison afterward.
Do not store access tokens, client secrets, browser storage or raw credential
responses in evidence.

Before allocating a seat, verify that the actual qualification CT principal can
inspect the configured reader group's effective permissions on both the release
and toolchain volumes. A successful package byte read does not establish this:
the ordinary deployment path also checks group access. An unknown permission
result is a preparation blocker. If the existing volume lacks metadata access
for CT, obtain approval for the specific missing privilege before changing it;
do not replace this check with an operator's more privileged result.

Use the exact WT candidate commit and public dependency inputs. Preserve the
owner's unrelated laptop `uv.lock` changes; build from a clean selected checkout.
Laptop installs use command-local Databricks-managed PyPI/npm proxies. Public
PyPI/npm references remain in the committed and deployed configuration.

## Build and pin the candidate

The release PEX targets Linux x86_64 and CPython 3.11. The existing PR CI
`packaged-runtime` job builds twice, compares bytes and smoke-tests in a clean
Linux container. Its exact-commit artifact is suitable for this checkpoint;
no separate Linux machine is required. Download that artifact and independently
verify its manifest and PEX hashes before staging it through CT.

An existing compatible operator build environment can also run the unchanged
builder with the full reviewed commit:

```bash
uv sync --frozen --no-group dev --group release
uv run --frozen --no-group dev --group release python scripts/build_release.py \
  --git-sha <full-reviewed-commit> --output-dir dist
```

Build twice and compare artifact/manifest bytes; run the existing packaged-runtime
smoke when the host supports its clean network-disabled container. Independently
verify source identity, logical content, ABI/platform, artifact size and hashes.
Use the existing operator staging/deployment tooling and isolated qualification
release/toolchain volumes. Select the exact candidate pin; do not substitute a
previous stable package or resolve a floating release during provisioning.

To make the verified, unchanged candidate bytes available to
CT's existing release resolver, publish a qualification-only prerelease under a
unique non-`v*` tag with `latest=false`, then use only the qualification
configuration's manifest URL/digest and the `configured` selection. Preserve the
CI manifest's honest `unreleased` tag. Ordinary release discovery excludes
prereleases. This does not change CI or introduce a deployment workflow; the
operator stages the assets and deploys qualification CT with its existing
verified CT artifact and the candidate pin. Restore the original qualification
pin after cleanup.

No hosted pipeline, Databricks Job, new build service or infrastructure is
required. The existing tagged release workflow remains its own established
mechanism; this milestone does not move operator deployment into GitHub.

Source mode is an existing rollback option, with a different qualification
claim. It requires an explicit scoped qualification configuration deployment
because the current qualification target uses package mode and read-only
settings. Record the changed configuration and exact source commit, then restore
the reviewed target configuration. Never silently treat a source-mode pass as a
PEX qualification pass or rewrite Labs configuration to reach a test candidate.

## Create through CT

Use the authenticated operator UI/API on qualification CT, rather than WT's
standalone simulator. The latter intentionally makes no CT requests and cannot
establish this integration result.

1. Create a one-seat run with `POST /api/labs`. Use `ctq-...` workspace prefixes,
   `ctq_...` catalog prefixes, `user_email_pattern: "labuser+{n}@awsbricks.com"`,
   `user_start_number: 199` and `requested_user_count: 1`. Supply the supported
   region/metastore and a monitored expiry that leaves time to verify and clean
   the test. Record run/unit/workspace/app identity and exact WT selection.
2. Run `POST /api/labs/{run_id}/preflight`. Resolve failures before provisioning.
3. Run `POST /api/labs/{run_id}/provision`. Read run/unit operations until the
   actual WT health/admission gate succeeds. Verify the installed source and
   package digests and the attendee binding through independent readback.
4. Log in as the real assigned attendee through the ordinary workshop login.
   Verify the existing owner/project recovery behavior and create representative current
   capture outputs, including discovery revision/withdrawal and identity. A
   normal attendee OAuth session may exercise the supported owner APIs; record
   that as API evidence separately from any observed rendered UI journey. Use
   the same capture policy as the workshop; do not add transcript/file capture.

Keep qualification model-policy/shared-budget mutation and telemetry-retention
settings at their reviewed values. A model execution journey is needed only
when a changed path depends on it. Do not infer native CLI/model success from
model metadata access or a synthetic emitted event.

## Verify delivery and replacement

Record the expected original entries and canonical digests before collection.
Exercise non-destructive paged reads through the existing authenticated admin
path. Fetching a later page must not delete the prefix. Invalid epoch/version/
range ACKs must leave retained state intact.

Use `POST /api/labs/{run_id}/stats/collect` to drive the deployed CT collector.
From a new qualification Lakebase connection, read `capture_streams`,
`capture_records` and the corresponding existing events/projections. Verify the
exact raw entry/digest, source binding, contiguous committed position and ACK
position. The authoritative readback must come after CT commits and before
source deletion; an API success or sync timestamp is insufficient.

Leave known output unacknowledged for a supported WT process restart and actual
redeployment. Observe the existing Workspace-file checkpoint beneath
`/Workspace/Users/<owner>/projects/.workshop-capture/<bindinghash>` with its
run/unit/workspace/owner binding. Verify the same source/epoch, retained bytes,
revision/key state and ACK prefix after replacement. Replay into CT, drop an
ACK response and retry; check for one logical outcome and correct retained
position. Independently restart CT when needed to prove it reads its committed
cursor rather than process memory.

Storage unavailability, corrupt/missing checkpoint, unknown output and any
intentional failure case need explicit visible outcomes. Preserve affected
sources when reconciliation is incomplete. Local unit tests may establish
failure injection and concurrency behavior that would be unsafe or unnecessary
to force against a live shared account; label that evidence accurately.

## Cleanup and evidence

After a fresh Lakebase read establishes preservation, request CT's teardown
dry-run and inspect its positively owned resources. Tear down only the test's
owned resources through CT and wait for actual workspace/resource release.
Confirm test-user release and retained CT capture rows/digests afterward. Keep
the qualification CT app/database and their history for the next candidate.

Record exact CT/WT commits, artifact/logical/configuration digests, source
bindings, expected and observed output counts/digests, fresh database evidence,
restart/deployment identities, phase timings and cleanup convergence. Compare
protected Labs app/database inventory with the preparation snapshot.

WT-01 proves producer retention and the commit-safe consumer interaction. It
does not certify sealed event completion, every final help entry, automated
teardown blocking, scheduled creation or dependent event turnover. Those remain
their separate implementation milestones.
