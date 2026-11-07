# Area guide: the attendee agent environment

**Scope:** everything WT puts in front of an attendee's coding agent:

- instructions;
- skills;
- helper commands;
- MCP servers;
- CLI configuration;
- the project scaffold contract.

Text here is **product payload read by attendee agents**, not guidance for agents working
on this repository.

## Entry points

| What | Where |
| --- | --- |
| Orchestration, run before every launch and on project selection | `server/user_content.py::provision` |
| Instruction composition | `server/user_content.py::_base_instructions`, `server/user_content.py::_write_instructions_locked` (persona, wizard or project, and demo-data overlays) |
| Templates | `assets/instructions/CLAUDE.md` and the slot files beside it: `terminal_safety.md`, `teammate.md`, `workshop_contract.md`, `lab_coach.md`, `discovery*.md`, `demo_data.md`, `project_*.md` |
| CLI config and tokens | `server/cli_config.py::configure_claude`, `server/cli_config.py::configure_codex`, `server/cli_config.py::configure_omnigent`, `server/cli_config.py::configure_databricks_cli` |
| Skills linking | `server/user_content.py::_link_skills`, using the runtime tree `server/user_content.py::shared_skills_dir` or the vendored `assets/skills/` |
| Helper CLIs | `assets/bin/*`, installed by `server/user_content.py::_install_cli_helpers` and `_install_project_helper` |
| Project scaffold contract | `assets/bin/workshop-init-project`: a managed block in project `AGENTS.md`/`CLAUDE.md`, committed |
| Environment isolation | `server/users.py::User.shell_env`, `server/users.py::_SHELL_ENV_ALLOWLIST` |

## Where things land

`~` is the attendee HOME, `DATA_ROOT/users/<slug>`. In production that is
`/app/python/source_code/data/users/<slug>`.

| File | Written by |
| --- | --- |
| `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` (same content, header swapped) | `server/user_content.py::_write_instructions_locked` |
| `~/.claude/settings.json` (gateway base URL, `apiKeyHelper`), `~/.claude.json` (trust, MCP `workshop` and `apx`; DeepWiki/Exa only with `ENABLE_PUBLIC_MCP`) | `server/cli_config.py`, `server/user_content.py::_write_claude_json` |
| `~/.codex/config.toml` (provider auth `command = "cat"`, MCP) | `server/cli_config.py::configure_codex` |
| `~/.databrickscfg` (`[DEFAULT]` app SP; `[me]` only with OBO) | `server/cli_config.py` |
| `~/.config/workshop/gateway-token` (0600; rotated in place) | `server/cli_config.py::update_tokens` |
| `~/.local/bin/` helpers, and the generated `databricks` wrapper | `server/user_content.py::_install_cli_helpers`, `server/users.py::User.bootstrap_home` |
| `~/.claude/skills/`, `~/.agents/skills/` (per-skill symlinks) | `server/user_content.py::_link_skills` |
| `~/.gitconfig` with a post-commit hook that syncs `~/projects` to the Workspace home | `server/user_content.py` |
| `~/projects/<name>/AGENTS.md`, `~/projects/<name>/CLAUDE.md` | `assets/bin/workshop-init-project` |

## Invariants

- **Repo-root instruction files must never exist.** Attendee homes are *inside* the
  deployed source tree on a source deploy (CT's git-clone path, `scripts/deploy_dev.py`). They
  are not inside it for the packaged PEX, which carries only the runtime roots. Claude Code
  loads `CLAUDE.md`, `CLAUDE.local.md` and `.claude/CLAUDE.md` from ancestor directories, so a
  repo-root copy would reach every attendee. Enforced by `tests/test_agent_guide.py`.
- **`~/.claude/CLAUDE.md` must exist before any Claude process starts.** It also shields
  attendees from the repo-root `AGENTS.md`. Claude Code 2.1.318 measurements, all from a
  directory below an AGENTS.md-only ancestor:
  - The ancestor `AGENTS.md` is loaded when no CLAUDE file exists in the chain. A `.git`
    boundary does not stop it, and an empty `CLAUDE.md` does not shadow it.
  - It is not loaded once a `CLAUDE.md` or `.claude/CLAUDE.md` exists in the chain. That
    includes the attendee's own `~/.claude/CLAUDE.md`, emulated via `CLAUDE_CONFIG_DIR`.
  - `server/user_content.py::provision` writes that file as a required step; a failure
    refuses the launch with 503. Do not make it optional, and do not launch Claude with a cwd
    outside the attendee HOME.
  - Codex 0.166.0 stopped at the project's `.git`, and in a non-git directory it read only
    that directory.
  - Re-measure on a Claude Code upgrade: run the probe in [CHANGE_GUIDE](../CHANGE_GUIDE.md#commands).
- **Codex reads the project-level AGENTS.md.** Omnigent's Codex runs with an isolated
  `CODEX_HOME` inside a git worktree, so it never sees `~/.codex/AGENTS.md`. That is why the
  scaffold commits the contract into the project (see the `assets/bin/workshop-init-project`
  header).
- **Deny-by-default PTY env.** Only allowlisted variables reach agents; `WORKSHOP_PAT` and
  client secrets never do. A variable reaches a PTY in one of two ways:
  - it is copied from the process env through `server/users.py::_SHELL_ENV_ALLOWLIST` (or the
    `LC_` prefix);
  - it is set explicitly in `server/users.py::User.shell_env`, as `WORKSHOP_CATALOG` is.
- **Rotating tokens are read from a file at request time** (Claude `apiKeyHelper`, Codex
  `auth.command`). Never bake a token into a config value a long-running CLI caches.
- **Vendored upstream skills are regenerated, not edited** (see the
  [toolchain guide](toolchain-and-release.md)). Workshop-authored skills, such as
  `assets/skills/workshop/` and `assets/skills/workshop-export/`, are edited directly.
  `scripts/refresh_vendored_skills.py` lists the fork-only set.
- **Startup context is a budget.** Every line added to `assets/instructions/` is loaded by
  every attendee agent at session start.
- **Public MCP servers stay off by default.** The agents run autonomously with a live
  credential (`WORKSHOP_AUTO_MODE`).

## Dependencies and consumers

- **Depends on:**
  - `server/credentials.py` for the token;
  - `server/model_policy.py` and `server/models.py` for model slots;
  - `server/bootstrap/install.py` for whether the runtime skills tree is ready;
  - `server/projects.py` for the active project;
  - `server/wizard.py` for the brief.
- **Consumed by:** the agent CLIs; Omnigent hosts (`server/omnigent_remote.py`); the
  generated-app evaluation harness (`evals/generated_apps/`), which asserts on the attendee
  experience.

## Tests

- Content and instructions: `tests/test_user_content.py`, `tests/test_workshop_contract.py`,
  `tests/test_persona.py`, `tests/test_teammate_config.py`, `tests/test_discovery_agent.py`,
  `tests/test_sweep_instruction_wording.py`, `tests/test_topic_precision.py`.
- Setup and skills: `tests/test_preparation.py`, `tests/test_project_setup_guarantee.py`,
  `tests/test_provider_payloads.py`, `tests/test_skills_provenance.py`.
- Helpers and env: `tests/test_shell_env.py`, `tests/test_databricks_cli_wrapper.py`,
  `tests/test_databricks_me.py`, `tests/test_app_deploy_tool.py`, `tests/test_workshop_export.py`.

## Commands

- `uv run --frozen python -m pytest tests/test_user_content.py tests/test_workshop_contract.py tests/test_preparation.py -q`
- `uv run --frozen python scripts/measure_startup_context.py`: what each harness loads at
  session start. It needs the installed CLIs *(inspected, not run)*.

## Common mistakes

- **Editing for the wrong audience.** Editing `assets/instructions/CLAUDE.md` while meaning
  to instruct *yourself*. It ships to attendees.
- **Missing the composition step.** Adding a new instruction file without wiring it into
  `server/user_content.py::_base_instructions`. Nothing loads files by globbing, except tests
  such as `tests/test_topic_precision.py`.
- **Missing the env allowlist.** Adding an env var for agents without extending
  `server/users.py::_SHELL_ENV_ALLOWLIST` or setting it in `server/users.py::User.shell_env`.
  It silently will not appear. Cover it in `tests/test_shell_env.py`.
- **Hand-editing generated content.** Fixing an upstream skill in place under
  `assets/skills/`. The next refresh reverts it, and `tests/test_skills_provenance.py` fails.
- **Testing only Claude.** Codex reads `~/.codex/AGENTS.md` and the project AGENTS.md,
  rewrites skill paths to `~/.agents/skills`, and has a different trust mechanism
  (`server/cli_config.py::ensure_codex_trusts`).
