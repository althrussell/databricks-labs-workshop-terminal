# frontend/AGENTS.md

Local rules for `frontend/`. Read the root [AGENTS.md](../AGENTS.md) first. The routing
table is in [docs/agent-guide/README.md](../docs/agent-guide/README.md).

## What differs here

- The toolchain is npm, not uv. CI pins Node `24.22.1` (`.github/workflows/ci.yml`). Run
  `npm ci` once.
- The build output is committed. `npm run build` runs `tsc --noEmit && vite build` and writes
  `../static/` (`frontend/vite.config.ts`). Commit the regenerated `static/` with the source
  change. CI rebuilds and fails on any `static/` diff. Never hand-edit `static/`.
- Tests are `npm test`, which runs `tsx --test src/*.test.ts`. Pure helpers (`frontend/src/*.ts`) have
  unit tests; components (`frontend/src/components/*.tsx`) mostly do not. Rendered browser coverage
  lives in `tests/test_*_rendered.py` and is opt-in (see CHANGE_GUIDE).
- Network I/O lives in a few modules only:
  - HTTP: `frontend/src/api.ts`.
  - App events: `frontend/src/events.ts` (`/ws/events`).
  - Terminal socket: `frontend/src/components/TerminalView.tsx` (`/ws/sessions/{id}`).
  - Network probe: `frontend/src/networkCheck.ts`.
  - Error telemetry: `frontend/src/telemetry.ts`.

  Keep new calls in those modules. A new or changed endpoint also needs its server route in
  `server/main.py` or `server/admin.py`; see FLOWS.
- Local dev: run `make dev-backend` (fake identity, `LOCAL_DEV=1`) and `make dev-frontend`
  (Vite on :5173, which proxies `/api` and `/ws` to :8000).

## Conventions

- Attendee-facing confirmations are visual: a thumbnail, icon or short label with `alt`/`title`.
  Do not add explanatory sentences in compact toolbars or bars (agent bar, PR #154). Reserve
  text for warnings the attendee must act on.
- Before finishing UI work, check the real rendered app, not just the tests.
- WebSocket close codes are a server contract. `4403`, `4404`, `4408`, `4410` and `4413`
  come from `server/ws.py`. Handle new codes on both sides.

## Before you finish

`npm test`, then `npm run build`, then `git status -- ../static` to confirm a rebuilt build
you intend to commit. If the change alters an API shape, also run the server tests named in
the task table. Apply the maintenance table in
[CHANGE_GUIDE](../docs/agent-guide/CHANGE_GUIDE.md#maintenance-table).
