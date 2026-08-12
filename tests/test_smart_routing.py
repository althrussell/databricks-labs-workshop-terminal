"""Unit tests for deploy/omnigent-app/smart_routing.py (no live Omnigent import).

Omnigent is not a Workshop Terminal test dependency — it only exists inside the
App image — so the few tests that need its types stand up fake modules. What
they are really pinning is our wiring: which backends get built, what settings
they carry, and that a disabled deployment produces bare caps.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "deploy" / "omnigent-app"


def _load_smart_routing():
    spec = importlib.util.spec_from_file_location(
        "workshop_smart_routing", APP_DIR / "smart_routing.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smart_routing = _load_smart_routing()


def _client(host: str = "https://x.cloud.databricks.com", token: str = "tok"):
    return SimpleNamespace(
        config=SimpleNamespace(
            host=host,
            authenticate=lambda: {"Authorization": f"Bearer {token}"},
        )
    )


# --- Fake Omnigent surface -------------------------------------------------
# Mirrors the 0.9.0 signatures this module binds to. If upstream changes one,
# these fakes go stale silently — which is why the live App build is also
# exercised against the real wheel before release.


@dataclass
class _FakeRoutingSettings:
    router_name: str = "task_v1"
    selection_model: str | None = None
    model_prefixes: tuple[str, ...] = ("databricks-", "system.ai.")
    menus: Any = None
    servable_aliases: Any = None


@dataclass
class _FakeRoutingBackends:
    external: Any = None
    local: Any = None

    def any(self):
        return self.external or self.local


@dataclass
class _FakeRuntimeCaps:
    routing_client: Any = None
    routing_backends: Any = None
    routing_settings: Any = None


@dataclass
class _FakeExternalRoutingClient:
    kwargs: dict = field(default_factory=dict)


@pytest.fixture
def fake_omnigent(monkeypatch):
    """Install a minimal fake ``omnigent`` package for the duration of a test."""
    built: dict[str, Any] = {}

    def _module(name: str, **attrs) -> ModuleType:
        module = ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
        return module

    def _external(**kwargs):
        client = _FakeExternalRoutingClient(kwargs=kwargs)
        built["external"] = client
        return client

    _module("omnigent")
    _module("omnigent.runtime")
    _module("omnigent.runtime.caps", RuntimeCaps=_FakeRuntimeCaps)
    _module("omnigent.server")
    _module("omnigent.server.routing_backend", RoutingBackends=_FakeRoutingBackends)
    _module(
        "omnigent.server.smart_routing",
        RoutingSettings=_FakeRoutingSettings,
        ExternalRoutingClient=_external,
        LLMRoutingClient=object,
    )
    return built


# --- Enablement and configuration -----------------------------------------


def test_smart_routing_enabled_by_default():
    # 0.9.0 falls back to the built-in judge when the account has no routing
    # API, so Auto no longer depends on a flag we cannot turn on.
    assert smart_routing.smart_routing_enabled({}) is True


def test_smart_routing_can_be_switched_off():
    for value in ("false", "FALSE", "0", "no", "off", ""):
        assert smart_routing.smart_routing_enabled({"WORKSHOP_SMART_ROUTING": value}) is False


def test_smart_routing_enabled_truthy_values():
    for value in ("true", "TRUE", "1", "yes", "on"):
        assert smart_routing.smart_routing_enabled({"WORKSHOP_SMART_ROUTING": value})


def test_judge_model_defaults_and_override():
    assert smart_routing.judge_model({}) == "databricks-gpt-5-6-luna"
    assert smart_routing.judge_model({"WORKSHOP_ROUTING_JUDGE_MODEL": "  "}) == (
        "databricks-gpt-5-6-luna"
    )
    assert (
        smart_routing.judge_model({"WORKSHOP_ROUTING_JUDGE_MODEL": "databricks-other"})
        == "databricks-other"
    )


def test_routing_base_url_from_databricks_host():
    url = smart_routing.routing_base_url(
        env={"DATABRICKS_HOST": "https://dbc-example.cloud.databricks.com"}
    )
    assert url == "https://dbc-example.cloud.databricks.com/ai-gateway/routing/v1"


def test_routing_base_url_explicit_override():
    url = smart_routing.routing_base_url(
        env={
            "DATABRICKS_HOST": "https://ignored.example",
            "WORKSHOP_ROUTING_BASE_URL": "https://custom.example/routing/v1/",
        }
    )
    assert url == "https://custom.example/routing/v1"


def test_routing_base_url_from_workspace_client():
    url = smart_routing.routing_base_url(workspace_client=_client("https://c.cloud.databricks.com"), env={})
    assert url == "https://c.cloud.databricks.com/ai-gateway/routing/v1"


def test_workspace_host_adds_scheme_and_prefers_env():
    assert smart_routing.workspace_host(_client(), {"DATABRICKS_HOST": "bare.example"}) == (
        "https://bare.example"
    )
    assert smart_routing.workspace_host(_client("https://from-client.example"), {}) == (
        "https://from-client.example"
    )


def test_routing_settings_keeps_upstream_defaults(fake_omnigent):
    settings = smart_routing.build_routing_settings({})
    assert settings.router_name == "task_v1"
    assert settings.model_prefixes == ("databricks-", "system.ai.")


def test_routing_settings_applies_overrides(fake_omnigent):
    settings = smart_routing.build_routing_settings(
        {
            "WORKSHOP_ROUTING_ROUTER_NAME": "task_v2",
            "WORKSHOP_ROUTING_MODEL_PREFIXES": "a-, b- ,",
        }
    )
    assert settings.router_name == "task_v2"
    assert settings.model_prefixes == ("a-", "b-")


# --- The judge -------------------------------------------------------------


def test_judge_reads_a_fresh_credential_per_connection():
    tokens = iter(["first", "second"])
    client = SimpleNamespace(
        config=SimpleNamespace(
            host="https://ws.cloud.databricks.com",
            authenticate=lambda: {"Authorization": f"Bearer {next(tokens)}"},
        )
    )
    judge = smart_routing.AppServicePrincipalJudge(client, "databricks-judge", env={})

    first = judge._connection()
    second = judge._connection()

    assert first["base_url"] == "https://ws.cloud.databricks.com/serving-endpoints"
    # A statically bound connection would 401 once the App's ambient OAuth
    # rolled over, which is the whole reason this is rebuilt per call.
    assert first["api_key"] == "first"
    assert second["api_key"] == "second"


def test_judge_rejects_a_credentialless_client():
    judge = smart_routing.AppServicePrincipalJudge(
        SimpleNamespace(config=SimpleNamespace(host="https://ws.example", authenticate=dict)),
        "databricks-judge",
        env={},
    )
    with pytest.raises(RuntimeError, match="no bearer token"):
        judge._connection()


def test_judge_route_fails_soft_and_reports_why(fake_omnigent, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("serving endpoint denied")

    policies = ModuleType("omnigent.runtime.policies")
    builder = ModuleType("omnigent.runtime.policies.builder")
    builder._build_policy_llm_client = _boom
    types_module = ModuleType("omnigent.spec.types")
    types_module.LLMConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "omnigent.runtime.policies", policies)
    monkeypatch.setitem(sys.modules, "omnigent.runtime.policies.builder", builder)
    monkeypatch.setitem(sys.modules, "omnigent.spec", ModuleType("omnigent.spec"))
    monkeypatch.setitem(sys.modules, "omnigent.spec.types", types_module)

    judge = smart_routing.AppServicePrincipalJudge(_client(), "databricks-judge", env={})
    result = asyncio.run(judge.route("do a thing", {"claude-native": ["m"]}))

    # A routing failure must cost the attendee a fallback, not the turn.
    assert result is None
    assert "serving endpoint denied" in judge.last_error


# --- Runtime caps ----------------------------------------------------------


def test_build_runtime_caps_is_bare_when_disabled(fake_omnigent):
    caps = smart_routing.build_runtime_caps(_client(), env={"WORKSHOP_SMART_ROUTING": "false"})
    assert caps.routing_client is None
    assert caps.routing_backends is None
    assert caps.routing_settings is None


def test_build_runtime_caps_attaches_both_backends(fake_omnigent):
    caps = smart_routing.build_runtime_caps(_client(), env={})

    assert isinstance(caps.routing_backends.external, _FakeExternalRoutingClient)
    assert isinstance(caps.routing_backends.local, smart_routing.AppServicePrincipalJudge)
    # Upstream prefers the external router and falls back to the judge, so the
    # single-client field has to be the external one where both exist.
    assert caps.routing_client is caps.routing_backends.external
    assert caps.routing_settings.router_name == "task_v1"

    kwargs = fake_omnigent["external"].kwargs
    assert kwargs["base_url"] == "https://x.cloud.databricks.com/ai-gateway/routing/v1"
    assert kwargs["router_name"] == "task_v1"
    assert kwargs["model_prefixes"] == ["databricks-", "system.ai."]


def test_build_runtime_caps_still_routes_when_the_external_router_cannot_be_built(
    fake_omnigent, monkeypatch
):
    monkeypatch.setattr(smart_routing, "build_external_routing_client", lambda *a, **k: None)

    caps = smart_routing.build_runtime_caps(_client(), env={})

    assert caps.routing_backends.external is None
    assert caps.routing_client is caps.routing_backends.local


def test_build_runtime_caps_is_bare_when_no_backend_can_be_built(fake_omnigent, monkeypatch):
    monkeypatch.setattr(smart_routing, "build_external_routing_client", lambda *a, **k: None)
    monkeypatch.setattr(smart_routing, "build_judge_routing_client", lambda *a, **k: None)

    caps = smart_routing.build_runtime_caps(_client(), env={})

    assert caps.routing_client is None
    assert caps.routing_backends is None


def test_workspace_client_bearer_auth_sets_authorization():
    httpx = pytest.importorskip("httpx")

    auth = smart_routing.WorkspaceClientBearerAuth(_client(token="rotated-token"))
    request = httpx.Request("POST", "https://example.com/routes:select")
    flows = list(auth.auth_flow(request))

    assert flows == [request]
    assert request.headers["Authorization"] == "Bearer rotated-token"
