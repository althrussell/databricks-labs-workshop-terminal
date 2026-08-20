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
# Mirrors the 0.10.0 signatures this module binds to. If upstream changes one,
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
        harness_bars_model=_fake_harness_bars_model,
    )
    return built


# Upstream's SMART_ROUTING_PI_EXCLUDED, folded the way upstream folds it: under
# pi the harness gateway 400s on every gpt-5.5/5.6 reasoning arm and on Claude
# Haiku. This is the guard the external path applies and the judge does not.
_PI_EXCLUDED = (
    "system.ai.claude-haiku-4-5",
    "system.ai.gpt-5-5",
    "system.ai.gpt-5-5-pro",
    "system.ai.gpt-5-6-luna",
    "system.ai.gpt-5-6-terra",
    "system.ai.gpt-5-6-sol",
)


def _fake_harness_bars_model(harness, model, **_kwargs):
    if harness != "pi":
        return False
    fold = smart_routing._bare_model_id
    return fold(model) in {fold(m) for m in _PI_EXCLUDED}


# --- Enablement and configuration -----------------------------------------


def test_smart_routing_enabled_by_default():
    # 0.10.0 falls back to the built-in judge when the account has no routing
    # API, so Auto no longer depends on a flag we cannot turn on.
    assert smart_routing.smart_routing_enabled({}) is True


def test_smart_routing_can_be_switched_off():
    for value in ("false", "FALSE", "0", "no", "off", ""):
        assert smart_routing.smart_routing_enabled({"WORKSHOP_SMART_ROUTING": value}) is False


def test_smart_routing_enabled_truthy_values():
    for value in ("true", "TRUE", "1", "yes", "on"):
        assert smart_routing.smart_routing_enabled({"WORKSHOP_SMART_ROUTING": value})


def test_judge_model_defaults_and_override():
    assert smart_routing.judge_model({}) == "system.ai.gpt-5-6-luna"
    assert smart_routing.judge_model({"WORKSHOP_ROUTING_JUDGE_MODEL": "  "}) == (
        "system.ai.gpt-5-6-luna"
    )
    assert (
        smart_routing.judge_model({"WORKSHOP_ROUTING_JUDGE_MODEL": "system.ai.other"})
        == "system.ai.other"
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
    judge = smart_routing.AppServicePrincipalJudge(
        client, "system.ai.judge", env={}
    )

    first = judge._connection()
    second = judge._connection()

    # Unity AI Gateway's chat-completions surface, which is where the judge's
    # model answers now that the per-model serving endpoints are retired.
    assert first["base_url"] == (
        "https://ws.cloud.databricks.com/ai-gateway/mlflow/v1"
    )
    # A statically bound connection would 401 once the App's ambient OAuth
    # rolled over, which is the whole reason this is rebuilt per call.
    assert first["api_key"] == "first"
    assert second["api_key"] == "second"


def test_judge_rejects_a_credentialless_client():
    judge = smart_routing.AppServicePrincipalJudge(
        SimpleNamespace(config=SimpleNamespace(host="https://ws.example", authenticate=dict)),
        "system.ai.judge",
        env={},
    )
    with pytest.raises(RuntimeError, match="no bearer token"):
        judge._connection()


def test_judge_route_fails_soft_and_reports_why(fake_omnigent, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("model service denied")

    policies = ModuleType("omnigent.runtime.policies")
    builder = ModuleType("omnigent.runtime.policies.builder")
    builder._build_policy_llm_client = _boom
    types_module = ModuleType("omnigent.spec.types")
    types_module.LLMConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "omnigent.runtime.policies", policies)
    monkeypatch.setitem(sys.modules, "omnigent.runtime.policies.builder", builder)
    monkeypatch.setitem(sys.modules, "omnigent.spec", ModuleType("omnigent.spec"))
    monkeypatch.setitem(sys.modules, "omnigent.spec.types", types_module)

    judge = smart_routing.AppServicePrincipalJudge(_client(), "system.ai.judge", env={})
    result = asyncio.run(judge.route("do a thing", {"claude-native": ["m"]}))

    # A routing failure must cost the attendee a fallback, not the turn.
    assert result is None
    assert "model service denied" in judge.last_error


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


# --- Judge menu shaping ----------------------------------------------------
# The judge is the fallback the external router leans on whenever task_v1 picks
# an arm this workspace cannot land, so a wrong candidate list here is a wrong
# answer in front of the room.


def test_the_judge_cannot_pick_a_model_the_harness_bars(fake_omnigent):
    """The screenshot failure: "No available harness can run gpt-5.6-luna".

    Upstream filters the external verdict through the pi bar but hands the
    judge the raw catalog, so the judge answered with a pi/gpt pair that was
    impossible at the moment it was made.
    """
    shaped = smart_routing.shape_judge_menu(
        {
            "pi": [
                "system.ai.gpt-5.6-luna",
                "system.ai.gpt-5.6-sol",
                "system.ai.glm-5-2",
            ]
        },
        env={},
    )

    assert shaped == {"pi": ["system.ai.glm-5-2"]}


def test_a_harness_with_nothing_left_is_dropped_not_offered_empty(fake_omnigent):
    shaped = smart_routing.shape_judge_menu(
        {"pi": ["system.ai.gpt-5.6-luna"], "codex": ["system.ai.gpt-5.6-luna"]},
        env={},
    )

    assert "pi" not in shaped
    assert shaped["codex"] == ["system.ai.gpt-5.6-luna"]


def test_candidates_are_ordered_cheapest_first(fake_omnigent):
    """luna 28 < terra 282 < sol 642 DBU per output token."""
    shaped = smart_routing.shape_judge_menu(
        {
            "codex": [
                "system.ai.gpt-5.6-sol",
                "system.ai.gpt-5.6-luna",
                "system.ai.gpt-5.6-terra",
            ]
        },
        env={},
    )

    assert shaped["codex"] == [
        "system.ai.gpt-5.6-luna",
        "system.ai.gpt-5.6-terra",
        "system.ai.gpt-5.6-sol",
    ]


def test_a_trivial_prompt_on_pi_does_not_buy_the_flagship(fake_omnigent):
    """The regression that reached a live workshop, in full.

    pi bars every GPT arm and Haiku, so its menu is Claude-only. While the cost
    order named the GPT arms alone, every survivor landed in the unranked bucket
    where the sole tiebreak is the model id -- and ``claude-fable-5``, the
    dearest endpoint the workspace serves, sorts ahead of opus and sonnet on the
    letter F. The judge reads position as price, so "hi" bought the flagship.

    Both halves of the fix are asserted here because either alone still fails:
    ordering Claude without excluding fable leaves it reachable as the "no cheap
    branch holds" fallback, and excluding fable without ordering Claude leaves
    opus-5 at the head on the same alphabetical accident.
    """
    shaped = smart_routing.shape_judge_menu(
        {
            "pi": [
                "system.ai.claude-opus-5",
                "system.ai.claude-fable-5",
                "system.ai.claude-sonnet-5",
                "system.ai.claude-haiku-4-5",
                "system.ai.gpt-5.6-luna",
            ]
        },
        env={},
    )

    assert shaped["pi"] == ["system.ai.claude-sonnet-5", "system.ai.claude-opus-5"]


def test_the_dearest_endpoint_is_barred_from_every_harness(fake_omnigent):
    """fable-5 is out of scope for a workshop, not merely last.

    Ordering it last would still hand it to the rubric's "no cheap branch holds"
    fallback, which selects the far end of the list.
    """
    shaped = smart_routing.shape_judge_menu(
        {
            "pi": ["system.ai.claude-fable-5"],
            "claude-sdk": ["system.ai.claude-fable-5", "system.ai.claude-sonnet-5"],
        },
        env={},
    )

    # pi keeps no candidate at all, so it is dropped rather than offered empty.
    assert shaped == {"claude-sdk": ["system.ai.claude-sonnet-5"]}


def test_the_deprecated_generation_is_not_a_candidate_at_all(fake_omnigent):
    """gpt-5.5 prices like sol and is a generation behind — never the answer."""
    shaped = smart_routing.shape_judge_menu(
        {
            "codex": [
                "system.ai.gpt-5.5",
                "system.ai.gpt-5-5-pro",
                "system.ai.gpt-5.6-luna",
            ]
        },
        env={},
    )

    assert shaped["codex"] == ["system.ai.gpt-5.6-luna"]


def test_an_unpriced_model_sorts_after_the_ones_we_priced(fake_omnigent):
    """Not knowing a price is not evidence that it is cheap."""
    shaped = smart_routing.shape_judge_menu(
        {"codex": ["system.ai.claude-opus-4-8", "system.ai.gpt-5.6-sol"]},
        env={},
    )

    assert shaped["codex"] == ["system.ai.gpt-5.6-sol", "system.ai.claude-opus-4-8"]


def test_order_and_exclusions_are_a_values_change_not_a_release(fake_omnigent):
    shaped = smart_routing.shape_judge_menu(
        {"codex": ["system.ai.gpt-5.6-luna", "system.ai.gpt-5.6-sol"]},
        env={
            "WORKSHOP_ROUTING_MODEL_ORDER": "gpt-5-6-sol,gpt-5-6-luna",
            "WORKSHOP_ROUTING_MODEL_EXCLUDE": "",
        },
    )

    assert shaped["codex"] == ["system.ai.gpt-5.6-sol", "system.ai.gpt-5.6-luna"]


def test_the_router_and_catalog_spellings_of_one_arm_compare_equal(fake_omnigent):
    """The router keys arms ``gpt-5-6-sol``; the catalog spells them dotted."""
    assert smart_routing._bare_model_id("system.ai.gpt-5.6-sol") == "gpt-5-6-sol"
    assert smart_routing._bare_model_id("system.ai.GPT-5.6-Sol[1M]") == "gpt-5-6-sol"
    # The retired spelling still folds. Nothing this repo writes produces it any
    # more, but the catalog is the workspace's to spell and a fold that stopped
    # recognising it would silently stop matching arms on a workspace that had
    # not finished migrating — which reads as "routing does nothing".
    assert smart_routing._bare_model_id("databricks-gpt-5.6-sol") == "gpt-5-6-sol"


def test_a_judge_with_no_runnable_candidate_returns_no_verdict(fake_omnigent, monkeypatch):
    """Better unrouted on the session default than routed somewhere dead."""
    built = {}

    policies = ModuleType("omnigent.runtime.policies")
    builder = ModuleType("omnigent.runtime.policies.builder")

    def _build(*_args, **_kwargs):
        built["called"] = True
        return object()

    builder._build_policy_llm_client = _build
    types_module = ModuleType("omnigent.spec.types")
    types_module.LLMConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "omnigent.runtime.policies", policies)
    monkeypatch.setitem(sys.modules, "omnigent.runtime.policies.builder", builder)
    monkeypatch.setitem(sys.modules, "omnigent.spec", ModuleType("omnigent.spec"))
    monkeypatch.setitem(sys.modules, "omnigent.spec.types", types_module)

    judge = smart_routing.AppServicePrincipalJudge(_client(), "system.ai.judge", env={})
    result = asyncio.run(judge.route("do a thing", {"pi": ["system.ai.gpt-5.6-luna"]}))

    assert result is None
    assert "harness bars" in judge.last_error
    # And it did not pay for a judge call to reach that conclusion.
    assert "called" not in built


def test_a_broken_shaper_costs_the_routing_not_the_turn(fake_omnigent, monkeypatch):
    """Shaping is an optimisation on the menu, so it may not end a turn.

    Its own guard covers importing ``harness_bars_model`` but not calling it,
    and that is upstream code walking model ids whose shape we do not control.
    Everything else in ``route`` already fails open; before this, shaping was
    the one step that could raise straight through it.
    """

    policies = ModuleType("omnigent.runtime.policies")
    builder = ModuleType("omnigent.runtime.policies.builder")
    builder._build_policy_llm_client = lambda *_a, **_k: object()
    types_module = ModuleType("omnigent.spec.types")
    types_module.LLMConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "omnigent.runtime.policies", policies)
    monkeypatch.setitem(sys.modules, "omnigent.runtime.policies.builder", builder)
    monkeypatch.setitem(sys.modules, "omnigent.spec", ModuleType("omnigent.spec"))
    monkeypatch.setitem(sys.modules, "omnigent.spec.types", types_module)

    def _explode(*_args, **_kwargs):
        raise RuntimeError("upstream changed the bar signature")

    monkeypatch.setattr(smart_routing, "shape_judge_menu", _explode)

    judge = smart_routing.AppServicePrincipalJudge(_client(), "system.ai.judge", env={})
    result = asyncio.run(
        judge.route("do a thing", {"pi": ["system.ai.claude-sonnet-5"]})
    )

    assert result is None
    assert "could not shape the judge menu" in judge.last_error


def test_codex_is_never_offered_a_chat_only_model(fake_omnigent):
    """glm-5-2 is upstream's first task_v1 codex arm and codex cannot run it.

    Responses-only CLI, and the gateway refuses the passthrough, so the pick is
    dead before it is made — this is the "glm in the codex model list" report.
    """
    shaped = smart_routing.shape_judge_menu(
        {
            "codex": [
                "system.ai.glm-5-2",
                "system.ai.qwen35-122b-a10b",
                # The retired spelling of a retired model: inert on every
                # catalogue, and still barred, because a stale bar costs a set
                # lookup where a missing one hangs a turn.
                "databricks-kimi-k3",
                "system.ai.gpt-5.6-luna",
            ]
        },
        env={},
    )

    assert shaped["codex"] == ["system.ai.gpt-5.6-luna"]


def test_a_harness_that_speaks_chat_keeps_those_models(fake_omnigent):
    """The bar is per harness, not global: pi runs glm fine."""
    shaped = smart_routing.shape_judge_menu({"pi": ["system.ai.glm-5-2"]}, env={})

    assert shaped["pi"] == ["system.ai.glm-5-2"]
