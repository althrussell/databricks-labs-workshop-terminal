from pathlib import Path

import yaml

from server import config as runtime_config


ROOT = Path(__file__).resolve().parents[1]


def test_app_command_prepares_otel_before_importing_fastapi():
    config = yaml.safe_load((ROOT / "app.yaml").read_text())

    assert config["command"] == [
        "python",
        "-m",
        "server.otel_bootstrap",
    ]


def test_app_yaml_declares_authoritative_numeric_service_principal_id_setting():
    config = yaml.safe_load((ROOT / "app.yaml").read_text())
    env = {item["name"]: item.get("value", "") for item in config["env"]}

    assert env["WORKSHOP_APP_SP_ID"] == ""


def test_runtime_config_reads_app_service_principal_id_at_call_time(monkeypatch):
    monkeypatch.setenv("WORKSHOP_APP_SP_ID", "12345")
    assert runtime_config.workshop_app_sp_id() == "12345"

    monkeypatch.setenv("WORKSHOP_APP_SP_ID", "67890")
    assert runtime_config.workshop_app_sp_id() == "67890"


def test_bootstrap_resolves_runtime_port_and_keeps_one_worker():
    from server.otel_bootstrap import uvicorn_command

    command = yaml.safe_load((ROOT / "app.yaml").read_text())["command"]

    assert "${DATABRICKS_APP_PORT}" not in command, (
        "Invalid value for '--port': '${DATABRICKS_APP_PORT}' "
        "is not a valid integer."
    )
    runtime = uvicorn_command({"DATABRICKS_APP_PORT": "8123"})
    assert runtime[runtime.index("--host") + 1] == "0.0.0.0"
    assert runtime[runtime.index("--port") + 1] == "8123"
    assert runtime.count("--workers") == 1
    assert runtime[runtime.index("--workers") + 1] == "1"


def test_app_yaml_declares_the_cross_repo_telemetry_identity_contract():
    config = yaml.safe_load((ROOT / "app.yaml").read_text())
    env = {item["name"]: item.get("value", "") for item in config["env"]}

    assert env["OTEL_TRACES_SAMPLER"] == "always_on"
    assert "authorization" in env[
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"
    ]
    assert "/api/wizard" in env["OTEL_PYTHON_FASTAPI_EXCLUDED_URLS"]
    assert "/api/certificate" in env["OTEL_PYTHON_FASTAPI_EXCLUDED_URLS"]
    assert env["WORKSHOP_RUN_ID"] == ""
    assert env["WORKSHOP_UNIT_ID"] == ""
    assert env["WORKSHOP_RELEASE_SHA"] == ""
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env


def test_runtime_dependencies_are_explicit_and_exactly_pinned():
    requirements = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert any(line.startswith("databricks-sdk==") for line in requirements)
    assert all("==" in line for line in requirements)


def test_requirements_is_a_fully_pinned_resolver_output():
    direct = {
        line.split("==", 1)[0].split("[", 1)[0]
        for line in (ROOT / "requirements.in").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    resolved = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip()
        and not line.startswith("#")
        and not line.startswith((" ", "--"))
    ]

    assert direct <= {line.split("==", 1)[0].split("[", 1)[0] for line in resolved}
    assert len(resolved) > len(direct)
    assert all("==" in line and not any(op in line for op in (">=", "<=", "~="))
               for line in resolved)


def test_the_documented_mirror_env_names_are_the_ones_app_yaml_ships():
    """Control Tower implements against the doc, so a rename that lands in one
    and not the other produces an event where the mirror is silently ignored."""
    config = yaml.safe_load((ROOT / "app.yaml").read_text())
    shipped = {item["name"] for item in config["env"]}
    contract = (ROOT / "docs" / "control-tower-implementation.md").read_text()
    manifest_doc = (ROOT / "docs" / "artifact-manifest.md").read_text()

    for name in ("WORKSHOP_TOOLCHAIN_MIRROR_PATH", "WORKSHOP_TOOLCHAIN_MIRROR_STRICT"):
        assert name in shipped
        assert name in contract
        assert name in manifest_doc


def test_the_mirror_is_documented_as_optional_in_both_places():
    """An operator who reads either doc must come away knowing an event that
    stages nothing still works."""
    for doc in ("control-tower-implementation.md", "artifact-manifest.md"):
        text = " ".join((ROOT / "docs" / doc).read_text().split())
        assert "optional" in text.lower()
        assert "off by default" in text.lower()


def test_the_docs_keep_the_grant_a_group_grant():
    """A per-SP grant races the bootstrap thread. The reasoning has to survive
    in the doc, or the next implementer will simplify it away."""
    contract = " ".join(
        (ROOT / "docs" / "control-tower-implementation.md").read_text().split()
    )

    assert "Do not grant the SP directly" in contract
    assert "reader group" in contract


def test_control_tower_catalog_contract_is_catalog_scoped_least_privilege():
    contract = (ROOT / "docs" / "control-tower-implementation.md").read_text()
    normalized = " ".join(contract.split())

    assert "app SP `MANAGE`, `USE_CATALOG`, and `CREATE_SCHEMA`" in normalized
    assert "only on that attendee's dedicated catalog" in normalized
    assert "attendee remains OWNER + `ALL PRIVILEGES`" in normalized
    assert "does not need ownership or metastore-wide `MANAGE`" in normalized
