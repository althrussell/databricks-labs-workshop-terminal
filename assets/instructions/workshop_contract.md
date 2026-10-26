<!-- workshop-workflow-contract:v3 -->
## How to build

Success is a useful result they recognise as theirs and can change again.

### Choose a path once

Recommend a path. If none is recorded, ask once: "Would you like a rapid
prototype to react to, or guided ideation to work through the problem and
design together?" Record (`workshop-workflow path rapid --by attendee`,
`workshop-workflow phase build`) once the project exists, and never ask again
on resume, follow-up or agent switch.

- **Rapid (POC scope, with SME + SA):** Focused POC slice with extras parked as
  "next". Confirm the MVP, Databricks building blocks (with *why*), and plan;
  wait before creating files or tables.
- **Guided (deeper discovery first):** Use Impeccable `shape` to explore design
  questions and options with a recommendation. End at: "Shall I build this
  direction, or refine it further?" Record when confirmed (`workshop-workflow
  direction "…" --confirmed-by attendee`), then confirm the plan and wait.

### Skills and verification

- **Data:** Check available data before inventing it: theirs, then
  `$WORKSHOP_DEMO_CATALOG` (read-only), then `$WORKSHOP_CATALOG`. Use fast
  `databricks tables list` checks before approval. Label sample data and
  simulated parts.
- **UI design:** Impeccable (`impeccable` skill) is the design authority:
  `impeccable context` (`PRODUCT.md`), `DESIGN.md`, `reference/craft-floor.md`,
  `$WORKSHOP_DESIGN_REFERENCES`, and run `detect` once (never render phone
  viewports or spawn `impeccable-finish-reviewer`).
- **Claims:** Share first preview to try in their browser (with `workshop-browser`, inspect desktop and exercise primary action; without, one fast pass); say what remains unverified. Do not install Playwright browsers or run `databricks apps validate`.
- **Resuming:** `workshop-workflow show` on resume; stage boundaries are a safe
  point to compact (`PRODUCT.md`, `DESIGN.md`, README).
