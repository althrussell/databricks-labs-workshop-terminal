# WT-02: sealed workshop closeout

WT-02 closes the existing WT capture stream, drains accepted work, produces fresh
model-free final facts and retains an immutable inventory. It uses the existing
authenticated admin API and Workspace-file journal. One WT milestone PR contains
the implementation, tests, contract fixtures and this ledger. The PR remains
unmerged for the operator's independent Bugbot review.

This is the producer half of final capture assurance. **CT-02 must commit and
independently verify these bytes in Lakebase before permitting teardown.** Until
CT-02 is qualified, today's CT teardown does not enforce this new protocol.

## Scope and boundaries

The payload policy and capture granularity are unchanged. Final capture covers
the existing event envelopes, discovery revisions/withdrawals, captured identity,
signals, extracted summaries and help history. It does not introduce a full file
or transcript archive. It adds no Job, background executor, service, database,
principal, deployment workflow or automatic deployment. Scheduling, event
dependencies, capacity checks, CT receipts and deletion enforcement remain CT
milestones. Closing UX remains WT-03.

Closeout is irreversible for this source epoch: new attendee input stays closed
even if a drain or final observation needs retry. Operators should begin it only
when the workshop is finished. The current WT app can drive the operation through
repeated admin requests; a stopped app does not run a separate worker. A supported
restart resumes the same retained capture ID and generation.

## Admin API

All routes use the existing admin authorization dependency. Discover support in
`GET /api/admin/state` under `capture_capabilities` (`sealed_closeout: true`,
`closeout_schema_version: 1`). Version-2 insight transport remains unchanged.

```http
POST /api/admin/capture-closeout/begin
Content-Type: application/json
```

```json
{
  "schema_version": 1,
  "capture_id": "operator-generated-id",
  "stream_id": "retained-insight-epoch",
  "source": {
    "run_id": "ct-run-id",
    "unit_id": "ct-unit-id",
    "workspace_id": "123",
    "owner": "labuser+199@example.com"
  },
  "drain_timeout_seconds": 120
}
```

The source must exactly match the journal binding and epoch. `capture_id` is a
1–128 character identifier (`[A-Za-z0-9][A-Za-z0-9._:-]*`). Timeout is an integer
from 1 to 600 seconds. Repeating the same request preserves its generation,
original deadline and final inventory. A different ID, source, epoch or timeout
returns 409. Unknown fields and invalid versions/types return 422.

```http
GET /api/admin/capture-closeout/status?capture_id=operator-generated-id
GET /api/admin/capture-closeout/status?capture_id=operator-generated-id&advance=false
GET /api/admin/capture-closeout/entries?capture_id=operator-generated-id&kind=insight&after=0&limit=1000
GET /api/admin/capture-closeout/entries?capture_id=operator-generated-id&kind=help&after=0&limit=1000
```

Begin persists the barrier before attempting bounded progress. Status advances
by default; `advance=false` is read-only. Each request performs bounded drain
work; callers poll rather than assume the first response is final. States are
`OPEN`, `CLOSING`, `INCOMPLETE` and `SEALED`. `INCOMPLETE` retains issues and can
resume the same operation when the cause is resolved; it never reopens input.
An orphaned accepted writer or irrecoverable history gap remains held.

Only `SEALED` with `complete: true` is a complete producer inventory. Every
response has `ct_verification_required: true`. A storage/integrity failure gives
a generic 503, never a successful empty stream. New producing HTTP requests
receive 409 once closing begins; terminal input and new sockets are also refused.
Existing admin ACK endpoints remain available. Entries return 409 before sealing,
422 for invalid cursors/kinds, and pages of 1–1000 original retained entries.
Page `high_water` is the last returned sequence; `remaining` determines whether
to continue. Pages do not acknowledge anything.

## Seal and inventory

Admission tickets persist before HTTP work or detached identity/summary workers
start. Existing terminal processes, paired hosts, readers, summary/identity work
and detached workspace-sync receipts are drained before final observations.
Process groups must stop and readers finish. A read-only Linux process census
detects remaining attendee HOME/cwd writers; uncertainty or unmanaged writers
holds closeout instead of killing unrecognized processes. A non-Linux host cannot
certify that production census. Failed accepted work remains explicit evidence.

Final facts bypass code caches and require fresh resource observations and the
journal-bound owner. Operator visitors cannot replace that attendee. Read/parse
failures, unavailable homes, unconfirmed or truncated resource responses and
capture-disabled sources cannot certify completeness. A valid Git repository
with no commits has verified zero history; corruption remains a failure.

Finalization never calls a model. Prepared observations are retained before
emission so retries and replacement containers reuse the same payloads and keys.
The final `insight.summary` extraction preserves a prior LLM envelope and links
its sequence, revision and original-entry digest under `prior_narrative`.
`workshop.signal` uses the existing schema. Empty material is explicit
`summary_facts[owner].status: "no_material"`, not a fabricated narrative.

The manifest binds schema/capability, capture ID/generation, source/epoch,
requested/completed times, categories, insight/help sequence inventories, drain
evidence, failed writers, final-output digest and explicit issues. Required
category counts can be zero when no such material was produced. A zero category
does not prove that an expected attendee or source was provisioned; CT owns its
expected-source inventory. ACKs do not change the seal or its original bytes.
Help history is retained after ACK. An older producer's already-omitted help
history has an explicit unverifiable floor and cannot become a complete seal.

All SHA-256 values use UTF-8 JSON with sorted keys, compact separators, literal
Unicode and finite numbers only. Insight hashes cover the original
`{"seq": N, "event": envelope}` wrapper before CT attribution; help hashes cover
the original help entries. Stream hashes cover ordered entry arrays; category
hashes cover the corresponding ordered entries. `manifest_sha256` hashes the
manifest without a self-hash field. `final_outputs_sha256` hashes the returned
`final_outputs` object. Sealed pages include the same generation and manifest
hash. The [golden corpus](../tests/fixtures/capture-closeout/v1-sealed.json)
provides valid bytes and negative consumer mutations for CT-02.

Legacy `GET /api/admin/stats?final=true` remains compatible while open. After
close begins it reads the frozen final statistics (503 until prepared), without
starting rolling summaries or appending output. It is not a final receipt.

## CT-02 integration requirements

CT must retain the expected sources independently of the reachable fleet; missing,
retired, incompatible or unreachable sources are holds. It must persist the
capture ID/generation, verify source and epoch, fetch every sealed insight/help
entry including already-ACKed history, commit original bytes and verify counts,
sequences, schema and entry/category/stream/manifest/final-output digests using
fresh Lakebase readback. Replay must preserve exact bytes. Only CT's verified
durable receipt can authorize deletion. `pending=0`, ACK high-water, a fresh sync
time or an HTTP 200 is insufficient.

CT currently prefers an existing LLM summary projection over an extraction.
CT-02 must verify and apply the separate final facts while preserving that prior
narrative; the old projection alone cannot prove final facts were applied.
Unsupported help kinds, output versions, digest conflicts and 99-of-100 coverage
must remain visible holds. App restart and expiry paths must honor the same gate.

## Ordinary app restart

A normal SIGTERM drains the current local terminal process groups and PTY
readers, paired hosts, accepted identity/summary workers and tracked Workspace
saves. Final terminal output is observed and attendee counters are checkpointed
under the original terminal lifetime before its durable ticket completes. A
failed checkpoint leaves failed-writer evidence; a timed-out write leaves its
original reservation intact.

This path keeps the workshop's durable phase OPEN and does not create the
`workspace-sync.closed` workshop-closeout marker. The replacement can accept
normal attendee work. Explicit workshop closeout remains responsible for sealing
and the final verified CT receipt.

[Databricks Apps allows 15 seconds after SIGTERM](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/best-practices).
The launcher caps Uvicorn's request drain at two seconds and lifespan shares one
ten-second deadline across shutdown stages. A blocked SDK call cannot extend that
deadline; the process reports an unverified shutdown and retains unresolved
durable writer evidence. SIGKILL, storage failure or unfinished writers cannot be
turned into successful preservation by deleting their receipts.

Regression coverage includes a real Uvicorn SIGTERM with an active child PTY,
shutdown-tail metadata and final counters, replacement admission, a failed final
checkpoint, an interrupted summary and blocked persistence past the deadline.

## Acceptance ledger

| Gate | Validation |
| --- | --- |
| Barrier and restart | Durable admission, late input rejection, orphaned/failed writers, replacement process and same-operation resume tests. |
| Drain | Real process-group/descendant and reader tests, paired-host shutdown, detached sync and identity/summary lifetime tests, Linux census fixtures. |
| Fresh final facts | Cache bypass, no model, owner isolation, unreadable/corrupt material, empty Git repository, census truncation and interrupted-emission retry tests. |
| Seal integrity | Independent entry/category/stream/manifest hashes; immutable replay across ACK and restart; tamper, gap, unknown-schema and storage-failure tests. |
| Authenticated flow | Real app routes and journal: discovery edit/withdrawal, help, begin, pages, ACK, replacement emitter, repeated begin and frozen stats. |
| Packaging/regression | Full source suite and existing CI, including Linux reproducible PEX/offline HTTP closeout smoke, frontend and browser checks. The packaged smoke runs real default writer draining and finalization with fixed cloud census observations. Results recorded on the PR. |
| Selected joint checkpoint | CT-02 owns the next real CT–WT final-receipt/teardown checkpoint. WT-02 does not claim today's CT has this gate. No attendee fleet is allocated for this producer-only PR. |

Validated on 16 October 2026. The integrated closeout/API/contract/admin run
passed **277 tests**, with one Linux-only test skipped on the macOS host. Separate
owned-module regressions passed, and all changed Python files pass Ruff and diff
checks. Independent reviews found and fixed failure-erasing retries, truncated
census assurance, missing shutdown lifecycle/tail output, unborn Git history and
detached-sync outcomes. The Linux host integration test verifies an explicit
incomplete hold when shared-runner processes cannot be inspected, including
successful terminal shutdown and retained lifecycle events. The isolated packaged
HTTP smoke requires successful sealing through the real default pipeline.
Final full-suite and exact-head Linux/package/frontend/
browser results are recorded in the milestone PR's checks and qualification
comment before handoff. The existing CI and public package inputs are unchanged.

Existing WT-01 live durability evidence remains valid for the underlying
persistence path; the new closeout guarantees have their own fault/restart/API/
process tests here. The next real fleet checkpoint tests CT-02's consumer and
teardown enforcement against this producer. WT-02 stays awaiting operator Bugbot
review and explicit merge approval.
