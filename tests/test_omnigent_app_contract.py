"""Contract tests for the dedicated Omnigent Databricks App artifact."""

import ast
import hashlib
import importlib.util
import json
import re
import tomllib
from pathlib import Path

import yaml
import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "deploy" / "omnigent-app"
EXAMPLES_DIR = ROOT / "docs" / "examples"
FIXTURES_DIR = ROOT / "tests" / "fixtures"


def _load_volume_probe():
    spec = importlib.util.spec_from_file_location(
        "omnigent_volume_probe", APP_DIR / "volume_probe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected_host_id(url: str, email: str = "alice@example.com") -> str:
    return hashlib.sha256(
        b"databricks-workshop-terminal/omnigent-host-id/v1\0"
        + url.rstrip("/").encode()
        + b"\0"
        + email.strip().lower().encode()
    ).hexdigest()[:32]


def _assert_schema(instance, schema, path="$"):
    for constraint in schema.get("allOf", []):
        _assert_schema(instance, constraint, path)
    if "if" in schema:
        try:
            _assert_schema(instance, schema["if"], path)
        except AssertionError:
            branch = schema.get("else")
        else:
            branch = schema.get("then")
        if branch is not None:
            _assert_schema(instance, branch, path)
    if "anyOf" in schema:
        failures = []
        for option in schema["anyOf"]:
            try:
                _assert_schema(instance, option, path)
                return
            except AssertionError as exc:
                failures.append(str(exc))
        raise AssertionError(f"{path} did not match any allowed schema: {failures}")
    if "const" in schema:
        assert instance == schema["const"], f"{path} must equal {schema['const']!r}"
    if "enum" in schema:
        assert instance in schema["enum"], f"{path} must be one of {schema['enum']!r}"
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        options = [{**schema, "type": option} for option in schema_type]
        for option in options:
            try:
                _assert_schema(instance, option, path)
                return
            except AssertionError:
                pass
        raise AssertionError(f"{path} must have one of types {schema_type!r}")
    if schema_type == "object":
        assert isinstance(instance, dict), f"{path} must be an object"
        for key in schema.get("required", []):
            assert key in instance, f"{path}.{key} is required"
        if schema.get("additionalProperties") is False:
            assert set(instance) <= set(schema.get("properties", {})), (
                f"{path} contains unexpected properties"
            )
        for key, value in instance.items():
            child = schema.get("properties", {}).get(key)
            if child is not None:
                _assert_schema(value, child, f"{path}.{key}")
    elif schema_type == "array":
        assert isinstance(instance, list), f"{path} must be an array"
        for index, value in enumerate(instance):
            _assert_schema(value, schema["items"], f"{path}[{index}]")
    elif schema_type == "string":
        assert isinstance(instance, str), f"{path} must be a string"
        assert len(instance) >= schema.get("minLength", 0), f"{path} is too short"
        if "pattern" in schema:
            assert re.fullmatch(schema["pattern"], instance), (
                f"{path} must match {schema['pattern']!r}"
            )
    elif schema_type == "boolean":
        assert isinstance(instance, bool), f"{path} must be a boolean"
    elif schema_type == "integer":
        assert isinstance(instance, int) and not isinstance(instance, bool), (
            f"{path} must be an integer"
        )
    elif schema_type == "null":
        assert instance is None, f"{path} must be null"


def test_app_yaml_binds_runtime_port_and_required_resources():
    config = yaml.safe_load((APP_DIR / "app.yaml").read_text())

    assert config["command"] == ["python", "app.py"]
    env = {entry["name"]: entry for entry in config["env"]}
    assert env["AP_LAKEBASE_ENDPOINT"]["valueFrom"] == "postgres"
    assert env["AP_ARTIFACT_VOLUME_PATH"]["valueFrom"] == "artifact_volume"
    assert env["OMNIGENT_AUTH_PROVIDER"]["value"] == "header"
    assert env["OMNIGENT_BUILD_VERSION"]["value"] == "0.7.0"
    assert (
        env["OMNIGENT_BUILD_SHA"]["value"] == "35519fb04743f66b30cac8a40695d5d72fa163ea"
    )
    assert set(env) == {
        "AP_LAKEBASE_ENDPOINT",
        "AP_ARTIFACT_VOLUME_PATH",
        "OMNIGENT_AUTH_PROVIDER",
        "OMNIGENT_BUILD_VERSION",
        "OMNIGENT_BUILD_SHA",
    }
    assert not {
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_SECRET",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    }.intersection(env)


def test_wrapper_retains_upstream_control_plane_topology():
    lock = json.loads((APP_DIR / "upstream-lock.json").read_text())
    source = (APP_DIR / "app.py").read_text()
    tree = ast.parse(source)

    assert lock == {
        "package": "omnigent[databricks]==0.7.0",
        "release_commit": "35519fb04743f66b30cac8a40695d5d72fa163ea",
        "verified_main_commit": "815cdbef431397a59ab296e194fce026d9e79b4f",
        "wheel_sha256": "cce277f39ec19f6370ac55e335a531cbc510d0249346130b70f54301a7ea0203",
    }
    assert "sys.version_info < (3, 12)" in source
    assert "host_store = HostStore(DB_URI)" in source
    assert "caps=RuntimeCaps()" in source
    assert 'os.environ["OMNIGENT_AUTH_PROVIDER"] = "header"' in source
    assert 'uvicorn.run(app, host="0.0.0.0", port=PORT)' in source
    assert "OMNIGENT_REMOTE_HOST_ENABLED" not in source
    assert "public_sharing=" not in source
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported_roots.isdisjoint({"pty", "subprocess", "tmux"})
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"Popen", "spawn", "system"}
        for node in ast.walk(tree)
    )


def test_wrapper_delegates_routes_to_upstream_app_factory():
    source = (APP_DIR / "app.py").read_text()

    assert "create_app(" in source
    assert '@app.get("/health")' not in source
    assert '@app.get("/api/version")' not in source


def test_artifact_volume_probe_writes_fsyncs_and_cleans_up(tmp_path, monkeypatch):
    probe = _load_volume_probe()
    fsynced: list[int] = []
    monkeypatch.setattr(probe.os, "fsync", fsynced.append)

    probe.probe_artifact_volume(tmp_path)

    assert fsynced
    assert list(tmp_path.iterdir()) == []


def test_artifact_volume_probe_failure_prevents_startup(tmp_path, monkeypatch):
    probe = _load_volume_probe()
    write_error = PermissionError("volume is read-only")

    def denied(*args, **kwargs):
        raise write_error

    monkeypatch.setattr(probe.Path, "open", denied)
    monkeypatch.setattr(
        probe.Path,
        "unlink",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PermissionError("cleanup is also denied")
        ),
    )
    with pytest.raises(RuntimeError, match="artifact Volume is not writable") as raised:
        probe.probe_artifact_volume(tmp_path)
    assert raised.value.__cause__ is write_error
    assert list(tmp_path.iterdir()) == []


def test_artifact_volume_probe_unlink_failure_prevents_startup(tmp_path, monkeypatch):
    probe = _load_volume_probe()
    monkeypatch.setattr(probe.os, "fsync", lambda fd: None)

    def denied(*args, **kwargs):
        raise PermissionError("volume does not permit deletion")

    monkeypatch.setattr(probe.Path, "unlink", denied)

    with pytest.raises(RuntimeError, match="artifact Volume probe cannot be removed"):
        probe.probe_artifact_volume(tmp_path)


def test_wrapper_probes_artifact_volume_before_health_app_creation():
    source = (APP_DIR / "app.py").read_text()
    assert "probe_artifact_volume(VOLUME_PATH)" in source
    assert source.index("probe_artifact_volume(VOLUME_PATH)") < source.index(
        "app = create_app("
    )


def test_app_source_snapshot_stays_below_platform_file_limit():
    ten_megabytes = 10 * 1024 * 1024
    files = [path for path in APP_DIR.rglob("*") if path.is_file()]

    assert files
    assert all(path.stat().st_size < ten_megabytes for path in files)
    assert not any(path.suffix == ".whl" for path in files)


def test_uv_project_pins_python_and_omnigent_release():
    project = tomllib.loads((APP_DIR / "pyproject.toml").read_text())
    lock_text = (APP_DIR / "uv.lock").read_text()
    locked = tomllib.loads(lock_text)

    assert not (APP_DIR / "requirements.txt").exists()
    assert not (APP_DIR / "requirements.in").exists()
    assert project["project"]["requires-python"] == ">=3.12,<3.13"
    assert project["project"]["dependencies"] == ["omnigent[databricks]==0.7.0"]
    assert project["tool"]["uv"]["package"] is False
    assert locked["requires-python"] == ">=3.12, <3.13"
    assert "pypi-proxy.dev.databricks.com" not in lock_text
    assert "files.pythonhosted.org" in lock_text
    lock_hosts = set(re.findall(r'https://([^/"\s]+)', lock_text))
    assert lock_hosts == {"files.pythonhosted.org"}
    packages = {(entry["name"], entry["version"]) for entry in locked["package"]}
    assert ("omnigent", "0.7.0") in packages
    assert ("omnigent-client", "0.7.0") in packages
    assert ("omnigent-ui-sdk", "0.7.0") in packages
    omnigent = next(entry for entry in locked["package"] if entry["name"] == "omnigent")
    assert len(omnigent["wheels"]) == 1
    wheel = omnigent["wheels"][0]
    assert wheel["url"].endswith("/omnigent-0.7.0-py3-none-any.whl")
    assert wheel["hash"] == (
        "sha256:cce277f39ec19f6370ac55e335a531cbc510d0249346130b70f54301a7ea0203"
    )


def test_control_tower_payload_example_matches_contract_schema():
    from server.omnigent_remote import REMOTE_HOST_STATES

    payload = json.loads(
        (EXAMPLES_DIR / "omnigent-control-tower-payload.json").read_text()
    )
    schema = json.loads(
        (FIXTURES_DIR / "omnigent-control-tower-payload.schema.json").read_text()
    )

    _assert_schema(payload, schema)
    serialized = json.dumps(payload).lower()
    assert "client_secret" not in serialized
    assert "access_token" not in serialized
    assert payload["contract_version"] == "1.4"
    assert payload["remote_host"]["enabled"] is True
    assert payload["remote_host"]["status"] == "waiting_for_token"
    assert set(schema["properties"]["remote_host"]["properties"]["status"]["enum"]) == (
        (REMOTE_HOST_STATES - {"disabled"}) | {"connected"}
    )
    assert payload["remote_host"]["authentication"] == {
        "kind": "obo_token_mirror",
        "owner_identity": "attendee",
        "refresh_trigger": "authenticated_browser_request",
        "static_secret_required": False,
    }
    assert payload["environment"]["ALLOW_SHARED_TOPOLOGY"] == "false"
    assert payload["environment"]["WORKSHOP_ATTENDEE_EMAIL"] == "alice@example.com"
    assert int(payload["environment"]["MAX_SESSIONS_GLOBAL"]) <= int(
        payload["environment"]["MAX_SESSIONS_PER_USER"]
    )
    expected_host_id = _expected_host_id(payload["environment"]["OMNIGENT_APP_URL"])
    assert payload["remote_host"]["expected_host_id"] == expected_host_id
    assert payload["remote_host"]["host_id_derivation"] == (
        "sha256(databricks-workshop-terminal/omnigent-host-id/v1"
        "\\0<normalized-server-url>\\0<normalized-attendee-email>)[:32]"
    )
    # A commit is preferred, but an attendee workspace can receive the app source
    # as a plain directory with no Git folder and therefore no head to read back.
    # Rejecting that discarded a host that had deployed fine, so a branch is
    # accepted as long as the payload says the revision is not immutable.
    assert payload["deployment"]["source_ref"]
    assert payload["deployment"]["source_ref_immutable"] is True
    assert re.fullmatch(r"[0-9a-f]{40}", payload["deployment"]["source_ref"])

    degraded = json.loads(json.dumps(payload))
    degraded["deployment"]["source_ref"] = "main"
    degraded["deployment"]["source_ref_immutable"] = False
    _assert_schema(degraded, schema)

    connected = json.loads(json.dumps(payload))
    connected["remote_host"]["enabled"] = True
    connected["remote_host"]["status"] = "connected"
    _assert_schema(connected, schema)


def test_readiness_example_requires_app_and_attendee_host():
    from server.omnigent_remote import REMOTE_HOST_STATES

    readiness = json.loads((EXAMPLES_DIR / "omnigent-readiness.json").read_text())
    schema = json.loads((FIXTURES_DIR / "omnigent-readiness.schema.json").read_text())
    expected_host_id = _expected_host_id(
        "https://event-123-alice-omnigent.example.databricksapps.com"
    )

    _assert_schema(readiness, schema)
    assert readiness["app"]["health_body"] == {"status": "ok"}
    assert readiness["app"]["ready"] is True
    assert readiness["remote_host"]["connected"] is False
    assert readiness["ready"] is False
    assert readiness["status"] == "provisioning"
    assert readiness["remote_host"]["status"] == "waiting_for_token"
    assert set(schema["properties"]["remote_host"]["properties"]["status"]["enum"]) == (
        (REMOTE_HOST_STATES - {"disabled"}) | {"connected"}
    )
    assert readiness["failure"] is None
    assert readiness["remote_host"]["expected_host_id"] == expected_host_id

    running = json.loads(json.dumps(readiness))
    running["remote_host"]["status"] = "running"
    _assert_schema(running, schema)

    ready = json.loads(json.dumps(readiness))
    ready["ready"] = True
    ready["status"] = "ready"
    ready["remote_host"] = {
        "required": True,
        "enabled": True,
        "connected": True,
        "status": "connected",
        "expected_host_id": expected_host_id,
        "host_id": expected_host_id,
        "last_seen_at": "2026-07-29T00:00:00Z",
    }
    ready["failure"] = None
    _assert_schema(ready, schema)

    invalid_ready = json.loads(json.dumps(readiness))
    invalid_ready["ready"] = True
    invalid_ready["status"] = "ready"
    with pytest.raises(AssertionError):
        _assert_schema(invalid_ready, schema)

    invalid_connected = json.loads(json.dumps(ready))
    invalid_connected["remote_host"]["host_id"] = None
    invalid_connected["remote_host"]["last_seen_at"] = None
    with pytest.raises(AssertionError):
        _assert_schema(invalid_connected, schema)

    false_connected_status = json.loads(json.dumps(readiness))
    false_connected_status["remote_host"]["status"] = "connected"
    with pytest.raises(AssertionError):
        _assert_schema(false_connected_status, schema)

    for local_state in REMOTE_HOST_STATES - {"disabled"}:
        local = json.loads(json.dumps(readiness))
        local["remote_host"]["status"] = local_state
        local["remote_host"]["connected"] = False
        if local_state in {"error", "stopped"}:
            local["status"] = "failed"
            local["failure"] = {
                "code": f"remote_host_{local_state}",
                "message": f"Remote host is {local_state}",
                "retryable": local_state == "error",
            }
        _assert_schema(local, schema)

        local["remote_host"]["connected"] = True
        local["remote_host"]["host_id"] = expected_host_id
        local["remote_host"]["last_seen_at"] = "2026-07-29T00:00:00Z"
        with pytest.raises(AssertionError):
            _assert_schema(local, schema)


def test_handoff_docs_cover_lifecycle_security_and_upstream_gap():
    contract = (ROOT / "docs" / "omnigent-control-tower-contract.md").read_text()
    checklist = (ROOT / "docs" / "omnigent-acceptance-checklist.md").read_text()
    readme = (APP_DIR / "README.md").read_text()

    for heading in (
        "## Confirmed upstream behavior",
        "## Authentication and ownership",
        "## Resource contract",
        "## Environment ownership",
        "## Provisioning sequence",
        "## Health and readiness",
        "## Retries and rollback",
        "## Teardown",
        "## Control Tower return value",
    ):
        assert heading in contract
    assert "815cdbef431397a59ab296e194fce026d9e79b4f" in contract
    assert "omnigent host --server <OMNIGENT_APP_URL> --non-interactive" in contract
    assert "omnigent run --server <OMNIGENT_APP_URL>" in contract
    assert "service principal" in contract
    assert "attendee" in contract
    assert "X-Forwarded-Email" in contract
    assert "obo" in contract.lower()
    assert "static bootstrap secret" in contract.lower()
    assert "credential fallback" in contract.lower()
    assert "enforced invariant" in contract.lower()
    assert "ALLOW_SHARED_TOPOLOGY=true" in contract
    assert "OMNIGENT_HOST_TOKEN" in contract
    assert "must not" in contract[contract.index("OMNIGENT_HOST_TOKEN") :].lower()
    assert "OMNIGENT_REMOTE_HOST_ENABLED" not in contract
    assert "does not start a local host or runner" in readme
    assert "DATABRICKS_APP_PORT" in readme
    assert "implementation gate" not in readme
    assert "attendee OBO token" in readme
    assert "no service-principal fallback" in readme
    assert "expiry" in readme
    assert "rotation" in readme
    assert "revocation" in readme
    assert "Provision" in checklist
    assert "Teardown" in checklist
