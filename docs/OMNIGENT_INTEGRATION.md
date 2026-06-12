# Omnigent Integration Plan

**Status: proposed — not yet implemented.**

Omnigent is Databricks' agent meta-harness (OSS launch imminent;
`omnigent==0.1.0rc2` on PyPI today). It wraps coding agents — Claude Code,
Codex, and YAML-declared custom agents — in managed sessions with a TUI, a
server, and a web UI. This plan makes Omnigent the **primary interface** in
the Workshop Terminal: attendees land in the `omnigent` TUI by default, with
Claude and Codex selectable as harnesses through it. Bare `claude` / `codex`
launch modes stay available as fallbacks.

## Decisions

| Question | Decision |
|---|---|
| Server topology | **Embedded, per-attendee** — each attendee's first `omnigent` invocation auto-spawns a local server keyed to their isolated `$HOME`. No separate server app, no shared-server auth. Sessions are ephemeral with the app — acceptable for workshops. |
| Default UX | Terminal boots straight into the `omnigent` TUI; `omnigent claude` / `omnigent codex` offered as direct session types too. |
| Fallback | Bare `claude` / `codex` catalog entries kept (demoted in order). The existing `AGENT_CATALOG_PATH` override remains the operator escape hatch to re-promote them mid-event. |
| Install source | PyPI (`omnigent==0.1.0rc2`), env-pinned via `OMNIGENT_VERSION`; bump to GA next week — one env change. |

## Why this fits the existing architecture

- **Per-user isolation falls out of per-user HOMEs.** Omnigent resolves all
  state via `Path.home()` (`~/.omnigent/config.yaml`, `~/.omnigent/local_server.pid`).
  Its local-server port picker bind-probes 6767 and falls back to a free port,
  and discovery goes through each HOME's pidfile — concurrent attendees on one
  container don't collide. Auto-spawned servers run in single-user loopback
  mode (`OMNIGENT_LOCAL_SINGLE_USER=1`), so no accounts/auth machinery is needed.
- **Credentials reuse the existing rotation plumbing.** Omnigent's `kind: gateway`
  provider supports `auth_command` — a shell command that prints a bearer token,
  refreshed on a 15-minute cadence that matches our token lifetime exactly. We
  write the rotating token to a per-user file and point `auth_command` at it;
  rotation never rewrites YAML.
- **The launch catalog is already config-driven.** New session types are pure
  `content/agents.json` entries — `launch_command()` handles multi-word
  commands, the frontend renders whatever the catalog returns (unknown icons
  fall back), and readiness gating works per-binary. **No frontend build and
  no backend launch-code changes are required.**

## Changes

### 1. Bootstrap installers — `server/bootstrap/install.py`

- `OMNIGENT_VERSION = os.environ.get("OMNIGENT_VERSION", "0.1.0rc2")` beside
  the `CODEX_VERSION` pin.
- `_install_tmux()` — omnigent's claude/codex wrappers hard-require tmux and
  the Apps runtime has no package manager. Skip if `shutil.which("tmux")`;
  else curl a pinned static musl build (e.g. `mjakob-gh/build-static-tmux`
  release asset, sha256-verified, `TMUX_STATIC_URL` env override) into the
  shared prefix `bin/` — same pattern as the databricks CLI installer.
- `_install_omnigent()` — ensure `uv` (probe PATH, else curl the astral
  installer into the shared prefix), then
  `uv tool install --python 3.12 omnigent==$OMNIGENT_VERSION` with
  `UV_TOOL_DIR` / `UV_TOOL_BIN_DIR` / `UV_PYTHON_INSTALL_DIR` pointed into the
  shared prefix. uv fetches a standalone CPython 3.12 — omnigent requires
  ≥3.12 regardless of the runtime Python, with no root needed. Stamp the
  installed version in a sidecar file and **reinstall when the pin changes**
  (unlike the exists→done shortcut the claude installer uses) so the GA bump
  actually takes effect on redeploy.
- Extend `status()["ready"]` with `"omnigent"` (venv **and** tmux complete);
  add both steps to the `run_in_background()` pool.

### 2. Per-attendee provisioning — `server/cli_config.py`, `server/users.py`

- New `configure_omnigent(user, token)` wired into `configure_all()`:
  - Write the rotating token to `~/.config/workshop/gateway-token` (0600).
    Add the same write to `update_tokens()` — the rotation fast path never
    touches YAML.
  - Write `~/.omnigent/config.yaml` (no `server:` key → per-user auto-spawn):

    ```yaml
    providers:
      databricks-gateway:
        kind: gateway
        default: true
        anthropic:
          base_url: <gateway_host()>/anthropic
          auth_command: cat <user.home>/.config/workshop/gateway-token
          models:
            default: <same opus/sonnet chain as configure_claude>
        openai:
          base_url: <gateway_host()>/openai/v1
          wire_api: responses
          auth_command: cat <user.home>/.config/workshop/gateway-token
          models:
            default: <CODEX_MODEL or databricks-gpt-5-5>
    ```

  - Reuse `gateway_host()` and the existing model-pick chains; fall back to
    `/serving-endpoints` base URLs when no gateway resolves, mirroring
    `configure_claude` / `configure_codex`.
- `users.py` `shell_env()` additions:
  - `TMUX_TMPDIR=$HOME/.cache/tmux` — **critical**: all attendees share one OS
    uid, and tmux's default socket dir is `/tmp/tmux-<uid>`; without this every
    attendee lands in the same tmux server.
  - `OMNIGENT_NO_UPDATE_CHECK=1` — the install is shared; never self-update
    (mirrors `DISABLE_AUTOUPDATER` for claude).

### 3. Session types — `content/agents.json` (catalog-only)

```json
{ "id": "omnigent",        "label": "Omnigent",          "command": "omnigent",        "requires": ["omnigent", "claude"], "order": 1 },
{ "id": "omnigent-claude", "label": "Omnigent · Claude", "command": "omnigent claude", "requires": ["omnigent", "claude"], "order": 2 },
{ "id": "omnigent-codex",  "label": "Omnigent · Codex",  "command": "omnigent codex",  "requires": ["omnigent", "codex"],  "order": 3 }
```

Existing `claude` / `codex` entries demoted to order 10/11 (fallbacks); `bash`
stays 99. Operators can re-promote bare modes mid-event via the existing
`AGENT_CATALOG_PATH` override without a deploy.

### 4. `app.yaml`

New env entries: `OMNIGENT_VERSION` (`"0.1.0rc2"`), optional `TMUX_STATIC_URL`.

### 5. Tests

`tests/test_omnigent_config.py` beside the existing suites:
- `configure_omnigent` writes valid YAML with the right base URLs, models, and
  `auth_command` path; token file is 0600.
- `update_tokens` refreshes the token file without rewriting YAML.
- Installer version-stamp logic triggers reinstall on pin change.
- Catalog gating: omnigent entries report `ready: false` until both readiness
  keys complete.

### 6. GA bump (next week)

One change: `OMNIGENT_VERSION` default → GA version. The sidecar version stamp
makes the redeploy reinstall; re-run the smoke checklist.

## Ordered tasks (de-risk first)

1. **Manual spike on the live app (before any code):** deploy current main to
   `workshop-terminal-dev`, open a bash attendee terminal, hand-install the
   static tmux + `uv tool install --python 3.12 omnigent==0.1.0rc2`, hand-write
   a `config.yaml`, run `omnigent` and `omnigent claude`. This validates every
   hard unknown in one session: Python 3.12 via uv on the Apps runtime, the
   static tmux binary, tmux-inside-xterm.js rendering/resize, local-server
   auto-spawn, and gateway `auth_command` auth.
2. **Memory check:** spawn 3–5 omnigent servers under distinct fake HOMEs,
   measure RSS → confirm the per-user-server model fits the container; if not,
   pivot to one shared server in accounts mode (documented fallback).
3. Bootstrap installers + `app.yaml` env.
4. `configure_omnigent` + token-file rotation + `shell_env()` additions;
   confirm a pre-written config skips omnigent's first-run wizard on rc2.
5. Catalog entries + readiness gating + tests.
6. End-to-end on `workshop-terminal-dev`: two attendee identities → distinct
   per-HOME server ports; token rotation survives >15 min inside a live
   `omnigent claude` session; PTY reattach/scrollback replay with tmux's
   alternate screen.
7. GA bump + redeploy + smoke.

## Risks

| Risk | Mitigation |
|---|---|
| Static tmux on the Apps container (no apt) | Task 1 spike; vendor the binary as a repo asset if the download is blocked |
| Per-user server memory (N × FastAPI + SQLite) | Task 2 measurement; named fallback: shared server, accounts mode |
| rc2 sharp edges (first-run wizard, provider routing) | Task 1 manual run before code; bare claude/codex fallback stays one click away |
| tmux-in-PTY rendering/resize in xterm.js | Task 1; verify SIGWINCH propagation and scrollback replay |
| First-launch latency (per-user server cold boot) | Acceptable for workshops; document "starting…"; optional pre-warm at session create later |
