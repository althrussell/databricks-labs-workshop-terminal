# AI Dev Day (Wed 4 Nov 2026, 150 seats) — WT + Control Tower pre-event audit

Audited against the **released event build `v2026.11.01.3` (= `origin/main`, `6526d94`)**
and Control Tower `main` (`72e878a`). Test suite on the release build: 4210 passed, 79 skipped.
All `file:line` references below are to `origin/main` unless marked `[CT]`.

> **Process warning.** The local checkout is on `codex/wt-r08-project-continuity`, which is
> **97 commits behind `origin/main`** and 12 ahead. None of the week's work on that branch is
> in the release, and `main` has its own `projects.py` / `build_restore.py`. Do not merge R08
> before Wednesday without a rebase and a full qualification run; treat it as post-event.
>
> **Status (branch `fix/event-hardening`, off `origin/main`).** W1–W4 and the should-do items
> below are implemented and tested on that branch; CT items are being handled by the CT team
> and are out of scope here. The `[FIXED]` tags in §1 and §3 mark what landed.
>
> **Correction to P0-4.** The released build pins `databricks-sdk 0.151.0`, whose `Refreshable`
> token source refreshes **asynchronously ~20 min before expiry** by default. The audit's
> "expired bearer on disk for ~260 s every hour" window assumed the older blocking
> ≤40 s rule and **does not occur on the normal path**. What remained real — and is now fixed —
> is the token wipe + red banner + CT alert on any transient validation failure, the wipe on
> near-expiry if the SDK's refresh had been failing, and `CLAUDE_CODE_MAX_RETRIES=0`.
> P0-4 is therefore a P1, not a P0.

---

## 1. Verdict

The terminal transport, credential hygiene, supply-chain pinning, entitlements and CT provisioning
core are solid and well-tested. The event risk is concentrated in **six operational decisions on the
CT side** and **a handful of WT code paths that only bite at 150-seat scale or across an 8-hour day**:

| Rank | Finding | Owner | Effort |
|---|---|---|---|
| **P0-1** | CT still pins WT **`v2026.10.23.2`**; event release not staged/promoted; toolchain mirror staged for the old ref; promote does not re-stage the mirror; preflight does not check coherence | CT ops | 1 h + canary |
| **P0-2** | Auto-hibernate + **60 min activation lead** cannot wake 300 apps at 150 seats (~90–110 min measured path) | CT ops | form setting |
| **P0-3** | `WORKSHOP_MODEL_POLICY_REQUIRED=true` + CT pins `claude-opus-5-5` / `gpt-6-1-sol` — if the live Model Pool lacks any pinned/chain model, **every seat is fail-closed at launch** | CT ops | 15 min verify |
| **P1 (was P0-4) [FIXED]** | Any transient 429/5xx/timeout on `current-user/me` wiped the bearer + red banner + CT alert; near-expiry also wiped; `CLAUDE_CODE_MAX_RETRIES=0` ended the turn on the first blip. (See correction above: the hourly expired-token window did not occur on SDK 0.151.) | WT code | done |
| **P1-1 [FIXED]** | Omnigent host **`killpg`'d 300 s after the attendee's OBO goes stale** (lid closed > 1 h) — in-flight Omni work lost. Default now 7200 s. | WT env | done |
| **P1-2 [FIXED]** | Boot resilience at 150 simultaneous cold boots: `skills`/`apx` not retryable → **permanent `/readyz` 503 → seat refused**; downloads had no jitter, 403 fatal, `Retry-After` ignored; skills clone timeout escaped retry; Omnigent PyPI wheels (163) bypass mirror with no retry. Now: 5 jittered attempts, 403/408 transient, `Retry-After` honoured, clone timeout retried, uv concurrency 8 + 3 attempts, skills/apx retryable, bounded background self-retry. | WT code | done |
| **P1-3** | Cost controls: `$100`/attendee **suspend** includes model tokens (removes attendee mid-session); form default gateway budget **`$0.10`**; Opus-5.5 driver + `CODEX_REASONING_EFFORT=high` for a free event; known per-seat token-rate 429 (F-020) | CT ops | form settings |
| **P1-4 [PARTLY FIXED]** | Orphaned agent dev servers (vite / `apx dev` / `uvicorn --reload`) after natural agent exit — **fixed** (process group swept after reap; exit notice names `/resume` / `codex resume`). Container-memory gauge + `NODE_OPTIONS` **not done** (post-event). | WT code | partial |
| **P1-5 [FIXED]** | **Ctrl+C with a selection sends SIGINT** (kills the agent turn) — no xterm key handler; the #1 attendee complaint on Windows/Linux laptops | WT frontend | done |
| **P1-6** | Capture journal (Workspace Files) is a **fail-closed dependency for the whole attendee UI** (503 "temporarily unavailable" on every request), sticky on validation faults, and only a *soft* `/readyz` check | WT code | guard + readyz |
| **P1-7** | No "admit anyway" override for a refused seat; a used seat cannot be redeployed in place → **provision ~10 % spare seats** | CT ops | plan |
| **P1-8** | CT `#285` (Lakebase pool / transaction release) not live-validated; CT default executor (~8 threads) starves console during provisioning/activation | CT ops/code | smoke + rollback tag |

---

## 2. Findings in detail

### 2.1 Control Tower — operational (decide before provisioning)

**P0-1 Release / mirror pin coherence.**
`[CT] app.yml:179`, `databricks.yml:475,658` pin `v2026.10.23.2` (Omnigent 0.15.0, Claude 2.1.296, no browser runtime, 19 mirror artifacts). The DB fleet default can override this, but `promote_fleet_default` (`[CT] api/wt_releases.py:417-489`) does not re-stage the toolchain mirror and `preflight.py` has no mirror-coverage check. The event build (`v2026.11.01.3`: Omnigent 0.19.1, Claude 2.1.310, Codex 0.164.0, Node 24.22.1, Databricks CLI 1.22.1, skills v0.2.34, 49 artifacts incl. chrome-headless-shell + 30 `.deb` from launchpad.net which 503'd on 30 Oct) is "released, not staged". Every EQL/persona-E2E fix (Omnigent card death, Codex trust, plan gate, attach-image, Omni OTel, browser) lives only in the new release.
*Fix:* Settings → WT releases: stage + verify + **promote `v2026.11.01.3`**. Settings → Toolchain mirror: **Stage → Verify**, expect `complete=true`, 49 artifacts. Canary 2 seats; confirm `/readyz` → `supply_chain.toolchain_mirror_served == artifact_count` and `release_pins` green. Post-event: preflight check `mirror.wt_ref == run.wt_git_sha && complete`.

**P0-2 Hibernate/activate at 150 seats.**
`[CT] new.tsx:318-321` lead 60 min; `_config.py:196` `activation_app_concurrency=10`; activation uses blocking `start_and_wait` (`real.py:10567`) on the ~8-thread default executor (F-3), group-serialised per workspace (`activation.py:485-498`), then `/readyz` admission per WT. Only rehearsed at 10 seats / 20 apps. CT stops automatic activation retries at attendee start (`operator-runbook.md:105`).
*Fix (safe):* **untick Auto-hibernate**; provision ≥ 3 h before doors and keep the fleet warm (~$15/seat idle). Alternative (unrehearsed): lead ≥ 150 min + `CONTROL_TOWER_ACTIVATION_APP_CONCURRENCY=25` + larger executor.

**P0-3 Model Pool ⊇ pins + chains.**
CT pins `ANTHROPIC_MODEL=system.ai.claude-opus-5-5`, `CODEX_MODEL=system.ai.gpt-6-1-sol`, `INSIGHT_SUMMARY_MODEL=claude-opus-5-5` (`[CT] provisioning.py:323,328-369`). Bootstrap pool (`model_policy.py:159-170`) does **not** include `claude-opus-5-5`, `gpt-6-1-sol`, `gpt-5-6-sol`, `gpt-5-3-codex`, `gpt-5-4-mini`; live pool is DB state. WT chains (`server/models.py:378-385,522-528`): driver `claude-opus-5-5→opus-5→4-8→4-6→sonnet…`, sonnet `claude-sonnet-5-5`, haiku `claude-haiku-5-5`, codex `gpt-5-6-terra→gpt-6-1-sol→gpt-5-6-sol→gpt-5-3-codex`, wizard `gpt-6-1-sol→gpt-5-4-mini→gpt-5-6-luna→claude-haiku-4-5→gpt-oss-120b`. `/readyz` deliberately does not verify against the live catalogue (`readiness.py:528-535`).
*Fix:* `databricks api get /api/2.1/unity-catalog/model-services` on one event workspace; confirm Model Pool enables every pin + at least one Sonnet, Haiku and Codex fallback; run static sync, confirm `static_policy_is_verified`. Decide driver deliberately (see P1-3).

**P1-3 Cost posture for a free 150-seat day.**
`[CT] app.yml:111-134` `$100`/attendee, breach action **`suspend`** (removes attendee from workspace), and "attendee spend" includes model-serving tokens (`repositories.py:4331-4336`) with billing lag; `new.tsx:324` `gatewayPerUserUsd: "0.10"` is the canary value; `CODEX_REASONING_EFFORT=high` + Opus 5.5 driver = frontier posture; F-020 per-seat token-rate 429 on `gpt-6-1-sol` at 0.5–1.7 M tokens/min.
*Fix:* `CONTROL_TOWER_COST_UNIT_BREACH_ACTION=none` (alert only) or tick "No spend cap", keep run-level LOCK as backstop; gateway budget **off** or ≥ $25–50/user; `codex_reasoning_effort=medium`; consider `claude-sonnet-5-5` as driver with Opus reachable via `/model`. Confirm FMAPI capacity for 150 seats (EQL: 100 seats ≈ 500 req/min, 50 M tok/min).

**P1-7 Spare seats.** `[CT] provisioning.py:4575-4583` — in package mode with closeout required, `READYZ_GATE=false` is a no-op; no `admit anyway`; used seats can't be redeployed in place (`closeout.py:914-924`). *Fix:* provision **165 for 150**; TA rule: broken used seat ⇒ move attendee to a spare.

**P1-8 CT build.** `#285` (`395d5ac`) landed after the 1 Nov validation on `7d7dd64`. *Fix:* deploy ≥ `72e878a`, smoke on a 2-seat run, keep `7d7dd64` as rollback. The executor starvation (F-3) is unchanged; P0-2's keep-warm avoids the worst of it.

**P2 CT items.** `insight_capture` promoted to a blocking admission check when closeout is required (`terminal_contract.py:233-272`) — a capture-storage hiccup refuses a healthy seat; decide whether the AE brief outranks a seat. `labs-us` target has no mirror path and no email secrets (`databricks.yml:559-710`) — irrelevant if on `labs`. `attendee_app_models` lane (`#282`, new) lists apps in every live seat every tick → ~150 `apps.list`/min on the shared 5 req/s Apps budget; watch 429s during activation. Set `event_end_at` correctly (wrap-up broadcasts key off it) and `termination_at ≥ end + 30 min`. Roster must carry real emails before `not_before 07:00`.

### 2.2 WT — authentication and token lifecycle

**P0-4 Expiry-blind SP bearer rotation** (`server/credentials.py:48-51,398-403,429-435,612-650`).
`_tick` probes every 300 s; `app_identity_bearer()` → SDK `config.authenticate()` which refreshes only when ≤ 40 s remain (`databricks-sdk 0.151.0`). Nothing schedules a probe relative to `_expires_at`. Per hour, per instance: ~80 % chance the probe lands with 61–300 s left → token adopted, **expires before next probe** → `gateway-token` and `[DEFAULT]` hold an expired bearer for `300 − remaining` s (mean ~2 min). Claude (`apiKeyHelper`, TTL 240 s, `CLAUDE_CODE_MAX_RETRIES=0`), Codex (`auth.command=cat`, 1 retry) and bare `databricks` all read that file → **401, turn aborted**, re-running the helper returns the same expired file. ~7 %/h the probe lands with 41–60 s left → `too close to expiry` → `UNHEALTHY` + `clear=True` → token wiped, CT alert, red banner "Coding agents may fail to authenticate", 503 on launch until next tick. `_effective_state_locked` (`:626-629`) also reports UNHEALTHY to `/api/agents` for the last 60 s of every token. Over 150 × 8 h ≈ 1,000 expired-token windows and ~80 flaps.
*Fix:*
1. Make `_tick` expiry-aware: probe when `now - _last_probe ≥ PROBE_INTERVAL` **or** `_expires_at - now ≤ PROBE_INTERVAL + OAUTH_EXPIRY_MARGIN`; `HEARTBEAT=10`.
2. Force an early mint when remaining < ~6 min (re-run `config.init_auth()` / fresh `ClientCredentials` — the secret is still on the `Config` object) so a new token is on disk ≥ 5 min before expiry.
3. In the near-expiry branch do **not** `clear=True`; keep serving the still-valid token, log a warning.
4. Transient probe failures (`request_error`, 429, 5xx — `:420-427,564-567`): retry 2–3× with jittered backoff before declaring UNHEALTHY; only clear on a definitive 401/403 identity mismatch. Add ±30 s jitter to `PROBE_INTERVAL`.
5. Unit test: fake clock, 3600 s `exp`, assert the on-disk token is never expired across 3 h.
6. Set `CLAUDE_CODE_MAX_RETRIES` to `3` (`cli_config.py:394-398`) — the budget-403 case it protects against is already surfaced by the frontend banner (`gatewayLimit.ts`), and a retry on a hard 403 costs no tokens.
*Verify in canary:* tail `~/.config/workshop/gateway-token` `exp` vs wall clock for 2 h on one seat.

**P1-1 Omnigent host stand-down** (`server/omnigent_remote.py:836-866,933-957`; `config.py:436-442` default 300 s). After the OBO mirror is stale for `OMNIGENT_STALE_GRACE_S`, the host **process group** is SIGTERM→SIGKILLed; runners under it die; the thread parks in `waiting_for_token` and restarts on the next OBO write. The 10-min keepalive iframe (`App.tsx:387-389,1136-1144`) and focus/visibility nudges keep OBO fresh **only while the tab runs JS** — a closed lid > 1 h loses in-flight Omni work (recovery is automatic on return). `config.py:409-416` sets runner idle to 0 precisely to survive lunch; the supervisor defeats it.
*Fix (event):* CT `env_overrides` `OMNIGENT_STALE_GRACE_S=7200`. *Fix (code):* on stale, don't kill — set `waiting_for_token`, refuse new launches (already done via `launch_block`), leave the process alone. Also: no restart cap and backoff floor ≈ 0 with stable-reset at 30 s (`:292-293,495-503`) → a host dropped every ~31 s respawns a Python+mlflow tree forever; add a cap (20/10 min → `crash_loop`), `OMNIGENT_HOST_STABLE_RUNTIME_S=300`, floor 2 s. Host reports `error` during install/preparation (`:731-746`) — add `installing`.

**P2 auth items.** Admin/TA can self-bind an **unbound** seat when SCIM fails (`auth.py:177-180,229-247`; `get_groups` collapses unknown→`set()`); no unbind endpoint — moot while CT always sets `WORKSHOP_ATTENDEE_EMAIL`, but add `DELETE /api/admin/attendee-binding` and fail closed on unknown groups post-event. No negative cache in `get_groups` (`auth.py:177-180`) → SCIM on every `/api/config` when SCIM is failing. TAs get a dead-end 403 on bound seats (`auth.py:248-256`; `App.tsx:955-964`) — admin API works, attendee UI does not; wording should say "use the Operator panel or the attendee's browser". Raw `CredentialError` text still reaches the attendee 503 in `create_session` (`main.py:1764-1770`). `databricks-me` installed unconditionally (`user_content.py:661-681`) and misreports "session expired" when OBO is off — CT sets `ENABLE_OBO=true`, so event-safe; gate the helper + CLAUDE.md §"two identities" on `config.obo_enabled()` later.

### 2.3 WT — boot, toolchain, readiness (150 simultaneous cold boots)

**P1-2 Boot resilience.**
- `retry_failed` (`server/bootstrap/install.py:2086-2111`) derives names from `STEPS_BY_BINARY` (`:438-442`) which never yields `skills` or `apx`; the `("apx", …)` entry is dead code. `skills: degraded` is terminal (`:1985-1992`), is a **hard `installers` requirement** (`readiness.py:414,710`), and `supply_chain` requires apx/bun/uv proof (`readiness.py:443-445,727`; `install.py:699-704`). One GitHub 403 on the skills clone or the bun zip ⇒ permanent `/readyz` 503 ⇒ CT refuses the seat, while Claude/Codex/Omnigent are perfectly usable.
- `_download_https` (`artifacts.py:355-377`): 3 attempts, 2 s/4 s, **no jitter**, 403 fatal (GitHub's abuse response), `Retry-After` ignored, no total deadline. Checksum mismatch not retried (correct). 150 instances retry in lockstep.
- Skills clone (`install.py:1838-1862`): retry only on non-zero rc; `TimeoutExpired` escapes to `degraded`; no `GIT_TERMINAL_PROMPT=0` / low-speed abort; SHA fallback unretried.
- Omnigent wheels: `uv pip install --require-hashes` (`:1532-1547`) from PyPI, 163 wheels ~250 MB, no outer retry, no `UV_CONCURRENT_DOWNLOADS` cap (uv default 50 × 150 instances through a few NAT IPs); comment `:1574-1575` admits the mirror doesn't cover it; CT docstring claims it does.
- `_set()` (`:353-364`) emits telemetry but no `logger.error`; node/claude/databricks/codex/apx failures are invisible in the Apps log UI.
- No `shutil.disk_usage` preflight; ~3–3.5 GB install + attendee `node_modules` on an undocumented ephemeral disk.
*Fix (in order):* (1) add `skills` and `apx` to `retry_failed` and a background self-retry (every 2–5 min with jitter while any step is `error/degraded`); consider making vendored-fallback skills **soft** for admission. (2) `_download_https`: treat 403/408 as transient, honour `Retry-After`, full jitter base ≥ 5 s cap 60 s, 5 attempts, total deadline. (3) Catch `TimeoutExpired` inside the clone loop; `-c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30`; `GIT_TERMINAL_PROMPT=0`. (4) `UV_CONCURRENT_DOWNLOADS=8`, `UV_HTTP_TIMEOUT=60`, wrap `uv pip install` in 2–3 jittered retries. (5) `logger.error` in `_set` on error/degraded. (6) Log `disk_usage(DATA_ROOT)` at boot and in `/api/setup-status`.

**P1 `/readyz` cost** (`main.py:2062-2067` → `install.status(include_proof=True)` `:554-559,569-714`). Every call re-hashes the Omnigent venv (~1 GB) + codex trees + binaries and spawns ~8 `--version` subprocesses (18–22 s measured) — CT polls every 20 s during the boot it is slowing down, on 2 vCPUs. *Fix:* return 503 fast while `installing`; cache the proof for 60–120 s or compute once at boot end and on retry.

**P1-6 Capture journal fail-closed** (`main.py:312-318`; `capture.py:21-29,60-95`; `capture_journal.py:168-176,550-567,741-851,1016-1017`; `event_emitter.py:178-197`; `closeout_http.py:53-89`; `users.py:704-706`). Runs on every non-dev boot regardless of `WORKSHOP_INSIGHT_CAPTURE`; needs `WORKSHOP_RUN_ID`, `WORKSHOP_UNIT_ID`, `DATABRICKS_WORKSPACE_ID` (Apps-runtime injected), `WORKSHOP_ATTENDEE_EMAIL` (all present in CT package mode) **and** live Workspace Files I/O as the app SP at boot. On failure: `/api/config` 503, `user_manager.get()` 503 (~23 endpoints), every mutating `/api/*` 503, session creation refused — "Workshop is temporarily unavailable". Validation faults (e.g. a stray object in `/Workspace/Users/<attendee>/projects/.workshop-capture/<digest>`) are **sticky for the process**. `/readyz` reports this only as soft `insight_capture` (`readiness.py:918`), so CT can admit a seat where every request 503s. *Fix:* make capture-held a **hard, non-attendee-dependent** `/readyz` check (or make it degrade to local-journal + warning instead of 503 on attendee paths); un-stick validation faults with a periodic re-init; surface the held reason in `/api/setup-status`.

**P2 boot items.** No-mirror Claude path now runs the pinned, checksum-verified installer (`:890-892,1053-1065`) but never uses the manifest `claude_binary` URL and has no outer retry (`:1033-1040`). Mirror `fetch` retries permission errors only; 429/5xx from Files API → silent internet fallback (`mirror.py:95-127`). `ToolchainMirror` classifies errors by `"404"/"403"` substrings on `str(error)` — two manifest hashes contain those digits (`mirror.py:53-66`). `initialize_app_identity()` SDK construction blocks lifespan before the port binds (`main.py:360`; SDK default retry budget ~300 s). Doc drift: `docs/control-tower-implementation.md:347-348` says Claude 2.1.283 / Codex 0.157.1; `app.yaml:191` says wizard head is `gpt-5-4-mini` (code: `gpt-6-1-sol`).

### 2.4 WT — runtime, sessions, frontend

**P1-4 Orphans and memory** (`server/sessions.py:535,583-591,789-805`; `operational.py:59-66`). `terminate()` now `killpg`s — good — but only `if exit_code is None and exit_signal is None` (`:583`); when the agent exits **naturally** (common: Claude `/exit`, Codex quit, crash), surviving group members (`apx dev`, vite, `uvicorn --reload`, `databricks sync`) are **not** signalled and are only drained at closeout (`:669-717`). No `TIOCSCTTY` (slave inherited, not opened → no kernel SIGHUP). The new `supervisor.py` is a subreaper that reaps zombies but never kills live orphans. Memory: only the server's own RSS is read; nothing reads cgroup `memory.current/max`; no `NODE_OPTIONS`. In a 6 GB container, Claude (0.3–1.5 GB) + `bun install` + `vite build` (1–2 GB) + orphaned dev servers ⇒ OOM-killer takes `node` ⇒ attendee sees "Session stopped by signal 9", relaunch starts a blank conversation (`terminalExit.ts:8-25`; `agents.py:161` no `--continue`).
*Fix:* killpg after PTY EOF too (drop the `:583` guard for the group signal, keep it for the leader); set `TIOCSCTTY` in `preexec_fn`; 5-min reaper sweep that kills processes whose session id belongs to a dead WT session; cgroup memory gauge in `build_snapshot()` + attendee toast at > 85 %; `NODE_OPTIONS=--max-old-space-size=1536` in `shell_env()`; exit notice: "Type `/resume` (Claude) or `codex resume` to continue".

**P1-5 Ctrl+C copies nothing** (`frontend/src/components/TerminalView.tsx:38-57,148-153`). No `attachCustomKeyEventHandler`; selecting a URL and pressing Ctrl+C sends `\x03` → cancels the turn (twice → exits the CLI). Cmd+C on macOS works. *Fix:* custom handler — if `ctrlKey && key==='c' && term.hasSelection()` → `navigator.clipboard.writeText(term.getSelection())`, `clearSelection()`, return `false`. Add a "Copy" toolbar affordance for projector demos. Rebuild `static/` and cut a patch release.

**P2 runtime items.** Post-commit `databricks sync` (`user_content.py:1025-1111`): `flock` covers only writer registration, not the sync; no `--exclude node_modules`; `.sync.log` appended forever — serialise with a lock + pending flag, add excludes, truncate > 1 MiB. Subscriber queue is 128 **messages** (64 KiB each) → overflow flapping on sustained bursts (`bun install` on wide terminals) with `term.clear()` + 2 MiB replay each cycle — coalesce adjacent output frames / byte-bound the queue. WS reconnect gives up after 12 attempts (~85–170 s) (`TerminalView.tsx:66,127-137`) — shorter than an Apps restart; retry indefinitely at the 10 s cap and on `visibilitychange`. `/api/agents` polled every 5 s forever (`App.tsx:385`) — stop when all agents ready. Font metrics measured before JetBrains Mono loads — `await document.fonts.ready` before `term.open()`. Latent `UnboundLocalError` in `create_session` (`main.py:1654,1784,1848`: `projects` bound inside `try`, used in `except`). Boot screen has no timeout. `supervisor.py` caps restarts at 5/600 s then exits 1 — a port squatter takes the terminal down after ~30 s instead of instantly; acceptable.

**Hard invariant to brief, not fix:** `MAX_SESSIONS_*=1` (`sessions.py:492-498`; `topology.py:23-28`) — **one PTY agent at a time**; switching Claude↔Codex kills the conversation; no side shell (use Claude `!cmd`). Omnigent is out-of-band (remote host) and can run alongside the one PTY agent.

### 2.5 WT — agent configuration, prompts, skills, wizard

Fixed on the release build (no action): Claude and Codex folder-trust pre-accepted before spawn (`cli_config.py:538-545,806-825`; `sessions.py:521-529`); `MCP_TOOL_TIMEOUT=3600000` (`cli_config.py:412`); `refresh-databricks-skills` / `workshop-agent-bricks-cli` excluded (`user_content.py:732-736`); composed CLAUDE.md ≈ 1.7k words, contract no longer duplicated in project memory; Omnigent 0.19.1 pinned consistently across WT and `deploy/omnigent-app`.

**P1 (part of P0-4) `CLAUDE_CODE_MAX_RETRIES=0`** — see §2.2.

**P2 agent items.**
- Starter prompt is typed into the PTY immediately after create (`App.tsx:89-101`; `main.py:1875-1900`) with no CLI-prompt-ready detection; opener via argv is safe, typed text may race the banner.
- apx MCP `feedback_prepare/submit` (creates **public GitHub issues**) is reachable from Claude — deny list only covers `databricks_apps_logs` (`cli_config.py:448-450,612`); Codex is whitelisted (`:797-799`). Add `mcp__apx__feedback_*` to the deny list.
- No destructive-action guard in instructions (no "never DROP/`apps delete`/`rm -rf` things you didn't create this session; never touch `workshop_demo`"). With `bypassPermissions` + SP `CAN_MANAGE`, add a 4-bullet guard to `workshop_contract.md`.
- Dev-server guidance incomplete: `apx/SKILL.md:63,107` says start the dev server before testing UI; nothing says the attendee's browser can't reach `localhost` → run `apx dev stop` before deploying, or use `workshop-browser`. Ties to P1-4.
- `DEFAULT_PERSONA="business"` (`user_content.py:179`), no env override — for a Dev Day the agent will avoid component names until the attendee flips it. Add `WORKSHOP_DEFAULT_PERSONA`.
- Default catalogue leads with Omnigent (`content/agents.json`), while the wizard's ideation path **prefers Claude** (`frontend/src/ideation.ts:32-40`) because Omni is refused while installing or when OBO is stale — two conversation planes on day one. Either reorder so the hero and wizard agree, or accept and brief. The "demote Omnigent fleet-wide" lever is **per-instance, in-memory** (`agents.py:25,92-108`); `scripts/ct_fleet.py` has no demote action — add one (loop `POST /api/admin/omnigent-tier`) or use CT `env_overrides OMNIGENT_ENABLED=false` + restart as the real fleet lever.
- Wizard head `gpt-6-1-sol` under a 12 s end-to-end deadline (`wizard_llm.py:25,222-237`) → at 09:00 p95 will fall back to the curated grid ("Suggestions were unavailable"). Pin `WORKSHOP_WIZARD_MODEL=gpt-5-6-luna` or haiku for the event. Note CT writes `WORKSHOP_ONBOARDING_WIZARD=false` per run from the form default (`[CT] simple-event.ts:76`) — confirm which you want.
- Entitlements: catalog `PATCH` every active cycle, not memoised (`entitlements.py:1214-1220`); never revokes; never touches `workshop_demo`. Deployed attendee apps run as their **own** SP — CT `#282` adds them to a model-access group; confirm `workshop_demo` is readable by that group / workspace `users`, or deployed apps 403 at runtime.
- No startup probe to the Omnigent App `/api/version` or a 1-token model call; `readiness.model_gateway` is soft. Run `scripts/smoke_models.py` against an event workspace on Tuesday.

---

## 3. Remediation plan

### Sunday night / Monday — WT patch release `v2026.11.0x` (code, ~1 day, one PR per bullet)

| # | Change | Files | Tests |
|---|---|---|---|
| W1 ✅ | Expiry-aware probe + forced early mint (`Config.init_auth()`) + no `clear` on near-expiry + retry/jitter on transient probe failures + serve held unexpired bearer with stale validation | `server/credentials.py` | 13 new tests incl. 3 h fake clock |
| W2 ✅ | `CLAUDE_CODE_MAX_RETRIES=3` | `server/cli_config.py` | updated |
| W3 ✅ | `skills`+`apx` in `retry_failed`; bounded background self-retry; jittered `_download_https` (403/408/425 transient, `Retry-After`); clone `TimeoutExpired` inside loop + `GIT_TERMINAL_PROMPT=0` + low-speed abort; `UV_CONCURRENT_DOWNLOADS=8` + 3 attempts on `uv pip install`; `logger.error` in `_set` | `server/bootstrap/install.py`, `artifacts.py` | 10 new tests |
| W4 (readyz cost) | `/readyz` fast 503 while installing; proof cached 90 s — **not done**, post-event | `server/bootstrap/install.py`, `readiness.py` | — |
| W5 ◐ | killpg after PTY EOF ✅ (verified with a real backgrounded process); `TIOCSCTTY`, cgroup memory gauge, `NODE_OPTIONS` — not done | `server/sessions.py` | 1 new test |
| W6 ◐ | Ctrl+C-copies-when-selected ✅; `/resume` hint in exit notice ✅ (exit frame now carries `agent`); WS retry indefinitely, stop `/api/agents` poll, `fonts.ready` — not done | `frontend/src/terminalKeys.ts`, `TerminalView.tsx`, `terminalExit.ts`; `static/` rebuilt | 8 new tests |
| W7 | Capture-held ⇒ hard `/readyz` check; periodic re-init of sticky faults; plain-English reason in `/api/setup-status` | `server/capture.py`, `readiness.py` | `test_readiness`, `test_capture*` |
| W8 ◐ | `OMNIGENT_STALE_GRACE_S` default 300 → 7200 ✅ (`config.py`, `app.yaml`); park-instead-of-kill, restart cap, backoff floor, `installing` state — not done | `server/config.py`, `app.yaml` | existing |
| W9 ◐ | Deny `mcp__apx__feedback_*` for Claude ✅; destructive-action guard, `apx dev stop`/localhost note, `WORKSHOP_DEFAULT_PERSONA`, `ct_fleet.py demote-omnigent` — not done | `server/cli_config.py` | 1 new test |
| W10 | Fix latent `UnboundLocalError` (`main.py` hoist `from . import projects`); friendly `CredentialError` in `create_session`; `.sync.log` rotation + sync lock + `--exclude node_modules` | `main.py`, `user_content.py` | existing |

Tag, build PEX, publish `release-manifest.json`. If time is short: **W1, W2, W3, W6 (Ctrl+C), W8 (env only)** are the must-haves; the rest can be env/brief mitigations.

### Monday — Control Tower operations

1. Deploy CT ≥ `72e878a`; smoke a 2-seat run; keep `7d7dd64` as rollback.
2. Stage + verify + **promote** the WT patch release (or `v2026.11.01.3` if the patch slips). Stage + verify the **toolchain mirror** for that ref (49 artifacts, `complete=true`).
3. Model Pool: enable every pin and chain fallback; static sync verified. Record the model-services list from an event workspace.
4. Create the run with: Auto-hibernate **off**; `codex_reasoning_effort=medium`; gateway budget **off** (or ≥ $25); `COST_UNIT_BREACH_ACTION=none` / no spend cap; `event_end_at` correct, `termination_at ≥ end + 30 min`; **165 seats**; `env_overrides`: `OMNIGENT_STALE_GRACE_S=7200`, `WORKSHOP_WIZARD_MODEL=gpt-5-6-luna`, (`WORKSHOP_DEFAULT_PERSONA=technical` if W9 ships), decide `WORKSHOP_ONBOARDING_WIZARD`.
5. Canary 2 seats on the final config for ≥ 2 h: `/readyz` green incl. `supply_chain.toolchain_mirror_served == artifact_count`, `release_pins`, `credentials`; tail `gateway-token` `exp` vs clock (P0-4); launch Claude, Codex, Omnigent from a fresh HOME — no trust/theme/login dialogs; deploy one APX app reading `workshop_demo.*` through the MCP tool and open it as the attendee; close the lid for 75 min and confirm Omni survives (P1-1); force a 429 and watch the attendee view; `id -u` ≠ 0; `df -h` + `du -sh data/`; measure `/readyz` latency.

### Tuesday — full provision + rehearsal

- Provision all 165 seats ≥ 3 h before Wednesday doors (realistically Tuesday evening, keep warm). Expect ~30 min pacing + tail + one repair pass.
- Fleet entitlement sweep, `tools/uc/grant_sweep.py` report, Comms → emails queued = seats, Industry Map review.
- Run `scripts/smoke_models.py` matrix against one event workspace.
- Spot-check 10 random seats: `/readyz`, launch each agent, `databricks current-user me` is the SP, `databricks-me current-user me` is the attendee.

### Wednesday morning (T-90)

- Confirm no seat hibernated; `/readyz` sweep all seats; Slack alert channel live; operator runbook open at the Omnigent rungs; spare-seat list printed for TAs.

---

## 4. Facilitator / TA brief (one slide)

- **One agent at a time** in the terminal; switching Claude↔Codex or a crash ends that conversation — type `/resume` (Claude) or `codex resume` to pick it up. No side shell: use the agent, or `!cmd` in Claude. Omnigent runs alongside via its own app.
- **Keep the Workshop Terminal tab open and the laptop awake** when using Omnigent; a closed lid over lunch (> 1 h) loses in-flight Omni work (auto-recovers on return).
- **Copying from the terminal:** macOS Cmd+C. Windows/Linux: right-click → Copy, or select and use the Copy button (if W6 ships). Ctrl+C without W6 **interrupts the agent**.
- Sign-in: OTP email arrives on the **first sign-in attempt**; fallback "Reset password → one-time password".
- First open of an attendee-deployed app shows a Databricks "Permission Requested" consent page — click Allow.
- The attendee's browser **cannot reach `localhost`** dev servers in the container — ask the agent to deploy (or use `workshop-browser`), and to `apx dev stop` when done.
- Red "credential unhealthy" banner that clears within 5 min = known flap (until W1); persistent = Operator panel → recover sign-in.
- Broken **used** seat ⇒ move the attendee to a spare seat; don't wait on a redeploy.
- Operator panel "pause Omnigent" is per-seat; the fleet lever is CT `env_overrides OMNIGENT_ENABLED=false` + restart (or `ct_fleet.py demote-omnigent` if W9 ships).

---

## 5. Verified solid — do not spend time here

Secret hygiene (client secret captured into one SDK client then scrubbed; non-dumpable process; PTY env allowlist; OBO never in env/logs; Omnigent plane gets an SP-free `databrickscfg`). Atomic, fsync'd credential writes with per-user lock + revision fencing. Identity binding of the SP bearer against `applicationId`/SCIM id. OBO capture ordering (rejects stale/out-of-order snapshots), keepalive iframe + nudges. Session isolation (owner-scoped lookups, claim-then-teardown terminate, registry lock through `Popen`), bounded scrollback/queues/journals everywhere, accept-before-close WS codes, restart ghosts, `supervisor.py` kill-guard + bounded restarts. Supply chain: every artifact sha256-pinned in-repo, override manifest may only redirect `source`, `--require-hashes` locks, verify-before-rename staging, Claude autoupdater disabled. Deploy tool: resumable, waits for exact deployment + compute state, honest about timeouts. Entitlements: fail-closed creator checks, 429 ledger rollback, idle cadence, never revokes. Wizard: strict schema, visible fallback, never blocks launch. CT: package-mode create→entitle→materialise→configure→deploy→readback, `/readyz` contract faithfully implemented, pacer with adaptive backoff, idempotent resume, two-identity auth with SDK-managed refresh and no attendee-credential rotation, bounded teardown with grant revocation, required apps, handouts reflecting real readiness.
