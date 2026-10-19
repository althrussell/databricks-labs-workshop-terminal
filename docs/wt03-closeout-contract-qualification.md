# WT-03 — closing UX and release qualification

WT-03 is delivered on `codex/closeout-contract-qualification` as one milestone PR.
It is stacked on the open [PR106](https://github.com/althrussell/databricks-labs-workshop-terminal/pull/106),
`e89aa3a173132072030f5a01d4db52cf64d9622d`. PR106's renewable, fenced closeout
journal audit and historical model-policy refusal proof are prerequisites already
consumed by merged CT-04. Neither PR is automatically merged. WT-03 precedes CT-08
in the [joint implementation plan](https://github.com/althrussell/databricks-labs-control-tower/blob/main/docs/event-readiness/2026-11-17-18/implementation-plan.md).

## Delivered boundary

| Surface | Behavior |
| --- | --- |
| Attendee lifecycle | Authenticated, read-only `/api/workshop-status` and config status expose only `open`, `closing`, `closed` and input availability. Reading never advances closeout. The existing durable barrier remains authoritative. |
| Closing and closed screen | Disable new attendee activity, launches, terminal input, preferences, project changes, certificates and Help submission. Resources and Help history remain readable. Closing sockets stop reconnecting. Later failed/stale reads cannot reopen input. |
| Attendee copy | Plain workshop closing/ended wording. No collection, account-team, insight-extraction or follow-up notices are added. Operator capture diagnostics remain in the admin interface. |
| Workshop documentation | The default **AI Docs** destination is `https://naveenb83.github.io/ai-dev-day/`. Intentional deployment/live content-pack overrides remain authoritative. |
| Public dependency inputs | Check every recognized root/paired Python lock/export/project source, npm resolution/project source, toolchain artifact publisher/pin and deployed registry setting. Nested requirement/constraint inputs are checked recursively. Unknown sources fail with no secret-bearing URL output. |
| Uploads | Dev, CT simulation, generated-app and CT-compatible deployment/observer/mirror paths validate a clean public source snapshot before client initialization. The bytes validated are the bytes uploaded. Read-only qualification, dry runs and cleanup remain usable. |
| Runtime/build | Existing CI, release and runtime installers explicitly use public PyPI/npm. Laptop managed-proxy transport stays local. Existing frozen/offline PEX assembly, hash/integrity checks, paired-app topology and operator-controlled deployment remain. |

No capture granularity, new infrastructure, Jobs, worker, principal, database,
notification channel, fleet-limit expansion or build/deploy system is introduced.
CT owns final Lakebase verification and deletion authorization. WT closing/closed
status is **not** a CT preservation receipt. Existing source epochs are never
reopened. A failed final collection remains held under the existing protocol.

## Qualification gates

| Gate | Required evidence |
| --- | --- |
| Source and UI | Complete Python suite; frontend tests/build; production assets tracked; native closing/closed/read-only Help and corrected-link checks; fresh page and retained state after restart. |
| Defensive upload controls | Dirty/nonpublic root, paired, npm and deployed settings cause zero remote client calls. Concurrent edits cannot substitute later local bytes after the snapshot. |
| Exact package | Existing Linux CPython 3.11 reproducible PEX/offline smoke, source/embedded manifest correspondence, lifecycle/helper presence and default pack. Callback/closeout golden corpus and negative contract tests remain. |
| Selected CT–WT checkpoint | Normal qualification CT creates one owned attendee workspace using the exact candidate release. Confirm release/callback identity, attendee activity, restart, final collection and fresh Lakebase source/receipt proof, then normal non-force teardown and pool return. Preserve earlier archives and Labs deployment. |
| Review | Independent review corrections, current-head CI and exact source/build/runtime/evidence scopes recorded below. Leave the PR open for operator Bugbot. |

## Acceptance record

Implementation and qualification are in progress. Do not treat this ledger as
live acceptance until the exact candidate and results below are recorded.
