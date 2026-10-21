# CT-08 runtime throughput qualification

The WT changes remove repeated work inside one model-configuration operation and
one deep readiness proof. They preserve fresh policy reads between operations,
final retained-policy validation, source binding checks and full directory
integrity verification on every readiness request.

## Changes and integrity boundaries

`PolicyStore.read_operation()` reads the durable journal at entry and again at
successful completion. Nested configuration helpers share only that operation's
snapshot, journal object and canonical source binding. A changed policy or
binding fails closed; a sticky journal/storage fault remains a fault. No policy
cache survives the operation. Configuration failures clear `cli_ready` so the
next launch must rebuild the complete configuration.

`FileDigestProof` reuses a binary's digest only inside one deep proof and only
while its real path, lstat/stat device, inode, mode, size, mtime and ctime remain
the same. Final validation rechecks every retained generation. Missing,
modified, replaced and retargeted inputs invalidate the proof. Directory trees
are still hashed from their bytes on each deep request; executable version
checks still run on each request. No proof cache persists across requests.

## Local measurements

These are offline measurements, not Databricks deployment-time results.

| Operation | Before | After | Scope |
|---|---:|---:|---|
| `configure_all` durable-storage list calls | 8 | 2 | Production `WorkspaceCaptureStorage`, in-memory SDK |
| `refresh_model_configs` durable-storage list calls | 8 | 2 | Fresh entry and final journal reads remain |
| File hash calls per deep proof | 15 | 8 | Controlled 96 MiB binary fixture |
| File bytes hashed per deep proof | 201,326,794 | 100,663,444 | Directory digest I/O excluded |
| Median deep proof duration | 0.100338 s | 0.063488 s | Four samples; mocked version output |

The configuration samples, including local configuration-file fsync, measured
0.009398 to 0.006207 seconds for `configure_all` and 0.004485 to 0.004170 seconds
for `refresh_model_configs`. They establish request-count reduction, not cloud
latency savings. Network, platform startup and full cold installation need the
CT event benchmark to establish their effect on deployment time.

The bounded timing recorder measured 1.263 microseconds per empty scope on this
Mac (median of six 100,000-scope loops); the recent ring remained at 128 entries.
This is local instrumentation overhead, not a deployment performance result.

Pinned wheelhouse and skills-archive changes are not included: their cold-path
contribution was not independently measured, so this release does not introduce
another artifact format or change package registries/install semantics.

## Diagnostic timing coverage

The existing admin-only `/api/admin/diagnostics` response now includes
`runtime_timings`, without another service, sink or attendee-facing UI. It
records inclusive monotonic durations and invocation counts for individual
installer/tool totals, venv creation, dependency installation, skills Git
subprocesses, artifact fetch/download/extract, file/artifact/directory hashes,
deep proof totals, each standard binary's proof, executable version commands,
skills proof, APX/Agent Bricks proof, readiness, identity authentication and
validation, OBO capture, and capture initialization/journal/workspace operations.
Nested stages inherit the current fixed tool label. For benchmark snapshots use
`/api/admin/diagnostics?timings_only=true`, which skips the normal diagnostic
readiness evaluation and avoids adding a deep proof to the measured work.

Only allowlisted stage/tool labels, clocks, numeric counters and outcomes enter
this recorder. It never stores operation arguments/results, filenames, URLs,
identities, credentials, exception text or attendee content. Aggregates are
bounded by those fixed labels; recent scopes are capped at 128 per process. A
process ID and start timestamp identify restarts. A new process starts fresh;
CT must retain benchmark readbacks before stopping or replacing an app.

Durations include nested operations and cannot be added together as exclusive
CPU time. Counts mean function/scope invocations, not SDK HTTP attempts, pages,
network requests or unique bytes. `errors` counts exceptions escaping a scope;
a handled installer/probe failure still needs its existing step/readiness
status. A returned scope does not by itself establish success. Whole-tree hash
I/O and version commands remain on every deep request. Timings do not introduce
cached proof acceptance or bypass any existing identity/integrity gates.

## Verification

The complete local suite passed: 3,741 tests, 69 skipped, in 311.25 seconds,
with loopback enabled for the existing local HTTP tests. The affected 311-test
aggregate passed, followed by a final 51-test timing, proof, diagnostics and
capture suite after adding the timing-only readback and tmux scope. The existing
Starlette/httpx deprecation warning remains. The public Python/npm source guard,
Python compilation and whitespace checks pass. An independent timing-only review
found no actionable functional, concurrency, privacy or retention issue.

Cloud deployment, attendee restart and verified closeout evidence belongs to the
paired CT-08 release qualification; the local measurements above make no claim
about those live outcomes.
