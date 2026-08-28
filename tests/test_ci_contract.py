from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _workflow():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def test_frontend_ci_is_blocking_and_deterministic():
    workflow = _workflow()
    job = workflow["jobs"]["frontend"]
    commands = "\n".join(
        step.get("run", "")
        for step in job["steps"]
        if isinstance(step, dict)
    )

    assert job.get("continue-on-error") is not True
    assert "npm ci" in commands
    assert "npm install" not in commands
    assert "npm test" in commands
    assert "npm run build" in commands
    assert "git diff --exit-code -- static" in commands
    assert "git status --porcelain -- static" in commands


def test_backend_ci_runs_full_suite_from_the_uv_lock():
    workflow = _workflow()
    job = workflow["jobs"]["backend"]
    commands = "\n".join(
        step.get("run", "")
        for step in job["steps"]
        if isinstance(step, dict)
    )

    assert "uv run --frozen --no-group release python -m pytest tests/ -q" in commands
    assert "uv lock --check" in commands
    assert "uv export --frozen" in commands
    assert "git diff --exit-code -- pyproject.toml uv.lock" in commands
    assert "uv sync --frozen --no-group release" in commands
    assert "pip install" not in commands
    assert workflow["jobs"]["backend"]["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.12",
    ]


def test_packaged_ci_is_linux_cp311_reproducible_and_offline_smoked():
    workflow = _workflow()
    job = workflow["jobs"]["package"]
    commands = "\n".join(
        step.get("run", "") for step in job["steps"] if isinstance(step, dict)
    )

    assert job["runs-on"] == "ubuntu-24.04"
    assert 'python-version": "3.11"' not in commands  # setup-python owns the ABI
    setup = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/setup-python@")
    )
    assert setup["with"]["python-version"] == "3.11"
    assert commands.count("scripts/build_release.py") == 2
    assert "cmp dist/workshop-terminal.pex dist-repro/workshop-terminal.pex" in commands
    assert "scripts/smoke_release_container.sh dist" in commands
    assert "pip install" not in commands


def test_tag_release_publishes_only_a_tested_pex_and_manifest():
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release.yml").read_text()
    )
    steps = workflow["jobs"]["release"]["steps"]
    commands = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    publish = next(
        step for step in steps if step.get("uses", "").startswith("softprops/action-gh-release@")
    )
    backend_test_command = "uv run --frozen --no-group release python -m pytest tests/ -q"
    backend_test_index = next(
        index
        for index, step in enumerate(steps)
        if backend_test_command in step.get("run", "")
    )
    build_index = next(
        index
        for index, step in enumerate(steps)
        if "python scripts/build_release.py" in step.get("run", "")
    )
    publish_index = steps.index(publish)

    assert "uv lock --check" in commands
    assert "uv export --frozen" in commands
    assert "git diff --exit-code -- pyproject.toml uv.lock requirements.txt requirements-dev.txt" in commands
    assert "uv sync --frozen --no-group release" in commands
    assert backend_test_command in commands
    assert backend_test_index < build_index < publish_index
    assert "scripts/smoke_release_container.sh dist" in commands
    assert "cmp dist/workshop-terminal.pex dist-repro/workshop-terminal.pex" in commands
    assert "dist/workshop-terminal.pex" in publish["with"]["files"]
    assert "dist/release-manifest.json" in publish["with"]["files"]
    assert "latest" not in commands.lower()


def test_frontend_ci_matches_deployed_node_pin():
    """CI must build the frontend on the Node attendees actually get.

    Derived from the installer pin rather than restated, so bumping
    ``NODE_VERSION`` can never leave CI testing a different major.
    """
    from server.bootstrap import install

    workflow = _workflow()
    setup = next(
        step
        for step in workflow["jobs"]["frontend"]["steps"]
        if step.get("uses", "").startswith("actions/setup-node@")
    )
    assert setup["with"]["node-version"] == install.NODE_VERSION


def test_frontend_tests_use_an_explicit_typescript_runtime():
    package = yaml.safe_load((ROOT / "frontend" / "package.json").read_text())
    test_command = package["scripts"]["test"]

    assert test_command.startswith("tsx --test ")
    assert "node --test" not in test_command
    assert package["devDependencies"]["tsx"]


def test_initial_sessions_failure_is_caught_and_surfaced():
    source = (ROOT / "frontend" / "src" / "App.tsx").read_text()
    request = source.split("api.sessions()", 1)[1].split("refreshAgents()", 1)[0]
    assert ".catch(" in request
    assert "setError(" in request


def test_development_requirements_are_fully_pinned():
    direct = {
        line.strip()
        for line in (ROOT / "requirements-dev.in").read_text().splitlines()
        if line.strip() and not line.startswith(("#", "-r "))
    }
    lock = [
        line.strip()
        for line in (ROOT / "requirements-dev.txt").read_text().splitlines()
        if line.strip()
        and not line.startswith("#")
        and not line.startswith((" ", "--"))
    ]

    assert any(line.startswith("pytest==") for line in direct)
    assert any(line.startswith("httpx==") for line in direct)
    assert all("==" in line for line in direct)
    assert len(lock) > len(direct)
    assert all("==" in line for line in lock)


def test_two_instance_runbook_names_external_actions_and_deferred_scale():
    runbook = (
        ROOT / "docs" / "two-instance-test-runbook.md"
    ).read_text().lower()

    for required in (
        "exactly two",
        "control tower",
        "ct_validation_hmac_key",
        "ct_attestation_key",
        "ct_attestation_key_id",
        "ct_attestation_public_key",
        "ed25519",
        "public-key-only",
        "private key",
        "report_id",
        "payload_hash",
        "nonce",
        "replay",
        "canonical payload",
        "validator never generates",
        "hash chain",
        "signed",
        "probe_attempt",
        "failed attempts",
        "ct_attestation",
        "operator_attestation",
        "direct oauth",
        "resource-manifest",
        "entitlement handoff ledger",
        "90-minute",
        "pause_started_at",
        "pause_completed_at",
        "closed-laptop",
        "wi-fi reconnect",
        "real resource",
        "ownership",
        "deletion/revocation receipt",
        "residual_session_ids",
        "confirm absence",
        "teardown",
        "10/100",
        "4–8h",
    ):
        assert required in runbook
    assert "save only booleans" not in runbook
    assert "external ct hmac" not in runbook
    assert "ct-owned verification key" not in runbook
