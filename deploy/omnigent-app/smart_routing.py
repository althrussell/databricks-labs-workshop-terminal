"""Build Omnigent Auto · smart routing for the workshop control plane.

Omnigent 0.9.0 routes from two backends. The external client calls Databricks AI
Gateway ``routes:select``; the built-in judge asks a small model instead. The
external one is richer but only serves gateway-backed harnesses, so upstream
prefers it and falls back to the judge — including when the workspace has no
routing API at all, which is our case: labs answers ``routes:select`` with
``ENDPOINT_NOT_FOUND`` / "not enabled for this account". 0.9.0 latches that
after one request, so the external client costs one call per process and the
judge serves every decision after it.

That fallback is what lets Auto ship on by default. We still configure the
external client, so an account that does have routing gets the better router
without a code change.

Routing is the only inference the App itself performs, and it is pinned to
``WORKSHOP_ROUTING_JUDGE_MODEL``. Harness inference stays on the Workshop
Terminal gateway-token path; do not widen the App service principal beyond
CAN_QUERY on the judge endpoint.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("omnigent-workshop-app.smart_routing")

_DEFAULT_JUDGE_MODEL = "databricks-gpt-5-6-luna"
_ROUTING_PATH = "/ai-gateway/routing/v1"


def smart_routing_enabled(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return source.get("WORKSHOP_SMART_ROUTING", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def judge_model(env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    return source.get("WORKSHOP_ROUTING_JUDGE_MODEL", "").strip() or _DEFAULT_JUDGE_MODEL


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


def workspace_host(
    workspace_client: Any,
    env: dict[str, str] | None = None,
) -> str:
    source = env if env is not None else os.environ
    host = (source.get("DATABRICKS_HOST") or "").strip().rstrip("/")
    if not host:
        host = str(getattr(workspace_client.config, "host", "") or "").rstrip("/")
    if not host:
        raise RuntimeError("DATABRICKS_HOST (or WorkspaceClient config.host) is required")
    return host if "://" in host else f"https://{host}"


def _app_bearer(workspace_client: Any) -> str:
    headers = workspace_client.config.authenticate()
    authorization = str(headers.get("Authorization") or "")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise RuntimeError("WorkspaceClient authenticate() returned no bearer token")
    return token


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
        request.headers["Authorization"] = f"Bearer {_app_bearer(self._workspace_client)}"
        yield request


class AppServicePrincipalJudge:
    """Upstream's judge, rebuilt per call so its credential cannot go stale.

    ``_build_policy_llm_client`` binds ``{"base_url", "api_key"}`` once, which a
    long-lived App outlives: ambient Apps OAuth expires in about an hour. The
    client is a dataclass wrapping an HTTP client, so rebuilding it is cheap
    next to the model call it exists to make.

    Satisfies upstream's ``RoutingClient`` protocol, including the ``last_error``
    that callers surface when a decision comes back empty.
    """

    def __init__(
        self,
        workspace_client: Any,
        model: str,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        self._workspace_client = workspace_client
        self._model = model
        self._env = env
        self.last_error: str | None = None

    def _connection(self) -> dict[str, str]:
        host = workspace_host(self._workspace_client, self._env)
        return {
            "base_url": f"{host}/serving-endpoints",
            "api_key": _app_bearer(self._workspace_client),
        }

    async def route(
        self,
        message: str,
        available_models: dict[str, list[str]],
    ) -> Any | None:
        from omnigent.runtime.policies.builder import _build_policy_llm_client
        from omnigent.server.smart_routing import LLMRoutingClient
        from omnigent.spec.types import LLMConfig

        self.last_error = None
        try:
            policy_client = _build_policy_llm_client(
                LLMConfig(model=self._model), self._connection()
            )
        except Exception as exc:  # noqa: BLE001 — fail open, the turn still runs
            self.last_error = f"routing judge unavailable: {exc}"
            logger.warning("Routing judge could not be built", exc_info=True)
            return None
        if policy_client is None:
            self.last_error = "routing judge client could not be built"
            return None

        judge = LLMRoutingClient(policy_client)
        result = await judge.route(message, available_models)
        self.last_error = judge.last_error
        return result


def build_routing_settings(env: dict[str, str] | None = None) -> Any:
    """Build ``RoutingSettings``, overriding only what the App configures.

    Everything left unset keeps the upstream default, which is deliberate: the
    router name (``task_v1``) and the model prefixes (``databricks-`` and
    ``system.ai.``) are facts about the gateway contract, not workshop policy.
    """
    from omnigent.server.smart_routing import RoutingSettings

    source = env if env is not None else os.environ
    overrides: dict[str, Any] = {}

    router_name = source.get("WORKSHOP_ROUTING_ROUTER_NAME", "").strip()
    if router_name:
        overrides["router_name"] = router_name

    prefixes_raw = source.get("WORKSHOP_ROUTING_MODEL_PREFIXES", "").strip()
    if prefixes_raw:
        prefixes = tuple(part.strip() for part in prefixes_raw.split(",") if part.strip())
        if prefixes:
            overrides["model_prefixes"] = prefixes

    return RoutingSettings(**overrides)


def build_external_routing_client(
    workspace_client: Any,
    *,
    settings: Any,
    env: dict[str, str] | None = None,
) -> Any | None:
    """Return an ``ExternalRoutingClient``, or ``None`` when it cannot be built."""
    try:
        import httpx
        from omnigent.server.smart_routing import ExternalRoutingClient
    except Exception:  # noqa: BLE001 — fail-soft at App startup
        logger.warning("External routing imports failed", exc_info=True)
        return None

    try:
        base_url = routing_base_url(workspace_client=workspace_client, env=env)
    except Exception:  # noqa: BLE001
        logger.warning("External routing base URL unresolved", exc_info=True)
        return None

    class _AppsSpAuth(httpx.Auth):
        def __init__(self, client: Any) -> None:
            self._inner = WorkspaceClientBearerAuth(client)

        def auth_flow(self, request):  # type: ignore[no-untyped-def]
            yield from self._inner.auth_flow(request)

    try:
        client = ExternalRoutingClient(
            base_url=base_url,
            router_name=settings.router_name,
            auth=_AppsSpAuth(workspace_client),
            model_prefixes=list(settings.model_prefixes),
            selection_model=settings.selection_model,
            menus=settings.menus,
            servable_aliases=settings.servable_aliases,
        )
    except Exception:  # noqa: BLE001
        logger.warning("ExternalRoutingClient construction failed", exc_info=True)
        return None

    logger.info(
        "External router configured: base_url=%s router_name=%s",
        base_url,
        settings.router_name,
    )
    return client


def build_judge_routing_client(
    workspace_client: Any,
    *,
    env: dict[str, str] | None = None,
) -> Any | None:
    """Return the built-in judge, or ``None`` when it cannot be built."""
    try:
        from omnigent.server.smart_routing import LLMRoutingClient  # noqa: F401
    except Exception:  # noqa: BLE001
        logger.warning("Routing judge imports failed", exc_info=True)
        return None

    model = judge_model(env)
    try:
        workspace_host(workspace_client, env)
    except Exception:  # noqa: BLE001
        logger.warning("Routing judge host unresolved", exc_info=True)
        return None

    logger.info("Routing judge configured: model=%s", model)
    return AppServicePrincipalJudge(workspace_client, model, env=env)


def build_runtime_caps(
    workspace_client: Any,
    *,
    env: dict[str, str] | None = None,
) -> Any:
    """Build ``RuntimeCaps``, attaching both routing backends when enabled."""
    from omnigent.runtime.caps import RuntimeCaps

    if not smart_routing_enabled(env):
        logger.info(
            "Smart routing disabled (WORKSHOP_SMART_ROUTING is not true); "
            "Auto option stays hidden"
        )
        return RuntimeCaps()

    try:
        from omnigent.server.routing_backend import RoutingBackends
    except Exception:  # noqa: BLE001
        logger.warning("Routing backend imports failed; routing stays off", exc_info=True)
        return RuntimeCaps()

    try:
        settings = build_routing_settings(env)
    except Exception:  # noqa: BLE001
        logger.warning("Routing settings unresolved; routing stays off", exc_info=True)
        return RuntimeCaps()

    backends = RoutingBackends(
        external=build_external_routing_client(
            workspace_client, settings=settings, env=env
        ),
        local=build_judge_routing_client(workspace_client, env=env),
    )
    primary = backends.any()
    if primary is None:
        logger.warning("No routing backend could be built; Auto stays hidden")
        return RuntimeCaps()

    return RuntimeCaps(
        routing_client=primary,
        routing_backends=backends,
        routing_settings=settings,
    )
