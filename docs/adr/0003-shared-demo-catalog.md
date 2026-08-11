# ADR 0003 — A shared demo catalog is long-lived state outside the teardown contract

- **Status:** accepted
- **Date:** 2026-08-11
- **Affects:** `databricks-labs-control-tower` (owns it), this repo (reads it)

## Context

The first ten minutes of a workshop are the ones that decide whether an attendee
gets anything out of the day, and they are currently spent on the wrong thing.
An attendee asks for a warranty dashboard; the agent has no data, so it writes a
generator, invents a schema, produces four hundred rows of `Customer 1`, and
only then starts on the dashboard. The result is a chart of fictional nothing,
built too late to iterate on. An attendee who wanted to see their own industry
gets a demo of the agent's imagination instead.

The obvious fix — ship demo data — collides with a property this system has
defended twice. [ADR 0001](0001-workshop-insight-capture.md) treats external
durable state as the thing to avoid, and `docs/ROADMAP.md` lists it under
*Deliberately not building* precisely because teardown is a single
`apps.delete` with nothing to reconcile.

## Decision

Create one `workshop_demo` catalog per Control Tower deployment, seeded by hand,
granted read-only to `control-tower-lab-users` and `control_tower_wt_callers`,
and never written to by anything in the running system.

Four properties make it something other than the state ADR 0001 rejected:

1. **Nothing in the running system owns it.** It is not provisioned per run, not
   torn down per run, and no code path in either app creates, writes to, or
   deletes it. Control Tower's service principal deliberately lacks the
   metastore admin privilege needed to create a catalog, so the app *cannot*
   acquire ownership by accident. A human runs
   `seed/demo_data/seed_demo_data.py` once when standing up a deployment.

2. **Teardown is unchanged.** `apps.delete` still removes everything a run
   created, because a run creates nothing here. The catalog outlives runs the
   same way the workspace and the metastore do — it is deployment furniture, not
   run state.

3. **Read-only is the enforcement, not the convention.** The grant is
   `USE CATALOG, USE SCHEMA, SELECT, READ VOLUME` at catalog level and never
   `MODIFY`. An attendee who wants to write does `DEEP CLONE` into their own
   catalog, which is a one-line instruction precisely *because* the alternative
   is impossible rather than merely discouraged. This is also what makes a
   reseed safe: nobody's work can live in a table the seed will replace.

4. **Absence degrades to absence.** `WORKSHOP_DEMO_CATALOG` unset means the
   Terminal advertises no demo data and the wizard shows its full unfiltered
   idea catalogue. Every deployment that has not adopted this is unaffected, and
   there is no half-configured state that produces a broken screen.

## Alternatives considered

- **Let the agent generate data per attendee, as today.** Rejected on evidence:
  it consumes the part of the hour that mattered, and the output is
  unrecognisable to the attendee, which is the specific thing a workshop is
  supposed to avoid. It also makes an idea catalogue impossible — you cannot
  promise a card is buildable if the data is invented per session.
- **A catalog per run, created and dropped by Control Tower.** Rejected. It puts
  catalog creation on the provisioning path, which means the service principal
  needs metastore admin, which is a much larger grant than anything else in this
  system holds. It also multiplies the seed cost by the number of runs for data
  that is identical every time.
- **One global catalog shared across regions.** Not possible: `labs` and
  `labs-us` are different metastores. Accepting one catalog per deployment is
  the consequence, and `_meta.seed_manifest` exists so an operator can answer
  "what is live in this region" without diffing data.
- **A Control Tower endpoint or scheduled job that reseeds.** Rejected, and this
  is the one worth being explicit about. The failure mode of an automated
  reseed is that it fires mid-workshop; the failure mode of a manual one is that
  somebody forgets. The second is recoverable and visible, the first destroys a
  live event. A human deciding when it runs is the only reliable guarantee it
  cannot fire at the wrong moment.
- **Copy the seed into each attendee's own catalog at provision time.** Rejected.
  It restores per-run state, multiplies storage by attendee count, and buys only
  the write access that `DEEP CLONE` already gives anyone who asks for it.

## Consequences

- **A new region silently ships without demo data until somebody seeds it.**
  Nothing errors — the Terminal verifies what exists before advertising it — so
  this degrades to attendees from that region seeing a thinner idea grid. That
  is the intended failure mode and also the reason it can go unnoticed for
  months, which is why it is a checklist item in `docs/10-operations.md` rather
  than a note.
- **The seed's schema names are a cross-repository interface.** They are the
  same strings the wizard's idea catalogue tags itself with, so renaming a
  schema empties the grid for that industry with no error anywhere. Both sides
  say so where someone would be about to change one.
- **The data has to be good enough to survive scrutiny.** A synthetic dataset
  whose churn label is uncorrelated with its usage table teaches an attendee
  that the exercise is fake, which is worse than having no data. The seed
  therefore derives labels from the facts that precede them rather than
  assigning both independently, and the notebook says so at each point where it
  would have been easier not to.
- **Attendees can read data they did not create, in a catalog they do not own.**
  This is the one genuinely new surface. It is acceptable only because every row
  in it is synthetic and the catalog comment says so, and it must stay that way:
  the moment anything real lands in `workshop_demo`, this ADR no longer covers
  it.
