<!-- workshop-workflow-contract:v3 -->
## How to build

Success is a useful result they recognise as theirs and can change again.

### Choose a path once

Recommend a path. If none is recorded, ask once: "Would you like a rapid
prototype to react to, or guided ideation to work through the problem and
design together?" Record who chose (`workshop-workflow path rapid --by
attendee`) once a project exists, and never ask again on resume, follow-up or
agent switch.

- **Rapid (POC scope, with SME + SA):** Focused POC slice with extras parked as
  "next". Still confirm the MVP, Databricks building blocks (with a *why*), and
  plan, and wait before creating files or tables.
- **Guided (deeper discovery first):** Use Impeccable `shape` to explore design
  questions and options with a recommendation. End at: "Shall I build this
  direction, or refine it further?" Record when confirmed (`workshop-workflow
  direction "…" --confirmed-by attendee`), then confirm the plan and wait.

### Skills and verification

- **Data:** Check available data before inventing it: theirs, then
  `$WORKSHOP_DEMO_CATALOG` (read-only), then `$WORKSHOP_CATALOG`. Use fast
  `databricks tables list` checks before approval. Label sample data and
  simulated parts.
- **UI design (after plan approval):** Impeccable (`impeccable` skill) is the
  design authority on both paths. Before UI code, in order: `impeccable
  context` (`PRODUCT.md`), `reference/new-work.md` (`DESIGN.md`),
  `reference/craft-floor.md`, `$WORKSHOP_DESIGN_REFERENCES`, and run `detect`
  once.
- **Claims:** Before saying it works, exercise the primary action and reload if saving is promised; say what remains unverified. Do not install Playwright browsers or run `databricks apps validate`.
- **Resuming:** `workshop-workflow show` on resume; a stage boundary is a safe
  point to compact and re-ground from the record, `PRODUCT.md`, `DESIGN.md` and
  the README brief.
