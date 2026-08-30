"""Write per-user CLI configs (claude / codex / databricks CLI).

Everything here is parameterized by user HOME: each attendee gets their own
config files under their own home directory, fed by the app's auto-refreshing
OAuth bearer.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import tempfile
import threading
from urllib.parse import urlparse

import requests
import yaml

from . import config, model_policy, models
from .users import User

logger = logging.getLogger(__name__)

_gateway_lock = threading.Lock()
_gateway_resolved: str | None = None  # None = never probed; "" = no gateway


# -- gateway / model discovery --


def _probe(url: str) -> bool:
    try:
        requests.get(url, timeout=2.0, allow_redirects=False)
        return True
    except requests.RequestException:
        return False


def gateway_host() -> str:
    """Resolve the Unity AI Gateway base once per process.

    Three sources, in order. An explicit ``DATABRICKS_GATEWAY_HOST`` is trusted
    without a probe, because an operator naming a host is a decision and not a
    guess. A ``DATABRICKS_WORKSPACE_ID`` (or an Azure ``adb-<digits>`` hostname
    to derive one from) builds the dedicated-subdomain form and probes it.
    Failing both, the workspace-hosted form ``<host>/ai-gateway``.

    The workspace-hosted form is last in the list and first in preference for
    everything that is not already configured: it needs no workspace id, works
    on AWS ``dbc-...`` hostnames that match neither shape above, and is what
    Databricks' own managed Claude Code settings point at. It is also not
    optional any more. This used to fall through to
    ``<host>/serving-endpoints/anthropic`` when no gateway could be found, and
    that surface is gone — it answers 404 — so there is no longer a second
    place to look. Returning the workspace-hosted base unprobed is therefore
    strictly better than returning nothing: if the gateway is unreachable the
    configs are wrong either way, and this way /readyz can say so.
    """
    global _gateway_resolved
    with _gateway_lock:
        if _gateway_resolved is not None:
            return _gateway_resolved

        explicit = os.environ.get("DATABRICKS_GATEWAY_HOST", "").strip().rstrip("/")
        if explicit:
            _gateway_resolved = config.ensure_https(explicit)
            return _gateway_resolved

        host = os.environ.get("DATABRICKS_HOST", "")
        ws_id = os.environ.get("DATABRICKS_WORKSPACE_ID", "").strip()
        if not ws_id:
            m = re.match(r"(?:https?://)?adb-(\d+)\.", host or "")
            ws_id = m.group(1) if m else ""
        if ws_id:
            if "azuredatabricks.net" in host.lower():
                candidate = f"https://{ws_id}.0.ai-gateway.azuredatabricks.net"
            else:
                candidate = f"https://{ws_id}.ai-gateway.cloud.databricks.com"
            if _probe(candidate):
                _gateway_resolved = candidate
                return candidate
            logger.info(
                "dedicated AI Gateway not reachable at %s — using the "
                "workspace-hosted gateway",
                candidate,
            )
        _gateway_resolved = workspace_gateway()
        return _gateway_resolved


def workspace_gateway() -> str:
    """The workspace-hosted Unity AI Gateway base, or empty with no host."""
    host = config.databricks_host()
    return f"{host}/ai-gateway" if host else ""


# Mirrors ``omnigent/pi_native_credentials.py::_is_databricks_ai_gateway_url``
# (verified against the deployed 0.10.0 wheel, where it moved to
# ``omnigent/databricks_ai_gateway.py``). Omnigent routes a base URL as the
# AI Gateway only when it is https, its hostname ends in a trusted
# Databricks-owned suffix, AND either the hostname carries an ``ai-gateway`` DNS
# label or the path starts with ``/ai-gateway/``. All three conditions are
# reproduced deliberately: a mirror that is merely close reports green for URLs
# Omnigent silently declines to route, which is the exact failure this check
# exists to catch.
_AI_GATEWAY_LABEL = "ai-gateway"
_TRUSTED_HOST_SUFFIXES = (
    ".cloud.databricks.com",
    ".azuredatabricks.net",
    ".gcp.databricks.com",
)


def _is_omnigent_gateway_form(url: str) -> bool:
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    # Anchored on the leading dot, so a look-alike ending in
    # `.cloud.databricks.com.evil.test` is rejected rather than trusted.
    if not any(hostname.endswith(suffix) for suffix in _TRUSTED_HOST_SUFFIXES):
        return False
    if _AI_GATEWAY_LABEL in hostname.split("."):
        return True
    # The trailing slash is load-bearing upstream: a bare `/ai-gateway` root does
    # NOT satisfy this, which is why the check below evaluates the derived base
    # URL rather than the configured root.
    return (parsed.path or "").startswith("/" + _AI_GATEWAY_LABEL + "/")


def beta_negotiation_env(gateway_backed: bool) -> dict:
    """How Claude Code should settle its ``anthropic-beta`` set.

    On the AI Gateway, Claude Code negotiates the beta set instead of sending
    every flag blindly, so the betas can stay on — and every deployment is on
    the gateway now that it is the only surface serving these models, so this
    is the branch that runs.

    The other branch is kept for the case where no host is configured at all
    and there is no gateway URL to hand a CLI. Disabling experimental betas
    also disables MCP tool search, which rides on ``advanced-tool-use``, and
    without it every MCP tool schema loads eagerly and inflates the context
    window. Omnigent 0.10.0 launches its own Claude terminals with
    ``CLAUDE_CODE_USE_GATEWAY=1`` for this reason, and re-adds the disable flag
    whenever it does not see that variable.
    """
    if gateway_backed:
        return {"CLAUDE_CODE_USE_GATEWAY": "1"}
    return {"CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"}


def gateway_status() -> dict:
    """How the Unity AI Gateway resolved, and what an unresolved one costs.

    Reported, never gating — but the cost is no longer narrow. This used to
    describe a governance-only tradeoff, because a deployment with no gateway
    fell back to ``<host>/serving-endpoints`` and still reached every model an
    attendee needed. That fallback is gone: the legacy per-model endpoints have
    been retired in favour of Unity Catalog model services, and
    ``<host>/serving-endpoints/anthropic/v1/messages`` now answers 404. There
    is one surface left, so an unresolved gateway is a broken event rather than
    an ungoverned one.

    What that changes in practice is which lever matters. ``resolved`` is now
    false only when no workspace host is configured at all, since the
    workspace-hosted form ``<host>/ai-gateway`` is derivable from the host
    alone — no workspace id, no cloud-specific hostname shape, nothing an AWS
    ``dbc-...`` deployment lacks. ``DATABRICKS_GATEWAY_HOST`` and
    ``DATABRICKS_WORKSPACE_ID`` remain the levers for pointing an event at a
    dedicated gateway subdomain instead, and are reported so an operator can
    see which of the three sources answered.
    """
    resolved = gateway_host()
    # Judge the URL Omnigent is actually handed, not the configured root. With
    # the workspace-hosted shape the root is `<host>/ai-gateway`, whose path
    # lacks the trailing slash upstream requires — so validating the root would
    # report amber for a deployment that works. `<root>/anthropic` is the value
    # configure_omnigent writes for its Anthropic provider.
    anthropic_base = f"{resolved}/anthropic" if resolved else ""
    explicit = bool(os.environ.get("DATABRICKS_GATEWAY_HOST", "").strip())
    workspace_id = bool(os.environ.get("DATABRICKS_WORKSPACE_ID", "").strip())
    host = os.environ.get("DATABRICKS_HOST", "")
    derivable = bool(re.match(r"(?:https?://)?adb-(\d+)\.", host or ""))
    # Three sources rather than two, because "constructed" no longer implies a
    # dedicated subdomain: the workspace-hosted form is also constructed, needs
    # none of the levers below, and is the one nearly every event will report.
    if not resolved:
        source = "unresolved"
    elif explicit:
        source = "explicit"
    elif resolved == workspace_gateway():
        source = "workspace"
    else:
        source = "constructed"
    return {
        "resolved": bool(resolved),
        "source": source,
        # Present so an operator can see WHICH lever is missing rather than
        # only that the gateway is absent.
        "gateway_host_set": explicit,
        "workspace_id_set": workspace_id,
        "workspace_id_derivable": derivable,
        "omnigent_gateway_form": _is_omnigent_gateway_form(anthropic_base),
    }


_MODEL_SERVICES_API = "/api/2.1/unity-catalog/model-services"
# Names come back namespaced as `model-services/system.ai.claude-opus-5`; the
# securable path prefix is Unity Catalog's, not part of the model's name.
_SECURABLE_PREFIX = "model-services/"


def discover_model_services(token: str) -> dict[str, frozenset[str]]:
    """What this workspace serves: canonical short name -> wires it answers on.

    Replaces discovery against ``/api/2.0/serving-endpoints``, which was not
    merely obsolete but actively misleading. That endpoint reports legacy
    foundation-model endpoints and went on reporting
    ``databricks-claude-opus-4-7`` as ``READY`` after invoking it began
    answering 501 ``no longer available``. Every consumer of it was therefore
    "verifying" a chain against a list that could not tell a live model from a
    retired one, which is how a resolved-and-verified config still broke a
    live event.

    Unity Catalog model services report ``supported_api_types`` instead of a
    readiness flag. Presence in the list is the liveness signal, and the list
    of wires is what lets :func:`server.models.resolve` refuse to put a
    chat-only model behind a Responses-wire harness.

    CT's policy is authoritative when present.  Return its catalogue before
    consulting the workspace API so direct callers cannot bypass an applied
    policy.  The required-but-pending state is an authoritative empty mapping,
    which also fails closed without making a discovery request.

    In an unmanaged deployment, an empty result means the call failed, and
    callers read it that way: they fall back to the head of each chain rather
    than concluding the workspace serves nothing.
    """
    governed = model_policy.direct_catalogue()
    if governed is not None:
        return governed

    host = config.databricks_host()
    if not host or not token:
        return {}
    services: dict[str, frozenset[str]] = {}
    url = f"{host}{_MODEL_SERVICES_API}"
    params: dict[str, str] = {}
    try:
        # Paginated defensively. Today every workspace we have seen returns its
        # whole catalogue in one page and ignores max_results, but a caller that
        # silently keeps the first page of a paginated API is a caller that
        # quietly loses models the day that changes.
        for _ in range(10):
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params or None,
                timeout=5,
            )
            resp.raise_for_status()
            body = resp.json()
            for service in body.get("model_services", []):
                name = (service.get("name") or "").strip()
                if name.startswith(_SECURABLE_PREFIX):
                    name = name[len(_SECURABLE_PREFIX) :]
                if not name:
                    continue
                services[models.catalogue_key(name)] = frozenset(
                    service.get("supported_api_types") or ()
                )
            token_next = body.get("next_page_token")
            if not token_next:
                break
            params = {"page_token": token_next}
    except Exception as e:
        logger.warning("model service discovery failed: %s", e)
        return {}
    return services


def current_model_catalogue(token: str) -> dict[str, frozenset[str]]:
    """CT's live policy when present, otherwise workspace discovery.

    A policy revision is authoritative even when it contains no models for WT.
    Treating that empty set as a discovery failure would restore every default
    ``system.ai`` model precisely when CT intended to deny all of them.
    """
    governed = model_policy.direct_catalogue()
    if governed is not None:
        return governed
    return discover_model_services(token)


# -- per-user config writers --


def configure_claude(
    user: User,
    token: str,
    *,
    write_token: bool = True,
    available: models.Catalogue | None = None,
) -> None:
    claude_dir = os.path.join(user.home, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    # The rotating token lives in a file an apiKeyHelper re-reads (below), never
    # baked statically into settings.json — a long-running `claude` process
    # would otherwise hold the token captured at startup and 401 the moment the
    # rotation loop moved on.
    if write_token:
        _write_gateway_token(user, token)

    gateway = gateway_host()
    base_url = f"{gateway}/anthropic" if gateway else ""

    # Which model fills each of Claude Code's three slots is a policy question,
    # answered once in server/models.py for every CLI an event runs. resolve()
    # returns fully-qualified `system.ai.*` service names, which is what the
    # gateway answers to and what Databricks' own managed Claude Code settings
    # carry.
    if available is None:
        available = current_model_catalogue(token)
    resolved = {
        role: model_policy.resolve_service(role, available)
        for role in ("driver", "frontier", "standard", "fast")
    }
    tag_header = model_policy.request_tags("claude")
    settings_path = os.path.join(claude_dir, "settings.json")
    settings = _read_json(settings_path)
    env = settings.setdefault("env", {})
    env.update(
        {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": resolved["driver"],
            "ANTHROPIC_DEFAULT_OPUS_MODEL": resolved["frontier"],
            "ANTHROPIC_DEFAULT_SONNET_MODEL": resolved["standard"],
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": resolved["fast"],
            "ANTHROPIC_CUSTOM_HEADERS": (
                "x-databricks-use-coding-agent-mode: true\n"
                f"Databricks-Ai-Gateway-Request-Tags: {tag_header}"
            ),
            **beta_negotiation_env(bool(gateway)),
            # The CLI install is shared across attendees — never self-update.
            "DISABLE_AUTOUPDATER": "1",
            # Re-run apiKeyHelper on this cadence (ms) so a live process always picks
            # up a changed app OAuth bearer every four minutes (matching the
            # omnigent harness refresh window). A 401 also forces an
            # immediate re-run, so a mid-flight rotation self-heals either way.
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "240000",
        }
    )
    # A static ANTHROPIC_AUTH_TOKEN in the env block would take precedence over
    # apiKeyHelper (making the helper dormant) and reintroduce the stale-token
    # 401. Drop it so the dynamic helper is the sole credential source.
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    # apiKeyHelper prints the current rotating token; Claude sends it as the
    # bearer to the gateway. Absolute path — Claude runs it via /bin/sh.
    settings["apiKeyHelper"] = f"cat {_gateway_token_path(user)}"
    # Workshop "auto mode": zero permission prompts inside the attendee's
    # isolated container HOME. WORKSHOP_AUTO_MODE=false restores safe prompts.
    if config.auto_mode_enabled():
        settings.setdefault("permissions", {})["defaultMode"] = "bypassPermissions"
        # One-time "are you sure?" prompt is honored from user-scope settings.
        settings["skipDangerousModePermissionPrompt"] = True
    else:
        settings.get("permissions", {}).pop("defaultMode", None)
        settings.pop("skipDangerousModePermissionPrompt", None)
    _write_json(settings_path, settings)

    claude_json_path = os.path.join(user.home, ".claude.json")
    claude_json = _read_json(claude_json_path)
    claude_json["hasCompletedOnboarding"] = True
    _write_json(claude_json_path, claude_json)


# The `[model_providers.X]` id in ~/.codex/config.toml. Omnigent's config
# points at this table by id, so the two files have to agree on the name.
_CODEX_MODEL_PROVIDER = "databricks"
_CODEX_PROVIDER_DISPLAY_NAME = "Databricks Model Serving"

# Codex once carried a second provider on the plain chat-completions surface so
# GLM, Kimi and Gemini — which answer on chat completions and nowhere else —
# were reachable as `codex --profile <name>`. codex-cli 0.144.6 removed the chat
# wire: `responses` is now the only accepted `wire_api`, and an unknown value
# does not degrade to a skipped provider, it invalidates the WHOLE config. Codex
# then falls back to its default config, which has no `databricks` provider, and
# every session dies at startup with "Model provider `databricks` not found" —
# the Omnigent native terminal included, since it copies this same file into its
# per-session CODEX_HOME.
#
# The gateway's Responses surface refuses those models too, which is now a fact
# the resolver checks rather than a claim in a comment: a model service lists
# the wires it answers on, and `system.ai.glm-5-2` does not list
# `openai/v1/responses`. So there is no wire that both Codex speaks and they
# answer. The comparison exercise moved to Omnigent, which still speaks chat —
# see models.COMPARISON_MODELS.

# What we key our entry under in ~/.omnigent/config.yaml. A separate name from
# the codex table id above, and prefixed like `databricks-gateway`, because we
# merge into a file whose other entries are not ours to remove and an
# unnamespaced `databricks` is exactly what a hand-written `kind: databricks`
# provider would be called.
_OMNIGENT_CODEX_PROVIDER = "databricks-codex"


def _codex_base_url() -> str:
    """The gateway's OpenAI Responses surface, spelled the way Omnigent parses.

    The gateway serves Responses under both `/ai-gateway/openai/v1` and
    `/ai-gateway/codex/v1` — both verified to answer `system.ai.*` model
    services. Omnigent's Databricks-aware Codex provider expects the second;
    using `/openai/v1` would make it derive an invalid Anthropic base at
    `/openai/v1/anthropic`.

    Empty only when no workspace host is configured, in which case there is no
    URL of any shape to write.
    """
    gateway = gateway_host()
    return f"{gateway}/codex/v1" if gateway else ""


def unified_chat_url() -> str:
    """The gateway's provider-agnostic chat-completions endpoint.

    One URL for every model regardless of vendor, which is what the server-side
    callers want: the wizard, the wrap summary and the model-comparison
    exercise each send a single turn and care only that the answer comes back.
    It replaces the per-model `<host>/serving-endpoints/<name>/invocations`
    URLs those callers used to build, which died with the legacy endpoints.
    """
    gateway = gateway_host()
    return f"{gateway}/mlflow/v1/chat/completions" if gateway else ""


def configure_codex(
    user: User,
    token: str,
    *,
    write_token: bool = True,
    available: models.Catalogue | None = None,
) -> None:
    codex_dir = os.path.join(user.home, ".codex")
    os.makedirs(codex_dir, exist_ok=True)

    # Same rotating token file Claude's apiKeyHelper reads; Codex re-runs the
    # provider auth command on a timer (below) so a live process never holds a
    # revoked/expired token.
    if write_token:
        _write_gateway_token(user, token)

    base_url = _codex_base_url()
    # Codex used to take CODEX_MODEL or a hardcoded default with no regard for
    # what the workspace actually serves, which is how it ended up pinned to a
    # model two generations old. It degrades like Claude does now, and the
    # Responses-wire filter means it can only degrade onto something Codex can
    # actually talk to.
    if available is None:
        available = current_model_catalogue(token)
    model = model_policy.resolve_service("codex", available)
    tag_header = model_policy.request_tags("codex")

    auto_mode = ""
    if config.auto_mode_enabled():
        # Workshop "auto mode": codex runs without approval prompts inside the
        # attendee's isolated container HOME.
        auto_mode = 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n'

    # A provider `auth` command is Codex's apiKeyHelper equivalent: Codex re-runs
    # it every refresh_interval_ms (and on a 401) and uses its stdout as the
    # bearer. Reading the rotating token file this way is what lets a
    # long-running Codex session survive token rotation — a static
    # `env_key = "OPENAI_API_KEY"` (read once at startup) does not.
    token_path = _gateway_token_path(user)
    config_toml = (
        "# Databricks Model Serving configuration (generated — do not edit)\n"
        f'model = "{model}"\n'
        f'model_provider = "{_CODEX_MODEL_PROVIDER}"\n'
        'web_search = "disabled"\n'
        f"{auto_mode}"
        "\n"
        f"[model_providers.{_CODEX_MODEL_PROVIDER}]\n"
        f'name = "{_CODEX_PROVIDER_DISPLAY_NAME}"\n'
        f'base_url = "{base_url}"\n'
        'wire_api = "responses"\n'
        f'http_headers = {{"Databricks-Ai-Gateway-Request-Tags" = {json.dumps(tag_header)}}}\n'
        # A Gateway 429 is an enforced boundary, not an invitation to multiply
        # traffic. One retry covers a transient connection without defeating
        # requester QPM/TPM policy.
        "request_max_retries = 1\n"
        "stream_max_retries = 1\n"
        "\n"
        f"[model_providers.{_CODEX_MODEL_PROVIDER}.auth]\n"
        'command = "cat"\n'
        f'args = ["{token_path}"]\n'
        "timeout_ms = 5000\n"
        "refresh_interval_ms = 240000\n"
    )
    _atomic_write_text(os.path.join(codex_dir, "config.toml"), config_toml)


def _gateway_token_path(user: User) -> str:
    return os.path.join(user.home, ".config", "workshop", "gateway-token")


def _write_gateway_token(user: User, token: str) -> None:
    """The rotating token file omnigent's auth_command reads (one line, 0600)."""
    with user.lock:
        _write_gateway_token_locked(user, token)


def _write_gateway_token_locked(user: User, token: str) -> None:
    path = _gateway_token_path(user)
    _atomic_write_text(path, f"{token}\n")


def configure_omnigent(
    user: User,
    token: str,
    *,
    write_token: bool = True,
    available: models.Catalogue | None = None,
) -> None:
    """Write ~/.omnigent/config.yaml: a default provider per model surface.

    Two entries, because omnigent reads the Claude surface differently from the
    Codex one. `databricks-gateway` declares the Anthropic family
    inline; `databricks-codex` points at the codex config table, which is what
    marks the endpoint as a Databricks AI Gateway rather than an anonymous
    proxy (see the note beside cli_config_provider below).

    Both auth_commands read the rotating token file, so this YAML is
    deterministic for a deployment and NEVER rewritten on rotation — only the
    token file is (see update_tokens). Between them the entries default every
    surface, which routes bare `omnigent`, `omnigent claude`, and
    `omnigent codex` with no selection and bypasses the first-run wizard.
    """
    if write_token:
        _write_gateway_token(user, token)

    gateway = gateway_host()
    anthropic_base = f"{gateway}/anthropic" if gateway else ""
    openai_base = _codex_base_url()

    # The same roles the CLIs resolve, so `omnigent claude` and `claude` agree
    # about what an event runs. These chains were a copy of configure_claude's
    # under a comment claiming they were one source of truth; now they are.
    if available is None:
        available = current_model_catalogue(token)
    claude_model = model_policy.resolve_service("driver", available)
    codex_model = model_policy.resolve_service("codex", available)

    # auth_command uses the absolute token path: omnigent re-runs it inside
    # tmux sessions whose $HOME handling we don't control.
    auth_command = f"cat {_gateway_token_path(user)}"
    # Omnigent recognises a Databricks AI Gateway by its URL. Every resolved
    # gateway now carries an `/ai-gateway/` path or an `ai-gateway` DNS label,
    # so this is true whenever a host is configured at all; the branch survives
    # only for the hostless case, where there is no URL to recognise.
    databricks_aware = bool(gateway)
    generated_provider = {
        "kind": "gateway",
        "default": ["anthropic"] if databricks_aware else True,
        "anthropic": {
            "base_url": anthropic_base,
            "auth_command": auth_command,
            "headers": {
                "Databricks-Ai-Gateway-Request-Tags": model_policy.request_tags(
                    "omnigent-claude"
                )
            },
            "models": {"default": claude_model},
        },
        "openai": {
            "base_url": openai_base,
            "wire_api": "responses",
            "auth_command": auth_command,
            "headers": {
                "Databricks-Ai-Gateway-Request-Tags": model_policy.request_tags(
                    "omnigent-codex"
                )
            },
            "request_max_retries": 1,
            "models": {"default": codex_model},
        },
    }
    # A `gateway` provider is, to omnigent, an anonymous OpenAI/Anthropic-shaped
    # proxy, so it resolves Codex down the vendor-direct path. That path assumes
    # vendor-native model ids and strips the qualifying `system.ai.` prefix.
    # The gateway only serves fully-qualified model-service names. The same
    # misread makes Codex declare itself unlaunchable: seeing no Databricks
    # provider to route through, it falls back to a `~/.codex/auth.json` that a
    # workshop never writes.
    #
    # Pinning the codex config table instead identifies the provider as a
    # Databricks AI Gateway, which keeps model ids intact and lets omnigent
    # enumerate the workspace's live model list rather than one default. The
    # gateway entry stays for the Claude surface, narrowed to `anthropic` so
    # exactly one provider owns each surface.
    cli_config_provider = (
        {
            "kind": "cli-config",
            "cli": "codex",
            "model_provider": _CODEX_MODEL_PROVIDER,
            "display_name": _CODEX_PROVIDER_DISPLAY_NAME,
            "default": ["openai"],
        }
        if databricks_aware
        else None
    )
    omnigent_dir = os.path.join(user.home, ".omnigent")
    os.makedirs(omnigent_dir, exist_ok=True)
    config_path = os.path.join(omnigent_dir, "config.yaml")
    with user.lock:
        try:
            with open(config_path) as handle:
                document = yaml.safe_load(handle) or {}
            if not isinstance(document, dict):
                document = {}
        except (OSError, yaml.YAMLError):
            document = {}
        tui = document.setdefault("tui", {})
        if not isinstance(tui, dict):
            tui = document["tui"] = {}
        tui["theme"] = "dark"
        runner = document.setdefault("runner", {})
        if not isinstance(runner, dict):
            runner = document["runner"] = {}
        runner["idle_timeout_s"] = config.omnigent_runner_idle_timeout()
        _apply_host_identity(document, user)
        providers = document.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = document["providers"] = {}
        providers["databricks-gateway"] = generated_provider
        if cli_config_provider is not None:
            providers[_OMNIGENT_CODEX_PROVIDER] = cli_config_provider
        else:
            providers.pop(_OMNIGENT_CODEX_PROVIDER, None)
        config_yaml = (
            "# Generated settings are merged by Workshop Terminal.\n"
            + yaml.safe_dump(document, sort_keys=False)
        )
        try:
            with open(config_path) as handle:
                if handle.read() == config_yaml:
                    os.chmod(config_path, 0o600)
                    return
        except OSError:
            pass
        _atomic_text_write(config_path, config_yaml, 0o600)


def _apply_host_identity(document: dict, user: User) -> None:
    """Pin the attendee's host identity to the one the supervisor launches.

    Omnigent commands read the host to launch their runner on from this file,
    while the supervised ``omnigent host`` takes its identity from the launch
    environment. Left unpinned the CLI invents a uuid, persists it, and then
    waits out its timeout for a daemon nobody runs — with the attendee's real
    host online beside it. An identity already written here is replaced, since a
    stale one keeps pointing at that absent daemon.

    Local deployments have no App to host against, so the CLI keeps owning the
    identity exactly as before.
    """
    from .omnigent_remote import stable_host_identity

    server_url = config.omnigent_app_url()
    if not server_url:
        return
    identity = stable_host_identity(user, server_url)
    document["host"] = {"host_id": identity.host_id, "name": identity.name}


def _atomic_text_write(path: str, content: str, mode: int) -> None:
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _databrickscfg_path(user: User) -> str:
    return os.path.join(user.home, ".databrickscfg")


def omnigent_databrickscfg_path(user: User) -> str:
    """The config the Omnigent host and every terminal below it resolve against.

    Deliberately a *different* file from ``~/.databrickscfg``: that one carries
    the app service principal in ``[DEFAULT]``, and the Omnigent plane must never
    be able to reach it. The runner's token factory falls back to Databricks SDK
    auth whenever the mirrored OIDC token is missing or stale, with no way to
    disable that fallback — so whatever this file can resolve is an identity the
    runner can silently assume. Holding only the attendee's own profile makes the
    worst case "acts as the attendee", which is what it should be acting as.
    """
    return os.path.join(user.home, ".config", "workshop", "omnigent-databrickscfg")


def _write_databrickscfg_profile(user: User, profile: str, token: str) -> None:
    """Read-modify-write a single profile in ``~/.databrickscfg`` without
    clobbering the others, under the per-user lock.

    The SP rotation thread writes ``[DEFAULT]`` and OBO capture writes ``[me]``
    from different threads; a whole-file overwrite (the old behaviour) would let
    one silently drop the other's profile. configparser preserves every section
    on round-trip, and ``[me]`` always sets its own ``host``+``token`` so the
    configparser ``[DEFAULT]`` inheritance can't bleed the SP token into it.
    """
    with user.lock:
        _write_databrickscfg_profile_locked(user, profile, token)


def _write_databrickscfg_profile_locked(user: User, profile: str, token: str) -> None:
    path = _databrickscfg_path(user)
    host = config.databricks_host()
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except (configparser.Error, OSError):
        parser = configparser.ConfigParser()
    if profile.upper() == "DEFAULT":
        parser["DEFAULT"]["host"] = host
        parser["DEFAULT"]["token"] = token
    else:
        if not parser.has_section(profile):
            parser.add_section(profile)
        parser[profile]["host"] = host
        parser[profile]["token"] = token
    import io

    output = io.StringIO()
    parser.write(output)
    _atomic_write_text(path, output.getvalue())


def configure_databricks_cli(user: User, token: str) -> None:
    """Write the SP rotating credential to ``[DEFAULT]`` — the agent's
    build/deploy/provision identity — preserving any ``[me]``/OBO profile."""
    _write_databrickscfg_profile(user, "DEFAULT", token)


def _write_omnigent_profile_locked(user: User, obo_token: str) -> None:
    """Mirror the attendee profile into the Omnigent-plane config.

    Whole-file write rather than read-modify-write: this file holds exactly one
    profile by design, and rebuilding it is how we guarantee no other credential
    can accumulate in it.
    """
    parser = configparser.ConfigParser()
    profile = config.obo_profile_name()
    parser.add_section(profile)
    parser[profile]["host"] = config.databricks_host()
    parser[profile]["token"] = obo_token
    import io

    output = io.StringIO()
    parser.write(output)
    _atomic_write_text(omnigent_databrickscfg_path(user), output.getvalue())


def update_me_profile(user: User, obo_token: str) -> None:
    """Write the attendee's OBO token to the ``[me]`` profile (governance-faithful
    Unity Catalog reads as the attendee), preserving ``[DEFAULT]``."""
    with user.lock:
        update_me_profile_locked(user, obo_token)


def update_me_profile_locked(user: User, obo_token: str) -> None:
    """Caller already holds ``user.lock``; avoid recursive lock acquisition."""
    _write_databrickscfg_profile_locked(user, config.obo_profile_name(), obo_token)
    _write_omnigent_profile_locked(user, obo_token)


def configure_all(user: User, token: str) -> None:
    # Reserve ordering before network discovery. If a rotation arrives while
    # model discovery runs, its newer revision commits the core files and this
    # initial configure is forbidden from rolling them back afterward.
    with user.lock:
        revision = _next_credential_revision_locked(user)
    # One discovery round-trip for every config this attendee gets. All three
    # writers resolve the same roles against the same workspace, and a room
    # signing in at once should not multiply that by the number of CLIs.
    available = current_model_catalogue(token)
    configure_claude(user, token, write_token=False, available=available)
    configure_codex(user, token, write_token=False, available=available)
    user.cli_ready.update({"claude", "codex", "databricks"})
    # Omnigent is feature-flagged off by default, and a workshop can be created
    # without it — don't write a config for a session type we don't offer.
    if config.omnigent_offered():
        configure_omnigent(user, token, write_token=False, available=available)
        user.cli_ready.add("omnigent")
    with user.lock:
        _commit_core_credentials_locked(user, token, revision)


def refresh_model_configs(user: User) -> None:
    """Rewrite configs for the next session without touching the live process.

    Existing Claude/Codex/Omnigent processes retain the configuration they
    started with.  This operation only changes files read by a later launch,
    which makes a CT policy fanout immediate without silently rerouting a turn
    already in flight.
    """
    available = current_model_catalogue("")
    if os.path.exists(os.path.join(user.home, ".claude", "settings.json")):
        configure_claude(user, "", write_token=False, available=available)
    if os.path.exists(os.path.join(user.home, ".codex", "config.toml")):
        configure_codex(user, "", write_token=False, available=available)
    if os.path.exists(os.path.join(user.home, ".omnigent", "config.yaml")):
        configure_omnigent(user, "", write_token=False, available=available)


def update_tokens(user: User, token: str) -> None:
    """Rotation fast path: rewrite the rotating token file the agents read.

    Claude (apiKeyHelper), Codex (provider ``auth`` command), and Omnigent
    (gateway ``auth_command``) all read the same ``gateway-token`` file at
    request time, so rotation is a single 1-line file write — none of their
    generated configs (settings.json / config.toml / config.yaml) is touched.
    A running agent process therefore always picks up the fresh token on its
    next refresh instead of holding the one captured at startup. Falls back to
    a full (re)configure only when an agent's config is missing (first run).
    """
    with user.lock:
        revision = _next_credential_revision_locked(user)
        _commit_core_credentials_locked(user, token, revision)
        missing_claude = not os.path.exists(
            os.path.join(user.home, ".claude", "settings.json")
        )
        missing_codex = not os.path.exists(
            os.path.join(user.home, ".codex", "config.toml")
        )
        missing_omnigent = config.omnigent_offered() and not os.path.exists(
            os.path.join(user.home, ".omnigent", "config.yaml")
        )

    if missing_claude:
        configure_claude(user, token, write_token=False)
    if missing_codex:
        configure_codex(user, token, write_token=False)

    # Omnigent config is only present/needed when the feature is enabled.
    if missing_omnigent:
        configure_omnigent(user, token, write_token=False)


def _next_credential_revision_locked(user: User) -> int:
    user._credential_revision += 1
    return user._credential_revision


def _commit_core_credentials_locked(user: User, token: str, revision: int) -> bool:
    if revision != user._credential_revision:
        return False
    _write_databrickscfg_profile_locked(user, "DEFAULT", token)
    _write_gateway_token_locked(user, token)
    return True


def _atomic_write_text(path: str, content: str) -> None:
    """Replace a credential file atomically with mode 0600."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".credential-", suffix=".tmp", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: str, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
