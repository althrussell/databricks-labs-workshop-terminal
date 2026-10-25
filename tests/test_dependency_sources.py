"""Public release inputs are allowlisted individually, not by banned host names."""
from pathlib import Path
import json
import subprocess

import pytest

from dependency_sources import DependencySourceError, validate_release_inputs, validate_source_files

ROOT = Path(__file__).resolve().parents[1]


def _validate(path, text):
    validate_source_files({path: text.encode()})


def test_actual_root_paired_locks_exports_manifests_and_app_registries_are_public():
    validate_release_inputs(ROOT)


@pytest.mark.parametrize("path", ["uv.lock", "deploy/omnigent-app/uv.lock"])
@pytest.mark.parametrize("replacement", [
    "https://unlisted.example/simple", "https://pypi-proxy.cloud.databricks.com/simple",
    "https://operator:secret@pypi.org/simple", "http://pypi.org/simple",
    "https://pypi.org:443/simple", "https://pypi.org/simple?token=secret",
])
def test_one_private_registry_mixed_with_public_entries_is_rejected(path, replacement):
    text = (ROOT / path).read_text().replace('registry = "https://pypi.org/simple"',
                                             f'registry = "{replacement}"', 1)
    with pytest.raises(DependencySourceError, match="non-public Python registry") as error:
        _validate(path, text)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("path", ["uv.lock", "deploy/omnigent-app/uv.lock"])
def test_every_python_wheel_and_sdist_is_checked(path):
    text = (ROOT / path).read_text().replace("https://files.pythonhosted.org/packages/",
                                             "https://unlisted.example/packages/", 1)
    with pytest.raises(DependencySourceError, match="non-public Python artifact"):
        _validate(path, text)


def test_python_lock_git_source_is_held_even_on_a_public_host():
    with pytest.raises(DependencySourceError, match="unapproved Python package source"):
        _validate("uv.lock", '[[package]]\nname="example"\nsource={git="https://github.com/example/pkg"}\n')


@pytest.mark.parametrize("setting", [
    '[[tool.uv.index]]\nurl="https://unlisted.example/simple"',
    '[tool.uv]\nfind-links=["https://unlisted.example/wheels"]',
    '[tool.uv.sources]\nexample={git="https://github.com/example/pkg"}',
    '[tool.uv.pip]\nindex-url="https://unlisted.example/simple"',
    '[project]\ndependencies=["pkg @ https://unlisted.example/pkg.whl"]',
    '[build-system]\nrequires=["pkg @ git+https://github.com/example/pkg"]',
])
def test_project_direct_sources_and_index_overrides_require_review(setting):
    with pytest.raises(DependencySourceError):
        _validate("deploy/omnigent-app/pyproject.toml", setting)


@pytest.mark.parametrize("path", ["requirements.txt", "requirements-dev.txt",
                                  "assets/artifacts/omnigent-0.19.1.lock",
                                  "assets/artifacts/agentbricks-0.6.0.lock"])
@pytest.mark.parametrize("line", [
    "--extra-index-url https://unlisted.example/simple", "--find-links https://unlisted.example/wheels",
    "example @ https://unlisted.example/example.whl", "git+https://github.com/example/pkg",
    "-r https://unlisted.example/requirements.txt", "--trusted-host pypi.org",
    "../local.whl", "/home/laptop/example.whl", "example-1.0-py3-none-any.whl", "ftp://host/pkg.whl",
])
def test_exports_and_hash_locks_cannot_override_public_sources(path, line):
    with pytest.raises(DependencySourceError):
        _validate(path, "example==1.0 --hash=sha256:" + "a" * 64 + "\n" + line + "\n")


def test_nested_requirement_must_be_part_of_the_checked_snapshot():
    files = {"requirements-dev.in": b"-r requirements.in\n", "requirements.in": b"example==1.0\n"}
    validate_source_files(files)
    with pytest.raises(DependencySourceError, match="present in the validated snapshot"):
        validate_source_files({"requirements-dev.in": files["requirements-dev.in"]})
    files["requirements.in"] += b"--extra-index-url https://unlisted.example/simple\n"
    with pytest.raises(DependencySourceError, match="non-public Python index"):
        validate_source_files(files)


@pytest.mark.parametrize("replacement", ["https://unlisted.example/", "https://npm-proxy.cloud.databricks.com/",
                                           "https://user:secret@registry.npmjs.org/", "http://registry.npmjs.org/"])
def test_every_npm_resolved_artifact_is_checked_including_json_escapes(replacement):
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
    dependency = next(entry for entry in lock["packages"].values() if "resolved" in entry)
    dependency["resolved"] = dependency["resolved"].replace("https://registry.npmjs.org/", replacement)
    text = json.dumps(lock).replace("/", "\\/")
    with pytest.raises(DependencySourceError, match="non-public resolved npm artifact") as error:
        _validate("frontend/package-lock.json", text)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("spec", ["file:../pkg", "git+https://github.com/example/pkg", "github:example/pkg",
                                  "example/pkg", "https://unlisted.example/pkg.tgz", "ssh://host/pkg"])
def test_npm_dependencies_and_nested_overrides_are_checked(spec):
    with pytest.raises(DependencySourceError, match="npm"):
        _validate("frontend/package.json", json.dumps({"overrides": {"parent": {"child": spec}}}))


def test_public_npm_alias_and_pypi_artifact_are_accepted():
    _validate("frontend/package.json", '{"dependencies":{"alias":"npm:example@1.0"}}')
    _validate("requirements.txt", "example @ https://files.pythonhosted.org/packages/example.whl\n")


@pytest.mark.parametrize("text", ["registry=https://unlisted.example/", "@scope:registry=https://unlisted.example/",
                                  "//registry.npmjs.org/:_authToken=secret", "registry=${LAPTOP_REGISTRY}"])
def test_deployable_npm_config_cannot_select_an_unreviewed_source(text):
    with pytest.raises(DependencySourceError):
        _validate("frontend/.npmrc", text)


@pytest.mark.parametrize("name", ["PIP_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_INDEX_CUSTOM_URL",
                                  "UV_FIND_LINKS", "NPM_CONFIG_REGISTRY", "npm_config_registry"])
def test_actual_app_environment_is_checked_after_yaml_patches(name):
    with pytest.raises(DependencySourceError, match="registry|sources"):
        _validate("app.yaml", f'env: [{{name: "{name}", value: "https://unlisted.example/simple"}}]')
    with pytest.raises(DependencySourceError, match="explicit public values"):
        _validate("deploy/omnigent-app/app.yaml", f'env: [{{name: "{name}", valueFrom: secret}}]')


def test_registry_credentials_in_yaml_anchors_are_held():
    with pytest.raises(DependencySourceError):
        _validate("app.yaml", 'registry: &registry "https://user:secret@pypi.org/simple"\nenv:\n'
                  '  - {name: UV_DEFAULT_INDEX, value: *registry}\n')


@pytest.mark.parametrize("source", ["https://unlisted.example/pkg.tgz", "https://npm-proxy.cloud.databricks.com/pkg.tgz",
                                    "https://user:secret@registry.npmjs.org/pkg.tgz", "../outside.lock"])
def test_every_artifact_manifest_source_is_reviewed(source):
    value = json.loads((ROOT / "assets/artifacts/manifest.json").read_text())
    value["artifacts"]["codex_npm_launcher_package"]["source"] = source
    with pytest.raises(DependencySourceError):
        _validate("assets/artifacts/manifest.json", json.dumps(value))


def test_invalid_sources_are_held_before_pex_packaging(monkeypatch, tmp_path):
    from scripts import build_release
    monkeypatch.setattr(build_release, "ROOT", tmp_path)
    called = []
    monkeypatch.setattr(build_release, "validate_build_host", lambda: called.append("host"))
    monkeypatch.setattr(build_release, "build_pex", lambda *args: called.append("pex"))
    with pytest.raises(DependencySourceError, match="missing"):
        build_release.main(["--output-dir", str(tmp_path / "dist"), "--allow-dirty"])
    assert called == []
    assert not (tmp_path / "dist").exists()


def test_clean_checkout_gate_includes_untracked_source(monkeypatch):
    import dependency_sources
    monkeypatch.setattr(dependency_sources.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 0, stdout="?? local-config.yaml\n"))
    with pytest.raises(DependencySourceError, match="dirty checkout"):
        validate_release_inputs(ROOT, require_clean=True)


def test_working_tree_source_validation_does_not_rewrite_locks_or_configs():
    paths = [ROOT / "uv.lock", ROOT / "frontend/package-lock.json", ROOT / "deploy/omnigent-app/uv.lock"]
    before = [path.read_bytes() for path in paths]
    validate_release_inputs(ROOT)
    assert [path.read_bytes() for path in paths] == before


def test_ci_release_and_both_app_manifests_select_public_registries():
    import yaml
    for path in (".github/workflows/ci.yml", ".github/workflows/release.yml",
                 ".github/workflows/skills-freshness.yml"):
        config = yaml.safe_load((ROOT / path).read_text())
        assert config["env"]["UV_DEFAULT_INDEX"] == "https://pypi.org/simple"
        assert config["env"]["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org/"
        assert "python scripts/check_dependency_sources.py" in (ROOT / path).read_text()
    for path in ("app.yaml", "deploy/omnigent-app/app.yaml"):
        env = {entry["name"]: entry.get("value") for entry in yaml.safe_load((ROOT / path).read_text())["env"]}
        assert env["UV_DEFAULT_INDEX"] == env["PIP_INDEX_URL"] == "https://pypi.org/simple"
        assert env["NPM_CONFIG_REGISTRY"] == env["npm_config_registry"] == "https://registry.npmjs.org/"


def test_deployed_python_installers_ignore_inherited_laptop_source_configuration():
    from server.bootstrap.package_sources import public_python_environment
    inherited = {"UV_INDEX_CUSTOM_URL": "https://private.example/simple", "UV_CONFIG_FILE": "/home/laptop/uv.toml",
                 "PIP_INDEX_URL": "https://private.example/simple", "PIP_EXTRA_INDEX_URL": "https://private.example/simple",
                 "UV_FIND_LINKS": "/home/laptop/wheels", "PIP_TRUSTED_HOST": "private.example", "PATH": "/bin"}
    runtime = public_python_environment(inherited)
    assert runtime["UV_DEFAULT_INDEX"] == runtime["UV_INDEX_URL"] == runtime["PIP_INDEX_URL"] == "https://pypi.org/simple"
    assert runtime["UV_NO_CONFIG"] == "true"
    assert runtime["PIP_CONFIG_FILE"] == "/dev/null"
    assert not any("private.example" in value or "/home/laptop" in value for value in runtime.values())
    assert inherited["UV_INDEX_CUSTOM_URL"].startswith("https://private.example")
    assert runtime["PATH"] == "/bin"


@pytest.mark.parametrize("path,text", [
    ("uv.toml", '[[index]]\nurl="https://unlisted.example/simple"'),
    ("pip.conf", '[global]\nindex-url=https://unlisted.example/simple'),
    ("deploy/omnigent-app/pip.ini", '[install]\nfind-links=/home/laptop/wheels'),
    ("app.yaml", 'env: [{name: PIP_CONFIG_FILE, value: /home/laptop/pip.conf}]'),
    ("deploy/omnigent-app/app.yaml", 'env: [{name: npm_config_userconfig, value: /home/laptop/.npmrc}]'),
])
def test_additional_config_files_and_deployed_config_redirects_are_validated(path, text):
    with pytest.raises(DependencySourceError):
        _validate(path, text)


@pytest.mark.parametrize("option", ["-r", "--requirement", "--requirement=", "-c", "--constraint", "--constraint="])
def test_custom_named_requirement_and_constraint_includes_are_checked_recursively(option):
    files = {"requirements.txt": (option + " deps.txt\n").encode(),
             "deps.txt": b"-r another.lock\n",
             "another.lock": b"--index-url https://unlisted.example/simple\n"}
    with pytest.raises(DependencySourceError, match="non-public Python index"):
        validate_source_files(files)
    files["another.lock"] = b"example==1.0\n"
    validate_source_files(files)
    files["another.lock"] = b"-r deps.txt\n"
    with pytest.raises(DependencySourceError, match="cyclic requirements"):
        validate_source_files(files)


@pytest.mark.parametrize("config", [
    '[tool.uv.pip]\nfind-links=["https://unlisted.example/wheels"]',
    '[tool.uv]\noverride-dependencies=["example @ https://unlisted.example/pkg.whl"]',
    '[tool.uv]\nconstraint-dependencies=["example @ https://unlisted.example/pkg.whl"]',
])
def test_nested_uv_pip_source_options_and_dependency_constraints_are_checked(config):
    with pytest.raises(DependencySourceError):
        _validate("pyproject.toml", config)


@pytest.mark.parametrize("key", ["index_url", "extra_index_url", "--extra-index-url", "find_links", "trusted_host"])
def test_pip_configuration_source_key_aliases_are_normalized(key):
    with pytest.raises(DependencySourceError):
        _validate("pip.conf", "[global]\n" + key + "=https://unlisted.example/simple\n")


def test_deployed_npm_registry_selects_root_not_a_package_or_unreviewed_subpath():
    with pytest.raises(DependencySourceError, match="public registry root"):
        _validate("app.yaml", 'env: [{name: NPM_CONFIG_REGISTRY, value: "https://registry.npmjs.org/custom/"}]')
