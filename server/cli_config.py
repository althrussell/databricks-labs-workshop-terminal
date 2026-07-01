"""Write per-user CLI configs (claude / codex / databricks CLI).

Adapted from CoDA's setup_claude.py / setup_codex.py / cli_auth.py, but
parameterized by user HOME: each attendee gets their own config files under
their own home directory, fed by their own rotating PAT.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import threading

import requests

from . import config
from .users import User

logger = logging.getLogger(__name__)

_gateway_lock = threading.Lock()
_gateway_resolved: str | None = None  # None = never probed; "" = no gateway


# -- gateway / model discovery (CoDA utils.py port) --

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

def configure_claude(user: User, token: str) -> None:
    claude_dir = os.path.join(user.home, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    # The rotating token lives in a file an apiKeyHelper re-reads (below), never
    # baked statically into settings.json — a long-running `claude` process
    # would otherwise hold the token captured at startup and 401 the moment the
    # rotation loop moved on.
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
    requested = os.environ.get("ANTHROPIC_MODEL", "").strip()
    default_chain = ([requested] if requested else []) + opus_chain + ["databricks-claude-sonnet-4-6"]
    settings_path = os.path.join(claude_dir, "settings.json")
    settings = _read_json(settings_path)
    env = settings.setdefault("env", {})
    env.update({
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_MODEL": _pick(default_chain, available, requested or opus_chain[0]),
        "ANTHROPIC_DEFAULT_OPUS_MODEL": _pick(opus_chain, available, opus_chain[0]),
        "ANTHROPIC_DEFAULT_SONNET_MODEL": _pick(
            ["databricks-claude-sonnet-4-6", "databricks-claude-sonnet-4-5"],
            available, "databricks-claude-sonnet-4-6"),
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": _pick(
            ["databricks-claude-haiku-4-5"], available, "databricks-claude-haiku-4-5"),
        "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: true",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        # The CLI install is shared across attendees — never self-update.
        "DISABLE_AUTOUPDATER": "1",
        # Re-run apiKeyHelper on this cadence (ms) so a live process always picks
        # up the rotated token well before the 15-min minted lifetime expires
        # (matches the omnigent harness refresh window). A 401 also forces an
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


def configure_codex(user: User, token: str) -> None:
    codex_dir = os.path.join(user.home, ".codex")
    os.makedirs(codex_dir, exist_ok=True)

    # Same rotating token file Claude's apiKeyHelper reads; Codex re-runs the
    # provider auth command on a timer (below) so a live process never holds a
    # revoked/expired token.
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
    path = _gateway_token_path(user)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"{token}\n")
    os.chmod(path, 0o600)


def configure_omnigent(user: User, token: str) -> None:
    """Write ~/.omnigent/config.yaml: one gateway provider, both model families.

    The provider's auth_command reads the rotating token file, so this YAML is
    deterministic for a deployment and NEVER rewritten on rotation — only the
    token file is (see update_tokens). `default: true` routes bare `omnigent`,
    `omnigent claude`, and `omnigent codex` through it with no selection, and
    a present default provider bypasses omnigent's first-run wizard entirely.
    """
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
    # of truth for which models an event runs.
    available = _discover_serving_endpoints(token)
    opus_chain = [
        "databricks-claude-opus-4-8",
        "databricks-claude-opus-4-7",
        "databricks-claude-opus-4-6",
    ]
    requested = os.environ.get("ANTHROPIC_MODEL", "").strip()
    claude_chain = ([requested] if requested else []) + opus_chain + ["databricks-claude-sonnet-4-6"]
    claude_model = _pick(claude_chain, available, requested or opus_chain[0])
    codex_model = os.environ.get("CODEX_MODEL", "").strip() or "databricks-gpt-5-5"

    # auth_command uses the absolute token path: omnigent re-runs it inside
    # tmux sessions whose $HOME handling we don't control.
    auth_command = f"cat {_gateway_token_path(user)}"
    config_yaml = (
        "# Generated by the workshop terminal — do not edit.\n"
        # A persisted theme skips omnigent's first-run theme picker; the
        # workshop terminal renders dark.
        "tui:\n"
        "  theme: dark\n"
        # Keep the background runner alive across attendee idle (lunch, closed
        # tab). Omnigent defaults this to 3600s and then exits with "runner
        # idle timeout reached" — surfacing as "Runner disconnected
        # unexpectedly" when the attendee returns. 0 disables the watchdog.
        "runner:\n"
        f"  idle_timeout_s: {config.omnigent_runner_idle_timeout()}\n"
        "providers:\n"
        "  databricks-gateway:\n"
        "    kind: gateway\n"
        "    default: true\n"
        "    anthropic:\n"
        f"      base_url: {anthropic_base}\n"
        f"      auth_command: {auth_command}\n"
        "      models:\n"
        f"        default: {claude_model}\n"
        "    openai:\n"
        f"      base_url: {openai_base}\n"
        "      wire_api: responses\n"
        f"      auth_command: {auth_command}\n"
        "      models:\n"
        f"        default: {codex_model}\n"
    )
    omnigent_dir = os.path.join(user.home, ".omnigent")
    os.makedirs(omnigent_dir, exist_ok=True)
    config_path = os.path.join(omnigent_dir, "config.yaml")
    # Skip no-op rewrites so config.yaml mtime only moves on real changes.
    try:
        with open(config_path) as f:
            if f.read() == config_yaml:
                return
    except OSError:
        pass
    with open(config_path, "w") as f:
        f.write(config_yaml)
    os.chmod(config_path, 0o600)


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
    path = _databrickscfg_path(user)
    host = config.databricks_host()
    with user.lock:
        parser = configparser.ConfigParser()
        try:
            parser.read(path)
        except (configparser.Error, OSError):
            parser = configparser.ConfigParser()  # corrupt file — rewrite clean
        if profile.upper() == "DEFAULT":
            parser["DEFAULT"]["host"] = host
            parser["DEFAULT"]["token"] = token
        else:
            if not parser.has_section(profile):
                parser.add_section(profile)
            parser[profile]["host"] = host
            parser[profile]["token"] = token
        with open(path, "w") as f:
            parser.write(f)
        os.chmod(path, 0o600)


def configure_databricks_cli(user: User, token: str) -> None:
    """Write the SP rotating credential to ``[DEFAULT]`` — the agent's
    build/deploy/provision identity — preserving any ``[me]``/OBO profile."""
    _write_databrickscfg_profile(user, "DEFAULT", token)


def update_me_profile(user: User, obo_token: str) -> None:
    """Write the attendee's OBO token to the ``[me]`` profile (governance-faithful
    Unity Catalog reads as the attendee), preserving ``[DEFAULT]``."""
    _write_databrickscfg_profile(user, config.obo_profile_name(), obo_token)


def configure_all(user: User, token: str) -> None:
    configure_databricks_cli(user, token)
    configure_claude(user, token)
    configure_codex(user, token)
    user.cli_ready.update({"claude", "codex", "databricks"})
    # Omnigent is feature-flagged off by default — don't write a config (or
    # spend an endpoint-discovery round-trip) for a session type we don't offer.
    if config.omnigent_enabled():
        configure_omnigent(user, token)
        user.cli_ready.add("omnigent")


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
    configure_databricks_cli(user, token)
    _write_gateway_token(user, token)

    if not os.path.exists(os.path.join(user.home, ".claude", "settings.json")):
        configure_claude(user, token)
    if not os.path.exists(os.path.join(user.home, ".codex", "config.toml")):
        configure_codex(user, token)

    # Omnigent config is only present/needed when the feature is enabled.
    if config.omnigent_enabled() and not os.path.exists(
        os.path.join(user.home, ".omnigent", "config.yaml")
    ):
        configure_omnigent(user, token)


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
