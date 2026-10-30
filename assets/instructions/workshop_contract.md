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
  questions and options with a recommendation. End at: "Shall I go with this
  direction, or refine it further?" Record when confirmed (`workshop-workflow
  direction "…" --confirmed-by attendee`). That isn't a go-ahead: put the plan
  and wait.

### Skills and verification

- **Data:** Check available data before inventing it: theirs, then
  `$WORKSHOP_DEMO_CATALOG` (read-only), then `$WORKSHOP_CATALOG`; joins across
  them are normal. Use fast `databricks tables list` checks before approval.
  Label sample data and simulated parts.
- **UI design:** Impeccable (`impeccable` skill) is the design authority:
  `impeccable context` (`PRODUCT.md`), `DESIGN.md`, `reference/craft-floor.md`,
  `$WORKSHOP_DESIGN_REFERENCES`, and run `detect` once (never render phone
  viewports or spawn `impeccable-finish-reviewer`).
- **Claims:** Share the first preview for them to try in their browser. Check it as the attendee with `workshop-browser inspect <url> --deployed` when that tool is available, otherwise do one fast pass yourself; say what remains unverified. Do not install Playwright browsers or run `databricks apps validate`, whatever a skill says; fold a skill's questions for the attendee (e.g. Analytics vs Lakebase synced tables) into your recommendation.
- **Resuming:** stage boundaries are a safe point to compact (`PRODUCT.md`, `DESIGN.md`, README).
