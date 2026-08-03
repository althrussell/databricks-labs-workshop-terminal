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

from . import config
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
    """Resolve the AI Gateway host once per process.

    Explicit DATABRICKS_GATEWAY_HOST is trusted; otherwise auto-construct from
    the workspace id (Azure: derivable from the host URL) and probe it.
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
            logger.info("AI Gateway not reachable at %s — using serving-endpoints", candidate)
        _gateway_resolved = ""
        return ""


# Mirrors ``omnigent/pi_native_credentials.py::_is_databricks_ai_gateway_url``
# (verified against the deployed 0.7.0 wheel). Omnigent routes a base URL as the
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


def gateway_status() -> dict:
    """How the AI Gateway resolved, and what an unresolved one costs.

    Reported, never gating, and the cost is narrower than it looks. With no
    gateway every config falls back to ``<host>/serving-endpoints``, which
    serves every model an attendee needs: Claude at
    ``/serving-endpoints/anthropic/v1/messages``, the newer GPT models at
    ``/serving-endpoints/responses``, and the chat-completions-only models
    (GLM and friends) at ``/serving-endpoints/chat/completions``. Omnigent's Pi
    harness routes per model across exactly those surfaces, deriving the
    ``/ai-gateway/codex/v1`` Responses path from the workspace host itself, so
    it does not need this variable to reach any particular model.

    What the fallback costs is governance: the gateway is where an event's
    traffic meets gateway policy, usage tracking and rate limits, and
    serving-endpoints bypasses all three. Worth reporting so an operator can
    fix the deployment; never worth blocking a workshop.

    Auto-construction cannot rescue this on AWS: the workspace id is read from
    ``DATABRICKS_WORKSPACE_ID`` or, failing that, an ``adb-<digits>`` hostname —
    which is Azure's shape. An AWS ``dbc-...`` workspace matches neither, so the
    candidate URL is never even built, let alone probed. Those two variables are
    therefore the deployment's only levers, and naming them here is what lets an
    operator fix it before an attendee finds it.
    """
    resolved = gateway_host()
    # Judge the URL Omnigent is actually handed, not the configured root. With
    # the workspace-hosted shape the root is `<host>/ai-gateway`, whose path
    # lacks the trailing slash upstream requires — so validating the root would
    # report amber for a deployment that works. `<root>/anthropic` is the value
    # configure_omnigent writes, and the surface Pi speaks natively.
    anthropic_base = f"{resolved}/anthropic" if resolved else ""
    explicit = bool(os.environ.get("DATABRICKS_GATEWAY_HOST", "").strip())
    workspace_id = bool(os.environ.get("DATABRICKS_WORKSPACE_ID", "").strip())
    host = os.environ.get("DATABRICKS_HOST", "")
    derivable = bool(re.match(r"(?:https?://)?adb-(\d+)\.", host or ""))
    if resolved:
        source = "explicit" if explicit else "constructed"
    else:
        source = "unresolved"
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


def _discover_serving_endpoints(token: str) -> set[str]:
    """READY serving-endpoint names — reflects in-geo model availability."""
    host = config.databricks_host()
    if not host or not token:
        return set()
    try:
        resp = requests.get(
            f"{host}/api/2.0/serving-endpoints",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        resp.raise_for_status()
        return {
            ep["name"] for ep in resp.json().get("endpoints", [])
            if ep.get("name") and ep.get("state", {}).get("ready") == "READY"
        }
    except Exception as e:
        logger.warning("serving-endpoint discovery failed: %s", e)
        return set()


def _pick(preferred: list[str], available: set[str], fallback: str) -> str:
    for model in preferred:
        if model in available:
            return model
    return fallback


# -- per-user config writers --

def configure_claude(user: User, token: str, *, write_token: bool = True) -> None:
    claude_dir = os.path.join(user.home, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    # The rotating token lives in a file an apiKeyHelper re-reads (below), never
    # baked statically into settings.json — a long-running `claude` process
    # would otherwise hold the token captured at startup and 401 the moment the
    # rotation loop moved on.
    if write_token:
        _write_gateway_token(user, token)

    gateway = gateway_host()
    base_url = (
        f"{gateway}/anthropic" if gateway
        else f"{config.databricks_host()}/serving-endpoints/anthropic"
    )

    available = _discover_serving_endpoints(token)
    # Newest-first degradation chains; ANTHROPIC_MODEL env pins per event
    # (e.g. databricks-claude-fable-5 for premium-tier workshops).
    opus_chain = [
        "databricks-claude-opus-4-8",
        "databricks-claude-opus-4-7",
        "databricks-claude-opus-4-6",
    ]
    sonnet_chain = [
        "databricks-claude-sonnet-5",
        "databricks-claude-sonnet-4-6",
        "databricks-claude-sonnet-4-5",
    ]
    requested = os.environ.get("ANTHROPIC_MODEL", "").strip()
    # Default to Sonnet 5 (fast, capable, cheaper than Opus) — the right
    # everyday driver for workshops; ANTHROPIC_MODEL pins Opus per event.
    default_chain = ([requested] if requested else []) + sonnet_chain + opus_chain
    settings_path = os.path.join(claude_dir, "settings.json")
    settings = _read_json(settings_path)
    env = settings.setdefault("env", {})
    env.update({
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_MODEL": _pick(default_chain, available, requested or sonnet_chain[0]),
        "ANTHROPIC_DEFAULT_OPUS_MODEL": _pick(opus_chain, available, opus_chain[0]),
        "ANTHROPIC_DEFAULT_SONNET_MODEL": _pick(
            sonnet_chain, available, sonnet_chain[0]),
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": _pick(
            ["databricks-claude-haiku-4-5"], available, "databricks-claude-haiku-4-5"),
        "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: true",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        # The CLI install is shared across attendees — never self-update.
        "DISABLE_AUTOUPDATER": "1",
        # Re-run apiKeyHelper on this cadence (ms) so a live process always picks
        # up a changed app OAuth bearer every four minutes (matching the
        # omnigent harness refresh window). A 401 also forces an
        # immediate re-run, so a mid-flight rotation self-heals either way.
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "240000",
    })
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


def configure_codex(user: User, token: str, *, write_token: bool = True) -> None:
    codex_dir = os.path.join(user.home, ".codex")
    os.makedirs(codex_dir, exist_ok=True)

    # Same rotating token file Claude's apiKeyHelper reads; Codex re-runs the
    # provider auth command on a timer (below) so a live process never holds a
    # revoked/expired token.
    if write_token:
        _write_gateway_token(user, token)

    gateway = gateway_host()
    base_url = (
        f"{gateway}/openai/v1" if gateway
        else f"{config.databricks_host()}/serving-endpoints"
    )
    model = os.environ.get("CODEX_MODEL", "").strip() or "databricks-gpt-5-5"

    auto_mode = ""
    if config.auto_mode_enabled():
        # Workshop "auto mode": codex runs without approval prompts inside the
        # attendee's isolated container HOME.
        auto_mode = (
            'approval_policy = "never"\n'
            'sandbox_mode = "danger-full-access"\n'
        )

    # A provider `auth` command is Codex's apiKeyHelper equivalent: Codex re-runs
    # it every refresh_interval_ms (and on a 401) and uses its stdout as the
    # bearer. Reading the rotating token file this way is what lets a
    # long-running Codex session survive token rotation — a static
    # `env_key = "OPENAI_API_KEY"` (read once at startup) does not.
    token_path = _gateway_token_path(user)
    config_toml = (
        "# Databricks Model Serving configuration (generated — do not edit)\n"
        f'model = "{model}"\n'
        'model_provider = "databricks"\n'
        'web_search = "disabled"\n'
        f"{auto_mode}"
        "\n"
        "[model_providers.databricks]\n"
        'name = "Databricks Model Serving"\n'
        f'base_url = "{base_url}"\n'
        'wire_api = "responses"\n'
        "\n"
        "[model_providers.databricks.auth]\n"
        'command = "cat"\n'
        f'args = ["{token_path}"]\n'
        "timeout_ms = 5000\n"
        "refresh_interval_ms = 240000\n"
    )
    with open(os.path.join(codex_dir, "config.toml"), "w") as f:
        f.write(config_toml)


def _gateway_token_path(user: User) -> str:
    return os.path.join(user.home, ".config", "workshop", "gateway-token")


def _write_gateway_token(user: User, token: str) -> None:
    """The rotating token file omnigent's auth_command reads (one line, 0600)."""
    with user.lock:
        _write_gateway_token_locked(user, token)


def _write_gateway_token_locked(user: User, token: str) -> None:
    path = _gateway_token_path(user)
    _atomic_write_text(path, f"{token}\n")


def configure_omnigent(user: User, token: str, *, write_token: bool = True) -> None:
    """Write ~/.omnigent/config.yaml: one gateway provider, both model families.

    The provider's auth_command reads the rotating token file, so this YAML is
    deterministic for a deployment and NEVER rewritten on rotation — only the
    token file is (see update_tokens). `default: true` routes bare `omnigent`,
    `omnigent claude`, and `omnigent codex` through it with no selection, and
    a present default provider bypasses omnigent's first-run wizard entirely.
    """
    if write_token:
        _write_gateway_token(user, token)

    gateway = gateway_host()
    anthropic_base = (
        f"{gateway}/anthropic" if gateway
        else f"{config.databricks_host()}/serving-endpoints/anthropic"
    )
    openai_base = (
        f"{gateway}/openai/v1" if gateway
        else f"{config.databricks_host()}/serving-endpoints"
    )

    # Same selection chains as configure_claude / configure_codex — one source
    # of truth for which models an event runs. Default to Sonnet 5; the
    # ANTHROPIC_MODEL env pins Opus (or another model) per event.
    available = _discover_serving_endpoints(token)
    sonnet_chain = [
        "databricks-claude-sonnet-5",
        "databricks-claude-sonnet-4-6",
        "databricks-claude-sonnet-4-5",
    ]
    opus_chain = [
        "databricks-claude-opus-4-8",
        "databricks-claude-opus-4-7",
        "databricks-claude-opus-4-6",
    ]
    requested = os.environ.get("ANTHROPIC_MODEL", "").strip()
    claude_chain = ([requested] if requested else []) + sonnet_chain + opus_chain
    claude_model = _pick(claude_chain, available, requested or sonnet_chain[0])
    codex_model = os.environ.get("CODEX_MODEL", "").strip() or "databricks-gpt-5-5"

    # auth_command uses the absolute token path: omnigent re-runs it inside
    # tmux sessions whose $HOME handling we don't control.
    auth_command = f"cat {_gateway_token_path(user)}"
    generated_provider = {
        "kind": "gateway",
        "default": True,
        "anthropic": {
            "base_url": anthropic_base,
            "auth_command": auth_command,
            "models": {"default": claude_model},
        },
        "openai": {
            "base_url": openai_base,
            "wire_api": "responses",
            "auth_command": auth_command,
            "models": {"default": codex_model},
        },
    }
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


def _write_databrickscfg_profile_locked(
    user: User, profile: str, token: str
) -> None:
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


def update_me_profile(user: User, obo_token: str) -> None:
    """Write the attendee's OBO token to the ``[me]`` profile (governance-faithful
    Unity Catalog reads as the attendee), preserving ``[DEFAULT]``."""
    _write_databrickscfg_profile(user, config.obo_profile_name(), obo_token)


def update_me_profile_locked(user: User, obo_token: str) -> None:
    """Caller already holds ``user.lock``; avoid recursive lock acquisition."""
    _write_databrickscfg_profile_locked(
        user, config.obo_profile_name(), obo_token
    )


def configure_all(user: User, token: str) -> None:
    # Reserve ordering before network discovery. If a rotation arrives while
    # model discovery runs, its newer revision commits the core files and this
    # initial configure is forbidden from rolling them back afterward.
    with user.lock:
        revision = _next_credential_revision_locked(user)
    configure_claude(user, token, write_token=False)
    configure_codex(user, token, write_token=False)
    user.cli_ready.update({"claude", "codex", "databricks"})
    # Omnigent is feature-flagged off by default — don't write a config (or
    # spend an endpoint-discovery round-trip) for a session type we don't offer.
    if config.omnigent_enabled():
        configure_omnigent(user, token, write_token=False)
        user.cli_ready.add("omnigent")
    with user.lock:
        _commit_core_credentials_locked(user, token, revision)


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
        missing_omnigent = config.omnigent_enabled() and not os.path.exists(
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


def _commit_core_credentials_locked(
    user: User, token: str, revision: int
) -> bool:
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
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)
