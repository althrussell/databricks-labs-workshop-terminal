# Learning interaction

**Job:** practise a skill and get feedback grounded in your actual answer.

- One clear next step at a time; the screen is not an operations dashboard.
- Feedback explains this answer: what was right, what to change, and why, in
  terms of what the learner did, not a generic "Incorrect".
- Progress counts completed work ("2 of 5 exercises done"), never time spent or
  clicks.
- Hints are offered, not forced; revealing the answer is a deliberate choice and
  is recorded as such.
- Narrow layout: the exercise and its feedback stack; the next step stays
  visible after feedback.

States: `loading`, `empty` (no exercises yet, with how to start), `error`
(checking failed; the answer is kept), `long-label`.

Checks: feedback is announced (`role="status"`); inputs are labelled; focus
moves to feedback after checking and to the next exercise after continuing;
focus visible; AA.
