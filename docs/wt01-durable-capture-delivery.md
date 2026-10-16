# WT-01: durable current-output delivery

WT-01 supplies the producer half of CT-01's commit-safe capture contract. This
milestone keeps the existing capture categories and content. It is delivered in
one WT branch and one PR, with the selected CT–WT qualification described in the
[runbook](wt01-qualification-runbook.md).

Qualified on 16 October 2026 through the existing qualification CT app and one
real attendee workspace. The [sanitized acceptance evidence](wt01-qualification-evidence-2026-10-16.json)
records ten passing gates, exact artifacts and hashes of the private receipts.
The selected checkpoint covers actual CT creation, pending-output restart and
redeployment, fresh Lakebase readback, owned teardown and retained history.

## Scope

The existing WT app retains unacknowledged current outputs, stable source
identity, sequences and logical revisions. Reads are non-destructive. The
existing authenticated CT admin caller explicitly acknowledges a prefix only
after its Lakebase transaction commits and fresh readback confirms it. Lost
responses replay safely. Persisted discovery revisions, withdrawals and attendee
ownership recover consistently with the delivery ledger. Unsupported output,
storage failures and missing state remain visible rather than silently becoming
an empty successful queue.

Use WT's existing local state and the attendee's existing Workspace-file
persistence. The retained checkpoint is scoped beneath
`/Workspace/Users/<owner>/projects/.workshop-capture/<bindinghash>`; CT already
grants the WT app principal access to the parent `projects` folder. Bind the
checkpoint to its event/run, unit, workspace and attendee. Reusing a pooled
login for another event must not select the previous event's state. Capture
checkpoints are operational state and must not appear as attendee projects or
be included in take-home exports. Local container files alone do not establish
redeployment durability.

The existing app and operator-controlled build/deployment remain the delivery
path. This change adds no Databricks Job, external worker, relay, separate
database, service, principal or blanket permission grant. It does not change
Labs CT. Future event scheduling, capacity dependencies, sealed final manifests,
input barriers, final help reconciliation and teardown authorization remain the
planned WT-02/CT-02 and later milestones. A drained live queue is not a final
capture receipt.

## Wire contract

`GET /api/admin/insight-events?after=N&stream=E&limit=1000` returns transport
`schema_version: 2`, `ack_mode: "explicit"` and the existing version-1 event
envelopes wrapped as `{"seq": N, "event": {...}}`. GET never acknowledges or
deletes, including when `after` is supplied. `high_water` is page-local, and
`pending` counts all unacknowledged records, including the returned page.

`POST /api/admin/insight-events/ack` takes
`{"schema_version":2,"stream_id":"E","through_seq":N}` and returns
`{"schema_version":2,"stream_id":"E","acked_through":N}`. Validate transport
version, epoch and emitted range before releasing the addressed prefix. A repeat
ACK is idempotent; stale ACKs cannot release another epoch's records. Exact
logical retries retain their original sequence and envelope, including their
original observation time. A changed logical body needs a new revision/key.

`EventEmitter.emit_transaction` commits producer recovery state and its envelope
in one retained operation. Its preparation callback can run again after a
competing app container wins the next revision, so the callback must only mutate
the supplied private state. An exact repeat of the type, attendee, payload and
logical key returns the original sequence, including after ACK; it does not
resurrect the record in the pending queue. Changed content under that key is a
visible conflict.

The journal stores create-only, hash-chained operations and verifies new writes
by readback. ACK changes the committed delivery prefix; it does not delete or
rewrite retained history. Optional anchored checkpoints accelerate recovery.
Immutable operations remain in the event's existing attendee workspace until
the supported cleanup removes that workspace. Capacity limits reject new work
while preserving accepted records, and rejected output must remain visible in
capture health. This is bounded retention under the existing capture policy,
not an additional transcript or source-file archive.

Deploy the CT-01 consumer before this producer. An older collector that only
uses GET cannot acknowledge version-2 retained data. Do not downgrade the new
producer to destructive GET merely to conceal that backlog.

The [shared fixture directory](../tests/fixtures/capture-delivery) is copied
byte-for-byte from CT PR182, including the canonical SHA-256 reference values.
The WT producer tests generate the two valid pages and ACK/empty responses
against that corpus. The version-1, unknown-event and sequence-gap fixtures also
remain shared consumer test inputs; their presence is not permission for a
healthy producer to emit an unsupported transport or manufacture gaps.

Canonical record hashes cover the original wrapped entry, before CT attribution,
using UTF-8 JSON with sorted keys, compact separators, literal Unicode and
nonfinite numbers forbidden. The fixture's blank legacy run/workspace fields
remain deliberate: CT supplies authoritative attribution from the authenticated
source binding.

## Acceptance ledger

| Requirement | Evidence required |
| --- | --- |
| CT/WT contract | Shared fixture bytes and canonical digests; exact version-2 pages/ACKs; page-local counters and non-destructive reads. |
| Replay and acknowledgement | Duplicate and lost-ACK tests; epoch/range/version rejection; no prefix release on failed persistence or incomplete CT commit. |
| Producer durability | Actual supported restart/redeploy preserves source identity, retained entries, acknowledged cursor, revision keys and visible faults. |
| Owner/discovery recovery | Same attendee/source after replacement, discovery updates and withdrawals retain their revisions, and another event cannot adopt the old state. |
| Bounded storage/failures | No silent required-output eviction; visible queue/storage/gap diagnostics; persistence failures cannot masquerade as delivery success. |
| Real CT–WT checkpoint | Qualification CT creates one real seat; captured data appears in fresh Lakebase readback before cleanup and remains after teardown. |
| Release provenance | Exact source/artifact/configuration hashes; public committed/deployed registries; unrelated laptop proxy lock preserved. |
| Isolation and cleanup | Protected Labs app/database unchanged; only positively owned test resources removed; qualification app/database and evidence retained. |

The full source suite and existing CI remain required. Select a real 1–2 seat
checkpoint when the changed CT–WT interaction requires it; ordinary PR checks
do not allocate a fleet. This milestone's first producer/consumer interaction
does require that selected checkpoint. A passing deployment state, `pending=0`
or a recent sync timestamp alone does not prove capture correctness.

## Completed qualification

The live package pins WT runtime commit
`c9aacd60dad8b2b372a7e62efa9915f420b37c03`. CT ran the reviewed prerequisite
from [CT PR183](https://github.com/althrussell/databricks-labs-control-tower/pull/183),
source `79b6cc06a7a0191adc8a7fb4d5b317df2d1b20ba`. The final WT evidence commit
changes documentation only; the existing CI checks also run on that PR head.

| Check | Observed result |
| --- | --- |
| Regression and release | 3,182 tests passed, 65 optional tests skipped; exact-runtime CI passed, including reproducible PEX/offline smoke, frontend and rendered-wizard checks. One existing Starlette/httpx deprecation warning remains. |
| Real owner capture | The assigned attendee's normal OAuth session exercised supported owner APIs. Captured name/identity survived; both discovery records retained revisions 1, 2 and 3, including withdrawals with cleared current projections. |
| Pending-output restart | CT collection was stopped; WT was stopped and restarted through the supported Apps API. All 49 accepted entries, including 16 pending entries, retained their exact bytes, source epoch, revision keys and ACK 33 while stopped and after restart. |
| CT restart and readback | Restarted CT recovered its durable cursor. A fresh read-only Lakebase connection verified the 80-entry audited source prefix within 81 committed records, with committed/ACK position 81 and no drops or stream issues. |
| CT-driven redeployment | CT replaced the WT deployment using the same pinned package. The original 84-entry prefix and pending discovery sequence 83 survived unchanged at ACK 81. Owner readback recovered revision 1, then accepted revision 2 and withdrawal revision 3. Settled readiness returned HTTP 200. |
| Pre-teardown checkpoint | All 123 independently audited source entries matched retained CT raw hashes; fresh Lakebase readback showed committed/ACK position 124. Repeated collection preserved unique logical outcomes. |
| Teardown and retention | CT's ordinary teardown removed the test workspace; account inventory returned to five workspaces and the assigned login became available. Fresh Lakebase readback retained all 124 pre-teardown records and contained 126 records/events after the final harvest. Both discovery histories and identity remained correct. |
| Restoration and isolation | Qualification CT returned to its original stable WT pin and configuration. A further fresh database read retained all 126 records/events. Labs app/database matched their baseline, and both original checkouts' unrelated lock changes were preserved. The temporary attendee OAuth profile was removed. |

Private receipt hashes in the evidence bind the source audits, database reads,
deployment identities and final inventory. Raw attendee bodies, credentials,
tokens and browser storage are excluded from the committed evidence. The exact
candidate PEX came from the existing PR package job and was hash-verified and
staged through the existing operator-controlled CT deployment path. No new build
or deployment workflow was introduced.

Paging, lost ACK responses, commit/storage failures, concurrency, corrupt or
missing state and capacity exhaustion have local contract/fault-test evidence.
They were not forcibly injected into the live account. Actual attendee API
evidence does not claim a rendered live browser or model-execution journey.
Recovery assertions cover captured names, discovery revisions and tombstones;
uncompleted wizard/profile drafts remain outside this existing capture policy.
The verified live prefix is not a sealed whole-event receipt: finalization and
teardown enforcement remain WT-02/CT-02.
