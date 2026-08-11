"""Build Omnigent Auto · smart routing for the workshop control plane.

Uses upstream ``ExternalRoutingClient`` against Databricks AI Gateway
``routes:select``. Candidate models still come from the attendee host's live
runner catalog; this module only supplies the server-side router client and
App-SP auth for the routing API call.

Harness inference stays on the Workshop Terminal gateway-token path. Do not
inject model-serving credentials here.

``WORKSHOP_SMART_ROUTING`` must be explicitly true. Labs (2026-08-11) returns
``ENDPOINT_NOT_FOUND`` / "routing/v1/routes:select is not enabled for this
account" — shipping Auto while that flag is off would show a dead picker
option. Enable the account product first, then set the env on the App.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("omnigent-workshop-app.smart_routing")

_DEFAULT_ROUTER_NAME = "task_v0"
_DEFAULT_MODEL_PREFIXES = ("databricks-",)
_ROUTING_PATH = "/ai-gateway/routing/v1"


def smart_routing_enabled(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return source.get("WORKSHOP_SMART_ROUTING", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def routing_base_url(
    *,
    workspace_client: Any | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Return the workspace-hosted AI Gateway routing base URL."""
    source = env if env is not None else os.environ
    explicit = source.get("WORKSHOP_ROUTING_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit

    host = (source.get("DATABRICKS_HOST") or "").strip().rstrip("/")
    if not host and workspace_client is not None:
        host = str(getattr(workspace_client.config, "host", "") or "").rstrip("/")
    if not host:
        raise RuntimeError(
            "DATABRICKS_HOST (or WorkspaceClient config.host) is required to "
            "build the AI Gateway routing URL"
        )
    parsed = urlparse(host if "://" in host else f"https://{host}")
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"invalid DATABRICKS_HOST for routing: {host!r}")
    return f"{parsed.scheme}://{parsed.netloc}{_ROUTING_PATH}"


class WorkspaceClientBearerAuth:
    """httpx Auth that mints a fresh App SP bearer on every request.

    Databricks Apps inject ambient OAuth into ``WorkspaceClient()``. Capturing a
    token once at startup would 401 after ~1h; re-calling ``authenticate()``
    refreshes like Omnigent's ``databricks_profile`` path without writing a
    CLI profile file into the App container.
    """

    def __init__(self, workspace_client: Any) -> None:
        self._workspace_client = workspace_client

    def auth_flow(self, request):  # type: ignore[no-untyped-def]
        import httpx

        if not isinstance(request, httpx.Request):
            raise TypeError("expected httpx.Request")
        headers = self._workspace_client.config.authenticate()
        authorization = headers.get("Authorization")
        if not authorization:
            raise RuntimeError("WorkspaceClient authenticate() returned no Authorization")
        request.headers["Authorization"] = authorization
        yield request


def build_routing_client(
    workspace_client: Any,
    *,
    env: dict[str, str] | None = None,
) -> Any | None:
    """Return an ``ExternalRoutingClient`` or ``None`` when disabled / misconfigured."""
    if not smart_routing_enabled(env):
        logger.info(
            "Smart routing disabled (WORKSHOP_SMART_ROUTING is not true); "
            "Auto option stays hidden"
        )
        return None

    try:
        import httpx
        from omnigent.server.smart_routing import ExternalRoutingClient
    except Exception:  # noqa: BLE001 — fail-soft at App startup
        logger.warning("Smart routing imports failed; leaving routing_client=None", exc_info=True)
        return None

    source = env if env is not None else os.environ
    try:
        base_url = routing_base_url(workspace_client=workspace_client, env=source)
    except Exception:  # noqa: BLE001
        logger.warning("Smart routing base URL unresolved; leaving routing_client=None", exc_info=True)
        return None

    router_name = (
        source.get("WORKSHOP_ROUTING_ROUTER_NAME", "").strip() or _DEFAULT_ROUTER_NAME
    )
    prefixes_raw = source.get("WORKSHOP_ROUTING_MODEL_PREFIXES", "").strip()
    if prefixes_raw:
        model_prefixes = [part.strip() for part in prefixes_raw.split(",") if part.strip()]
    else:
        model_prefixes = list(_DEFAULT_MODEL_PREFIXES)

    # httpx.Auth protocol: subclass is cleaner but duck-typing works if we
    # register as httpx.Auth. Prefer an explicit subclass instance.
    class _AppsSpAuth(httpx.Auth):
        def __init__(self, client: Any) -> None:
            self._inner = WorkspaceClientBearerAuth(client)

        def auth_flow(self, request):  # type: ignore[no-untyped-def]
            yield from self._inner.auth_flow(request)

    try:
        client = ExternalRoutingClient(
            base_url=base_url,
            router_name=router_name,
            auth=_AppsSpAuth(workspace_client),
            model_prefixes=model_prefixes,
        )
    except Exception:  # noqa: BLE001
        logger.warning("ExternalRoutingClient construction failed", exc_info=True)
        return None

    logger.info(
        "Smart routing enabled: base_url=%s router_name=%s model_prefixes=%s",
        base_url,
        router_name,
        model_prefixes,
    )
    return client


def build_runtime_caps(
    workspace_client: Any,
    *,
    env: dict[str, str] | None = None,
) -> Any:
    """Build ``RuntimeCaps``, attaching a routing client when enabled."""
    from omnigent.runtime.caps import RuntimeCaps

    routing_client = build_routing_client(workspace_client, env=env)
    if routing_client is None:
        return RuntimeCaps()
    return RuntimeCaps(routing_client=routing_client)
