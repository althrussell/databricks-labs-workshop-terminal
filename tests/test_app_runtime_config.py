from pathlib import Path

import yaml

from server import config as runtime_config


ROOT = Path(__file__).resolve().parents[1]


def test_app_command_uses_native_uvicorn_runtime_env_and_one_worker():
    config = yaml.safe_load((ROOT / "app.yaml").read_text())

    assert config["command"] == [
        "uvicorn",
        "server.main:app",
        "--workers",
        "1",
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


def test_list_form_app_command_never_passes_literal_port_substitution():
    command = yaml.safe_load((ROOT / "app.yaml").read_text())["command"]

    assert "${DATABRICKS_APP_PORT}" not in command, (
        "Invalid value for '--port': '${DATABRICKS_APP_PORT}' "
        "is not a valid integer."
    )
    assert "--host" not in command
    assert "--port" not in command
    assert command.count("--workers") == 1
    workers_index = command.index("--workers")
    assert command[workers_index + 1] == "1"


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


def test_control_tower_catalog_contract_is_catalog_scoped_least_privilege():
    contract = (ROOT / "docs" / "control-tower-implementation.md").read_text()
    normalized = " ".join(contract.split())

    assert "app SP `MANAGE`, `USE_CATALOG`, and `CREATE_SCHEMA`" in normalized
    assert "only on that attendee's dedicated catalog" in normalized
    assert "attendee remains OWNER + `ALL PRIVILEGES`" in normalized
    assert "does not need ownership or metastore-wide `MANAGE`" in normalized
