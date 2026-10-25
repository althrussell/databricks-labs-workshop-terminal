# Queue / detail

**Job:** work through items in priority order, understand why each is where it
is, and act on one without losing your place.

- The queue starts in the first desktop viewport: no hero, no KPI band above it.
- Rows align the fields people compare (priority, reason, age, owner) so the eye
  scans down a column, not across a card.
- Priority shows its rationale in words next to the rank, from the rules the
  owner agreed. Never invent a score.
- Filters are compact and change the actual rows; the count says what is shown.
- Selecting a row opens progressive detail beside it (below it when narrow); the
  primary action ("Take ownership", "Record outcome") lives in the detail and
  reports its real result.
- Narrow layout: the table becomes stacked rows; the action stays reachable.

States: `loading` (skeleton rows, no fake data), `empty` (says why and what to
do), `error` (says what failed and offers retry), `long-label` (truncates with a
title, never overlaps).

Checks: keyboard can reach every row and the action; focus is visible; the
selected row is announced (`aria-selected`); contrast meets AA.
