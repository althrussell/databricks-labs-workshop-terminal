from pathlib import Path
import tomllib

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


def test_backend_ci_runs_complementary_parallel_and_serial_layers():
    workflow = _workflow()
    job = workflow["jobs"]["backend"]
    commands = "\n".join(
        step.get("run", "")
        for step in job["steps"]
        if isinstance(step, dict)
    )
    test_commands = [
        line.strip()
        for line in commands.splitlines()
        if "python -m pytest" in line
    ]

    assert job["timeout-minutes"] == 15
    assert test_commands == [
        'uv run --frozen --no-group release python -m pytest tests/ -q -m "not host_process" -n 4 --dist loadfile --timeout=120 --durations=50 --durations-min=0.05 --junitxml=test-results/parallel.xml',
        "uv run --frozen --no-group release python -m pytest tests/test_closeout_writers.py tests/test_runtime_shutdown.py tests/test_supervisor.py -q -m host_process --timeout=300 --durations=50 --durations-min=0.05 --junitxml=test-results/host-process.xml",
    ]
    assert "uv lock --check" in commands
    assert "uv export --frozen" in commands
    assert "git diff --exit-code -- pyproject.toml uv.lock" in commands
    assert "uv sync --frozen --no-group release" in commands
    assert "pip install" not in commands
    assert job["strategy"]["matrix"]["python-version"] == ["3.11"]

    results = next(
        step
        for step in job["steps"]
        if step.get("name") == "Publish backend test results"
    )
    assert results["if"] == "always()"
    assert results["uses"] == "actions/upload-artifact@v4"
    assert results["with"]["path"] == "test-results/*.xml"
    assert results["with"]["retention-days"] == 14


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
    # The prepared browser (W3) must install and render in clean Linux too.
    browser = next(
        step
        for step in job["steps"]
        if "scripts/smoke_browser_container.sh" in step.get("run", "")
    )
    assert browser["timeout-minutes"] == 6
    assert browser["env"]["WT_BROWSER_SMOKE_STAGE"] == (
        "${{ github.workspace }}/dist/browser-stage"
    )
    cache = next(
        step
        for step in job["steps"]
        if step.get("uses") == "actions/cache@v4"
    )
    assert cache["with"]["path"] == "dist/browser-stage"
    assert "wt-browser-smoke-${{ runner.os }}-" in cache["with"]["restore-keys"]
    for source in (
        "assets/artifacts/manifest.json",
        "scripts/smoke_browser.py",
        "server/bootstrap/browser_runtime.py",
        "server/bootstrap/install.py",
    ):
        assert source in cache["with"]["key"]
    assert "pip install" not in commands


def test_ci_triggers_and_rendered_browser_coverage_match_the_release_gate():
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["push"]["branches"] == ["main"]
    assert "pull_request" in triggers

    browser = next(
        step
        for step in workflow["jobs"]["wizard-browser"]["steps"]
        if "python -m pytest" in step.get("run", "")
    )
    assert "tests/test_generated_app_browser_rendered.py" in browser["run"]

    makefile = (ROOT / "Makefile").read_text()
    assert '-m "not host_process" -n 4 --dist loadfile --timeout=120' in makefile
    assert "-m host_process --timeout=300" in makefile


def test_tag_release_requires_green_same_sha_ci_and_only_rebuilds_release():
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release.yml").read_text()
    )
    steps = workflow["jobs"]["release"]["steps"]
    commands = "\n".join(
        step.get("run", "") for step in steps if isinstance(step, dict)
    )
    gate = next(
        step for step in steps if step.get("name") == "Require successful main CI for the tagged commit"
    )
    gate_command = gate["run"]
    publish = next(
        step for step in steps if step.get("uses", "").startswith("softprops/action-gh-release@")
    )
    gate_index = steps.index(gate)
    build_index = next(
        index
        for index, step in enumerate(steps)
        if "python scripts/build_release.py" in step.get("run", "")
    )
    publish_index = steps.index(publish)

    assert workflow["permissions"]["actions"] == "read"
    assert gate["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "gh api --method GET" in gate_command
    assert "/actions/workflows/ci.yml/runs" in gate_command
    for required in (
        'head_sha="${GITHUB_SHA}"',
        "event=push",
        "branch=main",
        "status=completed",
        'run.get("head_sha") == sha',
        'run.get("event") == "push"',
        'run.get("head_branch") == "main"',
        'run.get("status") == "completed"',
        'run.get("conclusion") == "success"',
        "wait for its full CI workflow to pass",
    ):
        assert required in gate_command
    assert "sleep " not in gate_command
    assert gate_index < build_index < publish_index

    assert 'git rev-list -n 1 "${GITHUB_REF_NAME}"' in commands
    assert "uv lock --check" in commands
    assert "uv export --frozen" in commands
    assert "git diff --exit-code -- pyproject.toml uv.lock requirements.txt requirements-dev.txt" in commands
    assert "uv sync --frozen --no-group release" not in commands
    assert commands.count("python scripts/build_release.py") == 2
    assert "scripts/smoke_release_container.sh dist" in commands
    assert "cmp dist/workshop-terminal.pex dist-repro/workshop-terminal.pex" in commands
    assert "python -m pytest" not in commands
    assert "npm ci" not in commands
    assert "npm test" not in commands
    assert "npm run build" not in commands
    assert not any(step.get("uses", "").startswith("actions/setup-node@") for step in steps)
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

    required = {"httpx", "pytest", "pytest-asyncio", "pytest-timeout", "pytest-xdist"}
    direct_names = {line.split("==", 1)[0] for line in direct}
    lock_names = {line.split("==", 1)[0] for line in lock}
    dev_group = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["dev"]

    assert required <= direct_names
    assert required <= lock_names
    assert direct == set(dev_group)
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
