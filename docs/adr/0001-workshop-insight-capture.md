# ADR 0001 — Workshop insight capture reverses two stated principles

- **Status:** accepted, amended 2026-08-11 (see [Amendment](#amendment-2026-08-11--discovery-is-no-longer-agent-only))
- **Date:** 2026-07-30
- **Affects:** this repo and `databricks-labs-control-tower` (paired branches)

## Context

The Workshop Terminal exists to drive Databricks revenue, and the most valuable
thing that happens in a workshop is currently thrown away: an attendee explains
their real stack, the problem that brought them, and what is blocking them today
— to an agent, in a terminal, inside a workspace that gets deleted. The account
team learns none of it.

Two documented commitments stand in the way, and both were deliberate:

- `README.md`: "terminal output and attendee content are never persisted".
- `docs/ROADMAP.md`, under *Deliberately not building*: "Lakebase or any
  external state — teardown stays `apps.delete`".

These were not incidental. The no-external-state rule is what keeps teardown a
single `apps.delete` with no orphaned database to reconcile, and the
no-content rule is what lets us tell attendees their terminal is theirs.

## Decision

Capture consented workshop insight, and record the reversal rather than quietly
bypassing it.

1. **Durable state lives in Control Tower's Lakebase, not here.** Control Tower
   already operates a Lakebase and already polls this app's admin API. The
   Terminal buffers events in memory and Control Tower **collects** them on that
   existing authenticated call; the Terminal keeps nothing long-lived. Teardown
   stays `apps.delete`; there is still no database for this app to orphan.

   Delivery is by collection rather than push because Databricks Apps do not
   permit anything else: the platform proxy in front of every app — Control
   Tower's included — requires a Databricks identity on each request, so a
   Terminal posting with only a shared ingest token is rejected before Control
   Tower's code runs. Inverting the direction also removed the last thing that
   could be misconfigured on a deployed instance, since capture now needs no
   delivery settings at all.

2. **Capture is off by default.** `WORKSHOP_INSIGHT_CAPTURE` defaults to
   `false`. An operator turns it on per event, with attendee consent handled out
   of band through event registration terms. The state is reported in `/readyz`
   and the release manifest so it is provable after the fact which runs had it on.

3. **Two tiers, neither of them transcripts.** Behavioural signal (what they
   did — topics, sessions, resources, already gathered by `server/stats.py`) and
   agent-elicited discovery records (what they want — structured answers the
   attendee gave knowingly). Raw terminal output, file contents, stdout, and
   scrollback are never captured. That part of the original commitment is not
   amended, it is kept.

## What actually changed

The honest version: **attendee-authored content now leaves the instance.** Their
discovery answers, and at wrap a summary derived from their prompts and the
artifacts the agent wrote for them, travel to Control Tower.

What did not change:

- Raw terminal output is still never persisted or transmitted.
- This app still owns no database and requires none.
- Teardown is still `apps.delete`.
- Context still reaches an agent through memory files, never through fabricated
  user messages (`assets/instructions/discovery.md` is an instruction overlay,
  the same mechanism as `lab_coach.md`).

## Alternatives considered

- **Infer use cases from raw transcripts.** Rejected. Transcripts are enormous,
  ANSI-polluted, and mostly stack traces and file listings. An LLM over that
  produces plausible mush that a sales team cannot distinguish from
  hallucination — worse than no data, because it is confidently wrong about a
  customer.
- **Give the Terminal its own Lakebase.** Rejected. It buys nothing that Control
  Tower's Lakebase does not already provide, and costs the `apps.delete`
  teardown guarantee, which is the property doing the most work in this design.
- **Grant the Terminal's app service principal `CAN USE` on Control Tower so it
  can push.** Rejected. It is the only way to make push work through the Apps
  proxy, and it would mean Control Tower entitling a freshly minted service
  principal from a *foreign workspace* on every deploy and revoking it on every
  teardown — per attendee. Collection needs no grant at all.
- **Capture unconditionally with no flag.** Rejected. Consent has to be a
  deliberate per-event decision by whoever ran the registration, and the flag is
  what makes it auditable.

## Consequences

- An attendee-facing transparency surface is warranted, since their own answers
  are now stored elsewhere; see the insights pane work.
- The Terminal now has a soft dependency on Control Tower for insight to land
  anywhere. It stays soft: `event_emitter` is fail-soft with a bounded buffer, so
  a Control Tower outage degrades to lost insight, never to a broken workshop.
- A related promise was broken and has now been corrected rather than kept:
  `assets/instructions/CLAUDE.md` told attendees their work "survives after this
  environment is torn down", but teardown drops the catalog and deletes the
  workspace, destroying both the promote docs and their synced repos. Persisting
  their artifacts for them would mean the Terminal owning durable storage for
  attendee content — the exact dependency this ADR spends its Alternatives
  section rejecting — so the wording now says what is true: the Workspace sync
  survives a restart, not the workshop, and keeping the work means pushing to a
  remote the attendee owns or downloading it while the event is live. The wrap
  guidance and the wrap content pack both prompt for it.
  This asymmetry is worth naming: we route *our* copy of the insight to Lakebase
  and hand the attendee an instruction. It is defensible only because their route
  is a two-command push and ours cannot be delegated to them at all.

## Amendment (2026-08-11) — discovery is no longer agent-only

This ADR assumed a single producer. Decision 3 above says discovery records are
"agent-elicited", and the Alternatives section argues only about *how* an agent
should derive them. The opening wizard now produces them too, before any agent
has started.

**What changed.** The wizard's first two screens ask what the attendee came to
build; that answer becomes the terminal's first prompt *and* a
`discovery.record` at `confidence: high`. The wizard mints the `record_id` and
the agent is handed it, so the agent refines that record rather than opening a
second one.

**Why this does not reopen the argument this ADR settled.** The thing that made
agent elicitation delicate was that an agent asking questions to fill a schema
turns a build session into an interview — which is why the overlay is written
against interrogation and why partial records are the expected case. A wizard
does not have that failure mode: it is asked once, before anything is running,
and it is skippable. Nothing about the consent boundary moves either. The wizard
routes through the same `discovery.record()` path, which no-ops when
`WORKSHOP_INSIGHT_CAPTURE` is off, and a skipped wizard emits nothing at all —
declining is an answer, and recording it anyway would capture the one thing the
attendee explicitly withheld.

**What it costs.** `agent` is now a producer name rather than a harness name,
and a record's `agent` and `confidence` can change between revisions of the same
`record_id`. Consumers must supersede on `revision` alone; the contract says so
explicitly, because a consumer that preferred the high-confidence wizard
revision would freeze each record at what someone typed in the first ninety
seconds.

**What it buys.** The records that previously did not exist. Agent-elicited
discovery only ever fired for attendees who got far enough into a conversation
for the agent to have something to record, which systematically excluded the
people who stalled — and a person who stalled is exactly who an account team
most needs to hear about.
