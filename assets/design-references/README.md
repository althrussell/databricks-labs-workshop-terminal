# Composition references

Five small, working compositions for the jobs workshop apps most often do. They
are **references, not templates**: pick the one whose job matches the
attendee's, read its rationale, and adapt its structure to their data, brand and
words. Never copy one wholesale, and never force a job into the wrong one.

| Pattern | Use when the job is… |
|---|---|
| `queue-detail` | working through items in priority order and acting on one |
| `analytical-exploration` | understanding a measure over time or across groups |
| `guided-form` | capturing a decision or record that must be saved correctly |
| `conversational-workspace` | asking questions of data or an agent and keeping the evidence |
| `learning-interaction` | practising a skill with feedback on real answers |

Each folder has `README.md` (rationale and checks) and `Reference.tsx` (a React
component that compiles in a fresh APX app, with `default`, `long-label`,
`empty`, `loading` and `error` examples). `tokens.json` names the semantic
colour roles and the contrast pairs `scripts/check_design_references.py`
measures.

What every reference holds to, and what the checker enforces:

- every primary action has an observable outcome; no no-op handlers, no
  success message before the authoritative result, no silent mock fallback;
- states are real: loading, empty and error are shown, never hidden;
- keyboard focus is visible and contrast meets WCAG AA for the token pairs;
- narrow layouts transform tables into rows without hiding essential actions;
- sample data is labelled as sample, and no KPI is shown without its source.

Rendered screenshots are taken with `workshop-browser inspect` in a live
terminal. They are **pending owner review** until a person approves them:
pixel comparison alone cannot decide design quality.
