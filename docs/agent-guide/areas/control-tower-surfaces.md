# Area guide: Control Tower surfaces

**Scope:** every interface between WT and Control Tower (CT). CT is a separate repository
and a separate deployment, so a fleet can run an older CT against a newer WT, or the
reverse. Each surface here is a wire contract.

## Surfaces and their canonical docs

| Direction | Surface | Code | Canonical doc |
| --- | --- | --- | --- |
| CT → WT | admin router (`/api/admin/*`): state, presence, phase, broadcast, content pack, deadline, model policy, wizard model, agent controls, attendee profile, help, recover, Omnigent tier, diagnostics, entitlements | `server/admin.py::router` | [docs/admin-api.md](../../admin-api.md) |
| CT → WT | insight pull and ack, stats harvest | `server/admin.py::collect_insight_events`, `server/admin.py::acknowledge_insight_events`; the harvest route `server/admin.py::harvest_stats` wraps `server/stats.py::gather_user` and `server/stats.py::signal_events` | [docs/workshop-insight-contract.md](../../workshop-insight-contract.md), [docs/wt01-durable-capture-delivery.md](../../wt01-durable-capture-delivery.md) |
| CT → WT | sealed closeout | `server/admin.py::begin_capture_closeout`, `server/closeout.py::CloseoutManager` | [docs/wt02-sealed-workshop-closeout.md](../../wt02-sealed-workshop-closeout.md) |
| CT → WT | admission gate | `server/main.py::readyz`, `server/readiness.py::evaluate` | [docs/verification-gate.md](../../verification-gate.md), [docs/control-tower-implementation.md](../../control-tower-implementation.md) |
| CT → WT | deploy-time env vars, release digest | `server/config.py`, `app.yaml`, `scripts/build_release.py` | [env table](../../admin-api.md#deploy-time-configuration-env-vars), [docs/release-artifact.md](../../release-artifact.md) |
| WT → CT | help raise and messages; receipts | `server/help.py::raise_hand`, `server/help.py::post_attendee_message`, `server/help.py::acknowledge_message`, using `CONTROL_TOWER_URL` | [docs/admin-api.md](../../admin-api.md) (attendee help, presence `help_outbox`) |
| WT → CT | optional event push | `server/event_emitter.py::flush_loop`, enabled by `CONTROL_TOWER_INGEST_URL` and `CONTROL_TOWER_INGEST_TOKEN` | [docs/control-tower-implementation.md](../../control-tower-implementation.md) (the insight contract says pull only; see the [discrepancies](../REPO_MAP.md#known-doc-and-code-discrepancies)) |
| WT → OTel | logs, traces, metrics through the platform collector | `server/observability.py`, `server/telemetry.py` | [docs/observability.md](../../observability.md) |
| CT ↔ Omnigent | tier, readiness and pairing | `server/admin.py::set_omnigent_tier`, `server/omnigent_remote.py` | [docs/omnigent-control-tower-contract.md](../../omnigent-control-tower-contract.md) |

CT also reads WT's `docs/**/*.md` and `README.md` into its operator assistant's knowledge
corpus. The collector is `agents/ct-assistant/scripts/collect_corpus.py` in the CT repo, and its
`WT_DOCS_EXCLUDE` leaves out `docs/agent-guide/`. Any other doc here can surface in operator
answers. To add another doc that is not for operators, extend that exclude list in CT.

## Invariants

- **Admin auth.** Every `/api/admin/*` route requires `server/auth.py::require_admin`:
  `ADMIN_GROUP` membership through SCIM, for users or service principals.
  - Helper callbacks from attendee CLIs use a capability token instead
    (`server/main.py::_callback_identity`). Examples are `/api/obo/refresh`,
    `/api/entitlements/reconcile` and `/api/tools/app-deploy/event`.
- **Pull is non-destructive; only an ack releases retained events.**
  - Ack cursors are strict.
  - A bad cursor returns 422; unavailable capture returns 503.
- **Closeout admission.** Once closeout begins, producing requests are refused with 409
  `workshop_closing` (`server/closeout_http.py::CloseoutAdmissionMiddleware`).
  - A new attendee write route is a producer by default.
  - Exclusions are listed in `server/closeout_http.py::produces_capture`.
- **Capture writes are journaled.** They go to the attendee's Workspace folder, write-behind
  and hash-chained (`server/capture_journal.py`). Collection, closeout and shutdown flush
  before publishing.
- **What never leaves:** raw terminal output, tokens, and (to OTel) attendee identity or
  prompts. Capture of attendee-authored discovery content is opt-in
  (`WORKSHOP_INSIGHT_CAPTURE`, default off); see
  [ADR 0001](../../adr/0001-workshop-insight-capture.md).
- **Capability negotiation instead of version checks.** For example, presence advertises
  `server/help.py::HELP_CAPABILITIES`, so CT only sends assistant-role messages to terminals
  that can show them. Prefer this pattern for new optional behaviour.
- **In-memory steering state.** Content pack, phase and broadcast reset on restart to the
  deployed pack (`CONTENT_PACK_PATH` or `content/default_pack.json`). CT re-pushes.

## Consumers to check before changing a shape

- **The CT repository (`databricks-labs-control-tower`).** Its terminal contract and client
  code consume these routes. That repo is outside this checkout: search it for the route or
  field name before renaming anything.
- **The JSON schemas in `tests/fixtures/`.** For example `workshop-insight-events.schema.json`
  and `omnigent-control-tower-payload.schema.json`, and the closeout fixtures.
- **The operator panel** (`frontend/src/components/OperatorPanel.tsx`) and
  `scripts/push_content.py`, `scripts/pull_diagnostics.py`, `scripts/rehearse.py` and
  `scripts/soak.py`.

## Tests

- Steering and help: `tests/test_content.py`, `tests/test_operator_levers.py`,
  `tests/test_help.py`.
- Delivery and closeout: `tests/test_capture_admin.py`, `tests/test_capture_delivery_contract.py`,
  `tests/test_insight_contract.py`, `tests/test_insight_collection.py`, `tests/test_event_emitter.py`,
  `tests/test_closeout_api.py`, `tests/test_closeout_contract.py`.
- Gate, diagnostics and taxonomy: `tests/test_readiness.py`, `tests/test_admin_diagnostics.py`,
  `tests/test_event_taxonomy.py`, `tests/test_omnigent_app_contract.py`,
  `tests/test_ct_helpers.py`, `tests/test_ct_two_instance.py`.

Live, two-instance acceptance: [docs/two-instance-test-runbook.md](../../two-instance-test-runbook.md).

## Common mistakes

- **One-sided renames.** Renaming a field in a captured event or admin response without
  updating the contract doc, the schema fixture, *and* checking CT's consumer.
- **Assuming new fields are free.** The contract calls optional payload fields backward
  compatible for CT. But `tests/fixtures/workshop-insight-events.schema.json` sets
  `additionalProperties: false`, so a new field fails the contract tests until the schema and
  `docs/examples/` are updated. That is deliberate: it forces the doc update.
- **Unanticipated closeout refusals.** Adding a POST route that should keep working during
  closeout without adding it to the exclusions in `server/closeout_http.py::produces_capture`.
- **Trusting the broadcast doc.** `docs/admin-api.md` still says "banner", but the default
  surface is a toast.
- **Treating `server/events.py` as CT delivery.** It is in-process fan-out to browser tabs
  only. CT delivery is `server/event_emitter.py`.
- **Running mutating scripts casually.** `scripts/soak.py chaos` and the admin levers change
  a live instance; they are not read-only diagnostics.
