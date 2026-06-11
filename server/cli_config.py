"""Write per-user CLI configs (claude / codex / databricks CLI).

Adapted from CoDA's setup_claude.py / setup_codex.py / cli_auth.py, but
parameterized by user HOME: each attendee gets their own config files under
their own home directory, fed by their own rotating PAT.
"""

from __future__ import annotations

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

    gateway = gateway_host()
    base_url = (
        f"{gateway}/anthropic" if gateway
        else f"{config.databricks_host()}/serving-endpoints/anthropic"
    )

    available = _discover_serving_endpoints(token)
    requested = os.environ.get("ANTHROPIC_MODEL", "").strip() or "databricks-claude-opus-4-7"
    settings_path = os.path.join(claude_dir, "settings.json")
    settings = _read_json(settings_path)
    settings.setdefault("env", {}).update({
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_MODEL": _pick(
            [requested, "databricks-claude-opus-4-6", "databricks-claude-sonnet-4-6"],
            available, requested),
        "ANTHROPIC_DEFAULT_OPUS_MODEL": _pick(
            ["databricks-claude-opus-4-7", "databricks-claude-opus-4-6"],
            available, "databricks-claude-opus-4-7"),
        "ANTHROPIC_DEFAULT_SONNET_MODEL": _pick(
            ["databricks-claude-sonnet-4-6", "databricks-claude-sonnet-4-5"],
            available, "databricks-claude-sonnet-4-6"),
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": _pick(
            ["databricks-claude-haiku-4-5"], available, "databricks-claude-haiku-4-5"),
        "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: true",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    })
    # Workshop terminals run in the attendee's isolated container HOME — skip
    # permission prompts so labs flow without interruptions.
    settings.setdefault("permissions", {})["defaultMode"] = "bypassPermissions"
    settings["skipDangerousModePermissionPrompt"] = True
    _write_json(settings_path, settings)

    claude_json_path = os.path.join(user.home, ".claude.json")
    claude_json = _read_json(claude_json_path)
    claude_json["hasCompletedOnboarding"] = True
    _write_json(claude_json_path, claude_json)


def configure_codex(user: User, token: str) -> None:
    codex_dir = os.path.join(user.home, ".codex")
    os.makedirs(codex_dir, exist_ok=True)

    gateway = gateway_host()
    base_url = (
        f"{gateway}/openai/v1" if gateway
        else f"{config.databricks_host()}/serving-endpoints"
    )
    model = os.environ.get("CODEX_MODEL", "").strip() or "databricks-gpt-5-5"

    config_toml = (
        "# Databricks Model Serving configuration (generated — do not edit)\n"
        f'model = "{model}"\n'
        'model_provider = "databricks"\n'
        'web_search = "disabled"\n'
        "\n"
        "[model_providers.databricks]\n"
        'name = "Databricks Model Serving"\n'
        f'base_url = "{base_url}"\n'
        'env_key = "OPENAI_API_KEY"\n'
        'wire_api = "responses"\n'
    )
    with open(os.path.join(codex_dir, "config.toml"), "w") as f:
        f.write(config_toml)

    env_path = os.path.join(codex_dir, ".env")
    with open(env_path, "w") as f:
        f.write(f"OPENAI_API_KEY={token}\n")
    os.chmod(env_path, 0o600)


def configure_databricks_cli(user: User, token: str) -> None:
    cfg_path = os.path.join(user.home, ".databrickscfg")
    with open(cfg_path, "w") as f:
        f.write(f"[DEFAULT]\nhost = {config.databricks_host()}\ntoken = {token}\n")
    os.chmod(cfg_path, 0o600)


def configure_all(user: User, token: str) -> None:
    configure_databricks_cli(user, token)
    configure_claude(user, token)
    configure_codex(user, token)
    user.cli_ready.update({"claude", "codex", "databricks"})


def update_tokens(user: User, token: str) -> None:
    """Rotation fast path: swap the literal token in existing config files."""
    configure_databricks_cli(user, token)

    settings_path = os.path.join(user.home, ".claude", "settings.json")
    settings = _read_json(settings_path)
    if settings.get("env", {}).get("ANTHROPIC_AUTH_TOKEN") is not None:
        settings["env"]["ANTHROPIC_AUTH_TOKEN"] = token
        _write_json(settings_path, settings)
    else:
        configure_claude(user, token)

    codex_env = os.path.join(user.home, ".codex", ".env")
    if os.path.exists(codex_env):
        with open(codex_env, "w") as f:
            f.write(f"OPENAI_API_KEY={token}\n")
    else:
        configure_codex(user, token)


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
