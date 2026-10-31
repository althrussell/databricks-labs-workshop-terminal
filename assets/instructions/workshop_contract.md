<!-- workshop-workflow-contract:v3 -->
## How to build

Success is a useful result they recognise as theirs and can change again.

### Choose a path once

Recommend a path. If none is recorded, ask once: "Would you like a rapid prototype to react to, or guided ideation to design it together?" Record it with `workshop-workflow path rapid|guided --by attendee`; never ask again.

- **Rapid:** a focused POC slice; extras parked as "next". Confirm the MVP, building blocks and plan, and state the design direction in one line.
- **Guided:** Impeccable `shape` explores the design questions with a recommendation. End at "Shall I go with this direction, or refine it further?" and record `workshop-workflow direction "…" --confirmed-by attendee` once they confirm. That isn't a go-ahead: put the plan and wait.

### Plan approval

`workshop-init-project` and every other scaffold refuse until the attendee approves this build's plan.

- **Claude Code: you start in plan mode.** Ask the meeting questions with AskUserQuestion, save prefs, discover data read-only, put the plan with ExitPlanMode, and build after approval.
- **Codex, and Omnigent's Claude and Codex:** 1) put the plan as a question, "Start building this?" (`request_user_input`; AskUserQuestion on Omnigent's Claude); 2) on yes, run `workshop-workflow plan --approved --source attendee`; 3) then scaffold.
- **All:** the plan and "Shall I start, or change anything?" are one turn; only an explicit yes ("yes", "go ahead", "build it") is approval. "A rapid prototype, please." chooses a path, not approval. If `workshop-init-project` refuses, put the plan and wait.

### Data

- Check available data before inventing it: theirs, then `$WORKSHOP_DEMO_CATALOG` (read-only), then `$WORKSHOP_CATALOG`; joins are normal. The plan names the `workshop_demo` tables it will read, with their key columns.
- Label synthetic or assumed numbers where they are stated.
- Report the strongest planted signal, not the first: split by each category that looks like a cause before naming a driver. On synthetic data, call a pattern illustrative unless it clearly holds; never read a signal into data that has none.
- Never GRANT on `$WORKSHOP_DEMO_CATALOG`: read in place.

### Skills and verification

- **UI design:** Impeccable is the design authority: `impeccable context`, `DESIGN.md`, `reference/craft-floor.md`, `$WORKSHOP_DESIGN_REFERENCES`; run `detect` once, never `impeccable-finish-reviewer` or phone viewports.
- **Claims:** Share the first preview to try in their browser. Check it as the attendee with `workshop-browser inspect <url> --deployed` when available, else one fast pass yourself; say what remains unverified. Do not install Playwright browsers or run `databricks apps validate`, whatever a skill says; fold a skill's questions for the attendee (e.g. Analytics vs Lakebase synced tables) into your recommendation.
- **Deploys:** run the typecheck and tests before each deploy; if one fails, fix it or name it in the wrap.
- **Test records:** ask before writing one into their app data, label it as a test, and clean it up.
- **Agent Bricks:** if it says "MODEL is not enabled" or its CLI is missing, say so plainly and offer Genie or a simpler search by name. Never ship a lesser chat under the Agent Bricks name.
- **Resuming:** stage boundaries are a safe point to compact (`PRODUCT.md`, `DESIGN.md`, README).
