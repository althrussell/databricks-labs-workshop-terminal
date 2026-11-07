# Agent guide: discovery and task routing

The second document a coding agent reads, after the root [AGENTS.md](../../AGENTS.md). It
routes a task to the smallest set of code, tests and docs that owns it.

## Orientation in one screen

Workshop Terminal (WT) is a Databricks App. Each instance serves one attendee.

- **Browser.** The attendee opens the app. The React UI (`frontend/`, served from the
  committed `static/`) talks to FastAPI (`server/main.py`, `server/admin.py`, `server/ws.py`).
- **Agent terminals.** WT launches agent CLIs (Claude Code, Codex, Omnigent) in PTYs under a
  per-attendee HOME (`server/sessions.py`, `server/users.py`).
- **What the agent is given.** Credentials, CLI config, instructions, skills and helper
  commands (`server/credentials.py`, `server/cli_config.py`, `server/user_content.py`,
  `assets/`).
- **Toolchain.** At boot WT installs a checksum-pinned toolchain (`server/bootstrap/`,
  `assets/artifacts/manifest.json`).
- **Control Tower (CT).** A separate repository deploys the app, steers it through
  `/api/admin/*`, and pulls captured insight and closeout records. Contracts live in `docs/`.

Three audiences read text in this repository:

- **You:** AGENTS.md and `docs/agent-guide/`.
- **Operators and CT:** `docs/`. CT's operator assistant also ingests `docs/**/*.md`, except
  this guide.
- **Attendee agents:** `assets/instructions/`, `assets/skills/` and helper output.

Keep them apart. Editing attendee-agent text is a product change.

## Reading order

1. Root [AGENTS.md](../../AGENTS.md): constraints and workflow.
2. This file: find your row in the [task table](#task-routing-table).
3. The **Read first** document in that row: an area guide, flow section or canonical contract.
4. The listed entry points and tests in the code itself.
5. Stop. Use [REPO_MAP](REPO_MAP.md), [FLOWS](FLOWS.md) and [CHANGE_GUIDE](CHANGE_GUIDE.md) as
   lookups, not as preliminary reading.

**Your context is sufficient when** you can name:

- the entry point you will change;
- its callers or consumers (frontend `api.ts`, CT, attendee agents, other server modules);
- the tests that pin its behaviour;
- the docs in the maintenance table your change touches.

**Expand into another subsystem when** any of these holds:

- your change alters data that crosses a boundary in the [REPO_MAP diagram](REPO_MAP.md#component-diagram);
- a test you must touch imports a module you have not read;
- the row's **Read more when** column fires;
- code and docs disagree. Read the canonical contract and report the discrepancy.

## Task routing table

Paths are repository-relative; `module::symbol` names a function, class or constant in that file.
Area guides: [attendee environment](areas/attendee-agent-environment.md),
[toolchain and release](areas/toolchain-and-release.md),
[Control Tower surfaces](areas/control-tower-surfaces.md).

| Task or symptom | Owning area | Read first | Source entry points and symbols | Relevant tests | Read more when |
| --- | --- | --- | --- | --- | --- |
| Attendee UI change (component, layout, toast, panel) | `frontend/` | [frontend/AGENTS.md](../../frontend/AGENTS.md) | `frontend/src/App.tsx`, `frontend/src/api.ts`; components such as `frontend/src/components/AgentCards.tsx` (launch cards), `frontend/src/components/TerminalView.tsx`, `frontend/src/components/OperatorPanel.tsx`, `frontend/src/components/Wizard.tsx` | `frontend/src/*.test.ts`; opt-in `tests/test_*_rendered.py` | an API shape changes: open the matching server row |
| New or changed attendee HTTP endpoint | `server/main.py` | [FLOWS: launching an agent](FLOWS.md#2-launching-an-agent-terminal) for the route pattern | `server/main.py::get_config`, `server/auth.py::get_current_user`, `server/closeout_http.py::produces_capture` | the `client` fixture in `tests/conftest.py`; the area's `tests/test_*.py` | the route writes state: closeout admission and the capture journal apply ([CT surfaces](areas/control-tower-surfaces.md)) |
| Operator panel, admin API, phase, broadcast, content pack | `server/admin.py`, `server/content.py` | [CT surfaces](areas/control-tower-surfaces.md), then [docs/admin-api.md](../admin-api.md) | `server/admin.py::router`, `server/auth.py::require_admin`, `server/content.py::ContentService`, `server/events.py::EventHub` | `tests/test_content.py`, `tests/test_operator_levers.py`, `tests/test_admin_diagnostics.py` | CT calls it: check the CT consumer and keep `docs/admin-api.md` exact |
| 403 / 4403, "wrong seat", attendee binding, admin group | `server/auth.py`, `server/attendee.py`, `server/topology.py` | [FLOWS: identity](FLOWS.md#1-request-identity-and-seat-binding) | `server/auth.py::_check_access`, `server/attendee.py::bind`, `server/attendee.py::resolved_email`, `server/topology.py::validate_single_session` | `tests/test_auth.py`, `tests/test_attendee.py`, `tests/test_topology.py` | SCIM or service-principal callers are involved: read [docs/auth-identity-model.md](../auth-identity-model.md) |
| Agent CLI cannot authenticate, token rotation, gateway 401 | `server/credentials.py`, `server/cli_config.py`, `server/obo.py` | [FLOWS: credentials](FLOWS.md#3-credential-lifecycle) | `server/credentials.py::CredentialManager._fanout`, `server/cli_config.py::update_tokens`, `server/obo.py::OboManager`, `server/selfheal.py::SelfHealer` | `tests/test_credentials*.py`, `tests/test_cli_atomic_credentials.py`, `tests/test_obo*.py`, `tests/test_tab_closed.py` | identity design questions: [ADR 0002](../adr/0002-attendee-credential.md), [verification gate](../verification-gate.md) |
| Launch fails (409/503), session conflict, terminal disconnects or replays | `server/sessions.py`, `server/ws.py` | [FLOWS: launching](FLOWS.md#2-launching-an-agent-terminal) | `server/main.py::create_session`, `server/sessions.py::SessionManager`, `server/ws.py::session_socket`, `frontend/src/components/TerminalView.tsx` | `tests/test_sessions.py`, `tests/test_stream_limits.py`, `tests/test_session_store.py`, `tests/test_agent_selection.py` | install readiness is the cause: see the toolchain row |
| What attendee agents are told: instructions, skills, helper CLIs, MCP | `assets/`, `server/user_content.py` | [attendee environment](areas/attendee-agent-environment.md) | `server/user_content.py::provision`, `server/user_content.py::_base_instructions`, `server/cli_config.py::configure_claude`, `assets/bin/workshop-init-project` | `tests/test_user_content.py`, `tests/test_workshop_contract.py`, `tests/test_persona.py`, `tests/test_preparation.py`, `tests/test_skills_provenance.py` | startup context grows: `scripts/measure_startup_context.py`; the skills overlay changes: toolchain row |
| Onboarding wizard, projects, "Your build", build plan approval | `server/wizard*.py`, `server/projects.py`, `server/journey.py` | [FLOWS: build journey](FLOWS.md#5-attendee-build-journey) | `server/wizard.py::save`, `server/wizard_llm.py::suggest`, `server/projects.py::select`, `server/journey.py::view`, `server/build_plan.py` | `tests/test_wizard*.py`, `tests/test_projects.py`, `tests/test_journey.py`, `tests/test_build_restore.py`, `tests/test_design_workflow.py` | the change alters the agent's instructions: attendee environment row |
| App deploy tool, `databricks apps logs`, take-home export | `assets/bin/` | [docs/app-deploy-tool.md](../app-deploy-tool.md), [docs/take-home-export.md](../take-home-export.md) | `assets/bin/workshop-app-deploy`, `assets/bin/workshop-export`, `server/main.py::record_app_deploy_event`, `server/main.py::stream_app_logs_snapshot`, `server/main.py::export_code` | `tests/test_app_deploy_tool.py`, `tests/test_app_logs_endpoint.py`, `tests/test_workshop_export.py`, `tests/test_take_home.py` | grants on created resources: entitlements row |
| Bump Claude/Codex/Databricks CLI/Node/Omnigent/skills pins, installer failure | `server/bootstrap/` | [toolchain and release](areas/toolchain-and-release.md) | `server/bootstrap/install.py::run_in_background`, `server/bootstrap/install.py::SKILLS_REF`, `server/bootstrap/artifacts.py::load_manifest`, `scripts/build_artifact_manifest.py` | `tests/test_artifact_manifest.py`, `tests/test_installer_versions.py`, `tests/test_skills_provenance.py`, `tests/test_toolchain_mirror.py` | Omnigent version: also `deploy/omnigent-app/` ([docs/omnigent-acceptance-checklist.md](../omnigent-acceptance-checklist.md)) |
| Python or npm dependency change | `pyproject.toml`, `frontend/package.json` | [CHANGE_GUIDE: commands](CHANGE_GUIDE.md#commands) | `pyproject.toml`, `uv.lock`, `requirements.in`, `dependency_sources.py` | `tests/test_release_artifact.py`, `tests/test_dependency_sources.py`, `tests/test_ci_contract.py` | the package lands in the PEX: [docs/release-artifact.md](../release-artifact.md) |
| `/readyz` red, readiness check semantics | `server/readiness.py` | [FLOWS: start, bootstrap, readiness](FLOWS.md#4-process-start-toolchain-bootstrap-and-readiness) | `server/readiness.py::evaluate`, `server/readiness.py::durability`, `server/main.py::readyz` | `tests/test_readiness.py`, `tests/test_operational.py` | CT admission depends on it: [docs/verification-gate.md](../verification-gate.md) |
| Insight capture, CT pull/ack, sealed closeout | `server/event_emitter.py`, `server/capture*.py`, `server/closeout*.py` | [CT surfaces](areas/control-tower-surfaces.md), [FLOWS: delivery and closeout](FLOWS.md#7-control-tower-delivery-and-sealed-closeout) | `server/event_emitter.py::EventEmitter`, `server/capture.py::configure`, `server/closeout.py::CloseoutManager`, `server/admin.py::collect_insight_events` | `tests/test_capture_*.py`, `tests/test_closeout*.py`, `tests/test_insight_*.py`, `tests/test_event_emitter.py` | an event payload changes: [docs/workshop-insight-contract.md](../workshop-insight-contract.md) and `tests/fixtures/workshop-insight-events.schema.json`, which rejects unknown fields |
| Stats harvest (`GET /api/admin/stats`), per-attendee counters, certificate numbers | `server/stats.py` | [CT surfaces](areas/control-tower-surfaces.md), then the stats section of [docs/admin-api.md](../admin-api.md) | `server/admin.py::harvest_stats` (wrapper), `server/stats.py::gather_user`, `server/stats.py::signal_events`, `server/stats.py::STATS_SCHEMA_VERSION`, `server/users.py::User` counters | `tests/test_stats_schema.py`, `tests/test_stats_resource_census.py`, `tests/test_insight_signal.py`, `tests/test_insight_contract.py`, `tests/test_capture_stats_recovery.py` | a field reaches `workshop.signal`: the insight contract, the strict schema and `docs/examples/`; CT persists harvested stats, so check its consumer |
| OTel logs, traces and metrics; redaction | `server/observability.py`, `server/telemetry.py` | [docs/observability.md](../observability.md) | `server/otel_bootstrap.py::main`, `server/observability.py::safe_event_attributes`, `server/telemetry.py::emit` | `tests/test_observability.py`, `tests/test_otel_bootstrap.py`, `tests/test_event_taxonomy.py` | a new event name or attribute: update the taxonomy in `docs/observability.md` |
| Model pool, model policy, AI Gateway routing, spend kill switch | `server/model_policy.py`, `server/models.py`, `server/spend.py` | [docs/governed-ai-gateway.md](../governed-ai-gateway.md) | `server/model_policy.py::PolicyStore`, `server/model_policy.py::resolve_service`, `server/cli_config.py::gateway_host`, `server/spend.py::check_can_launch` | `tests/test_governed_model_policy.py`, `tests/test_models.py`, `tests/test_gateway_resolution.py`, `tests/test_spend.py` | the default model changes: [docs/model-comparison.md](../model-comparison.md) |
| Grants on resources the app SP creates (catalogs, apps) | `server/entitlements.py` | [docs/auth-identity-model.md](../auth-identity-model.md) | `server/entitlements.py::EntitlementManager`, `server/main.py::reconcile_entitlements` | `tests/test_entitlements.py`, `tests/test_attendee_app_group.py` | the shared demo catalog: [ADR 0003](../adr/0003-shared-demo-catalog.md) |
| Agent card disabled or withdrawn; blocked reasons (`credential_stale`, `credential_absent`, `operator_demoted`); agent catalogue | `server/agents.py`, `frontend/src/components/AgentCards.tsx` | [FLOWS: launching](FLOWS.md#2-launching-an-agent-terminal) (gates) | `server/agents.py::launch_block`, `server/agents.py::load_catalog`, `server/main.py::list_agents`, `frontend/src/components/AgentCards.tsx::cardState` | `tests/test_obo_gating.py`, `tests/test_operator_levers.py`, `tests/test_agent_selection.py`, `tests/test_pi_retirement_contract.py` | the reason comes from OBO freshness: [FLOWS §3](FLOWS.md#3-credential-lifecycle); from the operator lever: [docs/operator-runbook.md](../operator-runbook.md) |
| Omnigent tier, paired host, paired Omnigent App | `server/omnigent_remote.py`, `deploy/omnigent-app/` | [docs/omnigent-modes.md](../omnigent-modes.md), [deploy/omnigent-app/README.md](../../deploy/omnigent-app/README.md) | `server/omnigent_remote.py::RemoteHostManager`, `server/omnigent_remote.py::build_host_launch`, `server/agents.py::launch_block`, `deploy/omnigent-app/app.py` | `tests/test_omnigent_remote.py`, `tests/test_omnigent_config.py`, `tests/test_omnigent_app_contract.py`, `tests/test_smart_routing.py` | CT drives the tier: [docs/omnigent-control-tower-contract.md](../omnigent-control-tower-contract.md) |
| Help chat, raise hand, Workshop Assistant replies | `server/help.py` | [docs/admin-api.md](../admin-api.md) (help sections) | `server/help.py::post_attendee_message`, `server/help.py::ingest_operator_message`, `frontend/src/components/HelpChatPanel.tsx` | `tests/test_help.py` | the change touches what CT collects: [CT surfaces](areas/control-tower-surfaces.md) |
| Deploy-time env var or `app.yaml` default | `app.yaml`, `server/config.py` | [docs/admin-api.md: env vars](../admin-api.md#deploy-time-configuration-env-vars) | `server/config.py` accessors (some grouped, such as `server/config.py::branding`; `server/otel_bootstrap.py` reads a few directly), `app.yaml` | `tests/test_app_runtime_config.py`, `tests/test_insight_config.py`, `tests/test_shell_env.py` | it must reach agent terminals: add it to `server/users.py::_SHELL_ENV_ALLOWLIST` or set it in `server/users.py::User.shell_env`; CT sets it: [docs/control-tower-implementation.md](../control-tower-implementation.md) |
| Release PEX, CI workflow, reproducibility | `scripts/build_release.py`, `.github/workflows/` | [toolchain and release](areas/toolchain-and-release.md), then [docs/release-artifact.md](../release-artifact.md) | `scripts/build_release.py::RUNTIME_ROOTS`, `server/otel_bootstrap.py::uvicorn_command`, `.github/workflows/ci.yml` | `tests/test_release_artifact.py`, `tests/test_release_package_qualification.py`, `tests/test_ci_contract.py` | the change needs a live check: [docs/final-release-qualification.md](../final-release-qualification.md) |
| Generated-app evaluation harness | `evals/generated_apps/`, `scripts/*generated_app*.py` | [docs/generated-app-evaluation-runbook.md](../generated-app-evaluation-runbook.md) | `scripts/evaluate_generated_apps.py`, `scripts/run_generated_app_journey.py`, `evals/generated_apps/simulator.py` | `tests/test_generated_app_*.py`, `evals/generated_apps/test_baseline.py` (not in `make test`) | a live CT run: the runbook's external-action sections |
| Fleet operations, soak, rehearsal, diagnostics pull | `scripts/` | [docs/verification-gate.md](../verification-gate.md), [docs/operator-runbook.md](../operator-runbook.md) | `scripts/soak.py`, `scripts/rehearse.py`, `scripts/pull_diagnostics.py`, `server/log_collector.py::LogCollector` | `tests/test_scale_rehearsal.py`, `tests/test_soak_and_chaos.py`, `tests/test_log_collector.py` | before running any of these: `scripts/soak.py chaos` mutates a live instance |
| This guide or agent configuration | `AGENTS.md`, `docs/agent-guide/`, `.claude/settings.json` | [CHANGE_GUIDE: maintenance table](CHANGE_GUIDE.md#maintenance-table) | `tests/test_agent_guide.py` | `tests/test_agent_guide.py` | adding a nested AGENTS.md: list it in the root file |

## Search terms for unfamiliar symptoms

Error codes and reason strings are literal; `grep -rn '<term>' server frontend/src assets/bin`
finds the emitter. Leave `static/` out of searches: it is minified build output and matches
everything.

| You see | Search for | Usually owned by |
| --- | --- | --- |
| attendee refused with 403, or a socket closes with 4403 | `wrong_seat_identity`, `_check_access` | `server/auth.py`, `server/attendee.py` |
| launch returns 409 | `session_conflict`, `still installing`, `workshop_closing` | `server/main.py::create_session`, `server/bootstrap/install.py` |
| launch returns 503 | `credential_unavailable`, `obo_stale`, `preparation_failed`, `still syncing`, `session_configuration` | `server/main.py::create_session` |
| Omnigent card withdrawn | `credential_stale`, `credential_absent`, `operator_demoted` | `server/agents.py::launch_block`, `frontend/src/components/AgentCards.tsx::cardState` |
| writes refused near the end of an event | `workshop_closing`, `CaptureHeldError`, `model_policy_pending` | `server/closeout_http.py`, `server/workshop_lifecycle.py` |
| `/readyz` 503 | `credential_durability`, `release_pins`, `_soft` | `server/readiness.py` |
| restart left "ended" sessions | `server_restarted`, `load_prior` | `server/session_store.py`, `server/sessions.py` |
| a blank page served by the app | `frontend build missing` | `server/main.py::spa`, `static/` |
| gateway throttling or a budget banner | `gateway.request_limited`, `SpendBlocked` | `server/gateway_errors.py`, `server/spend.py`, `frontend/src/gatewayLimit.ts` |
| error surfaced to operators | `omnigent.error_detected`, `app.error_detected` | `server/log_collector.py` |
| "who created this resource" | `identity.resolved` | `server/identity.py` |

## When the guide is wrong

Fix it in the same change; the [maintenance table](CHANGE_GUIDE.md#maintenance-table) names
the document. `tests/test_agent_guide.py` fails on a stale path, symbol or link. It cannot
detect a stale *description*, so a route that misled you is worth one corrected sentence.
