# Workshop insight contract (C6)

Status: **normative**. This is the cross-repository contract for workshop
insight capture, between the Workshop Terminal (producer) and Control Tower
(consumer, owner of the Lakebase store and the Account Manager Brief).

Companion documents:

- [adr/0001-workshop-insight-capture.md](adr/0001-workshop-insight-capture.md) —
  why this reverses two stated principles, and what did *not* change.
- [control-tower-implementation.md](control-tower-implementation.md) — the
  existing C1–C5 contracts this extends.
- Control Tower's `docs/08-integration-architecture.md` — the consumer side.

Machine-readable schema: [../tests/fixtures/workshop-insight-events.schema.json](../tests/fixtures/workshop-insight-events.schema.json),
validated against the examples in [examples/](examples/) by
`tests/test_insight_contract.py`.

## Purpose

A workshop is the highest-signal customer conversation Databricks gets: an
attendee explains their real stack, the problem that brought them, and what is
blocking them today. Today all of it is destroyed at teardown. This contract
carries it to a place an account team can use.

The unit of value is a **per-customer Account Manager Brief**: *"Acme sent 3
attendees; they explored Lakebase and Genie, two shipped a pipeline, and they're
blocked on Postgres latency in their current stack with a Q3 timeline."*

## What is and is not captured

Captured:

- **Behavioural signal** — what the attendee did. Topics, sessions, minutes,
  code, workspace resource census. Already gathered by `server/stats.py`.
- **Elicited discovery** — what the attendee wants, in their own words, given
  knowingly to an agent that told them why it was asking.
- **Edge summary at wrap** — a summary derived from the attendee's prompts and
  the artifacts the agent wrote for them.

Never captured, by construction:

- Raw terminal output, scrollback, or PTY streams.
- File contents, stdout, stack-trace bodies, dataframe dumps.
- Credentials, tokens, or anything secret-shaped (stripped before emit; see
  [Redaction](#redaction)).

Transcripts are excluded deliberately, not for lack of ambition. They are
enormous, ANSI-polluted, and mostly stack traces and file listings; an LLM over
that produces plausible mush a sales team cannot distinguish from hallucination,
which is worse than no data because it is confidently wrong about a customer.

## Enablement and consent

| Setting | Default | Effect |
|---|---|---|
| `WORKSHOP_INSIGHT_CAPTURE` | `false` | Master switch. Off means no signal, no discovery, no summary, and no instruction overlay. |
| `DISCOVERY_ENABLED` | `true` | Sub-flag. Behavioural signal only when off — no agent-elicited questions. |

`WORKSHOP_INSIGHT_CAPTURE` is the only switch that matters. Capture needs no
delivery configuration at all — see [Transport](#transport) — so an instance
Control Tower deploys cannot be misconfigured into capturing nothing.

Consent is handled out of band, through the event's registration terms, by
whoever ran registration. The flag is what makes that decision auditable after
the fact: capture state is reported in `/readyz` and the release manifest, so
Control Tower can prove whether capture was on for a given run.

## Transport

**Control Tower collects; the Terminal does not push.** Insight is handed over on
the authenticated call Control Tower already makes to every instance:

```
GET {app_url}/api/admin/insight-events?after={seq}&stream={stream_id}
```

This is not a preference. Every Databricks App — the Terminal and Control Tower
both — sits behind a platform proxy that requires a Databricks identity on every
request: an SSO session, or an OAuth bearer for a principal holding `CAN USE` on
the target app. A `POST` carrying only `X-Ingest-Token` is rejected at the proxy
and never reaches Control Tower's application code, so the push path cannot work
from a deployed Terminal no matter how it is configured. Making it work would mean
Control Tower granting `CAN USE` to a freshly minted, foreign-workspace service
principal on every deploy and revoking it on every teardown — per attendee.

Collection avoids all of that by reusing the credential Control Tower already
holds to poll `/api/admin/stats`. The admin router's existing auth applies
unchanged, and the Terminal needs no outbound credential, no network egress, and
no ingest configuration.

Properties of the buffer (`server/event_emitter.py`) this contract depends on:

- **Never blocks the attendee.** `emit` appends to a bounded in-memory buffer and
  returns. Nothing on the attendee's path does I/O.
- **Always captures.** `emit` buffers regardless of configuration. There is no
  "delivery is unconfigured" state to disable it — that state *is* the normal
  posture for a deployed instance.
- **Bounded, drop-oldest.** Capped at 5000 events. Overflow is counted in
  `dropped` and reported on both the collect response and `/readyz`, because it is
  the one loss no later collection can undo.
- **Cursored and self-acknowledging.** `after` is Control Tower's cursor and
  doubles as an acknowledgement: events at or below it are discarded, so a
  collector that keeps up keeps the buffer flat.
- **Restart-safe.** `seq` restarts with the process, so each process has a
  `stream_id`. A cursor presented with a stale `stream_id` is refused and reset to
  zero rather than being used to discard a fresh buffer unread.
- **Idempotent.** Every event carries `idempotency_key`; Control Tower de-dupes on
  it. Collection is at-least-once by design — the cursor is in memory — so replay
  is routine.

One field a collected envelope cannot fill is `run_id`: a deployed Terminal is
never told which run it belongs to. Control Tower stamps identity from the unit it
polled, and namespaces the idempotency key with the run when the Terminal named
none — otherwise the same pooled `labuser001@` at next month's event would look
like a duplicate of this one. See [Idempotency](#idempotency).

`POST /api/ingest/events` remains the canonical envelope and still works for a
caller that presents a Databricks identity alongside the token (an operator, or a
service principal explicitly granted `CAN USE`). It is not the path deployed
Terminals use.

Discovery records are additionally surfaced on `GET /api/admin/stats`, which
remains a reconciliation channel: neither is authoritative alone, and
`idempotency_key` makes the overlap harmless.

## Envelope

Unchanged from C3a. Every insight event is an `AttendeeEventIn`:

| Field | Type | Notes |
|---|---|---|
| `schema_version` | integer | Envelope version. `1`. |
| `run_id` | uuid \| `""` | The Control Tower run. **Empty on the collect path** — a deployed Terminal is never told its run, so the collector stamps it from the unit it polled. Must be a UUID on the push path, where nothing else can supply it. |
| `workspace_id` | string \| null | The attendee's workspace. Stamped by the collector when the Terminal left it null. |
| `attendee` | string | The **pooled** lab identity (`labuser007@…`), not a real email. |
| `type` | enum | One of the three types below. |
| `occurred_at` | RFC 3339 | When the Terminal observed it. |
| `payload` | object | Type-specific; specified below. |
| `idempotency_key` | string | Stable per logical event. De-dupe key. |

**`attendee` is a pooled identity, and that is the whole reason the roster
exists.** The Terminal cannot know who is holding `labuser007@`. Resolving it to
a real person at a real company is Control Tower's job, via
`roster_entries` / `attendee_identities`. An unattributed attendee is reported as
such rather than guessed at.

## Event types

### `workshop.signal` — behavioural rollup

One per attendee per harvest. Supersedes rather than accumulates: the newest
signal for an attendee is the current view, so `idempotency_key` includes a
bucket (see [Idempotency](#idempotency)).

`payload`:

| Field | Type | Notes |
|---|---|---|
| `stats_schema_version` | integer | `3`. Mirrors `server/stats.py:STATS_SCHEMA_VERSION`. |
| `phase` | string | Content phase at capture (`intro`/`build`/`wrap`/…). |
| `minutes_building` | integer | Wall time since first seen. |
| `agent_sessions` | integer | Agent PTYs launched (excludes bash). |
| `terminal_sessions` | integer | All PTYs launched. |
| `topics` | string[] | Content-pack topic names observed, sorted. |
| `code` | object | `projects`, `commits`, `files`, `lines` from their repos. |
| `resources` | object | Workspace census: `jobs`, `pipelines`, `apps`, `dashboards`. |
| `errors` | integer | Failed launches / agent errors. |
| `idle_seconds` | integer \| null | Abandonment signal. |
| `discovery_records` | integer | Discovery records captured for this attendee. |
| `signal` | object | The derived rollup, below. |

`payload.signal` is the sales-legible reduction — the part a brief quotes
without arithmetic:

| Field | Type | Notes |
|---|---|---|
| `engagement` | enum | `observer` \| `explorer` \| `builder`. See below. |
| `primary_topic` | string \| null | Most-hit topic. |
| `topic_hits` | object | Topic name → hit count. Depth, not just presence. |
| `products` | string[] | Databricks products implicated, sorted. Pack topics minus lifecycle markers. |
| `resource_kinds` | string[] | Census keys with a non-zero count, sorted. |
| `shipped` | boolean | Committed code exists. |

`engagement` is deliberately coarse and honest about what it can know:

- `observer` — no agent session. Present, not building.
- `explorer` — agent sessions but nothing committed.
- `builder` — committed code exists.

**`explorer` is not a lesser category.** An attendee who spent an hour with an
agent and shipped nothing usually hit a wall, and the wall is the most valuable
thing a brief can tell an account team. Weighting `builder` above `explorer` in
downstream synthesis would systematically discard the best material.

### `discovery.record` — elicited discovery

Emitted when the agent has gathered enough signal, once per record. Multiple
records per attendee are expected as understanding improves; each is a separate
event, and the record supersedes prior ones with the same `record_id`.

Every field is optional except `record_id`, `captured_at` and `revision`. **A
partial record is the normal case** and must be stored: an attendee who described
their blockers but never named a timeline still yields a useful record, and a
schema that demanded completeness would push the agent into interrogating people.

| Field | Type | Notes |
|---|---|---|
| `record_id` | string | Stable per record. Re-emitting the same id is an update. |
| `captured_at` | RFC 3339 | First capture time. Unchanged by later revisions. |
| `revision` | integer | Starts at 1, bumped on every change. Last segment of `idempotency_key`. |
| `redacted_by_attendee` | boolean | Present and `true` only on a withdrawal; every content field is then blank. |
| `agent` | string | Which agent elicited it (`claude`/`codex`/`omnigent`). |
| `confidence` | enum | `low` \| `medium` \| `high` — the agent's own read of how firmly the attendee stated this. |
| `use_case_title` | string | Short label. |
| `use_case_summary` | string | Two or three sentences. |
| `goal` | string | What they came to do today. |
| `current_stack` | string[] | Named tools/vendors in their existing setup. |
| `databricks_products` | string[] | Products implicated. |
| `blockers` | string[] | What is stopping them today, in their words. |
| `interest_signals` | string[] | Stated buying/expansion interest. |
| `timeline` | string | Stated timing, verbatim (`"Q3"`, `"after our migration"`). |
| `industry` | string | If stated. |
| `redactions` | integer | Values stripped by the redaction pass. |

`confidence` exists so downstream synthesis can distinguish "they said they need
this by Q3" from "the agent inferred a timeline". A brief that presents an
inference as a customer commitment is actively harmful to the account team that
acts on it.

**How the agent reaches this.** `assets/bin/workshop-discovery` posts to
`POST /api/discovery` on the local app, authenticated by the attendee's callback
capability — the same path `workshop-grant-me` uses, so the agent cannot record
against another attendee. The helper is installed for every attendee regardless
of the flags, because an operator can turn capture on mid-run and homes are only
provisioned once; the endpoint answers `{"captured": false}` when capture is off,
which the helper reports without alarming anyone.

The instruction overlay `assets/instructions/discovery.md` is appended to the
agent's memory only when `DISCOVERY_ENABLED` resolves true. It is deliberately
written against interrogation — the agent is told to build what was asked for and
treat discovery as a by-product, that a partial record is expected, and to tell
the attendee once that it is recording and stop for the session if asked. The
capture mechanism here is an instruction, so the wording is load-bearing and is
covered by `tests/test_discovery_agent.py`.

**Withdrawal is an event, not an absence.** The attendee's insights pane lists
their own records with a Remove control (`GET /api/discovery`,
`POST /api/discovery/redact`). Removing one blanks it locally *and* emits a final
revision carrying `redacted_by_attendee: true`, because by then the original has
almost certainly already reached Control Tower. CT must treat the flag as one-way
and exclude the record from every brief; deleting the Lakebase row is CT's to do,
since the terminal cannot reach back into it. A record that is only blanked
locally leaves the attendee looking at an empty pane while the brief still quotes
them, which is worse than never offering the control.

### `insight.summary` — edge summary at wrap

One per attendee, at the `wrap` phase transition, with a non-LLM fallback at the
teardown `final=true` harvest. Stamped and idempotent: exactly once per attendee,
with one permitted exception described below.

| Field | Type | Notes |
|---|---|---|
| `summary_id` | string | Stable per attendee per run, so a regenerated summary supersedes rather than adds. |
| `generated_at` | RFC 3339 | |
| `generator` | enum | `llm` \| `extraction`. |
| `model` | string \| null | Serving endpoint used, when `generator` is `llm`. |
| `phase` | string | Phase at generation — `wrap` normally, whatever phase the instance was left in for the teardown fallback. |
| `headline` | string | One line. Required — a blank headline renders as a data bug rather than a quiet session, so the producer substitutes an explicit "no summary could be derived". |
| `what_they_built` | string | Prose. |
| `use_cases` | object[] | `title`, `summary`, `products`, `evidence`. |
| `blockers` | string[] | Deduped error first-lines and stated blockers. |
| `products` | string[] | Sorted. |
| `artifacts` | object[] | `kind`, `title`, `bytes`. Metadata only — never contents. |
| `prompt_count` | integer | Every attendee turn, including the context-free continuations withheld from the summariser's input. |
| `redactions` | integer | Secrets stripped while harvesting. |

`generator` must be surfaced wherever a summary is presented. An `extraction`
summary is keyword-level and thin — its `use_cases` are the attendee's own prompts
quoted verbatim, with no paraphrase offered — while an `llm` one is prose.
Rendering them identically would let a reader mistake the fallback's silence for a
finding.

**The one permitted repeat.** An `extraction` summary may later be superseded by
an `llm` one for the same attendee; the reverse must never happen. This exists
because the wrap trigger settles for extraction when the serving endpoint is
unreachable, and an operator re-running wrap after it recovers should not be stuck
with the thin version. The two carry different idempotency keys, so both reach
Control Tower, and **CT must prefer `llm` on the same `summary_id`** rather than
last-write-wins.

### Health events (pre-existing, now actually accepted)

`credential.health`, `entitlements.health`, and `operational.health` are emitted
by the Terminal today and were **rejected** by Control Tower's ingest allowlist
until this change — including `credential.health`, the documented early warning
for a missing grant before an event goes live. They are on the allowlist now.
They describe the instance, not a person, and arrive with `attendee: "system"`.

## Idempotency

`idempotency_key` is the only de-dupe mechanism, so its construction decides
whether repeated harvests accumulate or supersede:

| Type | Key shape | Effect |
|---|---|---|
| `workshop.signal` | `signal:{run_id}:{attendee}:{bucket}` | One row per time bucket per attendee. Bounded growth; latest wins in reporting. |
| `discovery.record` | `discovery:{run_id}:{attendee}:{record_id}:{revision}` | One row per revision; the projection keeps the highest. |
| `insight.summary` | `summary:{run_id}:{attendee}:{generator}` | At most one per generator per attendee. `llm` supersedes `extraction` on the shared `summary_id`; never the reverse. |

On the collect path `{run_id}` is empty, so these shapes collapse to
`signal::labuser001@…:{bucket}` and friends. Because
`attendee_events.idempotency_key` is globally unique, those keys would collide
across runs — the same pooled `labuser001@` at the next event would be silently
discarded as a duplicate, costing a whole attendee with no error raised anywhere.
**Control Tower therefore prefixes a runless key with the run it attributed the
event to**, and leaves a key that already names that run untouched so a pushed and
a collected copy of one event still de-duplicate against each other.

The signal bucket keeps a long workshop from writing an unbounded number of
near-identical rows while still preserving a coarse time series.

The discovery `revision` is what makes "re-emitting a record updates it" true.
De-duping on `{record_id}` alone cannot distinguish a refinement from a retried
flush, so every improvement after the first would be discarded as a duplicate —
and the withdrawal above could never be delivered at all. Because the retry
buffer can flush out of order, the projection must ignore a revision lower than
the one it already holds rather than blindly taking the last arrival.

## Redaction

Every payload passes a redaction step before it leaves the process. It strips
values that look like credentials — `dapi`-prefixed tokens, bearer tokens, PEM
blocks, JWTs, long high-entropy hex/base64 runs, and `key=value` pairs whose key
is secret-shaped — and counts what it removed in `redactions`.

This is a backstop, not the primary control. The primary control is that
free-text fields hold what the *attendee said about their business*, not machine
output. But an attendee can paste a connection string into a chat window while
describing their stack, and that must not become a row in Lakebase.

The count is reported rather than hidden: a record with a high `redactions` value
is a signal worth an operator's attention.

## Roster CSV (Control Tower side)

Uploaded to `POST /api/labs/{run_id}/roster`. Headers are matched
case-insensitively against a set of aliases, so an organiser's registration
export can be pasted without editing. Unknown columns are ignored.

| Canonical field | Accepted headers (examples) | Required |
|---|---|---|
| `email` | Email, Work Email, E-Mail, Attendee Email | **yes** |
| `full_name` | Name, Full Name, Attendee, Participant | no |
| `company` | Company, Customer, Account Name, Organisation, Employer | no |
| `company_domain` | Company Domain, Domain | no — defaults to the email's domain |
| `sfdc_account_id` | SFDC Account ID, Salesforce ID, Account ID, CRM ID | no |
| `job_title` | Title, Job Title, Role, Position | no |
| `ae_email` | AE, AE Email, Account Executive | no |
| `sa_email` | SA, SA Email, Solutions Architect | no |

Rules that matter:

- **Grouping keys on `company_domain`, not `company`.** Operators spell the same
  customer three ways in one file ("Acme", "Acme Corp", "ACME Inc"), which would
  split one customer into three briefs.
- **Unparseable rows are returned, not dropped.** A silently skipped attendee is
  insight attributed to nobody.
- **Attendees absent from the roster stay unattributed.** They are recorded as
  such and surfaced as an explicit bucket. Guessing would attribute one
  customer's insight to another, which is worse than a visible gap.
- Assignment is positional by default (nth roster row to nth unit) because
  pooled identities carry no information about who holds them. An exact match on
  a real work email wins over position, and an operator can correct any binding
  by hand — corrections survive a later roster re-upload.

## Account Manager Brief

Generated by Control Tower per company. Markdown is the source of truth; CSV and
any later PDF derive from it.

1. **Header** — customer, event, date, attendee count, AE and SA owners.
2. **Attendees** — name, title, what they built, topics explored, engagement.
3. **Use cases discovered** — title, summary, Databricks products implicated,
   stated timeline, confidence, evidence link.
4. **Buying signals and blockers** — quoted from discovery records.
5. **Recommended next actions** — concrete and assignable.
6. **Appendix** — raw discovery records.

Every claim must be traceable to a record or a signal. A brief that cannot show
its evidence is indistinguishable from a guess, and an account team that gets
burned once will not open the second one.

## Versioning

- The envelope carries `schema_version` (currently `1`).
- `workshop.signal` carries `stats_schema_version`, which tracks
  `server/stats.py:STATS_SCHEMA_VERSION` (currently `3`).
- Control Tower accepts stats schema `2` and `3`. It previously pinned `1` while
  the Terminal shipped `2` — a skew this change closes.
- Adding an optional payload field is backward compatible and does not need a
  version bump. Removing or retyping one does.
- A consumer that meets an unknown `type` must reject it (the allowlist is
  explicit on purpose, so additions are deliberate). A consumer that meets an
  unknown payload *field* must ignore it.
