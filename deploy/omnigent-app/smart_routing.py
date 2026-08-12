"""Build Omnigent Auto · smart routing for the workshop control plane.

Omnigent 0.9.0 routes from two backends. The external client calls Databricks AI
Gateway ``routes:select``; the built-in judge asks a small model instead.
Upstream prefers the external one and falls back to the judge per request.

**The judge is this workshop's deciding path.** ``routes:select`` is an
account-console preview, and it is switched off for this account, so the
external client 404s on its first call and latches itself unavailable for the
process. Every Auto verdict after that is the judge's. It therefore has to be
right on its own, not merely right as a fallback — see :func:`shape_judge_menu`.

Shaping is applied to the judge's menu and nowhere else, which is deliberate.
``task_v1`` treats its arm menu as part of the request contract and requires it
whole, so trimming the list handed to the external client would make the request
partial rather than safer. Upstream's own filtering reflects that split: it
drops harness-barred rows before either backend is called, then repairs an
external verdict after the fact with ``substitute_model``.

Routing is the only inference the App itself performs, and it is pinned to
``WORKSHOP_ROUTING_JUDGE_MODEL``. Harness inference stays on the Workshop
Terminal gateway-token path; do not widen the App service principal past the
one judge endpoint.
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


# Cheapest first. GPT is priced in DBU per output token — luna 28, terra 282,
# sol 642 — and Claude runs haiku, then sonnet, then opus. The two families are
# interleaved by tier rather than by a common price, which costs nothing in
# practice: a harness bar almost always leaves one family standing (pi bars
# every GPT arm, codex serves no Claude), so the interleave only breaks ties
# that a real menu rarely presents.
#
# Naming both families matters more than the interleave. The judge reads this
# list as cheapest-first, and anything absent from it lands in the unranked
# bucket, where the only tiebreak is the model id. Under pi, where every
# candidate was a Claude arm and none were named here, that tiebreak sorted
# ``claude-fable-5`` ahead of haiku, opus and sonnet on the letter F alone --
# so "hi" bought the flagship.
_DEFAULT_MODEL_ORDER = (
    "claude-haiku-4-5",
    "gpt-5-6-luna",
    "claude-sonnet-5",
    "gpt-5-6-terra",
    "claude-opus-5",
    "gpt-5-6-sol",
)

# Never routable, whatever the prompt.
#
# gpt-5.5 prices like sol (642) and is a generation behind, so there is no task
# it is the right answer for. claude-fable-5 is the dearest endpoint the
# workspace serves and is deliberately out of scope for a workshop; it is
# excluded rather than ordered last because last is still reachable, and the
# judge falls back to the *last* entry when no cheap branch holds.
_DEFAULT_MODEL_EXCLUDE = ("gpt-5-5", "gpt-5-5-pro", "claude-fable-5")

# Models that answer only on chat-completions, per harness that cannot speak it.
# codex-cli 0.147.0 is Responses-only and the gateway refuses these outright
# ("Responses API passthrough is not supported for model databricks-glm-5-2"),
# so a codex verdict naming one is dead before it is made. Not a global
# exclusion: pi speaks chat and runs them fine.
#
# This cannot be fixed by trimming the router's menu, which is why it is a
# judge-side list. glm-5-2 is the first entry in SMART_ROUTING_TASK_V1_CODEX_ARMS
# and task_v1 requires that menu in full, so upstream injects the arm whether or
# not the workspace can serve it.
#
# Hardcoded because the judge is handed bare model ids. The catalog knows the
# real answer -- entries carry ``wire_apis``, and the rule is "codex needs
# OPENAI_RESPONSES" -- so if this list ever needs a fourth entry, fetch the
# catalog instead of extending it.
_HARNESS_CHAT_ONLY_BARS: dict[str, tuple[str, ...]] = {
    "codex": ("glm-5-2", "kimi-k3", "gemini-3-6-flash"),
}


def _bare_model_id(model: str) -> str:
    """Fold a model id to the spelling comparisons use.

    The router keys arms bare and dashed (``gpt-5-6-sol``); a catalog spells the
    same arm prefixed and dotted (``databricks-gpt-5.6-sol``). Upstream folds
    them the same way — dots to dashes, prefix and ``[1m]`` context suffix off,
    case folded — so the two vocabularies compare equal.
    """
    bare = model.strip().lower()
    for prefix in ("databricks-", "system.ai."):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
            break
    return bare.replace(".", "-").removesuffix("[1m]")


def _configured_ids(source: Any, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = source.get(key, "").strip()
    if not raw:
        return default
    return tuple(_bare_model_id(part) for part in raw.split(",") if part.strip())


def shape_judge_menu(
    available_models: dict[str, list[str]],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Drop what the judge must not pick, and order what is left by cost.

    Three corrections upstream does not make, and one it already makes:

    - **Cost order.** The ``RoutingClient`` protocol specifies each list as
      "ordered cheapest → most powerful", and the judge's rubric relies on it,
      falling back to the *last* entry when no cheap branch holds. Nothing
      enforces the contract on the way in, and the catalog does not arrive
      sorted, which is what put a three-word prompt on the most expensive arm
      in the workspace.
    - **Deprecated arms.** See :data:`_DEFAULT_MODEL_EXCLUDE`.
    - **Wire mismatches.** See :data:`_HARNESS_CHAT_ONLY_BARS`. This is the gap
      that bites hardest: ``_HARNESS_EXCLUDED_MODELS`` upstream covers pi only,
      so nothing stops a codex verdict naming an arm codex cannot speak to.
    - **Harness bars**, which ``route_turn`` already applies to ``available``
      before either backend sees it. Re-applied here because that filter is
      keyed on the harness having an exclusion entry at all, and because this
      judge is reachable from any caller holding the client, not just through
      ``route_turn``. Agreeing with upstream costs one set lookup per candidate.

    A harness left with no candidate is dropped rather than offered empty, and
    an empty result means "no verdict" — the turn runs unrouted on its default,
    which is always better than routing it somewhere it cannot run.

    :param available_models: Harness -> candidate model ids, as upstream built it.
    :param env: Environment to read overrides from; ``None`` reads ``os.environ``.
    :returns: The same mapping, filtered and ordered.
    """
    try:
        from omnigent.server.smart_routing import harness_bars_model
    except Exception:  # noqa: BLE001 — never let shaping take routing down
        logger.warning("Harness-bar import failed; judge menu left unshaped", exc_info=True)
        harness_bars_model = None  # type: ignore[assignment]

    source = env if env is not None else os.environ
    order = _configured_ids(source, "WORKSHOP_ROUTING_MODEL_ORDER", _DEFAULT_MODEL_ORDER)
    excluded = set(
        _configured_ids(source, "WORKSHOP_ROUTING_MODEL_EXCLUDE", _DEFAULT_MODEL_EXCLUDE)
    )

    def rank(model: str) -> tuple[int, str]:
        bare = _bare_model_id(model)
        # Unranked models sort after ranked ones rather than at the cheap end:
        # an id we have no price for is not evidence that it is cheap.
        return (order.index(bare) if bare in order else len(order), bare)

    shaped: dict[str, list[str]] = {}
    for harness, candidates in available_models.items():
        chat_only = set(_HARNESS_CHAT_ONLY_BARS.get(harness, ()))
        kept = [
            model
            for model in candidates
            if _bare_model_id(model) not in excluded
            and _bare_model_id(model) not in chat_only
            and not (harness_bars_model and harness_bars_model(harness, model))
        ]
        if kept:
            shaped[harness] = sorted(kept, key=rank)

    dropped = {
        harness: [m for m in candidates if m not in shaped.get(harness, [])]
        for harness, candidates in available_models.items()
    }
    if any(dropped.values()):
        logger.info(
            "Judge menu shaped: kept=%s dropped=%s",
            {h: list(models) for h, models in shaped.items()},
            {h: models for h, models in dropped.items() if models},
        )
    return shaped


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
        available_models = shape_judge_menu(available_models, env=self._env)
        if not available_models:
            self.last_error = "no candidate model survives this session's harness bars"
            return None
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
