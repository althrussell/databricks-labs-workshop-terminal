<!-- workshop-workflow-contract:v3 -->
## How to build

Success is a useful result they recognise as theirs and can change again, not
just a deployed app. This contract owns pacing, the path and readiness claims
in every harness; skill advice follows it.

### Choose a path once

Recommend a path for the likely user problem. If none is recorded, ask once:
"Would you like a rapid prototype to react to, or guided ideation to work
through the problem and design together?" Guided suits a vague problem;
rapid suits a well-defined task. "Just build it" means rapid. Record who chose
(`workshop-workflow path rapid --by attendee`) and never ask again on resume,
follow-up or agent switch.

**Rapid:** state who it's for, the first useful version and the material
assumptions; ask only about gaps that change the outcome; then build an
end-to-end slice with truthful data and working actions.

**Guided:** use Impeccable `shape`, a few questions whose answers change the
design and genuinely different options with a recommendation. End at a short
brief: "Shall I build this direction, or refine it further?" Record the
direction only when they confirm it (`workshop-workflow direction "…"
--confirmed-by attendee`).

### Design

Impeccable (the `impeccable` skill) is the design authority on both paths.
Rapid means no interview, not fewer design steps. Before writing any UI code,
in this order:
1. Run the skill's `impeccable context`; if `PRODUCT.md` is missing, write it
   first from the request.
2. For a new screen, load the skill's `reference/new-work.md`, choose the
   direction and record it in `DESIGN.md`.
3. Read `reference/craft-floor.md` immediately before UI edits.
4. Adapt the matching layout in `$WORKSHOP_DESIGN_REFERENCES`.
5. Before handing over, run `detect` once and fix what it finds.

### Truthful data and claims

**Check available data before inventing it:** theirs, then
`$WORKSHOP_DEMO_CATALOG`, then `$WORKSHOP_CATALOG`. Label sample data and
simulated parts. Before saying it works, exercise the primary action and
reload if saving is promised; say what remains unverified. Never claim a review
you did not run. Do not install Playwright browsers or run `databricks apps validate` routinely.

### Keeping the record

`workshop-workflow show` when you start, resume or take over; continue from the
next unresolved step and never restart discovery. A stage boundary is a safe
point to compact: re-ground from the record, `PRODUCT.md`, `DESIGN.md` and the
README brief.
