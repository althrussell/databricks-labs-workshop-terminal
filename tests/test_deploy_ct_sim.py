import json
import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import deploy_ct_sim


APP_YAML = Path(__file__).parents[1] / "app.yaml"


def _valid_argv():
    return [
        "--profile", "event-profile",
        "--attendee", "attendee@example.com",
        "--skills-ref", "v1.2.3",
        "--anthropic-model", "databricks-claude-sonnet-5",
        "--codex-model", "databricks-gpt-5-6-codex",
    ]


def _env_values(content):
    document = yaml.safe_load(content)
    return {item["name"]: item.get("value", "") for item in document["env"]}


def test_uploaded_yaml_patch_sets_current_event_contract_without_mutating_repo():
    original = APP_YAML.read_bytes()
    settings = deploy_ct_sim._event_settings(
        attendee="attendee@example.com",
        catalog="event_catalog",
        scopes="catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql",
        admin_group="event_admins",
        skills_ref="v1.2.3",
        anthropic_model="databricks-claude-sonnet-5",
        codex_model="databricks-gpt-5-6-codex",
        claude_code_version="2.1.216",
        codex_cli_version="0.144.6",
        databricks_cli_version="1.8.0",
        omnigent_version="0.7.0",
        node_version="24.18.1",
        pi_cli_version="0.83.0",
        gateway_host="https://dbc-af3ed11d-d267.cloud.databricks.com/ai-gateway",
        workshop_pat="",
    )

    patched = deploy_ct_sim._patch_app_yaml(original, settings)
    values = _env_values(patched)

    assert {
        key: values[key]
        for key in settings
    } == settings
    assert values["WORKSHOP_PAT"] == ""
    assert APP_YAML.read_bytes() == original


def test_every_setting_the_simulation_patches_is_declared_in_app_yaml():
    """The patcher rewrites declared keys; it does not add them.

    So a contract variable added to the simulation but never declared here fails
    at deploy time against a real workspace, which is a slow and expensive place
    to find out. This is the cheap version of that discovery.
    """
    args = deploy_ct_sim._parse_args(_valid_argv())
    settings = deploy_ct_sim._event_settings_from_args(args, args.attendee, "")

    undeclared = sorted(set(settings) - set(_env_values(APP_YAML.read_bytes())))

    assert undeclared == [], (
        f"deploy_ct_sim patches {undeclared} but app.yaml does not declare them — "
        "add the entry, with the comment explaining what Control Tower sets it to"
    )


def _gateway_error(value):
    """Return the parser error for a --gateway-host value, or None if accepted."""
    with pytest.raises(SystemExit):
        deploy_ct_sim._parse_args(_valid_argv() + ["--gateway-host", value])


def test_the_gateway_host_is_optional_because_its_absence_is_a_degradation_not_a_break():
    """Without it every CLI falls back to <host>/serving-endpoints and the
    workshop still runs on Claude and Codex. What it costs is Pi's gateway-only
    models, which Workshop Terminal reports as a soft readiness check."""
    args = deploy_ct_sim._parse_args(_valid_argv())
    settings = deploy_ct_sim._event_settings_from_args(args, args.attendee, "")

    assert settings["DATABRICKS_GATEWAY_HOST"] == ""


def test_the_workspace_hosted_gateway_form_is_accepted():
    """The preferred shape: the gateway hostname IS the workspace hostname, so
    Omnigent never has to fall back to resolving ~/.databrickscfg."""
    args = deploy_ct_sim._parse_args(
        _valid_argv()
        + ["--gateway-host", "https://dbc-af3ed11d-d267.cloud.databricks.com/ai-gateway"]
    )
    settings = deploy_ct_sim._event_settings_from_args(args, args.attendee, "")

    assert settings["DATABRICKS_GATEWAY_HOST"].endswith("/ai-gateway")


def test_a_dedicated_ai_gateway_subdomain_is_also_accepted():
    """Omnigent routes it, so refusing it here would be stricter than upstream —
    it is just the more fragile of the two shapes."""
    deploy_ct_sim._parse_args(
        _valid_argv()
        + ["--gateway-host", "https://1234567890.ai-gateway.cloud.databricks.com"]
    )


def test_a_gateway_host_that_omnigent_would_ignore_is_rejected():
    """A workspace root with no ai-gateway label or path is the failure that costs
    a workshop its gateway-only models while every log stays clean, so it has to
    fail here instead."""
    _gateway_error("https://dbc-af3ed11d-d267.cloud.databricks.com")


def test_a_non_databricks_gateway_host_is_rejected():
    """Omnigent gates on a trusted host suffix before it will route a base URL as
    the gateway, and the attendee bearer would otherwise be pointed off-platform."""
    _gateway_error("https://evil.example.com/ai-gateway")


def test_a_plain_http_gateway_host_is_rejected():
    _gateway_error("http://dbc-af3ed11d-d267.cloud.databricks.com/ai-gateway")


def test_a_gateway_host_carrying_a_provider_suffix_is_rejected():
    """The terminal appends the provider itself — /anthropic for Claude, /codex/v1
    for the OpenAI-completions models GLM routes through. Handed a host that
    already carries one, it would build .../anthropic/anthropic and route nowhere.
    The subdomain form is the trap: it satisfies the ai-gateway label check while
    carrying the suffix, so the label check alone cannot catch it."""
    _gateway_error("https://1234567890.ai-gateway.cloud.databricks.com/anthropic")
    _gateway_error(
        "https://dbc-af3ed11d-d267.cloud.databricks.com/ai-gateway/anthropic"
    )


def test_the_pi_cli_version_reaches_the_deployment_env():
    """It sits in EXACT_DEFAULTS, but a default that is never patched into the
    app's env leaves the installer pinning whatever the image happened to ship."""
    args = deploy_ct_sim._parse_args(_valid_argv())
    settings = deploy_ct_sim._event_settings_from_args(args, args.attendee, "")

    assert settings["PI_CLI_VERSION"] == deploy_ct_sim.EXACT_DEFAULTS["pi_cli_version"]


def test_a_floating_pi_cli_version_is_rejected():
    """Every other CLI pin is validated as an exact semver; an unvalidated one
    would let a workshop drift onto an untested Pi mid-event."""
    with pytest.raises(SystemExit):
        deploy_ct_sim._parse_args(_valid_argv() + ["--pi-cli-version", "latest"])


def test_uploaded_yaml_patch_preserves_comments_order_and_unrelated_bytes():
    source = b"""# leading comment
command: [python, app.py]
env:
  # target comment
  - name: TARGET
    note: keep-this-order
    value: "old"  # keep-inline
  - name: OTHER
    value: untouched
# trailing comment
"""

    patched = deploy_ct_sim._patch_app_yaml(source, {"TARGET": "new # value"})

    assert patched == source.replace(b'"old"', b'"new # value"')
    assert yaml.safe_load(patched)["env"][0]["value"] == "new # value"


def test_second_stage_app_sp_patch_reuploads_uploaded_yaml_before_deploy():
    uploads = []
    workspace = SimpleNamespace(
        workspace=SimpleNamespace(
            upload=lambda path, content, **kwargs: uploads.append(
                (path, content.read(), kwargs)
            )
        )
    )
    original = deploy_ct_sim._patch_app_yaml(
        APP_YAML.read_bytes(), {"WORKSHOP_APP_SP_ID": ""}
    )

    patched = deploy_ct_sim._patch_uploaded_app_yaml_with_sp_id(
        workspace,
        "/Workspace/Users/deployer/apps/event-app",
        "AUTO",
        original,
        SimpleNamespace(service_principal_id=12345),
    )

    assert _env_values(patched)["WORKSHOP_APP_SP_ID"] == "12345"
    assert uploads == [
        (
            "/Workspace/Users/deployer/apps/event-app/app.yaml",
            patched,
            {"format": "AUTO", "overwrite": True},
        )
    ]


@pytest.mark.parametrize("service_principal_id", [None, "", "sp-123"])
def test_second_stage_app_sp_patch_rejects_missing_or_non_numeric_id(
    service_principal_id,
):
    workspace = SimpleNamespace(
        workspace=SimpleNamespace(
            upload=lambda *args, **kwargs: pytest.fail("must not upload invalid ID")
        )
    )

    with pytest.raises(RuntimeError, match="numeric service_principal_id"):
        deploy_ct_sim._patch_uploaded_app_yaml_with_sp_id(
            workspace,
            "/Workspace/Users/deployer/apps/event-app",
            "AUTO",
            APP_YAML.read_bytes(),
            SimpleNamespace(service_principal_id=service_principal_id),
        )


def test_parser_defaults_to_no_static_pat():
    args = deploy_ct_sim._parse_args([
        "--profile", "event-profile",
        "--attendee", "attendee@example.com",
        "--skills-ref", "v1.2.3",
        "--anthropic-model", "databricks-claude-sonnet-5",
        "--codex-model", "databricks-gpt-5-6-codex",
    ])

    assert args.with_emergency_pat is False


def test_event_mode_rejects_emergency_pat_before_workspace_mutation():
    with pytest.raises(SystemExit):
        deploy_ct_sim._parse_args(_valid_argv() + ["--with-emergency-pat"])

    args = deploy_ct_sim._parse_args(
        _valid_argv() + ["--non-event-mode", "--with-emergency-pat"]
    )
    assert args.with_emergency_pat is True


def test_parser_captures_explicit_existing_catalog_provenance():
    args = deploy_ct_sim._parse_args(
        _valid_argv()
        + [
            "--catalog-existing-owner", "fevm-owner",
            "--catalog-existing-creator", "fevm-creator",
            "--catalog-existing-type", "MANAGED_CATALOG",
            "--catalog-existing-isolation-mode", "ISOLATED",
            "--catalog-existing-storage-root", "s3://dedicated/catalog",
        ]
    )

    assert deploy_ct_sim._catalog_provenance_from_args(args) == {
        "owner": "fevm-owner",
        "creator": "fevm-creator",
        "catalog_type": "MANAGED_CATALOG",
        "isolation_mode": "ISOLATED",
        "storage_root": "s3://dedicated/catalog",
    }


def test_existing_catalog_provenance_requires_all_fields_or_none():
    args = deploy_ct_sim._parse_args(
        _valid_argv() + ["--catalog-existing-owner", "fevm-owner"]
    )

    with pytest.raises(ValueError, match="all provenance fields"):
        deploy_ct_sim._catalog_provenance_from_args(args)


def test_existing_catalog_provenance_supports_explicit_null_storage_root():
    args = deploy_ct_sim._parse_args(
        _valid_argv()
        + [
            "--catalog-existing-owner", "fevm-owner",
            "--catalog-existing-creator", "fevm-creator",
            "--catalog-existing-type", "MANAGED_CATALOG",
            "--catalog-existing-isolation-mode", "ISOLATED",
            "--catalog-existing-storage-root", "null",
        ]
    )

    assert deploy_ct_sim._catalog_provenance_from_args(args)["storage_root"] is None


def test_existing_catalog_rejects_missing_api_metadata_even_with_provenance():
    info = SimpleNamespace(
        owner="fevm-owner",
        created_by=None,
        catalog_type="MANAGED_CATALOG",
        isolation_mode="ISOLATED",
        storage_root=None,
    )
    provenance = {
        "owner": "fevm-owner",
        "creator": "fevm-creator",
        "catalog_type": "MANAGED_CATALOG",
        "isolation_mode": "ISOLATED",
        "storage_root": None,
    }

    with pytest.raises(RuntimeError, match="missing creator"):
        deploy_ct_sim._require_catalog_provenance(info, provenance)


@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "feature/event",
        "refs/heads/main",
        "refs/tags/v1.2.3",
        "release-1.2.3",
        "v1.2",
        "v01.2.3",
        "a" * 39,
        "https://example.com/repo.git#v1.2.3",
        " v1.2.3",
    ],
)
def test_skills_ref_rejects_mutable_or_nonconforming_refs(ref):
    argv = _valid_argv()
    argv[argv.index("--skills-ref") + 1] = ref

    with pytest.raises(SystemExit):
        deploy_ct_sim._parse_args(argv)


@pytest.mark.parametrize(
    "ref",
    [
        "a" * 40,
        "A" * 40,
        "v1.2.3",
        "v1.2.3-rc.1+event.5",
    ],
)
def test_skills_ref_accepts_only_full_sha_or_strict_version_tag(ref):
    argv = _valid_argv()
    argv[argv.index("--skills-ref") + 1] = ref

    assert deploy_ct_sim._parse_args(argv).skills_ref == ref


@pytest.mark.parametrize(
    "value",
    [
        "latest",
        "stable",
        "^1.2.3",
        "~1.2.3",
        ">=1.2.3",
        "1.2.x",
        "1.2.*",
        "v1.2.3",
        "01.2.3",
        "1.2.3-01",
        "1.2.3 ",
        "1.2.3\u0663",
        "https://example.com/tool-1.2.3.tgz",
    ],
)
@pytest.mark.parametrize(
    "flag",
    [
        "--claude-code-version",
        "--codex-cli-version",
        "--omnigent-version",
        "--databricks-cli-version",
        "--node-version",
    ],
)
def test_tool_versions_reject_floating_ranges_urls_and_invalid_semver(flag, value):
    with pytest.raises(SystemExit):
        deploy_ct_sim._parse_args(_valid_argv() + [flag, value])


@pytest.mark.parametrize(
    "flag",
    [
        "--claude-code-version",
        "--codex-cli-version",
        "--omnigent-version",
        "--databricks-cli-version",
        "--node-version",
    ],
)
def test_tool_versions_accept_exact_semver_with_prerelease_and_build(flag):
    args = deploy_ct_sim._parse_args(
        _valid_argv() + [flag, "1.2.3-rc.1+event.5"]
    )

    assert getattr(args, flag[2:].replace("-", "_")) == "1.2.3-rc.1+event.5"


@pytest.mark.parametrize(
    "flag",
    ["--anthropic-model", "--codex-model"],
)
@pytest.mark.parametrize(
    "value",
    [
        "latest",
        "stable",
        "claude",
        "codex",
        "endpoint-latest",
        "databricks-claude-sonnet",
        "model*",
        "model@latest",
        " model-1",
        "https://example.com/model-1",
    ],
)
def test_model_names_reject_floating_or_non_endpoint_values(flag, value):
    argv = _valid_argv()
    argv[argv.index(flag) + 1] = value

    with pytest.raises(SystemExit):
        deploy_ct_sim._parse_args(argv)


@pytest.mark.parametrize("flag", ["--no-obo", "--no-entitlements"])
def test_event_mode_rejects_disabled_required_features(flag):
    with pytest.raises(SystemExit):
        deploy_ct_sim._parse_args([
            "--profile", "event-profile",
            "--attendee", "attendee@example.com",
            "--skills-ref", "v1.2.3",
            "--anthropic-model", "databricks-claude-sonnet-5",
            "--codex-model", "databricks-gpt-5-6-codex",
            flag,
        ])

    args = deploy_ct_sim._parse_args([
        "--profile", "event-profile",
        "--attendee", "attendee@example.com",
        "--skills-ref", "v1.2.3",
        "--anthropic-model", "databricks-claude-sonnet-5",
        "--codex-model", "databricks-gpt-5-6-codex",
        "--non-event-mode",
        flag,
    ])
    assert args.non_event_mode is True


def test_event_mode_requires_all_baseline_obo_scopes_and_allows_extras():
    missing_sql = deploy_ct_sim.DEFAULT_SCOPES.rsplit(",", 1)[0]
    with pytest.raises(SystemExit):
        deploy_ct_sim._parse_args([
            "--profile", "event-profile",
            "--attendee", "attendee@example.com",
            "--skills-ref", "v1.2.3",
            "--anthropic-model", "databricks-claude-sonnet-5",
            "--codex-model", "databricks-gpt-5-6-codex",
            "--scopes", missing_sql,
        ])

    requested = deploy_ct_sim._parse_scopes(
        deploy_ct_sim.DEFAULT_SCOPES + ",files"
    )
    assert deploy_ct_sim._applied_scopes_include(
        requested,
        requested + ["workspace.workspace"],
    )
    assert not deploy_ct_sim._applied_scopes_include(
        requested,
        requested[:-1],
    )
    with pytest.raises(RuntimeError, match="missing requested OBO scopes"):
        deploy_ct_sim._require_applied_scopes(
            SimpleNamespace(user_api_scopes=requested[:-1]),
            requested,
        )


def test_profile_is_required_or_loaded_from_environment():
    base = [
        "--attendee", "attendee@example.com",
        "--skills-ref", "v1.2.3",
        "--anthropic-model", "databricks-claude-sonnet-5",
        "--codex-model", "databricks-gpt-5-6-codex",
    ]
    with pytest.raises(SystemExit):
        deploy_ct_sim._parse_args(base, environ={})

    args = deploy_ct_sim._parse_args(
        base,
        environ={"DATABRICKS_CONFIG_PROFILE": "from-env"},
    )
    assert args.profile == "from-env"


def test_workspace_client_receives_only_explicit_profile():
    calls = []

    def workspace_client(**kwargs):
        calls.append(kwargs)
        return object()

    deploy_ct_sim._make_workspace_client(workspace_client, "event-profile")

    assert calls == [{"profile": "event-profile"}]


def test_catalog_grant_plan_is_catalog_scoped_and_least_privilege():
    statements = deploy_ct_sim._catalog_sql_plan(
        "event_catalog",
        "attendee@example.com",
        "app-client-id",
    )

    assert statements == [
        "CREATE CATALOG IF NOT EXISTS `event_catalog` "
        "COMMENT 'Workshop CT-sim per-attendee catalog'",
        "GRANT ALL PRIVILEGES ON CATALOG `event_catalog` TO `attendee@example.com`",
        "GRANT MANAGE ON CATALOG `event_catalog` TO `attendee@example.com`",
        "GRANT MANAGE, USE CATALOG, CREATE SCHEMA ON CATALOG `event_catalog` "
        "TO `app-client-id`",
        "ALTER CATALOG `event_catalog` OWNER TO `attendee@example.com`",
    ]
    assert all("METASTORE" not in statement for statement in statements)


def test_existing_catalog_without_explicit_provenance_fails_closed():
    statements = []
    workspace = SimpleNamespace(
        catalogs=SimpleNamespace(
            get=lambda name: SimpleNamespace(
                name=name,
                owner="fevm-owner",
                created_by="fevm-creator",
                catalog_type="MANAGED_CATALOG",
                isolation_mode="ISOLATED",
                storage_root="s3://dedicated/catalog",
            ),
        ),
        warehouses=SimpleNamespace(
            list=lambda: [SimpleNamespace(id="warehouse", enable_serverless_compute=True)]
        ),
    )

    def execute(statement, warehouse_id, wait_timeout):
        statements.append(statement)
        return SimpleNamespace(status=SimpleNamespace(state="SUCCEEDED", error=None))

    workspace.statement_execution = SimpleNamespace(execute_statement=execute)

    assert deploy_ct_sim._provision_catalog(
        workspace,
        "event_catalog",
        "attendee@example.com",
        "app-client-id",
    ) is False
    assert statements == []


def test_existing_catalog_exact_provenance_skips_create_and_verifies_grants():
    statements = []
    catalog_reads = [0]
    catalog = SimpleNamespace(
        name="event_catalog",
        owner="fevm-owner",
        created_by="fevm-creator",
        catalog_type="MANAGED_CATALOG",
        isolation_mode="ISOLATED",
        storage_root="s3://dedicated/catalog",
    )
    assignments = [
        SimpleNamespace(
            principal="attendee@example.com",
            privileges=["ALL_PRIVILEGES", "MANAGE"],
        ),
        SimpleNamespace(
            principal="app-client-id",
            privileges=["MANAGE", "USE_CATALOG", "CREATE_SCHEMA"],
        ),
    ]

    def get_catalog(name):
        catalog_reads[0] += 1
        if catalog_reads[0] > 1:
            catalog.owner = "attendee@example.com"
        return catalog

    workspace = SimpleNamespace(
        catalogs=SimpleNamespace(get=get_catalog),
        grants=SimpleNamespace(
            get=lambda securable_type, full_name: SimpleNamespace(
                privilege_assignments=assignments
            )
        ),
        warehouses=SimpleNamespace(
            list=lambda: [SimpleNamespace(id="warehouse", enable_serverless_compute=True)]
        ),
    )

    def execute(statement, warehouse_id, wait_timeout):
        statements.append(statement)
        return SimpleNamespace(status=SimpleNamespace(state="SUCCEEDED", error=None))

    workspace.statement_execution = SimpleNamespace(execute_statement=execute)

    assert deploy_ct_sim._provision_catalog(
        workspace,
        "event_catalog",
        "attendee@example.com",
        "app-client-id",
        provenance={
            "owner": "fevm-owner",
            "creator": "fevm-creator",
            "catalog_type": "MANAGED_CATALOG",
            "isolation_mode": "ISOLATED",
            "storage_root": "s3://dedicated/catalog",
        },
    ) is True
    assert not any(statement.startswith("CREATE CATALOG") for statement in statements)
    assert statements == [
        "GRANT ALL PRIVILEGES ON CATALOG `event_catalog` TO `attendee@example.com`",
        "GRANT MANAGE ON CATALOG `event_catalog` TO `attendee@example.com`",
        "GRANT MANAGE, USE CATALOG, CREATE SCHEMA ON CATALOG `event_catalog` "
        "TO `app-client-id`",
        "ALTER CATALOG `event_catalog` OWNER TO `attendee@example.com`",
    ]


def test_existing_catalog_metadata_mismatch_causes_no_mutation():
    statements = []
    workspace = SimpleNamespace(
        catalogs=SimpleNamespace(
            get=lambda name: SimpleNamespace(
                name=name,
                owner="someone-else",
                created_by="fevm",
                catalog_type="MANAGED_CATALOG",
                isolation_mode="ISOLATED",
                storage_root="s3://dedicated/catalog",
            )
        ),
        warehouses=SimpleNamespace(
            list=lambda: [SimpleNamespace(id="warehouse", enable_serverless_compute=True)]
        ),
        statement_execution=SimpleNamespace(
            execute_statement=lambda **kwargs: statements.append(kwargs)
        ),
    )

    assert deploy_ct_sim._provision_catalog(
        workspace,
        "event_catalog",
        "attendee@example.com",
        "app-client-id",
        provenance={
            "owner": "expected-owner",
            "creator": "fevm",
            "catalog_type": "MANAGED_CATALOG",
            "isolation_mode": "ISOLATED",
            "storage_root": "s3://dedicated/catalog",
        },
    ) is False
    assert statements == []


def test_post_deploy_success_requires_healthy_direct_oauth(monkeypatch):
    responses = iter(
        [
            SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "credential": {
                        "state": "rotating",
                        "source": "app_identity_oauth",
                        "healthy": True,
                    }
                },
            ),
            SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "credential": {
                        "state": "degraded",
                        "source": "emergency_workshop_pat",
                        "healthy": False,
                    }
                },
            ),
        ]
    )
    calls = []
    monkeypatch.setattr(
        deploy_ct_sim.requests,
        "get",
        lambda url, **kwargs: calls.append((url, kwargs)) or next(responses),
    )
    workspace = SimpleNamespace(
        config=SimpleNamespace(
            authenticate=lambda: {"Authorization": "Bearer deployer-oauth"}
        )
    )

    assert deploy_ct_sim._verify_direct_oauth(workspace, "https://app.test") is True
    assert deploy_ct_sim._verify_direct_oauth(workspace, "https://app.test") is False
    assert calls[0][0] == "https://app.test/api/admin/presence"


def test_post_deploy_oauth_check_retries_during_runtime_startup(monkeypatch):
    states = iter(["unknown", "rotating"])
    sleeps = []
    monkeypatch.setattr(
        deploy_ct_sim.requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {
                "credential": {
                    "state": next(states),
                    "source": "app_identity_oauth",
                    "healthy": True,
                }
            },
        ),
    )
    workspace = SimpleNamespace(
        config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer ct"})
    )

    assert deploy_ct_sim._verify_direct_oauth(
        workspace,
        "https://app.test",
        attempts=2,
        interval=3,
        sleep=sleeps.append,
    ) is True
    assert sleeps == [3]


def test_post_deploy_acceptance_seeds_obo_reconciles_and_requires_green_readyz(
    monkeypatch
):
    calls = []
    responses = iter([
        SimpleNamespace(
            status_code=200,
            json=lambda: {
                "credential": {
                    "state": "rotating",
                    "source": "app_identity_oauth",
                    "healthy": True,
                    "last_successful_at": 995.0,
                },
                "obo": {"present": True, "fresh": True},
            },
        ),
        SimpleNamespace(status_code=200, json=lambda: {"ok": True}),
        SimpleNamespace(
            status_code=200,
            json=lambda: {"ready": True, "status": "ready"},
        ),
    ])

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)

    result = deploy_ct_sim._post_deploy_acceptance(
        "https://app.test",
        attendee="attendee@example.com",
        attendee_token="attendee-oauth",
        request=request,
        now=1000.0,
        workshop_pat_present=False,
    )

    assert result == {"ok": True, "blocker": None}
    assert [(method, url.rsplit("/", 1)[-1]) for method, url, _ in calls] == [
        ("GET", "config"),
        ("POST", "reconcile"),
        ("GET", "readyz"),
    ]
    assert "attendee-oauth" not in repr(result)


def test_post_deploy_acceptance_rejects_pat_and_missing_attendee_exchange():
    blocked = deploy_ct_sim._post_deploy_acceptance(
        "https://app.test",
        attendee="attendee@example.com",
        attendee_token="",
        request=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must fail before HTTP")
        ),
        now=1000.0,
        workshop_pat_present=False,
    )
    pat = deploy_ct_sim._post_deploy_acceptance(
        "https://app.test",
        attendee="attendee@example.com",
        attendee_token="token",
        request=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must reject PAT before HTTP")
        ),
        now=1000.0,
        workshop_pat_present=True,
    )

    assert blocked["ok"] is False
    assert "external Control Tower attendee OAuth exchange" in blocked["blocker"]
    assert pat["ok"] is False
    assert "WORKSHOP_PAT" in pat["blocker"]


def test_deploy_helper_contains_no_token_permission_or_mint_calls():
    source = Path(deploy_ct_sim.__file__).read_text()

    assert "token_management" not in source
    assert "_grant_token_can_use" not in source
    assert "tokens.create" not in source


@pytest.mark.parametrize(
    "state",
    ["FAILED", "PENDING", "RUNNING", "CANCELED", "CLOSED", "unknown"],
)
def test_sql_rejects_every_non_succeeded_terminal_or_incomplete_state(state):
    response = SimpleNamespace(
        status=SimpleNamespace(state=state, error=None),
    )
    workspace = SimpleNamespace(
        statement_execution=SimpleNamespace(
            execute_statement=lambda **kwargs: response,
        ),
    )

    with pytest.raises(RuntimeError, match=state):
        deploy_ct_sim._sql(workspace, "warehouse", "SELECT 1")


def test_sql_accepts_only_succeeded():
    response = SimpleNamespace(
        status=SimpleNamespace(state="SUCCEEDED", error=None),
    )
    workspace = SimpleNamespace(
        statement_execution=SimpleNamespace(
            execute_statement=lambda **kwargs: response,
        ),
    )

    assert deploy_ct_sim._sql(workspace, "warehouse", "SELECT 1") is response


class _Apps:
    def __init__(self, create_error=None, update_error=None):
        self.create_error = create_error
        self.update_error = update_error
        self.created = []
        self.updated = []

    def create_and_wait(self, app):
        self.created.append(app)
        if self.create_error:
            raise self.create_error

    def update(self, name, app):
        self.updated.append((name, app))
        if self.update_error:
            raise self.update_error

    def get(self, name):
        return SimpleNamespace(name=name)


class _App:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_event_create_fails_closed_instead_of_retrying_without_scopes():
    apps = _Apps(create_error=RuntimeError("scope rejected"))
    workspace = SimpleNamespace(apps=apps)

    with pytest.raises(RuntimeError, match="scope rejected"):
        deploy_ct_sim._create_app(
            workspace,
            _App,
            "event-app",
            ["catalog.catalogs:read", "sql"],
        )

    assert len(apps.created) == 1
    assert apps.created[0].user_api_scopes == ["catalog.catalogs:read", "sql"]


def test_existing_app_update_preserves_scopes_and_fails_closed():
    apps = _Apps(
        create_error=RuntimeError("already exists"),
        update_error=RuntimeError("scope update rejected"),
    )
    workspace = SimpleNamespace(apps=apps)

    with pytest.raises(RuntimeError, match="scope update rejected"):
        deploy_ct_sim._create_app(
            workspace,
            _App,
            "event-app",
            ["catalog.catalogs:read", "sql"],
        )

    assert apps.updated[0][1].user_api_scopes == ["catalog.catalogs:read", "sql"]


def test_dry_run_plan_is_secret_free_and_does_not_call_workspace_api(capsys):
    plan = deploy_ct_sim._dry_run_plan(
        name="event-app",
        attendee="attendee@example.com",
        catalog="event_catalog",
        scopes=["catalog.catalogs:read", "sql"],
        settings={
            "WORKSHOP_ATTENDEE_EMAIL": "attendee@example.com",
            "WORKSHOP_PAT": "dapi-secret-value",
            "ENABLE_OBO": "true",
        },
        admin_group="event_admins",
        profile="event-profile",
        emergency_pat=True,
    )

    deploy_ct_sim._print_dry_run(plan)
    output = capsys.readouterr().out
    decoded = json.loads(output)

    assert "dapi-secret-value" not in output
    assert decoded["patched_settings"]["WORKSHOP_PAT"] == "<redacted-emergency-pat>"
    assert decoded["mutates_workspace"] is False
    assert decoded["credential_mode"] == "degraded_emergency_pat"
    assert decoded["profile"] == "event-profile"
    assert decoded["patched_settings"]["WORKSHOP_APP_SP_ID"] == (
        "<resolved-after-app-create>"
    )


def test_deployment_summary_distinguishes_client_and_numeric_app_sp_ids(capsys):
    deploy_ct_sim._print_summary(
        SimpleNamespace(
            name="event-app",
            profile="event-profile",
            catalog="event_catalog",
        ),
        "deployer@example.com",
        "attendee@example.com",
        SimpleNamespace(url="https://app.test", service_principal_id=12345),
        "client-uuid",
        False,
        False,
        False,
        [],
        True,
        True,
        {
            "name": "event-admins",
            "app_sp_member": True,
            "deployer_member": True,
        },
    )

    output = capsys.readouterr().out
    assert "app SP client id: client-uuid" in output
    assert "app SP numeric id: 12345" in output
    assert "app SP id       :" not in output


def test_real_main_dry_run_never_imports_or_calls_databricks_api(monkeypatch, capsys):
    imported = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "databricks" or name.startswith("databricks."):
            raise AssertionError("dry-run must not import the Databricks SDK")
        return imported(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = deploy_ct_sim.main([
        "--dry-run",
        "--profile", "event-profile",
        "--attendee", "attendee@example.com",
        "--skills-ref", "v1.2.3",
        "--anthropic-model", "databricks-claude-sonnet-5",
        "--codex-model", "databricks-gpt-5-6-codex",
    ])

    assert result == 0
    assert json.loads(capsys.readouterr().out)["mutates_workspace"] is False


def test_admin_group_resolution_failure_is_fatal():
    workspace = SimpleNamespace(
        groups=SimpleNamespace(list=lambda **kwargs: []),
    )

    with pytest.raises(RuntimeError, match="exactly one"):
        deploy_ct_sim._configure_admin_group(
            workspace,
            "missing-group",
            "app-client-id",
            SimpleNamespace(groups=[]),
        )
