"""The ai-gateway user scope on a deployed App: declared before its first deploy, never removed.

An App calls models as the attendee (OBO), so its bundle must declare the
``ai-gateway`` user authorization scope. These tests pin the bundle edit, the
checks around it, and what the agent is told after a deploy.
"""

from __future__ import annotations

import pytest

from tests.test_app_deploy_tool import (
    FakeBackend,
    _app,
    _controller,
    _env,
    deploy_tool,
)

yaml = pytest.importorskip("yaml")  # independent parser for the edited bundle (transitive dependency)

APX_BUNDLE = """bundle:
  name: demo

sync:
  include:
    - .build

resources:
  apps:
    demo:
      name: demo-app
      description: "demo created with apx"
      source_code_path: ./.build

targets:
  dev:
    mode: development
    default: true
"""


def _scopes(text: str, key: str = "demo") -> list[str]:
    return list(yaml.safe_load(text)["resources"]["apps"][key].get("user_api_scopes") or [])


# -- the text edit ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "before"),
    [
        (APX_BUNDLE, []),
        (
            "resources:\n  apps:\n    demo:\n      name: demo\n      user_api_scopes:\n"
            "        - sql\n        - files  # keep this\n      source_code_path: ./\n",
            ["sql", "files"],
        ),
        (
            "resources:\n  apps:\n    demo:\n      name: demo\n      user_api_scopes: [sql, \"files\"]  # note\n",
            ["sql", "files"],
        ),
        ("resources:\n  apps:\n    demo:\n      name: demo\n      user_api_scopes: []\n", []),
        ("resources:\n  apps:\n    'demo':\n      name: demo\n", []),
        # YAML allows an indentless sequence under a mapping key: the items sit at the key's own indent.
        (
            "resources:\n  apps:\n    demo:\n      name: demo\n      user_api_scopes:\n      - sql\n      - files\n"
            "      source_code_path: ./\n",
            ["sql", "files"],
        ),
        ("resources:\n  apps:\n    demo:\n      name: demo", []),  # no trailing newline
        ("resources:\r\n  apps:\r\n    demo:\r\n      name: demo\r\n", []),  # CRLF
        # Colon-qualified scope names (such as catalog.catalogs:read) are scalar scopes, not mappings.
        (
            "resources:\n  apps:\n    demo:\n      name: demo\n      user_api_scopes:\n"
            "        - catalog.catalogs:read\n",
            ["catalog.catalogs:read"],
        ),
    ],
)
def test_the_scope_is_added_and_every_existing_scope_kept(text, before):
    edited = deploy_tool._yaml_add_ai_gateway(text, "demo")

    assert edited is not None and edited != text
    if "\r\n" in text:
        assert "\r\n" in edited
    after = _scopes(edited.replace("\r\n", "\n"))
    assert after == [*before, "ai-gateway"]


def test_an_app_that_already_declares_the_scope_is_left_byte_identical():
    text = APX_BUNDLE.replace("source_code_path: ./.build", "source_code_path: ./.build\n      user_api_scopes:\n        - ai-gateway")

    assert deploy_tool._yaml_add_ai_gateway(text, "demo") == text


def test_the_inserted_block_keeps_the_app_mapping_intact():
    edited = deploy_tool._yaml_add_ai_gateway(APX_BUNDLE, "demo")
    app = yaml.safe_load(edited)["resources"]["apps"]["demo"]

    assert app == {
        "name": "demo-app",
        "description": "demo created with apx",
        "source_code_path": "./.build",
        "user_api_scopes": ["ai-gateway"],
    }
    assert yaml.safe_load(edited)["targets"] == {"dev": {"mode": "development", "default": True}}


@pytest.mark.parametrize(
    ("text", "key"),
    [
        (APX_BUNDLE, "other"),  # the key is not an app in this bundle
        ("resources:\n  apps:\n    demo:\n      name: demo\n      user_api_scopes: sql\n", "demo"),  # scalar scopes
        ("resources:\n  apps: {demo: {name: demo}}\n", "demo"),  # flow mapping this reader does not edit
        ("bundle:\n  name: demo\n", "demo"),  # no resources at all
        # object-valued scope entries: the reader would have to understand them, so it refuses
        ("resources:\n  apps:\n    demo:\n      name: demo\n      user_api_scopes:\n      - sql\n      - name: x\n        other: y\n", "demo"),
        ("resources:\n  apps:\n    demo:\n      name: demo\n      user_api_scopes:\n      - name: sql\n", "demo"),
        ("resources:\n  apps:\n    demo:\n      name: demo\n      user_api_scopes: [{name: sql}]\n", "demo"),
    ],
)
def test_a_form_the_editor_cannot_edit_safely_is_refused_not_guessed(text, key):
    assert deploy_tool._yaml_add_ai_gateway(text, key) is None


def test_app_keys_are_read_from_the_apps_mapping():
    text = APX_BUNDLE.replace(
        "    demo:\n",
        "    demo:\n      name: demo-app\n      source_code_path: ./a\n    second:\n      name: second\n",
    )

    assert deploy_tool._yaml_app_keys(text) == ["demo", "second"]


# -- declaring in a project (the scaffold path) ----------------------------------

def test_the_scaffold_declares_the_scope_once_and_reports_the_file(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "databricks.yml").write_text(APX_BUNDLE)

    assert deploy_tool.declare_ai_gateway_in_project(project) == ["databricks.yml"]
    assert _scopes((project / "databricks.yml").read_text()) == ["ai-gateway"]
    assert deploy_tool.declare_ai_gateway_in_project(project) == []


def test_an_app_in_a_resources_file_is_declared_too(tmp_path):
    project = tmp_path / "proj"
    (project / "resources").mkdir(parents=True)
    (project / "databricks.yml").write_text("bundle:\n  name: demo\ninclude:\n  - resources/*.yml\n")
    (project / "resources" / "app.yml").write_text(APX_BUNDLE.split("\ntargets")[0].replace("bundle:\n  name: demo\n", ""))

    assert deploy_tool.declare_ai_gateway_in_project(project) == ["app.yml"]
    assert _scopes((project / "resources" / "app.yml").read_text()) == ["ai-gateway"]


def test_an_unsupported_form_fails_the_scaffold_without_touching_the_file(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    text = "resources:\n  apps:\n    demo:\n      name: demo\n      user_api_scopes: sql\n"
    (project / "databricks.yml").write_text(text)

    with pytest.raises(deploy_tool.DeployError, match="Add `- ai-gateway`"):
        deploy_tool.declare_ai_gateway_in_project(project)
    assert (project / "databricks.yml").read_text() == text


def test_the_cli_flag_declares_the_scope_without_deploying(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "databricks.yml").write_text(APX_BUNDLE)

    assert deploy_tool.main(["--declare-ai-gateway", str(project)]) == 0
    assert _scopes((project / "databricks.yml").read_text()) == ["ai-gateway"]
    assert capsys.readouterr().out == ""


# -- the deploy: the bundle is edited and checked before anything is submitted -----

class BundleBackend(FakeBackend):
    """Resolves the bundle from the project's databricks.yml, as `bundle validate` would."""

    def __init__(self, project, *, before=None, after=None, validate_errors=(), **kwargs):
        super().__init__(**kwargs)
        self.project = project
        self.before = before  # the live App before this deploy; None means it does not exist yet
        self.after = after or _app(active="new", scopes=("sql", "ai-gateway"))
        self.validate_errors = list(validate_errors)  # raised by successive bundle_validate calls
        self.validations = 0

    def bundle_summary(self, _project, _target):
        # The deployed state is not scripted; only the local bundle is.
        return yaml.safe_load((self.project / "databricks.yml").read_text())

    def bundle_validate(self, _project, _target):
        self.validations += 1
        if self.validate_errors:
            error = self.validate_errors.pop(0)
            if error is not None:
                raise deploy_tool.CliError(error)
        return yaml.safe_load((self.project / "databricks.yml").read_text())

    def get_app(self, _project, _name):
        if not self.submitted:
            if self.before is None:
                raise deploy_tool.CliError("app not found")
            return self.before
        return self.after


def test_a_first_deploy_declares_the_scope_before_submitting(tmp_path):
    env, project = _env(tmp_path)
    (project / "databricks.yml").write_text(APX_BUNDLE)
    backend = BundleBackend(project, before=None)

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert backend.submissions == 1
    assert _scopes((project / "databricks.yml").read_text()) == ["ai-gateway"]
    assert "clicks Allow once" in result["message"]


def test_an_existing_app_without_the_scope_gets_it_and_the_attendee_is_asked_to_allow(tmp_path):
    env, project = _env(tmp_path)
    (project / "databricks.yml").write_text(APX_BUNDLE.replace("source_code_path: ./.build", "source_code_path: ./.build\n      user_api_scopes: [sql]"))
    backend = BundleBackend(project, before=_app(scopes=("sql",)))

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert _scopes((project / "databricks.yml").read_text()) == ["sql", "ai-gateway"]
    assert "reopen the App and click Allow" in result["message"]


def test_a_deploy_that_leaves_the_live_app_without_the_scope_says_so(tmp_path):
    env, project = _env(tmp_path)
    (project / "databricks.yml").write_text(APX_BUNDLE)
    backend = BundleBackend(project, before=_app(scopes=("sql",)), after=_app(active="new", scopes=("sql",)))

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert "does not show the ai-gateway scope" in result["message"]


def test_a_deploy_that_strips_an_existing_scope_warns_even_when_present_before(tmp_path):
    env, project = _env(tmp_path)
    declared = APX_BUNDLE.replace("source_code_path: ./.build", "source_code_path: ./.build\n      user_api_scopes: [ai-gateway]")
    (project / "databricks.yml").write_text(declared)
    backend = BundleBackend(project, before=_app(scopes=("ai-gateway",)), after=_app(active="new", scopes=()))

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert "does not show the ai-gateway scope" in result["message"]


def test_a_bundle_that_already_declares_the_scope_is_not_edited_or_checked_again(tmp_path):
    env, project = _env(tmp_path)
    declared = APX_BUNDLE.replace("source_code_path: ./.build", "source_code_path: ./.build\n      user_api_scopes: [ai-gateway]")
    (project / "databricks.yml").write_text(declared)
    backend = BundleBackend(project, before=_app(scopes=("ai-gateway",)))

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert (project / "databricks.yml").read_text() == declared
    assert backend.validations == 0
    assert result["message"] == ""


def test_an_edit_that_drops_an_existing_scope_is_undone_and_nothing_is_deployed(tmp_path):
    env, project = _env(tmp_path)
    original = APX_BUNDLE.replace("source_code_path: ./.build", "source_code_path: ./.build\n      user_api_scopes: [sql]")
    (project / "databricks.yml").write_text(original)

    class LossyBackend(BundleBackend):
        """Once the bundle carries ai-gateway, the resolved bundle drops the existing scope."""

        def bundle_validate(self, project, target):
            resolved = super().bundle_validate(project, target)
            if "ai-gateway" in (self.project / "databricks.yml").read_text():
                resolved["resources"]["apps"]["demo"]["user_api_scopes"] = ["ai-gateway"]
            return resolved

    backend = LossyBackend(project, before=_app(scopes=("sql",)))

    with pytest.raises(deploy_tool.DeployError, match="did not keep the app's scopes"):
        _controller(env, backend).deploy(project_path=str(project))
    assert (project / "databricks.yml").read_text() == original
    assert backend.submissions == 0


def test_an_edit_that_cannot_be_checked_is_undone_and_nothing_is_deployed(tmp_path):
    env, project = _env(tmp_path)
    (project / "databricks.yml").write_text(APX_BUNDLE)
    original = (project / "databricks.yml").read_text()
    # First bundle_validate: the resolved bundle before the edit. Second: the check after it fails.
    backend = BundleBackend(project, before=_app(scopes=()), validate_errors=[None, "validate failed"])

    with pytest.raises(deploy_tool.DeployError, match="could not be checked"):
        _controller(env, backend).deploy(project_path=str(project))
    assert (project / "databricks.yml").read_text() == original
    assert backend.submissions == 0


def test_a_scope_check_that_cannot_run_is_reported_not_silently_skipped(tmp_path):
    env, project = _env(tmp_path)
    (project / "databricks.yml").write_text("bundle:\n  name: demo\n")  # the App is not in a file it can edit

    class Unreadable(FakeBackend):
        def bundle_validate(self, _project, _target):
            raise deploy_tool.CliError("bundle validate timed out")

    result = _controller(env, Unreadable()).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert "could not be checked" in result["message"]
    assert "databricks.yml" in result["message"]


def test_the_cli_prints_the_follow_up_for_the_agent(tmp_path, monkeypatch, capsys):
    env, project = _env(tmp_path)
    (project / "databricks.yml").write_text(APX_BUNDLE)
    backend = BundleBackend(project, before=_app(scopes=("sql",)))
    real_controller = deploy_tool.DeployController  # captured before the patch below
    monkeypatch.setattr(
        deploy_tool,
        "DeployController",
        lambda *_args, **_kwargs: real_controller(env, backend=backend, reporter=lambda r, s: None, poll_seconds=0),
    )

    assert deploy_tool.main(["--project", str(project), "--target", "default"]) == 0
    out = capsys.readouterr().out
    assert "App is live:" in out
    assert "reopen the App and click Allow" in out


def test_the_apx_scaffold_commits_a_bundle_that_declares_the_scope(tmp_path, monkeypatch):
    """workshop-init-project declares the scope before its first commit, so the App is created with it."""
    import json
    import os
    import subprocess
    from pathlib import Path

    from tests.plan_approval import approve_for_env

    root = Path(__file__).resolve().parents[1]
    home, binaries = tmp_path / "home", tmp_path / "bin"
    binaries.mkdir()
    template = home / ".config/workshop/project-memory.md"
    template.parent.mkdir(parents=True)
    template.write_text("APX default\n")
    # A stand-in for `apx init`: writes the APX template's bundle, which declares no scope.
    fake = binaries / "apx"
    fake.write_text(
        """#!/usr/bin/env python3
import pathlib,sys
root=pathlib.Path(sys.argv[2]);root.mkdir(parents=True)
(root/'pyproject.toml').write_text('[tool.apx.metadata]\\n')
(root/'databricks.yml').write_text('''bundle:
  name: scaffolded

resources:
  apps:
    scaffolded-app:
      name: "scaffolded"
      source_code_path: ./.build
''')
"""
    )
    fake.chmod(0o755)
    for name in ("uv", "bun"):
        (binaries / name).symlink_to(fake)
    env = os.environ | {
        "HOME": str(home), "PATH": f"{binaries}:{os.environ['PATH']}",
        "DATABRICKS_CONFIG_PROFILE": "DEFAULT", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    approve_for_env(env, "scoped")

    result = subprocess.run(
        ["bash", str(root / "assets/bin/workshop-init-project"), "scoped", "--apx", "--json"],
        env=env, text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ready"] is True
    project = home / "projects/scoped"
    committed = subprocess.run(
        ["git", "-C", str(project), "show", "HEAD:databricks.yml"], text=True, capture_output=True, check=True,
    ).stdout
    assert _scopes(committed, key="scaffolded-app") == ["ai-gateway"]


def test_a_scope_list_is_not_reported_missing_when_the_payload_omits_it():
    """Unknown is not absent: the agent must not be told to fix a scope the API did not describe."""
    message = deploy_tool.DeployController._scope_message(
        None, {"ai_gateway_before": "unknown"}, {"name": "demo-app"}
    )

    assert "were not returned" in message
    assert "does not show" not in message


def test_a_refusal_in_a_later_bundle_file_leaves_the_earlier_file_unedited(tmp_path):
    project = tmp_path / "proj"
    (project / "resources").mkdir(parents=True)
    (project / "databricks.yml").write_text(APX_BUNDLE)
    refusing = "resources:\n  apps:\n    web:\n      name: web\n      user_api_scopes: sql\n"
    (project / "resources" / "web.yml").write_text(refusing)
    before = (project / "databricks.yml").read_text()

    with pytest.raises(deploy_tool.DeployError, match="Nothing was changed"):
        deploy_tool.declare_ai_gateway_in_project(project)
    assert (project / "databricks.yml").read_text() == before
    assert (project / "resources" / "web.yml").read_text() == refusing


def test_a_bundle_file_that_is_not_utf8_is_a_one_line_refusal(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "databricks.yml").write_bytes("bundle:\n  name: caf\xe9\n".encode("latin-1"))

    assert deploy_tool.main(["--declare-ai-gateway", str(project)]) == 1
    assert "not UTF-8" in capsys.readouterr().err


def test_a_span_replacement_refuses_anchors_out_of_order(tmp_path):
    from server.bootstrap import skill_projection

    page = tmp_path / "page.md"
    page.write_text("## Troubleshooting\nDeclare the model service as a `uc_securable` app resource\n")

    with pytest.raises(RuntimeError, match="out of order"):
        skill_projection._replace_span(
            page, "Declare the model service as a `uc_securable` app resource", "## Troubleshooting", "x", strict=True
        )
    assert page.read_text().startswith("## Troubleshooting")


def test_omitted_user_api_scopes_falls_back_to_effective_scopes_from_apps_get(tmp_path):
    """The Go CLI omits an empty user_api_scopes (omitempty) while returning effective_user_api_scopes."""
    env, project = _env(tmp_path)
    (project / "databricks.yml").write_text(APX_BUNDLE)
    before = {k: v for k, v in _app(scopes=()).items() if k != "user_api_scopes"}
    before["effective_user_api_scopes"] = ["iam.access-control:read", "iam.current-user:read"]
    after = {k: v for k, v in _app(active="new", scopes=()).items() if k != "user_api_scopes"}
    after["effective_user_api_scopes"] = ["iam.access-control:read", "iam.current-user:read"]
    backend = BundleBackend(project, before=before, after=after)

    result = _controller(env, backend).deploy(project_path=str(project))

    assert result["status"] == "succeeded"
    assert "does not show the ai-gateway scope" in result["message"]


def test_an_uneditable_bundle_resource_is_refused_even_when_bundle_validate_errors(tmp_path):
    env, project = _env(tmp_path)
    (project / "databricks.yml").write_text(
        "resources:\n  apps:\n    demo:\n      name: demo-app\n      user_api_scopes: sql\n"
    )
    backend = BundleBackend(project, before=_app(scopes=()), validate_errors=["invalid user_api_scopes"])
    backend.bundle_summary = lambda _p, _t: {
        "resources": {"apps": {"demo": {"name": "demo-app", "url": "https://demo.databricksapps.com"}}}
    }

    with pytest.raises(deploy_tool.DeployError, match="Add `- ai-gateway`"):
        _controller(env, backend).deploy(project_path=str(project))
    assert backend.submissions == 0


def test_llm_config_user_token_skips_control_plane_validation_and_rejects_whitespace(monkeypatch):
    import importlib.machinery
    import importlib.util
    import sys
    import types
    from pathlib import Path

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=lambda **kwargs: kwargs))
    overlay = (
        Path(__file__).resolve().parents[1]
        / "assets/skill-overlays/databricks-apps-python/examples/llm_config.py"
    )
    spec = importlib.util.spec_from_loader(
        "llm_config_overlay_test",
        importlib.machinery.SourceFileLoader("llm_config_overlay_test", str(overlay)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv("DATABRICKS_SERVING_BASE_URL", "https://dbc.example.com/ai-gateway/mlflow/v1")
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_MODEL", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(
        module,
        "_requests_module",
        lambda: (_ for _ in ()).throw(AssertionError("control-plane GET must not run for user_token")),
    )

    client = module.create_foundation_model_client(user_token="  obo-token-123  ")
    assert client == {"api_key": "obo-token-123", "base_url": "https://dbc.example.com/ai-gateway/mlflow/v1"}
    config = module.validate_databricks_llm_config(user_token="  obo-token-123  ")
    assert config.auth_mode == "user-token"
    assert config.model == "system.ai.claude-sonnet-5-5"
    with pytest.raises(module.DatabricksLLMConfigError, match="No Databricks auth configured"):
        module.get_databricks_llm_config(user_token="   ")
