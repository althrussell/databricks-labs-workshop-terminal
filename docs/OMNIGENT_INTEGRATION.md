# Design: Omnigent as the Primary Workshop Interface

**Status: proposed — not yet implemented.**
**Scope:** Workshop Terminal only. No changes to the Omnigent project itself.

## 1. Context

Omnigent is Databricks' agent meta-harness (public OSS launch imminent;
`omnigent==0.1.0rc2` is on PyPI today). It wraps coding agents — Claude Code,
Codex, and YAML-declared custom agents — in managed sessions: a TUI, a local
FastAPI server, session persistence, sub-agent orchestration (the bundled
`polly` orchestrator), and a web UI.

Today the Workshop Terminal launches **bare** `claude` / `codex` CLIs. This
design makes Omnigent the **default session type**: attendees land in the
`omnigent` TUI, with `omnigent claude` and `omnigent codex` as direct session
types, and the bare CLIs kept as demoted fallbacks.

### Goals

- Omnigent TUI is the first button an attendee sees and the one that works.
- Claude and Codex both usable through Omnigent, authenticated against the
  same Databricks model gateway the app already wires up.
- Zero new credential surface: reuse the existing vended-PAT → rotating-token
  chain unchanged.
- GA version bump is a single env change.
- Bare `claude` / `codex` remain one click away if the rc misbehaves mid-event.

### Non-goals

- No shared/central Omnigent server app. Sessions are ephemeral with the
  workshop container.
- No Omnigent web UI exposure to attendees (the TUI is the interface; the
  per-user web UI would need per-user ports through the Apps proxy — out of
  scope).
- No changes to the operator/admin API. Catalog reordering via the existing
  `AGENT_CATALOG_PATH` override is the operator escape hatch.

## 2. Architecture overview

```
Databricks App container (single uvicorn worker)
│
├─ FastAPI app (existing)            ── PTY sessions, auth, rotation, catalog
│
├─ shared prefix  DATA_ROOT/shared/
│   ├─ bin/   node, claude, codex, databricks, tmux*, omnigent*   (*new)
│   ├─ uv-tools/omnigent/            ── uv-managed venv, CPython 3.12*
│   └─ uv-python/                    ── uv-downloaded interpreters*
│
└─ per-attendee HOME  DATA_ROOT/users/<slug>/
    ├─ .claude/settings.json         ── existing (bare-claude fallback path)
    ├─ .codex/config.toml, .env      ── existing (bare-codex fallback path)
    ├─ .config/workshop/gateway-token   ── NEW: rotating token file (0600)
    ├─ .omnigent/config.yaml         ── NEW: gateway provider, written once
    ├─ .omnigent/local_server.pid    ── omnigent-managed (auto-spawned server)
    └─ .cache/tmux/                  ── NEW: per-user tmux socket dir
```

**Per-attendee servers, not shared.** Omnigent resolves all state via
`Path.home()`. An attendee's first `omnigent` invocation auto-spawns a local
server recorded in *their* `~/.omnigent/local_server.pid`. The port picker
bind-probes 6767 and falls back to an OS-assigned free port; discovery is
always via the pidfile, never an assumed port — so N attendees on one
container cannot collide. Auto-spawned servers run with
`OMNIGENT_LOCAL_SINGLE_USER=1` (loopback `local` identity) — no accounts mode,
no passwords, no auth machinery.

The trust model is unchanged from today: all attendees share one OS uid and
isolation is cooperative/HOME-based. A motivated attendee could already read
another's `~/.databrickscfg`; the omnigent loopback servers add nothing
qualitatively new.

## 3. Component design

### 3.1 Bootstrap installers — `server/bootstrap/install.py`

Two new install steps following the existing patterns (idempotent, shared
prefix, readiness keys, background pool).

**Pins** (top of file, beside `CODEX_VERSION`):

```python
OMNIGENT_VERSION = os.environ.get("OMNIGENT_VERSION", "0.1.0rc2")
TMUX_STATIC_URL = os.environ.get(
    "TMUX_STATIC_URL",
    "https://github.com/mjakob-gh/build-static-tmux/releases/download/v3.5a/tmux.linux-amd64.gz",
)
TMUX_STATIC_SHA256 = os.environ.get("TMUX_STATIC_SHA256", "<pin at implementation time>")
UV_INSTALLER_URL = os.environ.get("UV_INSTALLER_URL", "https://astral.sh/uv/install.sh")
```

**`_install_tmux()`** — omnigent's `claude` / `codex` wrappers hard-require
tmux (refuse to start without it) and the Apps runtime has no package manager.

1. If `shutil.which("tmux")` or `$PREFIX/bin/tmux` exists → complete.
2. `curl -fsSL $TMUX_STATIC_URL | gunzip > $PREFIX/bin/tmux` (download to a
   temp path first, verify sha256, then move into place; `chmod 755`).
3. Smoke: `tmux -V` exits 0.

The static musl build is fully self-contained (no terminfo dependency issues
under `TERM=xterm-256color`). Fallback if the GitHub release is unreachable:
vendor the gzipped binary under `assets/bin/` in this repo (decision deferred
to the de-risk spike; the URL path keeps the repo small).

**`_install_omnigent()`** — omnigent requires Python ≥3.12; the Apps runtime
Python must not be assumed. `uv` solves both: it downloads a standalone
CPython and manages an isolated tool venv, no root required.

1. Resolve `uv`: `shutil.which("uv")`, else
   `curl -fsSL $UV_INSTALLER_URL | env UV_INSTALL_DIR=$PREFIX/bin sh`.
2. Install with all uv state on the persistent volume:

   ```python
   env = _install_env()
   env.update({
       "UV_TOOL_DIR": os.path.join(prefix, "uv-tools"),
       "UV_TOOL_BIN_DIR": os.path.join(prefix, "bin"),
       "UV_PYTHON_INSTALL_DIR": os.path.join(prefix, "uv-python"),
   })
   subprocess.run(
       [uv, "tool", "install", "--force", "--python", "3.12",
        f"omnigent=={OMNIGENT_VERSION}"],
       env=env, timeout=600, ...
   )
   ```

   `uv tool install` drops the `omnigent` entry point into `UV_TOOL_BIN_DIR`
   (= the shared `bin/` every user already symlinks).
3. **Version stamp:** write `$PREFIX/omnigent.version` after success; on boot,
   if the stamp ≠ `OMNIGENT_VERSION`, reinstall (`--force` makes this safe).
   This is deliberately different from the claude installer's exists→done
   shortcut — it's what makes the GA bump a pure env change (§8).

**Readiness:** `status()["ready"]["omnigent"] = (omnigent complete AND tmux
complete)`. Both steps join the `run_in_background()` thread pool (they are
independent of node).

Failure isolation: if either step errors, only the omnigent catalog entries
stay disabled ("installing…" → operators see the error in `/api/setup-status`);
bare claude/codex are unaffected.

### 3.2 Credentials — `server/cli_config.py`

#### Token file (new)

`~/.config/workshop/gateway-token`, mode 0600, containing the current rotating
token. Written by `configure_omnigent()` at first session and by
`update_tokens()` on every rotation (a 1-line file write — the YAML config is
**never** rewritten on rotation).

#### `configure_omnigent(user, token)` (new, called from `configure_all`)

Writes `~/.omnigent/config.yaml`. Literal output (gateway case):

```yaml
# Generated by the workshop terminal — do not edit.
providers:
  databricks-gateway:
    kind: gateway
    default: true
    anthropic:
      base_url: https://<ws-id>.ai-gateway.cloud.databricks.com/anthropic
      auth_command: cat /app/python/source_code/data/users/<slug>/.config/workshop/gateway-token
      models:
        default: databricks-claude-opus-4-8     # _pick() over the same chain as configure_claude
    openai:
      base_url: https://<ws-id>.ai-gateway.cloud.databricks.com/openai/v1
      wire_api: responses
      auth_command: cat /app/python/source_code/data/users/<slug>/.config/workshop/gateway-token
      models:
        default: databricks-gpt-5-5             # CODEX_MODEL env or default
```

Schema notes (verified against omnigent `onboarding/provider_config.py`):

- `kind: gateway` entries carry per-family blocks (`anthropic:` / `openai:`)
  directly under the entry.
- Exactly one secret source per family; we use `auth_command` — a shell
  command that prints a bearer token, re-run by omnigent on a 15-minute
  refresh cadence (`auth_refresh_interval_ms=900000` default), which matches
  the app's 15-min token TTL / 10-min rotation exactly.
- `auth_command` uses the **absolute** token path (no `$HOME` expansion
  dependence).
- `default: true` makes this provider the default for **both** families it
  serves — bare `omnigent`, `omnigent claude`, and `omnigent codex` all route
  through it with no further selection.
- No `server:` key in the config → omnigent auto-spawns the per-user local
  server on first use.
- When `gateway_host()` resolves empty, fall back to
  `{databricks_host()}/serving-endpoints/anthropic` and
  `{databricks_host()}/serving-endpoints` — mirroring `configure_claude` /
  `configure_codex` exactly.
- Model defaults reuse the existing `_discover_serving_endpoints()` +
  `_pick()` chains verbatim — one source of truth for model selection.

`config.yaml` is written idempotently on every `configure_all` (same as the
claude/codex configs); its content is deterministic for a given deployment,
so rewrites are no-ops in practice. Attendee edits are intentionally
clobbered on reconnect (same policy as `settings.json` today).

#### `update_tokens(user, token)` (modified)

Add one branch: if `~/.config/workshop/gateway-token` exists, rewrite it
(0600). If missing, call `configure_omnigent(user, token)`. Symmetric with
the existing claude/codex branches.

#### Credential precedence — the decided rule

There are two auth paths into Claude Code and they coexist by design:

| Path | Mechanism | Used by |
|---|---|---|
| App-managed `~/.claude/settings.json` | `env.ANTHROPIC_AUTH_TOKEN` (static, file rewritten every 10 min) + `ANTHROPIC_BASE_URL` | bare `claude` sessions (today's behavior, unchanged) |
| Omnigent provider | terminal-env `ANTHROPIC_BASE_URL` + `apiKeyHelper` = the provider's `auth_command` (dynamic, re-minted ≤15 min) | `omnigent claude` sessions |

Verified omnigent behavior (`claude_native.py`): the wrapper injects
`ANTHROPIC_BASE_URL` and an `apiKeyHelper` into the **tmux terminal process
env** — it does not write or modify `~/.claude/settings.json`. Claude Code
gives an `ANTHROPIC_AUTH_TOKEN` from settings.json precedence over
`apiKeyHelper`, so inside an omnigent session the effective behavior is
**parity with bare claude** (settings token wins; the apiKeyHelper is a
dormant redundant path that becomes load-bearing only if the settings token
is absent).

**Rule: the app's `update_tokens()` remains the single rotation authority for
both paths** (settings.json token + gateway-token file). Neither path ever
fights the other because omnigent never touches settings.json and the app
never touches `~/.omnigent`. The >15-minute live-session test (§7) validates
both paths under rotation in one run.

Codex is simpler: omnigent's codex wrapper builds its provider config from the
omnigent provider entry (auth_command-based) independently of
`~/.codex/config.toml`; bare codex keeps using `.codex/.env`. Same
single-rotation-authority rule applies.

### 3.3 Per-user environment — `server/users.py`

`shell_env()` gains two entries:

```python
# All attendees share one OS uid; tmux's default socket dir is /tmp/tmux-<uid>,
# which would merge every attendee into ONE tmux server (cross-user session
# visibility + key collisions). Per-user socket dir restores isolation.
"TMUX_TMPDIR": os.path.join(self.home, ".cache", "tmux"),
# The omnigent install is shared across attendees — never self-update
# (mirrors DISABLE_AUTOUPDATER for claude).
"OMNIGENT_NO_UPDATE_CHECK": "1",
```

`bootstrap_home()` adds `.cache/tmux` and `.config/workshop` to the directory
list. The existing `_link_shared_binaries()` already picks up `omnigent` and
`tmux` from the shared bin — no change.

### 3.4 Session types — `content/agents.json` (catalog only)

```json
[
  { "id": "omnigent",        "label": "Omnigent",          "description": "Databricks' agent meta-harness — managed sessions over Claude and Codex.", "icon": "sparkles", "command": "omnigent",        "requires": ["omnigent"],          "order": 1 },
  { "id": "omnigent-claude", "label": "Omnigent · Claude", "description": "Claude Code in a managed Omnigent session.",                               "icon": "sparkles", "command": "omnigent claude", "requires": ["omnigent", "claude"], "order": 2 },
  { "id": "omnigent-codex",  "label": "Omnigent · Codex",  "description": "Codex in a managed Omnigent session.",                                     "icon": "bot",      "command": "omnigent codex",  "requires": ["omnigent", "codex"],  "order": 3 },
  { "id": "claude",          "...": "existing entry, order → 10" },
  { "id": "codex",           "...": "existing entry, order → 11" },
  { "id": "bash",            "...": "existing entry, order 99 unchanged" }
]
```

`requires` semantics:

- `omnigent-claude` / `omnigent-codex` need the **native CLI binary** present
  (omnigent wraps it in tmux) → require both keys.
- Bare `omnigent` derives its harness from configured credentials (verified
  in omnigent `cli.py`: anthropic creds → the `polly` orchestrator on the
  claude-sdk harness, which is the bundled SDK — **no** `claude` binary
  needed) → requires only `omnigent`.

No backend changes: `launch_command()` already wraps multi-word commands in
`bash -c "<cmd>; exec /bin/bash"`. No frontend changes: `LaunchBar` renders
the catalog as ordered, unknown icons fall back to the terminal icon, and
readiness gating per `requires` key already works.

### 3.5 `app.yaml`

```yaml
  # --- Omnigent (agent meta-harness) ---
  - name: OMNIGENT_VERSION
    value: "0.1.0rc2"
  - name: TMUX_STATIC_URL
    value: ""
```

(`TMUX_STATIC_SHA256` pinned in code; env override exists for emergencies.)

## 4. Process lifecycle

New long-lived per-attendee processes appear with this design:

| Process | Started by | Survives PTY close? |
|---|---|---|
| omnigent local server (uvicorn) | first `omnigent` invocation | **yes** (daemonized, pidfile) |
| tmux server + session | `omnigent claude` / `codex` | **yes** (that's tmux's job) |
| claude / codex inside tmux | omnigent wrapper | yes, inside tmux |

Policy:

- **Surviving the PTY is a feature, not a leak**: an attendee whose websocket
  drops (or who closes the tab) reconnects, launches the same session type,
  and omnigent **reattaches** to the existing tmux session and running server
  — work in progress survives. This is strictly better than today's bare
  CLIs, which die with the PTY.
- **The app's idle reaper (`SESSION_IDLE_TIMEOUT_SECONDS`) is unchanged**: it
  reaps the *PTY*, not the attendee's background processes. Orphaned omnigent
  servers idle at ~zero CPU; memory is the constraint (§6, de-risk task 2).
- **Cleanup**: none in v1 beyond container restart (Control Tower redeploys
  reset everything — `DATA_ROOT` persistence notwithstanding, processes die
  with the container). If the memory measurement demands it, v1.1 adds a
  sweep in the existing reaper: for each user idle > N hours, `tmux -S
  <their socket> kill-server` + `omnigent server stop`-equivalent
  (kill pidfile PID). Explicitly out of v1 scope.

## 5. Failure modes

| Failure | Behavior |
|---|---|
| tmux/omnigent download blocked at boot | `omnigent*` catalog entries stay "installing…" with the error in `/api/setup-status`; bare claude/codex unaffected. Operators see it immediately. |
| omnigent rc bug mid-workshop | Operator re-promotes bare entries via `AGENT_CATALOG_PATH` content-pack override — no redeploy. |
| Gateway token expired in token file | `auth_command` is re-run per refresh; next mint reads the rotated file. A single failed request window (<rotation period) is possible, same as today. |
| Attendee deletes `~/.omnigent/config.yaml` | Next session's `configure_all` rewrites it (omnigent would otherwise drop into its first-run wizard — annoying, not dangerous). |
| Port exhaustion / pidfile corruption | Omnigent's picker falls back to OS-assigned ports; a corrupt pidfile means one orphaned server and a fresh spawn — degraded, not broken. |
| Two PTYs, same user, same time | Both resolve the same pidfile → share one local server. Supported (it's omnigent's normal multi-client model). |

## 6. Memory budget (gate for this design)

Per attendee at steady state: one uvicorn server (FastAPI + SQLite) + one
tmux server + the harness process. The de-risk spike measures real RSS; the
go/no-go line is **30 concurrent attendees fitting the app container with
≥25% headroom** (matching `MAX_SESSIONS_GLOBAL=30`). If it doesn't fit, the
fallback design is one shared `omnigent server` in accounts mode +
per-attendee `omnigent login` — significantly more auth machinery, which is
exactly why measurement comes before code.

## 7. Test plan

**De-risk spike (before any code, on `workshop-terminal-dev`):**

1. Bash attendee terminal → hand-install static tmux + uv + omnigent rc2
   exactly per §3.1 commands.
2. Hand-write §3.2's `config.yaml` + token file → `omnigent` (TUI renders in
   xterm.js? wizard skipped?), `omnigent claude` (tmux alternate screen,
   resize via SIGWINCH, gateway round-trip).
3. RSS measurement per §6.

**Unit tests (`tests/test_omnigent_config.py`):**

- `configure_omnigent` writes parseable YAML; correct base URLs (gateway and
  serving-endpoints fallback); `auth_command` carries the absolute per-user
  path; token file is 0600.
- `update_tokens` rewrites the token file without touching `config.yaml`
  (mtime assertion); creates both via `configure_omnigent` when absent.
- Installer: version stamp mismatch triggers reinstall; match short-circuits.
- Catalog: `omnigent-*` entries report `ready: false` until both readiness
  keys complete; `omnigent` needs only its own key.

**Live E2E (deployed dev app):**

- Two attendee identities → distinct pidfiles/ports; sessions don't cross.
- One `omnigent claude` session held >15 min with prompts before/after a
  rotation boundary — validates **both** credential paths of §3.2 under
  rotation.
- Kill the PTY (idle reaper), reconnect, relaunch → reattach to the same
  tmux session with scrollback intact.
- Bare `claude` still works (fallback regression).

## 8. Rollout

1. Land this design; implement behind the catalog (omnigent entries simply
   appear when ready).
2. Deploy to `workshop-terminal-dev`; run the E2E list.
3. **GA bump (next week):** change `OMNIGENT_VERSION` env → GA version;
   redeploy. The version stamp forces the reinstall; re-run the E2E smoke.
4. First real event runs with bare claude/codex still in the catalog at
   order 10/11. Remove them (or not) based on observed stability.

## 9. Open questions

- Static tmux availability/compat on the Apps runtime image — **answered by
  spike task 1** (vendor-in-repo is the fallback).
- Whether rc2's first-run wizard is fully bypassed by a pre-written
  `config.yaml` — spike task 2; if not, pin the exact missing key with the
  omnigent team before GA.
- Memory at 30 attendees — spike task 3, gates the whole single-container
  design (§6).
