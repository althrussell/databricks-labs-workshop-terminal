# ADR 0001 — Workshop insight capture reverses two stated principles

- **Status:** accepted
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
