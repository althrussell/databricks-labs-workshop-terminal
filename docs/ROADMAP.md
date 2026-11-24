# Workshop Terminal — Roadmap

The Workshop Terminal owns **how you build**: the agentic workbench where an
attendee meets a coding agent, builds something real on Databricks, and walks
out wanting more. It must excite and delight **standalone** — no points, no
leaderboard, no game required.

> **Boundary (see Quest alignment below):** anything that *judges or rewards*
> the work — missions, validators, scoring, teams, leaderboards — belongs to
> [Databricks Quest](https://github.com/deepbasu123/databricks-quest). The
> Terminal helps *do* the work. The workspace itself is the only integration
> layer between them; deep links are config, never code.

The delight thesis: a learner doesn't need a score to feel momentum. They need
a **fast first win**, an environment that **feels alive and personal**, a
**rescue before frustration**, and something **worth taking home**.

---

## Status at handover (24 Nov 2026)

This section replaces the original Phase 0–4 plan, which predated the 2026 event season. The
fleet runs release `v2026.11.24.1`. The live list of open work is the repository's issues.

### Shipped

| Area | What attendees and operators have | Reference |
|---|---|---|
| Agents | One-click Omnigent, Claude Code and Codex, with credentials wired up and no token screen | README, `docs/auth-identity-model.md` |
| Isolation and resilience | Per-attendee instance and HOME; terminals survive refreshes and reconnects | README |
| Building | A governed App deploy-and-wait tool; a coached first run and onboarding wizard; Genie hand-off by space id | `docs/app-deploy-tool.md`, `docs/admin-api.md` |
| Take-home | Code export to the attendee's catalog with a **Download my code** button; the brag certificate | `docs/take-home-export.md` |
| Help | Raise a hand, help-desk messages and the Workshop Assistant | `docs/attendee-messaging.md` |
| Phone | Phone tab and voice input | `docs/mobile-and-voice.md` |
| Insight | Insight nuggets with topic detection and coach nudges (first cut); insight capture to Control Tower, including insight v2 | `docs/workshop-insight-contract.md`, `docs/adr/0001-workshop-insight-capture.md` |
| Closeout | A sealed, bounded closeout that Control Tower verifies before teardown | `docs/wt02-sealed-workshop-closeout.md` |
| Operators | Operator panel, admin API, floor runbook, cobranding | `docs/admin-api.md`, `docs/operator-runbook.md` |
| Platform | Agent Bricks CLI (opt-in), governed AI Gateway models, observability | `docs/agent-bricks-cli.md`, `docs/governed-ai-gateway.md`, `docs/observability.md` |

### Open issues

- **#177:** audit the remaining mutating `/api` routes for capture-writer classification, so that a failed read never blocks the seal.
- **#178:** `workshop-browser` should wait for a slow answer before its screenshot.

### Ideas carried forward (status not verified at handover)

These came from the original plan. Some may be partly built; check the code before starting one.

- **First minute:**
  - starter-prompt chips that type into the terminal as the attendee;
  - actionable insight CTAs;
  - boot narration while CLIs install;
  - a personal welcome.
- **Feels alive:**
  - a topic trail;
  - phase-change moments;
  - workbench comfort (tab names, split terminals, font size, light theme).
- **Operators:**
  - a read-only session view;
  - needs-help signals;
  - a room pulse (aggregate, never a leaderboard);
  - a content-pack dry-run.
- **Take-home:**
  - a personal learning recap;
  - a continue-at-home pack;
  - a Quest hand-off link.

## Platform track (continuous)

- **Content pack library** — `content/packs/` with curated packs per event
  type (data-engineering day, GenAI day, cobranded customer sample) so an
  operator starts from a great default, not a blank JSON.
- **Anonymous telemetry** — card clicks, chip usage, time-to-first-prompt;
  feeds both operator pulse and pack curation. (Fleet telemetry export has been
  off since the 4 Nov 2026 ingestion incident; see `docs/observability.md`.)
- **Quest alignment ADR** — the boundary above, recorded in both repos, with
  the routing rule: scoring/validation features → Quest; in-session
  assistance → Terminal.
- **Model/version pinning per event** — already env-driven; keep current with
  gateway model churn.
- **A durable attendee credential** — the agent builds as the app service
  principal because nothing attendee-shaped can write: the OBO scope vocabulary
  has no write scope, the attendee cannot mint a PAT, an admin cannot mint one
  for them, and the Apps proxy rejects PATs on the runner hop. The one surviving
  route is a custom OAuth U2M app integration holding a refresh token
  server-side, gated on three account-level questions. Post-event, scoped in
  [adr/0002-attendee-credential.md](adr/0002-attendee-credential.md).

## Deliberately not building

- Missions, checkpoints, validators, points, badges, teams, leaderboards,
  projector scoreboards — that's **Quest Event Mode**. The Terminal's phases
  pace *content*, never goals.
- A Lakebase or database *owned by this app* — teardown stays `apps.delete`.
  **Amended** by workshop insight capture: durable insight now lives in Control
  Tower's existing Lakebase, reached over the ingest API. The Terminal itself
  still owns no external state, so this constraint holds where it was load-bearing
  — the reversal is that attendee-authored discovery answers leave the instance
  at all. See [adr/0001-workshop-insight-capture.md](adr/0001-workshop-insight-capture.md).
  **Also amended** by the shared `workshop_demo` catalog: it is long-lived and
  outside any run, but Control Tower's operator owns it, no code path here or
  there creates or writes it, and teardown is still `apps.delete` because a run
  creates nothing in it. See [adr/0003-shared-demo-catalog.md](adr/0003-shared-demo-catalog.md).
- Prompt injection as fake user messages — context goes through memory files;
  user-visible chips the attendee clicks are the only way text enters a
  terminal on their behalf.
