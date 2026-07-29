import importlib


EXPECTED_SCOPES = (
    "catalog.catalogs:read,"
    "catalog.schemas:read,"
    "catalog.tables:read,"
    "sql"
)


def _good_inputs(tmp_path):
    state_path = tmp_path / "sessions.json"
    env = {
        "MAX_SESSIONS_PER_USER": "3",
        "MAX_SESSIONS_GLOBAL": "3",
        "ALLOW_SHARED_TOPOLOGY": "false",
        "DATABRICKS_CLIENT_ID": "app-client",
        "WORKSHOP_ATTENDEE_EMAIL": "alice@example.com",
        "WORKSHOP_APP_SP_ID": "12345",
        "SESSION_STATE_PATH": str(state_path),
        "WORKSHOP_CATALOG": "workshop_alice",
        "ENABLE_ENTITLEMENTS": "true",
        "ENABLE_OBO": "true",
        "OBO_SCOPES": EXPECTED_SCOPES,
        "OMNIGENT_ENABLED": "true",
        "AI_DEV_KIT_REF": "v1.2.3",
        "CLAUDE_CODE_VERSION": "2.1.216",
        "CODEX_CLI_VERSION": "0.144.6",
        "OMNIGENT_VERSION": "0.7.0",
        "DATABRICKS_CLI_VERSION": "1.8.0",
        "ANTHROPIC_MODEL": "databricks-claude-sonnet-5",
        "CODEX_MODEL": "databricks-gpt-5-5",
    }
    credential = {
        "configured": True,
        "rotating": True,
        "healthy": True,
        "degraded": False,
        "state": "rotating",
        "source": "app_identity_oauth",
        "token_expires_in": 600,
        "last_successful_at": 900.0,
        "last_error": None,
        "validation_diagnostic": {
            "result": "matched",
            "expected_application_id": "app-client",
            "observed_application_id": "app-client",
            "expected_service_principal_id": "12345",
            "observed_service_principal_id": "12345",
        },
    }
    installer = {
        "steps": {
            name: {"status": "complete", "error": None}
            for name in (
                "node",
                "claude",
                "codex",
                "databricks",
                "skills",
                "tmux",
                "omnigent",
            )
        },
        "ready": {
            "bash": True,
            "claude": True,
            "codex": True,
            "omnigent": True,
        },
        "installing": False,
        "artifact_manifest": {"ok": True, "artifact_count": 12},
        "artifact_proof": {"reusable": True},
        "release_manifest": {
            name: {
                "enabled": True,
                "expected": version,
                "actual": version,
                "match": True,
            }
            for name, version in {
                "claude": "2.1.216",
                "codex": "0.144.6",
                "databricks": "1.8.0",
                "omnigent": "0.7.0",
            }.items()
        } | {
            "ai_dev_kit": {
                "enabled": True,
                "expected": "v1.2.3",
                "actual": "v1.2.3",
                "match": True,
                "source": "network",
                "resolved_commit": "a" * 40,
                "checksum": "b" * 64,
            }
        },
    }
    entitlements = {
        "enabled": True,
        "catalog": "workshop_alice",
        "ok": True,
        "thread_alive": True,
        "last_verified_at": 900.0,
        "verified_email": "alice@example.com",
        "verified_catalog": "workshop_alice",
        "verification_source": "background",
        "last_error": None,
    }
    obo = {
        "enabled": True,
        "present": True,
        "fresh": True,
        "configured_scopes": EXPECTED_SCOPES.split(","),
        "observed_scopes": EXPECTED_SCOPES.split(","),
        "verified_scopes": EXPECTED_SCOPES.split(","),
        "validation_state": "verified",
        "validated_at": 900.0,
    }
    secret_protection = {
        "initialized": True,
        "env_scrubbed": True,
        "non_dumpable": True,
        "ok": True,
        "error": None,
    }
    return env, credential, installer, entitlements, obo, secret_protection


def _evaluate(tmp_path, *, mutate_env=None, credential=None, installer=None,
              entitlements=None, obo=None, secret_protection=None,
              writable=True, now=1000.0):
    readiness = importlib.import_module("server.readiness")
    (
        env,
        good_credential,
        good_installer,
        good_entitlements,
        good_obo,
        good_secret_protection,
    ) = _good_inputs(tmp_path)
    if mutate_env:
        mutate_env(env)
    return readiness.evaluate(
        env=env,
        credential_status=credential or good_credential,
        installer_status=installer or good_installer,
        entitlement_status=entitlements or good_entitlements,
        obo_status=obo or good_obo,
        secret_protection_status=secret_protection or good_secret_protection,
        writable_probe=lambda _: writable,
        now=now,
    )


def test_production_readiness_requires_numeric_verified_app_sp_binding(tmp_path):
    missing = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.pop("WORKSHOP_APP_SP_ID"),
    )
    unverified = _evaluate(
        tmp_path,
        credential={
            **_good_inputs(tmp_path)[1],
            "validation_diagnostic": {
                "result": "identity_mismatch",
                "expected_service_principal_id": "12345",
            },
        },
    )
    non_numeric = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update(WORKSHOP_APP_SP_ID="sp-123"),
    )

    for report in (missing, unverified, non_numeric):
        assert report["ready"] is False
        assert report["checks"]["app_sp_binding"]["ok"] is False


def test_production_readiness_requires_verified_configured_client_uuid(tmp_path):
    missing = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.pop("DATABRICKS_CLIENT_ID"),
    )
    mismatched = _evaluate(
        tmp_path,
        credential={
            **_good_inputs(tmp_path)[1],
            "validation_diagnostic": {
                **_good_inputs(tmp_path)[1]["validation_diagnostic"],
                "observed_application_id": "different-client",
            },
        },
    )

    for report in (missing, mismatched):
        assert report["ready"] is False
        assert report["checks"]["app_sp_binding"]["ok"] is False


def test_production_readiness_reports_verified_app_sp_binding(tmp_path):
    report = _evaluate(tmp_path)

    assert report["checks"]["app_sp_binding"] == {
        "ok": True,
        "state": "green",
        "detail": "app client UUID and numeric service-principal identity are authoritatively verified",
        "expected_application_id": "app-client",
        "observed_application_id": "app-client",
        "expected_service_principal_id": "12345",
        "observed_service_principal_id": "12345",
        "validation_result": "matched",
    }


def test_all_hard_checks_green_is_ready(tmp_path):
    report = _evaluate(tmp_path)

    assert report["ready"] is True
    assert report["status"] == "ready"
    assert set(report["checks"]) == {
        "topology",
        "attendee_identity",
        "credentials",
            "app_sp_binding",
        "secret_protection",
        "installers",
        "supply_chain",
        "session_state",
        "catalog",
        "entitlements",
        "obo",
        "release_pins",
    }
    assert all(check["ok"] for check in report["checks"].values())


def test_secret_protection_fails_closed_when_env_or_linux_hardening_failed(tmp_path):
    report = _evaluate(
        tmp_path,
        secret_protection={
            "initialized": True,
            "env_scrubbed": True,
            "non_dumpable": False,
            "ok": False,
            "error": "prctl failed",
        },
    )

    assert report["checks"]["secret_protection"]["ok"] is False
    assert report["ready"] is False


def test_topology_requires_single_attendee_configuration(tmp_path):
    report = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"MAX_SESSIONS_GLOBAL": "30"}),
    )

    assert report["checks"]["topology"]["ok"] is False
    assert report["ready"] is False


def test_shared_topology_escape_hatch_never_becomes_release_ready(tmp_path):
    report = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update(
            {
                "ALLOW_SHARED_TOPOLOGY": "true",
                "MAX_SESSIONS_GLOBAL": "3",
                "MAX_SESSIONS_PER_USER": "3",
            }
        ),
    )

    assert report["checks"]["topology"]["ok"] is False
    assert report["ready"] is False


def test_attendee_identity_binding_is_required(tmp_path):
    report = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"WORKSHOP_ATTENDEE_EMAIL": ""}),
    )

    assert report["checks"]["attendee_identity"]["ok"] is False
    assert report["ready"] is False


def test_credentials_require_rotation_without_pat_fallback(tmp_path):
    env_report = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"WORKSHOP_PAT": "do-not-expose"}),
    )
    status_report = _evaluate(
        tmp_path,
        credential={
            "configured": True,
            "rotating": False,
            "healthy": False,
            "state": "degraded",
            "source": "vended_pat",
        },
    )

    assert env_report["checks"]["credentials"]["ok"] is False
    assert status_report["checks"]["credentials"]["ok"] is False
    assert "do-not-expose" not in repr(env_report)
    assert "WORKSHOP_PAT" not in repr(env_report)


def test_installers_require_every_enabled_agent_and_support_tool(tmp_path):
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    installer["steps"]["databricks"]["status"] = "error"

    report = _evaluate(tmp_path, installer=installer)

    assert report["checks"]["installers"]["ok"] is False
    assert "databricks" in report["checks"]["installers"]["missing"]


def test_session_state_requires_configured_writable_path(tmp_path):
    missing = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"SESSION_STATE_PATH": ""}),
    )
    unwritable = _evaluate(tmp_path, writable=False)

    assert missing["checks"]["session_state"]["ok"] is False
    assert unwritable["checks"]["session_state"]["ok"] is False


def test_catalog_must_be_configured(tmp_path):
    report = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"WORKSHOP_CATALOG": ""}),
    )

    assert report["checks"]["catalog"]["ok"] is False


def test_entitlements_must_be_enabled_and_healthy(tmp_path):
    disabled = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"ENABLE_ENTITLEMENTS": "false"}),
    )
    unhealthy = _evaluate(
        tmp_path,
        entitlements={"enabled": True, "ok": False, "last_error": "failed"},
    )

    assert disabled["checks"]["entitlements"]["ok"] is False
    assert unhealthy["checks"]["entitlements"]["ok"] is False


def test_entitlements_require_recent_attendee_catalog_proof_and_live_loop(tmp_path):
    no_attendee_proof = _evaluate(
        tmp_path,
        entitlements={
            "enabled": True,
            "ok": True,
            "thread_alive": True,
            "last_verified_at": None,
            "verified_email": None,
            "verified_catalog": None,
        },
    )
    dead_loop = _evaluate(
        tmp_path,
        entitlements={
            "enabled": True,
            "ok": True,
            "thread_alive": False,
            "last_verified_at": 900.0,
            "verified_email": "alice@example.com",
            "verified_catalog": "workshop_alice",
            "verification_source": "background",
        },
    )

    assert no_attendee_proof["checks"]["entitlements"]["ok"] is False
    assert dead_loop["checks"]["entitlements"]["ok"] is False


def test_obo_requires_enablement_and_expected_scopes(tmp_path):
    disabled = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"ENABLE_OBO": "false"}),
    )
    missing_scope = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"OBO_SCOPES": "sql"}),
    )

    assert disabled["checks"]["obo"]["ok"] is False
    assert missing_scope["checks"]["obo"]["ok"] is False
    assert "catalog.catalogs:read" in missing_scope["checks"]["obo"]["missing"]


def test_obo_good_hint_without_observed_real_scopes_stays_pending(tmp_path):
    report = _evaluate(
        tmp_path,
        obo={
            "enabled": True,
            "configured_scopes": EXPECTED_SCOPES.split(","),
            "observed_scopes": [],
            "verified_scopes": [],
            "validation_state": "pending",
            "validated_at": None,
        },
    )

    assert report["checks"]["obo"]["ok"] is False
    assert report["checks"]["obo"]["validation_state"] == "pending"
    assert report["checks"]["obo"]["external_validation_pending"] is True


def test_obo_requires_present_fresh_and_recent_observation(tmp_path):
    _, _, _, _, good_obo, _ = _good_inputs(tmp_path)
    absent = _evaluate(tmp_path, obo={**good_obo, "present": False})
    expired = _evaluate(tmp_path, obo={**good_obo, "fresh": False})
    stale = _evaluate(
        tmp_path,
        obo={**good_obo, "validated_at": 1.0},
        now=1000.0,
    )

    assert absent["checks"]["obo"]["ok"] is False
    assert expired["checks"]["obo"]["ok"] is False
    assert stale["checks"]["obo"]["ok"] is False
    assert stale["checks"]["obo"]["max_age_seconds"] > 0


def test_release_pins_require_fixed_ref_cli_versions_and_models(tmp_path):
    branch_tip = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"AI_DEV_KIT_REF": "main"}),
    )
    missing = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"CLAUDE_CODE_VERSION": ""}),
    )

    assert branch_tip["checks"]["release_pins"]["ok"] is False
    assert "AI_DEV_KIT_REF" in branch_tip["checks"]["release_pins"]["missing"]
    assert missing["checks"]["release_pins"]["ok"] is False
    assert "CLAUDE_CODE_VERSION" in missing["checks"]["release_pins"]["missing"]


def test_release_pins_require_installed_versions_to_match(tmp_path):
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    installer["release_manifest"]["claude"].update(
        {"actual": "2.1.215", "match": False}
    )

    report = _evaluate(tmp_path, installer=installer)

    assert report["checks"]["release_pins"]["ok"] is False
    assert "claude" in report["checks"]["release_pins"]["mismatched"]
    assert report["release_manifest"]["claude"]["expected"] == "2.1.216"
    assert report["release_manifest"]["claude"]["actual"] == "2.1.215"


def test_readyz_requires_reviewed_artifact_manifest(tmp_path):
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    installer["artifact_manifest"] = {
        "ok": False,
        "error": "artifact manifest is incomplete",
    }

    report = _evaluate(tmp_path, installer=installer)

    assert report["ready"] is False
    assert report["checks"]["supply_chain"]["ok"] is False


def test_readyz_rejects_version_shaped_output_with_untrusted_binary_checksum(tmp_path):
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    installer["artifact_proof"] = {
        "reusable": False,
        "manifest": {
            "binaries": {
                "node": {
                    "expected": "22.14.0",
                    "actual": "22.14.0",
                    "actual_checksum": "f" * 64,
                    "reusable": False,
                }
            }
        },
    }

    report = _evaluate(tmp_path, installer=installer)

    assert report["checks"]["supply_chain"]["ok"] is False


def test_release_pins_require_fetched_ai_dev_kit_ref_not_vendored_fallback(tmp_path):
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    installer["release_manifest"]["ai_dev_kit"].update(
        {
            "actual": None,
            "match": False,
            "source": "vendored_fallback",
            "resolved_commit": None,
        }
    )

    report = _evaluate(tmp_path, installer=installer)

    assert report["checks"]["release_pins"]["ok"] is False
    assert "ai_dev_kit" in report["checks"]["release_pins"]["mismatched"]
    assert report["release_manifest"]["ai_dev_kit"]["source"] == "vendored_fallback"


def test_release_pins_accept_verified_prewarmed_ai_dev_kit(tmp_path):
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    installer["release_manifest"]["ai_dev_kit"]["source"] = "prewarmed"

    report = _evaluate(tmp_path, installer=installer)

    assert report["checks"]["release_pins"]["ok"] is True


def test_credentials_require_recent_successful_oauth_validation(tmp_path):
    stale = {
        **_good_inputs(tmp_path)[1],
        "last_successful_at": 1.0,
    }

    report = _evaluate(tmp_path, credential=stale, now=1000.0)

    assert report["checks"]["credentials"]["ok"] is False
    assert report["checks"]["credentials"]["last_successful_at"] == 1.0


def test_real_journal_probe_rejects_directory_and_leaves_no_files(tmp_path):
    readiness = importlib.import_module("server.readiness")
    journal = tmp_path / "sessions.json"

    assert readiness._path_writable(str(tmp_path)) is False
    assert readiness._path_writable(str(journal)) is True
    assert list(tmp_path.iterdir()) == []


def test_journal_probe_allows_read_only_existing_file_when_parent_replace_works(
    tmp_path
):
    readiness = importlib.import_module("server.readiness")
    journal = tmp_path / "sessions.json"
    journal.write_text('{"existing": {"id": "existing"}}')
    journal.chmod(0o444)

    try:
        assert readiness._path_writable(str(journal)) is True
        assert journal.read_text() == '{"existing": {"id": "existing"}}'
    finally:
        journal.chmod(0o644)


def test_readyz_returns_200_only_when_every_hard_check_is_green(client, monkeypatch):
    from server import main

    green = {
        "status": "ready",
        "ready": True,
        "checks": {"topology": {"ok": True, "state": "green"}},
    }
    red = {
        "status": "not_ready",
        "ready": False,
        "checks": {"topology": {"ok": False, "state": "red"}},
    }

    monkeypatch.setattr(main.readiness, "evaluate_runtime", lambda: green)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == green

    monkeypatch.setattr(main.readiness, "evaluate_runtime", lambda: red)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == red


def test_healthz_remains_liveness_only_when_readiness_is_red(client, monkeypatch):
    from server import main

    monkeypatch.setattr(
        main.readiness,
        "evaluate_runtime",
        lambda: {"status": "not_ready", "ready": False, "checks": {}},
    )

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
