"""Invalid checkout inputs are held before any project package resolution."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

import pytest
import yaml

import dependency_sources


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SOURCE = "https://operator:bootstrap-secret@private.example/simple"


@pytest.fixture
def bootstrap_checkout(tmp_path):
    root = tmp_path / "release"
    names = set(dependency_sources.REQUIRED_INPUTS)
    names.update(path.relative_to(ROOT).as_posix()
                 for path in (ROOT / "assets/artifacts").glob("*.*"))
    names.update({"dependency_sources.py", "scripts/check_dependency_sources.py"})
    for name in names:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)

    # No real network/package manager runs in these ordering tests. The fake uv
    # models the unsafe default (project resolution before command execution),
    # then executes the real guard only for the isolated bootstrap form.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "uv"
    executable.write_text(f"#!{sys.executable}\n" + '''
from pathlib import Path
import json
import os
import subprocess
import sys

args = sys.argv[1:]
record = {"args": args, "cwd_files": sorted(p.name for p in Path.cwd().iterdir()),
          "env": {k: v for k, v in os.environ.items()
                  if k.upper().startswith(("UV_", "PIP_", "NPM_CONFIG_", "BUN_CONFIG_"))
                  or k in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}}
Path(os.environ["BOOTSTRAP_TRACE"]).write_text(json.dumps(record))
if not {"--no-project", "--no-config", "--isolated", "--no-env-file"}.issubset(args):
    Path(os.environ["PROJECT_FETCH_MARKER"]).write_text("project resolution reached")
    print("unsafe project resolution attempted")
    raise SystemExit(90)
command = args.index("python")
interpreter = args[args.index("--python") + 1]
raise SystemExit(subprocess.call([interpreter, *args[command + 1:]]))
''')
    executable.chmod(0o755)
    trace = tmp_path / "bootstrap.json"
    marker = tmp_path / "project-fetch"
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "BOOTSTRAP_TRACE": str(trace), "PROJECT_FETCH_MARKER": str(marker),
        "UV_INDEX": PRIVATE_SOURCE, "UV_INDEX_CUSTOM_URL": PRIVATE_SOURCE,
        "UV_CONFIG_FILE": str(root / "uv.toml"), "UV_ENV_FILE": str(root / ".env"),
        "PIP_INDEX_URL": PRIVATE_SOURCE, "PIP_CONFIG_FILE": str(root / "pip.conf"),
        "npm_config_registry": PRIVATE_SOURCE, "BUN_CONFIG_REGISTRY": PRIVATE_SOURCE,
        "PYTHONPATH": str(root / "unreviewed-python"), "VIRTUAL_ENV": str(root / ".venv"),
    }
    return root, env, trace, marker


def _run_bootstrap(checkout):
    root, env, trace, marker = checkout
    result = subprocess.run([sys.executable, "scripts/check_dependency_sources.py"],
                            cwd=root, env=env, capture_output=True, text=True, check=False)
    record = json.loads(trace.read_text())
    args = record["args"]
    assert args[0] == "run"
    assert args[args.index("--default-index") + 1] == "https://pypi.org/simple"
    assert args[args.index("--with") + 1] == "PyYAML==6.0.3"
    assert args[args.index("--python") + 1] == sys.executable
    assert "--no-python-downloads" in args and "--no-managed-python" in args
    assert "--no-build" in args
    assert args[args.index("--keyring-provider") + 1] == "disabled"
    assert args[args.index("python") + 1] == "-I"
    assert record["cwd_files"] == []
    assert record["env"] == {}
    assert not marker.exists()
    assert "bootstrap-secret" not in result.stdout + result.stderr
    assert "private.example" not in result.stdout + result.stderr
    return result


@pytest.mark.parametrize("poison", ["project", "lock", "npm", "config"])
def test_poisoned_inputs_fail_redacted_before_project_resolution(bootstrap_checkout, poison):
    root = bootstrap_checkout[0]
    if poison == "project":
        with (root / "pyproject.toml").open("a") as stream:
            stream.write(f'\n[[tool.uv.index]]\nurl = "{PRIVATE_SOURCE}"\n')
        expected = "pyproject.toml: non-public Python index"
    elif poison == "lock":
        path = root / "uv.lock"
        path.write_text(path.read_text().replace("https://pypi.org/simple", PRIVATE_SOURCE, 1))
        expected = "uv.lock: non-public Python registry"
    elif poison == "npm":
        path = root / "frontend/package-lock.json"
        path.write_text(path.read_text().replace("https://registry.npmjs.org/", PRIVATE_SOURCE, 1))
        expected = "frontend/package-lock.json: non-public resolved npm artifact"
    else:
        (root / "uv.toml").write_text(f'[[index]]\nurl = "{PRIVATE_SOURCE}"\n')
        expected = "uv.toml: non-public Python index"
    result = _run_bootstrap(bootstrap_checkout)
    assert result.returncode == 1
    assert expected in result.stdout


def test_public_checkout_passes_without_using_inherited_private_settings(bootstrap_checkout):
    result = _run_bootstrap(bootstrap_checkout)
    assert result.returncode == 0, result.stderr
    assert "Public Python/npm release and deployment sources verified." in result.stdout


@pytest.mark.parametrize("filename", ["ci.yml", "release.yml", "skills-freshness.yml"])
def test_all_dependency_jobs_wait_for_public_source_validation(filename):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
    jobs = workflow["jobs"]
    gate = jobs["public-sources"]
    commands = [step["run"] for step in gate["steps"] if "run" in step]
    assert commands == ["python scripts/check_dependency_sources.py"]
    assert gate.get("continue-on-error") is not True
    assert not gate.get("if")

    for name, job in jobs.items():
        if name == "public-sources":
            continue
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        if any(command in commands for command in ("uv ", "npm ", "npx ", "bun ")):
            needs = job.get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            assert "public-sources" in needs, f"{filename}: {name} can bypass source validation"
            assert "always()" not in job.get("if", "")
