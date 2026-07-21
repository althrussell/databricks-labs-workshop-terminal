"""Control Tower simulator deployment overrides."""

from scripts.deploy_ct_sim import _patch_app_yaml


APP_YAML = b"""env:
  - name: ANTHROPIC_MODEL
    value: ""
  - name: CODEX_MODEL
    value: ""
"""


def test_patch_app_yaml_sets_explicit_model_overrides():
    patched = _patch_app_yaml(
        APP_YAML,
        pat="",
        enable_obo=False,
        enable_ent=False,
        catalog="",
        scopes="",
        anthropic_model="databricks-claude-opus-4-8",
        codex_model="databricks-gpt-5-5",
    )

    assert b'value: "databricks-claude-opus-4-8"' in patched
    assert b'value: "databricks-gpt-5-5"' in patched


def test_patch_app_yaml_leaves_model_defaults_when_overrides_omitted():
    patched = _patch_app_yaml(
        APP_YAML,
        pat="",
        enable_obo=False,
        enable_ent=False,
        catalog="",
        scopes="",
        anthropic_model="",
        codex_model="",
    )

    assert patched == APP_YAML
