# Guided form

**Job:** capture a decision or record that must be saved correctly the first
time.

- Every field has a visible label (never only a placeholder) and says why it is
  needed when that is not obvious.
- Validation happens on submit and on leaving a field, next to the field, in
  words that say how to fix it; focus moves to the first problem.
- Submission is genuine: the button shows progress, and success is shown only
  after the authoritative save returns. No success toast before the outcome.
- A failed save keeps everything typed and says what failed and what to do.
- The saved state is clear afterwards ("Saved 10:42 by you"), and the form can
  be edited again.
- Narrow layout: one column, the submit button stays at the end of the form.

States: `loading` (the record is being fetched), `empty` (a new record),
`error` (save failed, input kept), `long-label` (long option labels wrap).

Checks: labels are programmatically associated; errors use `aria-invalid` and
`aria-describedby`; keyboard order matches visual order; focus visible; AA.
