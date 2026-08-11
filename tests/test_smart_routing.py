"""Unit tests for deploy/omnigent-app/smart_routing.py (no live Omnigent import)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

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


def test_smart_routing_disabled_by_default():
    assert smart_routing.smart_routing_enabled({}) is False
    assert smart_routing.smart_routing_enabled({"WORKSHOP_SMART_ROUTING": "false"}) is False


def test_smart_routing_enabled_truthy_values():
    for value in ("true", "TRUE", "1", "yes", "on"):
        assert smart_routing.smart_routing_enabled({"WORKSHOP_SMART_ROUTING": value})


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
    client = SimpleNamespace(
        config=SimpleNamespace(host="https://from-client.cloud.databricks.com")
    )
    url = smart_routing.routing_base_url(workspace_client=client, env={})
    assert url == "https://from-client.cloud.databricks.com/ai-gateway/routing/v1"


def test_build_routing_client_returns_none_when_disabled():
    client = SimpleNamespace(config=SimpleNamespace(host="https://x.example", authenticate=lambda: {}))
    assert smart_routing.build_routing_client(client, env={}) is None


def test_workspace_client_bearer_auth_sets_authorization():
    httpx = pytest.importorskip("httpx")

    class _Client:
        class config:
            @staticmethod
            def authenticate():
                return {"Authorization": "Bearer rotated-token"}

    auth = smart_routing.WorkspaceClientBearerAuth(_Client())
    request = httpx.Request("POST", "https://example.com/routes:select")
    flows = list(auth.auth_flow(request))
    assert flows == [request]
    assert request.headers["Authorization"] == "Bearer rotated-token"
