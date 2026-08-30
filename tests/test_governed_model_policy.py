from __future__ import annotations

import json
import os
import tokenize
import tomllib

import pytest
import yaml

from server import cli_config, gateway_errors, model_policy, spend, wizard_llm
from server.users import User, user_manager
from .conftest import ALICE


def _entry(
    name: str,
    capability: str,
    *,
    principals: list[str] | None = None,
) -> dict:
    return {
        "service_name": name,
        "enabled": True,
        "capabilities": [capability],
        "principal_classes": principals or ["lab_user", "wt_sp"],
        "limit_profile": {
            "name": "workshop-standard",
            "workload_class": "chat",
            "mode": "bounded",
            "default_requester_tpm": 1000,
            "default_requester_qpm": 10,
            "service_tpm": 10000,
            "service_qpm": 100,
            "budget_class": "workshop",
        },
    }


def _body(revision: int = 1) -> dict:
    return {
        "revision": revision,
        "pool": [
            _entry("system.ai.claude-sonnet-5", "claude"),
            _entry("system.ai.gpt-5-6-terra", "codex"),
            _entry("system.ai.gpt-oss-120b", "chat"),
        ],
        "denied_models": ["system.ai.claude-opus-4-8"],
        "restart_processes": False,
    }


def _apply(client, body: dict) -> dict:
    response = client.put("/api/admin/model-policy", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_ct_contract_is_authenticated_exact_and_idempotent(client, as_admin):
    first = _apply(client, _body())
    second = _apply(client, _body())

    assert first == {
        "revision": 1,
        "applied": True,
        "changed": True,
        "verified": True,
        "positive_checks": [
            "system.ai.claude-sonnet-5",
            "system.ai.gpt-5-6-terra",
            "system.ai.gpt-oss-120b",
        ],
        "negative_checks": ["system.ai.claude-opus-4-8"],
        "processes_restarted": False,
    }
    assert second["changed"] is False
    assert second["positive_checks"] == first["positive_checks"]


def test_non_admin_cannot_apply_policy(client, as_non_admin):
    response = client.put("/api/admin/model-policy", json=_body())
    assert response.status_code in {401, 403}
    assert model_policy.store.snapshot().revision == 0


def test_ct_managed_terminal_blocks_launch_until_policy_arrives(
    client, launchable_agents, monkeypatch
):
    monkeypatch.setenv("WORKSHOP_MODEL_POLICY_REQUIRED", "true")

    response = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Workshop model permissions are still syncing — try again shortly"
    )


def test_ct_managed_terminal_does_not_discover_models_before_policy(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MODEL_POLICY_REQUIRED", "true")
    monkeypatch.delenv("WORKSHOP_WIZARD_MODEL", raising=False)
    monkeypatch.setattr(
        cli_config,
        "discover_model_services",
        lambda _token: pytest.fail("managed WT must not discover around CT policy"),
    )

    assert model_policy.direct_catalogue() == {}
    assert cli_config.current_model_catalogue("unused") == {}
    with pytest.raises(wizard_llm.ModelUnavailable, match="no wizard model service"):
        wizard_llm._pick_model("unused")


def test_stale_and_same_revision_drift_are_conflicts(client, as_admin):
    _apply(client, _body(2))

    stale = client.put("/api/admin/model-policy", json=_body(1))
    drifted = _body(2)
    drifted["denied_models"] = ["system.ai.gpt-oss-20b"]
    conflict = client.put("/api/admin/model-policy", json=drifted)

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_model_policy"
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "model_policy_revision_conflict"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body["pool"][0].update(service_name="claude-sonnet-5"),
        lambda body: body["pool"][0].update(capabilities=["mystery"]),
        lambda body: body["pool"].append(dict(body["pool"][0])),
        lambda body: body["denied_models"].append(body["pool"][0]["service_name"]),
        lambda body: body.update(restart_processes=True),
    ],
)
def test_malformed_policy_is_rejected_atomically(client, as_admin, mutation):
    body = _body()
    mutation(body)
    response = client.put("/api/admin/model-policy", json=body)
    assert response.status_code == 422
    assert model_policy.store.snapshot().revision == 0


def test_policy_survives_process_state_reset(client, as_admin):
    _apply(client, _body(7))
    model_policy.store.reset_for_tests()

    restored = model_policy.store.snapshot()
    assert restored.revision == 7
    assert restored.allowed_services("chat") == ("system.ai.gpt-oss-120b",)


def test_live_policy_changes_direct_wizard_selection_without_restart(
    client, as_admin, monkeypatch
):
    monkeypatch.setenv("WORKSHOP_WIZARD_MODEL", "system.ai.gpt-oss-120b")
    _apply(client, _body(1))
    assert wizard_llm._pick_model("unused") == "system.ai.gpt-oss-120b"

    next_body = _body(2)
    next_body["pool"] = [
        entry
        for entry in next_body["pool"]
        if entry["service_name"] != "system.ai.gpt-oss-120b"
    ]
    next_body["denied_models"] = [
        "system.ai.claude-opus-4-8",
        "system.ai.gpt-oss-120b",
    ]
    _apply(client, next_body)
    with pytest.raises(wizard_llm.ModelUnavailable, match="not enabled"):
        wizard_llm._pick_model("unused")


@pytest.fixture
def model_policy_env(monkeypatch):
    values = {
        "WORKSHOP_RUN_ID": "run-1",
        "WORKSHOP_UNIT_ID": "unit-2",
        "WORKSHOP_RELEASE_SHA": "a" * 40,
        "DATABRICKS_GATEWAY_HOST": "https://ws.cloud.databricks.com/ai-gateway",
        "OMNIGENT_ENABLED": "true",
        "WORKSHOP_AGENTS": "omnigent,claude,codex",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_generated_configs_use_only_policy_approved_system_models(
    client, as_admin, model_policy_env, monkeypatch, tmp_path
):
    monkeypatch.setattr("server.config.users_root", lambda: str(tmp_path / "users"))
    monkeypatch.setattr(
        cli_config,
        "discover_model_services",
        lambda _token: pytest.fail("the applied policy is authoritative"),
    )
    _apply(client, _body())
    user = User("alice@example.com")
    user.bootstrap_home()

    cli_config.configure_all(user, "secret-token")

    with open(os.path.join(user.home, ".claude", "settings.json")) as handle:
        claude = json.load(handle)
    with open(os.path.join(user.home, ".codex", "config.toml"), "rb") as handle:
        codex = tomllib.load(handle)
    with open(os.path.join(user.home, ".omnigent", "config.yaml")) as handle:
        omnigent = yaml.safe_load(handle)

    claude_env = claude["env"]
    assert claude_env["ANTHROPIC_MODEL"] == "system.ai.claude-sonnet-5"
    assert claude_env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-sonnet-5"
    assert "main.wt_services" not in json.dumps(claude)
    assert codex["model"] == "system.ai.gpt-5-6-terra"
    assert "main.wt_services" not in json.dumps(codex)
    providers = omnigent["providers"]
    assert (
        providers["databricks-gateway"]["anthropic"]["models"]["default"]
        == "system.ai.claude-sonnet-5"
    )
    assert (
        providers["databricks-gateway"]["openai"]["models"]["default"]
        == "system.ai.gpt-5-6-terra"
    )

    header = codex["model_providers"]["databricks"]["http_headers"][
        "Databricks-Ai-Gateway-Request-Tags"
    ]
    assert json.loads(header) == {
        "agent": "codex",
        "workshop_run_id": "run-1",
        "workshop_unit_id": "unit-2",
        "wt_release": "a" * 40,
    }
    assert codex["model_providers"]["databricks"]["request_max_retries"] == 1


def test_policy_replay_refreshes_existing_agent_configs(
    client, as_admin, model_policy_env, monkeypatch, tmp_path
):
    monkeypatch.setattr("server.config.users_root", lambda: str(tmp_path / "users"))
    _apply(client, _body())
    user = user_manager.get("alice@example.com")
    user.bootstrap_home()
    cli_config.configure_all(user, "secret-token")

    codex_path = os.path.join(user.home, ".codex", "config.toml")
    claude_path = os.path.join(user.home, ".claude", "settings.json")
    with open(codex_path, "w", encoding="utf-8") as handle:
        handle.write('model = "stale-service"\n')
    with open(claude_path, "w", encoding="utf-8") as handle:
        json.dump({"env": {"ANTHROPIC_MODEL": "stale-service"}}, handle)

    _apply(client, _body(2))

    with open(codex_path, "rb") as handle:
        codex = tomllib.load(handle)
    with open(claude_path, encoding="utf-8") as handle:
        claude = json.load(handle)
    assert codex["model"] == "system.ai.gpt-5-6-terra"
    assert claude["env"]["ANTHROPIC_MODEL"] == "system.ai.claude-sonnet-5"

    # An idempotent replay is also the recovery path after a partial config
    # refresh failure, so it must run the fanout again instead of returning
    # early merely because the durable revision already exists.
    with open(codex_path, "w", encoding="utf-8") as handle:
        handle.write('model = "stale-again"\n')
    replay = _apply(client, _body(2))
    with open(codex_path, "rb") as handle:
        assert tomllib.load(handle)["model"] == "system.ai.gpt-5-6-terra"
    assert replay["changed"] is False


def test_production_system_model_names_are_confined_to_policy_adapters():
    root = os.path.join(os.path.dirname(__file__), "..", "server")
    approved = {"models.py", "model_policy.py"}
    violations: list[str] = []
    for name in os.listdir(root):
        if not name.endswith(".py") or name in approved:
            continue
        path = os.path.join(root, name)
        with tokenize.open(path) as handle:
            tokens = tokenize.generate_tokens(handle.readline)
            for token in tokens:
                if token.type == tokenize.COMMENT:
                    continue
                if token.type == tokenize.STRING and token.string.startswith(
                    ('"""', "'''")
                ):
                    continue
                if "system.ai." in token.string:
                    violations.append(f"{name}:{token.start[0]}")
    assert violations == []


class _Response:
    def __init__(self, body: dict, retry_after: str | None = None):
        self.status_code = 429
        self._body = body
        self.headers = {"retry-after": retry_after} if retry_after else {}

    def json(self):
        return self._body


def test_gateway_429_copy_distinguishes_rate_from_exhausted_allowance():
    temporary = gateway_errors.classify(
        _Response({"error": {"code": "rate_limit", "message": "TPM exceeded"}}, "12")
    )
    exhausted = gateway_errors.classify(
        _Response({"error": {"code": "budget_exhausted"}})
    )
    assert temporary is not None and temporary.reason == "gateway_rate_limited"
    assert "12 seconds" in temporary.message
    assert exhausted is not None and exhausted.reason == "gateway_allowance_exhausted"
    assert exhausted.retry_after_seconds is None


def test_emergency_disable_can_terminate_the_only_active_session(
    client, as_admin, launchable_agents
):
    spend.reset()
    launched = client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)
    assert launched.status_code == 200

    stopped = client.post(
        "/api/admin/agent-controls",
        json={
            "enabled": False,
            "terminate_active": True,
            "reason": "gateway incident",
        },
    )
    assert stopped.status_code == 200
    assert stopped.json()["terminated_active"] is True
    assert client.get("/api/sessions", headers=ALICE).json()["sessions"] == []
