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
        "MAX_SESSIONS_PER_USER": "1",
        "MAX_SESSIONS_GLOBAL": "1",
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
        "SKILLS_REF": "v1.2.3",
        "CLAUDE_CODE_VERSION": "2.1.228",
        "CODEX_CLI_VERSION": "0.144.6",
        "OMNIGENT_VERSION": "0.9.0",
        "DATABRICKS_CLI_VERSION": "1.8.0",
        "NODE_VERSION": "24.18.1",
        "PI_CLI_VERSION": "0.83.0",
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
                "claude": "2.1.228",
                "codex": "0.144.6",
                "databricks": "1.8.0",
                "node": "24.18.1",
                "pi": "0.83.0",
                "omnigent": "0.9.0",
            }.items()
        } | {
            "databricks_agent_skills": {
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
              delivery=None, gateway=None, writable=True, now=1000.0):
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
        delivery_status=delivery,
        gateway_status=gateway,
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
    hard = {
        name: check
        for name, check in report["checks"].items()
        if not check.get("soft")
    }
    assert set(hard) == {
        "topology",
        "attendee_identity",
        "credentials",
        "credential_durability",
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
    assert all(check["ok"] for check in hard.values())
    assert set(report["checks"]) - set(hard) == {
        "insight_capture",
        "model_gateway",
        "model_profile",
        "workspace_sync",
    }


def test_absent_gateway_is_reported_amber_and_never_blocks_the_workshop(tmp_path):
    """An unresolved gateway now means no model is reachable at all — the
    serving-endpoints surface this used to fall back to has been retired with
    the legacy endpoints it served. It stays soft anyway: gating a room out of a
    workshop is a worse outcome than letting them in to find out, and the check
    says which variable closes the gap."""
    report = _evaluate(tmp_path, gateway={"resolved": False, "source": "unresolved"})

    assert report["ready"] is True
    check = report["checks"]["model_gateway"]
    assert check["soft"] is True
    assert check["state"] == "amber"
    # Naming the levers is the point: an operator reading /readyz should not
    # have to grep the source to learn which variable to set. DATABRICKS_HOST
    # leads now, because the workspace-hosted gateway is derived from it and a
    # deployment missing it is missing the only thing that is not optional.
    assert "DATABRICKS_HOST" in check["detail"]
    assert "DATABRICKS_GATEWAY_HOST" in check["detail"]


def test_gateway_resolved_in_a_shape_omnigent_ignores_is_still_amber(tmp_path):
    """Resolving a URL is not the same as resolving one Omnigent will route
    through. A URL lacking both the ai-gateway DNS label and the /ai-gateway path
    leaves Omnigent deriving its own paths from the workspace host, so the
    configured value buys nothing and green would be a lie."""
    report = _evaluate(
        tmp_path,
        gateway={
            "resolved": True,
            "source": "explicit",
            "omnigent_gateway_form": False,
        },
    )

    check = report["checks"]["model_gateway"]
    assert check["state"] == "amber"
    assert check["ok"] is False
    assert report["ready"] is True


def test_gateway_resolved_in_omnigent_form_is_green(tmp_path):
    report = _evaluate(
        tmp_path,
        gateway={
            "resolved": True,
            "source": "explicit",
            "omnigent_gateway_form": True,
        },
    )

    check = report["checks"]["model_gateway"]
    assert check["state"] == "green"
    assert check["ok"] is True
    assert check["source"] == "explicit"


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


def test_a_workshop_without_omnigent_is_ready_without_it(tmp_path):
    """The operator deselected Omnigent, so the bootstrap never installed it.

    Requiring it anyway held the room out of admission for a harness it was
    never going to launch: nothing installed tmux or the harness, this check
    still demanded both, and Control Tower blocks on the 503.
    """
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    for step in ("tmux", "omnigent"):
        del installer["steps"][step]

    report = _evaluate(
        tmp_path,
        installer=installer,
        mutate_env=lambda env: env.update({"WORKSHOP_AGENTS": "claude,codex"}),
    )

    assert report["checks"]["installers"]["ok"] is True
    assert report["checks"]["installers"]["missing"] == []


def test_a_workshop_that_kept_omnigent_still_waits_for_it(tmp_path):
    """The selection narrows what is required; it does not excuse what is offered."""
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    del installer["steps"]["omnigent"]

    report = _evaluate(
        tmp_path,
        installer=installer,
        mutate_env=lambda env: env.update({"WORKSHOP_AGENTS": "omnigent,claude"}),
    )

    assert report["checks"]["installers"]["ok"] is False
    assert "omnigent" in report["checks"]["installers"]["missing"]


def test_readiness_reports_which_harnesses_this_instance_actually_has(tmp_path):
    """The Omnigent App advertises polly workers from its own container and
    cannot see this one. Pi is advisory here — the instance is ready without it
    — so the App has no way to know a pi worker would dispatch into nothing
    unless this instance says so."""
    _, _, installer, _, _, _ = _good_inputs(tmp_path)

    without_pi = _evaluate(tmp_path, installer=installer)
    installer["ready"]["pi"] = True
    with_pi = _evaluate(tmp_path, installer=installer)

    assert without_pi["checks"]["installers"]["harnesses"] == ["claude", "codex"]
    assert without_pi["checks"]["installers"]["ok"] is True
    assert with_pi["checks"]["installers"]["harnesses"] == ["claude", "codex", "pi"]


def test_degraded_skills_fail_readiness_and_are_named_separately(tmp_path):
    """A vendored-fallback skills install is usable but unreviewed. It must fail
    readiness, and it must not be reported as merely incomplete."""
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    installer["steps"]["skills"]["status"] = "degraded"

    report = _evaluate(tmp_path, installer=installer)
    installers = report["checks"]["installers"]

    assert installers["ok"] is False
    assert installers["degraded"] == ["skills"]
    assert "skills" in installers["missing"]
    assert "degraded fallback" in installers["detail"]


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
    # The reason travels with the verdict. Without it an operator sees only
    # "unhealthy" and cannot tell a missing grant from an unreachable catalog.
    assert unhealthy["checks"]["entitlements"]["last_error"] == "failed"


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


def test_entitlements_name_the_app_whose_catalog_grant_failed(tmp_path):
    """An operator watching a hundred instances needs the app, not "unhealthy".

    At servco the symptom was an attendee's app unable to read the catalog while
    the attendee themselves could, and ``last_error`` truncates to five messages
    across every resource type, so the app that is actually blocked can fall off
    the end of it.
    """
    blocked = _evaluate(
        tmp_path,
        entitlements={
            "enabled": True,
            "catalog": "workshop_alice",
            "ok": False,
            "thread_alive": True,
            "last_verified_at": 900.0,
            "verified_email": "alice@example.com",
            "verified_catalog": "workshop_alice",
            "verification_source": "background",
            "last_error": "something else entirely",
            "handoff": {
                "summary": {},
                "details": [
                    {
                        "resource_type": "app-service-principals",
                        "resource_id": "showroom",
                        "state": "failed",
                        "error": "403 PERMISSION_DENIED",
                    },
                    {
                        "resource_type": "app-service-principals",
                        "resource_id": "loyalty",
                        "state": "handed_off",
                        "error": None,
                    },
                    {"resource_type": "jobs", "resource_id": "11", "state": "failed"},
                ],
            },
        },
    )

    check = blocked["checks"]["entitlements"]
    assert check["ok"] is False
    assert check["app_service_principals"]["failed"] == [
        {"app": "showroom", "error": "403 PERMISSION_DENIED"}
    ]
    assert check["app_service_principals"]["granted"] == ["loyalty"]
    assert "showroom" in check["detail"]


def test_entitlements_report_app_grants_clean_when_none_failed(tmp_path):
    healthy = _evaluate(tmp_path)

    assert healthy["checks"]["entitlements"]["ok"] is True
    assert healthy["checks"]["entitlements"]["app_service_principals"] == {
        "granted": [],
        "failed": [],
        "ok": True,
    }


def test_obo_says_which_condition_failed(tmp_path):
    """"Required scopes are missing" was printed when none were.

    Every servco instance reported that after the event, with an empty missing
    list, because the real cause was a validation older than the max age. It
    sent the post-mortem looking for a scope misconfiguration that did not
    exist, and an operator triaging a live instance would lose the same time.
    """
    good_obo = _good_inputs(tmp_path)[4]

    stale = _evaluate(tmp_path, obo={**good_obo, "validated_at": 1.0})
    unverified = _evaluate(
        tmp_path, obo={**good_obo, "validation_state": "pending"}
    )
    short_scopes = _evaluate(
        tmp_path,
        obo={**good_obo, "verified_scopes": ["sql"]},
    )

    assert "stale" in stale["checks"]["obo"]["detail"]
    assert stale["checks"]["obo"]["missing"] == []
    assert "pending" in unverified["checks"]["obo"]["detail"]
    # And when scopes genuinely are missing, it still says so — and names them.
    assert "catalog.tables:read" in short_scopes["checks"]["obo"]["detail"]


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


def test_a_fresh_instance_is_only_red_on_the_check_its_attendee_must_turn_green(
    tmp_path,
):
    """The admission contract, from Control Tower's side.

    Scope verification needs a real attendee token, and one arrives only when a
    browser forwards it — so a perfectly provisioned instance nobody has opened
    is red on ``obo`` and green everywhere else. A Control Tower that took the
    documented "poll until 200" literally would therefore fail every OBO unit at
    provisioning, before the attendee it is waiting for could possibly exist.

    Two things make that answerable rather than a judgement call in the other
    repo: ``obo`` is the *only* hard check in this state, and it says so on
    itself. CT blocks on hard reds that are not ``attendee_dependent``.
    """
    _, _, _, _, good_obo, _ = _good_inputs(tmp_path)

    report = _evaluate(
        tmp_path,
        obo={
            **good_obo,
            "present": False,
            "fresh": False,
            "observed_scopes": [],
            "verified_scopes": [],
            "validation_state": "pending",
            "validated_at": None,
        },
    )

    blocking = [
        name
        for name, check in report["checks"].items()
        if not check.get("soft")
        and not check["ok"]
        and not check.get("attendee_dependent")
    ]
    assert blocking == [], blocking
    assert report["checks"]["obo"]["attendee_dependent"] is True
    assert "no attendee has opened this instance yet" in report["checks"]["obo"]["detail"]
    # Still not `ready`: an instance whose attendee has arrived and whose OBO is
    # broken must fail, and one bit cannot say both things.
    assert report["ready"] is False


def test_a_disabled_or_misscoped_obo_says_which_it_is(tmp_path):
    """"OBO is disabled or required scopes are missing" made an operator check
    both, and neither answer was in the report."""
    disabled = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"ENABLE_OBO": "false"}),
    )
    misscoped = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"OBO_SCOPES": "sql"}),
    )

    assert disabled["checks"]["obo"]["detail"] == "OBO is disabled"
    assert misscoped["checks"]["obo"]["detail"].startswith(
        "required scopes are missing: "
    )


def test_release_pins_require_fixed_ref_cli_versions_and_models(tmp_path):
    branch_tip = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"SKILLS_REF": "main"}),
    )
    missing = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"CLAUDE_CODE_VERSION": ""}),
    )

    assert branch_tip["checks"]["release_pins"]["ok"] is False
    assert "SKILLS_REF" in branch_tip["checks"]["release_pins"]["missing"]
    assert missing["checks"]["release_pins"]["ok"] is False
    assert "CLAUDE_CODE_VERSION" in missing["checks"]["release_pins"]["missing"]


def test_every_binary_boot_installs_has_to_be_pinned(tmp_path):
    """Node and pi count as release inputs, same as the CLIs they run.

    Node arrived as a pin nobody checked, and pi shipped later on the same
    footing: an event could deploy with either unset, install whatever the
    installer defaulted to that week, and pass readiness. The versions attendees
    run are the reproducibility claim, so the check has to cover all of them.
    """
    for name in ("NODE_VERSION", "PI_CLI_VERSION"):
        report = _evaluate(
            tmp_path, mutate_env=lambda env, n=name: env.update({n: ""})
        )
        pins = report["checks"]["release_pins"]
        assert pins["ok"] is False, name
        assert name in pins["missing"], name


def test_a_raised_pin_without_a_reinstall_is_a_mismatch_not_a_green(tmp_path):
    """A pin is a claim about the running terminal, so it needs a witness.

    Raising a version in the deployment does not reinstall anything: the binary
    on disk stays where it was. Without pairing each pin against the installed
    version, /readyz would report the new number while attendees ran the old one.
    """
    for env_name, tool in (("NODE_VERSION", "node"), ("PI_CLI_VERSION", "pi")):
        report = _evaluate(
            tmp_path,
            mutate_env=lambda env, n=env_name: env.update({n: "99.99.99"}),
        )
        pins = report["checks"]["release_pins"]
        assert pins["ok"] is False, tool
        assert tool in pins["mismatched"], tool
        assert report["release_manifest"][tool]["match"] is False, tool


def test_pi_is_only_a_required_pin_when_omnigent_is_on(tmp_path):
    """Pi is installed for Omnigent alone, so it is a pin for Omnigent alone."""
    report = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update(
            {"OMNIGENT_ENABLED": "false", "PI_CLI_VERSION": "", "OMNIGENT_VERSION": ""}
        ),
    )

    missing = report["checks"]["release_pins"]["missing"]
    assert "PI_CLI_VERSION" not in missing
    assert "OMNIGENT_VERSION" not in missing


def test_a_model_pin_is_reported_but_never_required(tmp_path):
    """An event that names a cost posture instead of a model is fully configured.

    Requiring ANTHROPIC_MODEL would put a copy of a chain head into every
    deployment to go stale there, which is the drift server/models.py removed.
    What an operator does need is to see whether a pin is in play, because a
    pin and a profile can disagree on purpose.
    """
    unpinned = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update({"ANTHROPIC_MODEL": "", "CODEX_MODEL": ""}),
    )
    pinned = _evaluate(
        tmp_path,
        mutate_env=lambda env: env.update(
            {"WORKSHOP_MODEL_PROFILE": "economy", "ANTHROPIC_MODEL": "custom-endpoint"}
        ),
    )

    assert unpinned["checks"]["release_pins"]["ok"] is True
    assert unpinned["checks"]["model_profile"]["pins"] == {}
    assert pinned["checks"]["model_profile"]["profile"] == "economy"
    assert pinned["checks"]["model_profile"]["pins"] == {
        "ANTHROPIC_MODEL": "custom-endpoint"
    }
    assert pinned["checks"]["model_profile"]["ok"] is True


def test_release_pins_require_installed_versions_to_match(tmp_path):
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    installer["release_manifest"]["claude"].update(
        {"actual": "2.1.215", "match": False}
    )

    report = _evaluate(tmp_path, installer=installer)

    assert report["checks"]["release_pins"]["ok"] is False
    assert "claude" in report["checks"]["release_pins"]["mismatched"]
    assert report["release_manifest"]["claude"]["expected"] == "2.1.228"
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


def test_release_pins_require_fetched_skills_ref_not_vendored_fallback(tmp_path):
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    installer["release_manifest"]["databricks_agent_skills"].update(
        {
            "actual": None,
            "match": False,
            "source": "vendored_fallback",
            "resolved_commit": None,
        }
    )

    report = _evaluate(tmp_path, installer=installer)

    assert report["checks"]["release_pins"]["ok"] is False
    assert "databricks_agent_skills" in report["checks"]["release_pins"]["mismatched"]
    assert report["release_manifest"]["databricks_agent_skills"]["source"] == "vendored_fallback"


def test_release_pins_accept_an_unset_skills_ref_because_the_repo_owns_it(tmp_path):
    """CT sets no skills ref; the reviewed tag lives in the repo's manifest."""
    unset = _evaluate(
        tmp_path, mutate_env=lambda env: env.update({"SKILLS_REF": ""})
    )
    disagreeing = _evaluate(
        tmp_path, mutate_env=lambda env: env.update({"SKILLS_REF": "v9.9.9"})
    )

    assert unset["checks"]["release_pins"]["ok"] is True
    assert unset["release_manifest"]["databricks_agent_skills"]["expected"] == "v1.2.3"
    # Setting it still has to agree with what bootstrap actually installed.
    assert disagreeing["checks"]["release_pins"]["ok"] is False
    assert (
        "databricks_agent_skills"
        in disagreeing["checks"]["release_pins"]["mismatched"]
    )


def test_release_pins_accept_verified_prewarmed_skills(tmp_path):
    _, _, installer, _, _, _ = _good_inputs(tmp_path)
    installer["release_manifest"]["databricks_agent_skills"]["source"] = "prewarmed"

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


def _capture_env(**overrides):
    def mutate(env):
        env.update(
            {
                "WORKSHOP_INSIGHT_CAPTURE": "true",
                "CONTROL_TOWER_INGEST_URL": "https://ct.example.com",
                "CONTROL_TOWER_INGEST_TOKEN": "ingest-token",
                "WORKSHOP_RUN_ID": "run-1",
                **overrides,
            }
        )

    return mutate


def test_insight_capture_is_off_by_default_and_reported_as_amber(tmp_path):
    report = _evaluate(tmp_path)
    check = report["checks"]["insight_capture"]

    assert check["state"] == "amber"
    assert check["detail"] == "insight capture is off"
    assert check["requested"] == "off"
    assert check["discovery"] is False
    assert report["release_manifest"]["insight_capture"] == {
        "enabled": False,
        "expected": "off",
        "actual": "off",
        "match": True,
        "delivery": "pull",
    }


def test_insight_capture_reports_both_tiers_when_delivering(tmp_path):
    both = _evaluate(tmp_path, mutate_env=_capture_env())
    signal_only = _evaluate(
        tmp_path, mutate_env=_capture_env(DISCOVERY_ENABLED="false")
    )

    assert both["checks"]["insight_capture"]["state"] == "green"
    assert both["checks"]["insight_capture"]["discovery"] is True
    assert both["release_manifest"]["insight_capture"]["actual"] == "signal+discovery"
    # The behavioural rollup without the conversational tier is a supported
    # posture, not a half-configured one.
    assert signal_only["checks"]["insight_capture"]["state"] == "green"
    assert signal_only["checks"]["insight_capture"]["discovery"] is False
    assert signal_only["release_manifest"]["insight_capture"]["actual"] == "signal"


def test_capture_without_push_configuration_is_the_normal_posture(tmp_path):
    """Not a misconfiguration: Control Tower deploys terminals with no ingest
    settings at all and collects the buffer on its harvest instead. Reporting this
    as red — which it did while push looked like the only route — would have every
    real workshop showing a red check for the feature that was working."""
    report = _evaluate(
        tmp_path, mutate_env=_capture_env(CONTROL_TOWER_INGEST_TOKEN="")
    )
    check = report["checks"]["insight_capture"]

    assert check["ok"] is True
    assert check["state"] == "green"
    assert check["delivery"] == "pull"
    assert check["push_configured"] is False
    assert report["release_manifest"]["insight_capture"]["actual"] == "signal+discovery"


def test_capture_reports_whether_anything_has_actually_collected(tmp_path):
    """The one fact configuration can't establish.

    Under pull a delivery path always exists, so "is it working" can only be
    answered by whether Control Tower has used it. An operator checking mid-event
    needs to tell "nobody has come for these yet" from "they are being taken".
    """
    fresh = _evaluate(
        tmp_path,
        mutate_env=_capture_env(CONTROL_TOWER_INGEST_URL=""),
        delivery={"delivery": "pull", "collections": 0, "pending": 4, "dropped": 0},
    )
    collecting = _evaluate(
        tmp_path,
        mutate_env=_capture_env(CONTROL_TOWER_INGEST_URL=""),
        delivery={"delivery": "pull", "collections": 7, "pending": 2, "dropped": 0},
    )

    assert "awaiting Control Tower's first collection" in fresh["checks"]["insight_capture"]["detail"]
    assert fresh["checks"]["insight_capture"]["collected"] is False
    assert "collected 7 times" in collecting["checks"]["insight_capture"]["detail"]
    assert collecting["checks"]["insight_capture"]["collected"] is True


def test_dropped_events_are_red_but_never_gate(tmp_path):
    """Real, unrecoverable loss: the buffer is bounded, so a collector that stops
    collecting eventually costs events no later harvest can recover. This is the
    condition worth alarming on, and it still must not take a workshop down."""
    report = _evaluate(
        tmp_path,
        mutate_env=_capture_env(),
        delivery={"delivery": "pull", "collections": 3, "pending": 5000, "dropped": 42},
    )
    check = report["checks"]["insight_capture"]

    assert check["ok"] is False
    assert check["state"] == "red"
    assert "42 events were dropped" in check["detail"]
    # The whole point of the soft flag: a broken commercial feature must not take
    # an attendee's workshop down with it.
    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["release_manifest"]["insight_capture"] == {
        "enabled": True,
        "expected": "signal+discovery",
        "actual": "lossy",
        "match": False,
        "delivery": "pull",
    }


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
