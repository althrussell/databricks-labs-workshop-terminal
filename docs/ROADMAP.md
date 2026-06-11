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

## Phase 0 — Event-ready (hardening, now)

The unglamorous list that makes the first real event boring (in the good way).

| Item | Notes | Size |
|---|---|---|
| Merge Control Tower PR #5 | Vended credential, platform_admins, content packs, branding | S |
| Repo access decision | Make repo public, or set `workshop_app_git_pat` in CT | S |
| Operator panel E2E in a labs workspace | Needs the `platform_admins` group created there | S |
| 10-user load + chaos test | Concurrent agents, reconnect storms, redeploy mid-session | M |
| CT live content push | CT SP is already in the admin group; wire phase/broadcast/pack push into CT's run view | M |

## Phase 1 — First-minute magic

The blank prompt is the scariest moment of the workshop. Kill it.

| Item | What it feels like | Size |
|---|---|---|
| **Starter-prompt chips** | Clickable chips above the terminal ("Explore the workshop dataset", "Build my first pipeline") that *type into the terminal as the user* — visible, theirs, no fabricated context. Defined per phase in the content pack. | M |
| **Actionable insight CTAs** | Insight cards stop being ads: the Lakebase card's CTA pastes "add a Lakebase table to my app" into the active session. Content pack gains an optional `prompt` per nugget. | S |
| **Boot theater** | While CLIs install, the empty state narrates the setup live ("Fetching the latest Databricks skills… ✓") from `/api/setup-status` — anticipation instead of a spinner. | S |
| **Personal welcome** | Greet by name in the header, "your agent is ready" moment with motion when claude's button goes live. | S |

## Phase 2 — Feels alive, feels personal

| Item | What it feels like | Size |
|---|---|---|
| **Topic trail** | A footer strip of chips — "Your session: Lakebase · Genie · MLflow" — built from the topic detection we already run. A personal trail of what *they* explored (not a score); each chip links to docs. | S |
| **Coach nudges** | Stuck heuristics (idle mid-build, repeated error patterns in output) trigger a gentle, dismissible nudge: "Paste the error to your agent and say *diagnose and fix*." | M |
| **Phase moments** | When the operator advances the phase, attendees get a tasteful interstitial ("☕ Break's over — Build phase: here's what's next") instead of a silent content swap. | S |
| **Workbench comfort** | Rename tabs, side-by-side split terminals, font size, light theme. The room projects confidence when the tool feels premium. | M |

## Phase 3 — Operator superpowers

Standalone events live or die on facilitation. Give the people running the
room eyes and reach.

| Item | What it feels like | Size |
|---|---|---|
| **Read-only session view** | Operator clicks an attendee in presence → watches their terminal live (PTYs are already server-side). Floor support without hovering over shoulders. | M |
| **Needs-help signals** | The stuck heuristics surface in the operator panel: "3 attendees idle >10m in Build". Triage at a glance. | S |
| **Room pulse** | Aggregate, anonymous: active terminals, topics trending in the room, phase distribution. Operational awareness — explicitly not a leaderboard. | M |
| **Pack dry-run** | Operator previews a content pack's phases/cards/chips as attendees will see them, before the event. | S |

## Phase 4 — The take-home

The workshop ends; the relationship shouldn't.

| Item | What it feels like | Size |
|---|---|---|
| **Personal learning recap** | At wrap phase, an agent generates "what you built today" from the attendee's synced repos + topic trail, with Customer Academy links matched to *their* topics. Rendered as the final card and written to their Workspace home. | L |
| **Continue-at-home pack** | A one-pager in their Workspace: how to run Claude on Databricks in their own workspace, with their workshop repos already synced and waiting. | S |
| **Quest hand-off slot** | When Quest is deployed alongside, the recap deep-links to "see what it was worth" — a config link, no dependency. | S |

## Platform track (continuous)

- **Content pack library** — `content/packs/` with curated packs per event
  type (data-engineering day, GenAI day, cobranded customer sample) so an
  operator starts from a great default, not a blank JSON.
- **Anonymous telemetry** — card clicks, chip usage, time-to-first-prompt;
  feeds both operator pulse and pack curation.
- **Quest alignment ADR** — the boundary above, recorded in both repos, with
  the routing rule: scoring/validation features → Quest; in-session
  assistance → Terminal.
- **Model/version pinning per event** — already env-driven; keep current with
  gateway model churn.

## Deliberately not building

- Missions, checkpoints, validators, points, badges, teams, leaderboards,
  projector scoreboards — that's **Quest Event Mode**. The Terminal's phases
  pace *content*, never goals.
- Lakebase or any external state — teardown stays `apps.delete`.
- Prompt injection as fake user messages — context goes through memory files;
  user-visible chips the attendee clicks are the only way text enters a
  terminal on their behalf.
