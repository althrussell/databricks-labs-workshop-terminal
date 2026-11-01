"""Write per-user CLI configs (claude / codex / databricks CLI).

Everything here is parameterized by user HOME: each attendee gets their own
config files under their own home directory, fed by the app's auto-refreshing
OAuth bearer.
"""

from __future__ import annotations

import configparser
from functools import wraps
import json
import logging
import os
import re
import tempfile
import threading
from urllib.parse import urlparse

import requests
import yaml

from . import build_plan, config, model_policy, models
from .capture_journal import CaptureError
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
# (verified against the 0.15.0 and 0.19.1 wheels, where it lives in
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


#: Claude Code's non-essential startup and background probes send parameters
#: the Databricks AI Gateway rejects (313 x HTTP 400 at EQL session start, 27 Oct
#: 2026). Claude reads these from settings.json for terminal sessions; the
#: Omnigent host exports them too, because its Claude harnesses still answered
#: 400 at session start on the 28 Oct E2E run.
CLAUDE_QUIET_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
}


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
    window. Omnigent (0.15.0 through 0.19.1) launches its own Claude terminals with
    ``CLAUDE_CODE_USE_GATEWAY=1`` for this reason, and re-adds the disable flag
    whenever it does not see that variable.

    Negotiating is visible in the gateway's usage table: the first request of
    a Claude session answers HTTP 400 and the retry a second or two later, on
    the same model, answers 200 (test1025: one or two per session start, on
    the main model and on Haiku). That is the negotiation, not a fault. The
    gateway records no reason for the 400, so which beta it refuses cannot be
    pinned from here; disabling the betas would stop the 400s at the cost of
    MCP tool search, which is the worse trade.
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


def _policy_read_operation(operation):
    @wraps(operation)
    def configure(user: User, *args, **kwargs):
        try:
            with model_policy.store.read_operation():
                return operation(user, *args, **kwargs)
        except (model_policy.ModelPolicyConfigurationError, CaptureError):
            # A next launch must redo the whole config operation after a raced
            # policy/source change, rather than accepting existing stale files.
            user.cli_ready.clear()
            raise
    return configure


@_policy_read_operation
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
            # With a whole room on one regional model pool, 429/529 and the odd
            # 5xx are normal; Claude Code's default (10 retries) would hide a
            # blocked AI Gateway budget behind minutes of backoff, while 0 ended
            # the attendee's turn on the first blip. Three retries ride out a
            # burst in seconds and a budget 403 (which costs no tokens to retry)
            # still surfaces fast — the terminal stream scanner shows the
            # allowance-exhausted banner the moment it appears.
            "CLAUDE_CODE_MAX_RETRIES": "3",
            **CLAUDE_QUIET_ENV,
        }
    )
    # A static ANTHROPIC_AUTH_TOKEN in the env block would take precedence over
    # apiKeyHelper (making the helper dormant) and reintroduce the stale-token
    # 401. Drop it so the dynamic helper is the sole credential source.
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    # apiKeyHelper prints the current rotating token; Claude sends it as the
    # bearer to the gateway. Absolute path — Claude runs it via /bin/sh.
    settings["apiKeyHelper"] = f"cat {_gateway_token_path(user)}"
    # A deploy waits for the App to go live, which runs past Claude Code's
    # default MCP tool timeout (two deploys were cut at about 182 s, 30 Oct).
    # One hour is the helper's own ceiling (MAX_TIMEOUT_SECONDS).
    env["MCP_TOOL_TIMEOUT"] = str(MCP_TOOL_TIMEOUT_MS)
    # Workshop "auto mode": zero permission prompts inside the attendee's
    # isolated container HOME. WORKSHOP_AUTO_MODE=false restores safe prompts.
    permissions = settings.setdefault("permissions", {})
    # This is the only local MCP capability Workshop Terminal injects. Name it
    # explicitly even in auto mode so switching that mode off later does not
    # turn a multi-minute deploy into an approval deadlock.
    deploy_permission = "mcp__workshop__deploy_databricks_app"
    allowed = permissions.setdefault("allow", [])
    if deploy_permission not in allowed:
        allowed.append(deploy_permission)
    # Plan mode (a new build, before the attendee's yes) still has to run the
    # workshop helpers without a prompt. Only the planning subcommands are
    # pre-allowed: `plan --approved` is never, so the attendee's yes is not
    # something an agent can grant itself. A broad rule from an earlier release
    # is removed, or it would keep that door open.
    allowed[:] = [rule for rule in allowed if rule not in _RETIRED_ALLOW_RULES]
    for rule in PLAN_MODE_ALLOW_RULES:
        if rule not in allowed:
            allowed.append(rule)
    _set_plan_approval_hook(settings)
    # Omnigent hands every agent its embedded-browser tools, but they only work
    # with the Omnigent desktop app as their renderer. In the Workshop Terminal
    # there never is one, so each call answers "no browser renderer is
    # connected" and the agent loops on it (test1025, seat 1). Omnigent runs
    # Claude with user settings, so a deny here removes them; agents verify an
    # app over HTTP instead. Deny rules hold in bypassPermissions mode too.
    denied = permissions.setdefault("deny", [])
    for tool in OMNIGENT_RENDERER_TOOLS:
        if config.omnigent_offered() and tool not in denied:
            denied.append(tool)
        elif not config.omnigent_offered() and tool in denied:
            denied.remove(tool)
    # F-012 (31 Oct): the apx MCP's logs tool shells out to `databricks auth
    # token`, which a seat's OAuth cache never satisfies, so every call failed.
    # Agents read a deployed app's logs with `databricks apps logs` instead.
    for tool in APX_UNUSABLE_TOOLS:
        if tool not in denied:
            denied.append(tool)
    if gateway:
        if "WebSearch" not in denied:
            denied.append("WebSearch")
    if not denied:
        permissions.pop("deny", None)
    # No defaultMode here (F-003, 31 Oct). A base mode of bypassPermissions in
    # user settings made every session start in bypass, plan launches included,
    # and Claude Code then announced "Session was started with bypass
    # permissions" on each start. Full auto is now a launch flag: the terminal
    # passes --dangerously-skip-permissions after plan approval or on resume
    # (agents.claude_auto_flags), and Omnigent gets the same flag from its own
    # config (configure_omnigent). Any defaultMode an earlier release wrote is
    # removed here.
    permissions.pop("defaultMode", None)
    if config.auto_mode_enabled():
        # One-time "are you sure?" prompt is honored from user-scope settings.
        settings["skipDangerousModePermissionPrompt"] = True
    else:
        settings.pop("skipDangerousModePermissionPrompt", None)
    _write_json(settings_path, settings)

    claude_json_path = os.path.join(user.home, ".claude.json")
    claude_json = _read_json(claude_json_path)
    claude_json["hasCompletedOnboarding"] = True
    if config.auto_mode_enabled():
        # Claude Code 2.1.30x asks "Make auto mode your default permission
        # mode?" on first launch when the default mode is not auto, which stops
        # the attendee before they can type. Declining records exactly this and
        # keeps bypass permissions; pre-answer it the same way.
        claude_json["hasSeenAutoDefaultNudge"] = True
    _write_json(claude_json_path, claude_json)
    # Pre-trust every build folder, whichever agent created it (R-004): a
    # Codex-made build otherwise opens Claude on its folder-trust prompt, where
    # "No, exit" is the highlighted default.
    _update_claude_json_trust(claude_json_path, codex_trusted_folders(user))


#: Claude Code's MCP tool-call ceiling, in milliseconds (one hour).
MCP_TOOL_TIMEOUT_MS = 3600000

#: Helpers that must run without a prompt while a build's plan is still awaiting
#: the attendee's yes. Plan mode prompts for shell commands outside its read-only
#: set, so without these a planning turn stops on every `workshop-workflow show`.
PLAN_MODE_ALLOW_RULES = (
    "Bash(workshop-workflow show)",
    "Bash(workshop-workflow show *)",
    "Bash(workshop-workflow prefs)",
    "Bash(workshop-workflow prefs *)",
    "Bash(workshop-workflow phase *)",
    "Bash(workshop-workflow plan --put *)",
    "Bash(workshop-workflow plan --check *)",
    "Bash(workshop-discovery *)",
)

#: Rules an earlier release wrote that pre-approved the whole helper, approval included.
_RETIRED_ALLOW_RULES = ("Bash(workshop-workflow *)",)


def _is_plan_approval_group(group: object) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = group.get("hooks")
    return isinstance(handlers, list) and any(
        isinstance(handler, dict) and handler.get("command") == build_plan.PLAN_APPROVAL_COMMAND
        for handler in handlers
    )


def _set_plan_approval_hook(settings: dict) -> None:
    """Record the attendee's approval when Claude leaves plan mode.

    PostToolUse fires only after a successful tool call, so a rejected plan (the
    attendee choosing "No, keep planning") records nothing. The group is
    replaced rather than appended, so repeated configuration never duplicates it.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = settings["hooks"] = {}
    groups = hooks.get("PostToolUse")
    kept = [group for group in groups if not _is_plan_approval_group(group)] if isinstance(groups, list) else []
    kept.append({
        "matcher": "ExitPlanMode",
        "hooks": [{"type": "command", "command": build_plan.PLAN_APPROVAL_COMMAND}],
    })
    hooks["PostToolUse"] = kept


def _trust_claude_folder(claude_json: dict, folder: str) -> None:
    projects = claude_json.get("projects")
    if not isinstance(projects, dict):
        projects = claude_json["projects"] = {}
    entry = projects.get(folder)
    if not isinstance(entry, dict):
        entry = projects[folder] = {}
    entry["hasTrustDialogAccepted"] = True


def _claude_folder_trusted(claude_json: dict, folder: str) -> bool:
    projects = claude_json.get("projects")
    entry = projects.get(folder) if isinstance(projects, dict) else None
    return isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True


def _read_text_or_none(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _update_claude_json_trust(path: str, folders: list[str]) -> bool:
    """Trust ``folders`` in ~/.claude.json, safely against Claude's own saves.

    Claude rewrites this file too. So: nothing is written when every folder is
    already trusted; a file that does not parse is never written (that would
    erase the attendee's settings); and the file is re-read just before the
    atomic replace, re-applying the change if it moved in between. Returns False
    when it could not be done safely.
    """
    for _ in range(3):
        try:
            before = _read_text_or_none(path)
        except OSError:
            return False
        if before is None:
            data: dict = {}
        else:
            try:
                data = json.loads(before)
            except ValueError:
                return False
            if not isinstance(data, dict):
                return False
        missing = [folder for folder in folders if not _claude_folder_trusted(data, folder)]
        if not missing:
            return True
        for folder in missing:
            _trust_claude_folder(data, folder)
        try:
            current = _read_text_or_none(path)
        except OSError:
            return False
        if current != before:
            continue  # Claude saved in the meantime; apply the change to its version
        _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
        return True
    return False


def ensure_claude_trusts(user: User, folder: str) -> None:
    """Trust a launch folder for Claude before it opens there (a build made since
    the config was written). Only the trust flag changes; every other key stays."""
    path = os.path.join(user.home, ".claude.json")
    if not os.path.isfile(path):
        return  # not configured yet; configure_claude writes the file and trusts then
    _update_claude_json_trust(path, [os.path.realpath(folder)])


#: apx MCP tools Claude must not reach on a seat. Codex's apx server is an
#: allowlist (`enabled_tools` in configure_codex) and leaves all of these out.
#: - databricks_apps_logs cannot work (F-012, 31 Oct): it shells out to
#:   `databricks auth token`, which a seat's OAuth cache never satisfies.
#: - feedback_prepare/feedback_submit file *public GitHub issues* on the apx
#:   repository as whoever holds the token. An autonomous agent in
#:   bypassPermissions mode must never be able to post an attendee's project
#:   details to a public tracker on their behalf.
APX_UNUSABLE_TOOLS = (
    "mcp__apx__databricks_apps_logs",
    "mcp__apx__feedback_prepare",
    "mcp__apx__feedback_submit",
)

#: Omnigent 0.15's embedded-browser tools, as Claude sees them through
#: Omnigent's MCP server. They need the Omnigent desktop app as a renderer.
OMNIGENT_RENDERER_TOOLS = tuple(
    f"mcp__omnigent__browser_{action}"
    for action in ("navigate", "snapshot", "click", "type", "screenshot")
)

# The `[model_providers.X]` id in ~/.codex/config.toml. Omnigent's config
# points at this table by id, so the two files have to agree on the name.
_CODEX_MODEL_PROVIDER = "databricks"
_CODEX_AUTO_COMPACT_TOKEN_LIMIT = 120000
_CODEX_PROVIDER_DISPLAY_NAME = "Databricks Model Serving"

# What `workshop-app-deploy --mcp` reads to report back to the terminal. Claude
# Code hands a stdio MCP server its whole environment; Codex hands it only a
# small default allowlist plus the names listed here. Without these the helper
# deployed fine but its callbacks were silently skipped, so Codex deploys left
# no app.deploy event, no deploy screenshot and no early app grant (28 Oct E2E,
# seat 2, for both the Codex terminal and Omnigent's codex-native harness).
_WORKSHOP_MCP_ENV_VARS = (
    "WORKSHOP_APP_URL",
    "WORKSHOP_USER_EMAIL",
    "WORKSHOP_PROJECTS_ROOT",
    "WORKSHOP_APP_DEPLOY_STATE_DIR",
    "WORKSHOP_APP_DEPLOY_POLL_SECONDS",
)

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


@_policy_read_operation
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
        f'model_reasoning_effort = "{config.codex_reasoning_effort()}"\n'
        # Sweep contract, decision 3 (31 Oct) set these lines verbatim. Only the
        # effort value comes from config.codex_reasoning_effort(). The owner keeps
        # reasoning summaries visible (1 Nov, F-009 accepted): hide_agent_reasoning
        # = true did not hide them in Codex's terminal UI anyway.
        'model_reasoning_summary = "auto"\n'
        "hide_agent_reasoning = false\n"
        "show_raw_agent_reasoning = false\n"
        # The contract says this is for a verified model/gateway route
        # (system.ai.gpt-6-1-sol on labs). Written unconditionally as the
        # contract's lines are; the final test must confirm the resolved model.
        "model_supports_reasoning_summaries = true\n"
        # Codex does not know the gateway's model ids, so without an explicit
        # threshold it compacts only near the model's full window (~247k seen on
        # 29 Oct). A long Omni-Codex session then re-sent ~200k tokens a request
        # and hit the seat's token-rate limit for over an hour (F-020). Omnigent
        # copies this file into each Codex-native session, so this covers both.
        f"model_auto_compact_token_limit = {_CODEX_AUTO_COMPACT_TOKEN_LIMIT}\n"
        f'model_provider = "{_CODEX_MODEL_PROVIDER}"\n'
        'web_search = "disabled"\n'
        f"{auto_mode}"
        "\n"
        # Lets Codex put choices to the attendee as a clickable form
        # (request_user_input) instead of a numbered list it has to type back.
        "[features]\n"
        "collaboration_modes = true\n"
        # Without this, request_user_input exists only in Plan mode, which can't
        # save the answers (prefs/path) the meeting needs.
        "default_mode_request_user_input = true\n"
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
        "\n"
        "[mcp_servers.workshop]\n"
        f"command = {json.dumps(os.path.join(user.home, '.local', 'bin', 'workshop-app-deploy'))}\n"
        'args = ["--mcp"]\n'
        f"env_vars = {json.dumps(list(_WORKSHOP_MCP_ENV_VARS))}\n"
        'enabled_tools = ["deploy_databricks_app"]\n'
        "startup_timeout_sec = 10\n"
        "tool_timeout_sec = 3600\n"
        "\n[mcp_servers.apx]\n"
        f"command = {json.dumps(os.path.join(config.shared_prefix(), 'bin', 'apx'))}\n"
        'args = ["mcp"]\n'
        # No databricks_apps_logs: it fails on every seat (F-012, 31 Oct).
        # `databricks apps logs <app>` reads the same logs.
        'enabled_tools = ["start", "stop", "restart", "logs", "check", "routes", '
        '"get_route_info", "refresh_openapi", "docs", "search_registry_components", '
        '"add_component", "list_registry_components"]\n'
        "startup_timeout_sec = 20\n"
        + codex_trust_toml(codex_trusted_folders(user))
    )
    _atomic_write_text(os.path.join(codex_dir, "config.toml"), config_toml)


def codex_trusted_folders(user: User) -> list[str]:
    """The builds root and every build in it, by real path.

    Codex asks "Do you trust the contents of this directory?" for any launch
    folder (or its git root) without a ``[projects."<path>"]`` trust entry.
    Agents launch in the attendee's current build, so once an attendee had
    created one, every new Codex tab opened on a trust prompt (24 Oct 2026
    rehearsal). Paths are real paths because Codex canonicalises the folder.
    """
    root = os.path.join(user.home, "projects")
    folders = {os.path.realpath(root)}
    if os.path.isdir(root):
        for entry in os.scandir(root):
            if entry.is_dir(follow_symlinks=False):
                folders.add(os.path.realpath(entry.path))
    return sorted(folders)


def codex_trust_toml(folders: list[str]) -> str:
    return "".join(f'\n[projects.{json.dumps(folder)}]\ntrust_level = "trusted"\n' for folder in folders)


def ensure_codex_trusts(user: User, folder: str) -> None:
    """Trust a launch folder before Codex opens in it (a build made since the
    config was written). A no-op when Codex is not configured yet."""
    path = os.path.join(user.home, ".codex", "config.toml")
    if not os.path.isfile(path):
        return
    folder = os.path.realpath(folder)
    with open(path, encoding="utf-8") as handle:
        current = handle.read()
    if f"[projects.{json.dumps(folder)}]" in current:
        return
    _atomic_write_text(path, current.rstrip("\n") + "\n" + codex_trust_toml([folder]))


def _gateway_token_path(user: User) -> str:
    return os.path.join(user.home, ".config", "workshop", "gateway-token")


def _write_gateway_token(user: User, token: str) -> None:
    """The rotating token file omnigent's auth_command reads (one line, 0600)."""
    with user.lock:
        _write_gateway_token_locked(user, token)


def _write_gateway_token_locked(user: User, token: str) -> None:
    path = _gateway_token_path(user)
    _atomic_write_text(path, f"{token}\n")


@_policy_read_operation
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

    Omni-Claude plan approval (sweep contract §2): Omni-Claude never runs native
    plan mode. It uses the workflow gate (`workshop-workflow plan --put` /
    `--approved`), as Codex does. Its full-auto mode comes from the claude-native
    harness args below, not from ~/.claude/settings.json (F-003, 31 Oct).
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
        _apply_omnigent_claude_args(document, config.auto_mode_enabled())
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


#: Claude Code's full-permission launch flag, the one Omni-Claude gets in auto mode.
_CLAUDE_AUTO_FLAG = "--dangerously-skip-permissions"


def _apply_omnigent_claude_args(document: dict, auto_mode: bool) -> None:
    """Give Omni-Claude its full-auto mode as a launch argument (F-003, 31 Oct).

    Omnigent's claude-native terminal merges ``harness.claude-native.args`` from
    this file into its launch, and its per-session settings sidecar sets no
    permission mode of its own. So once WT stopped writing
    ``permissions.defaultMode`` to the user settings, Omni-Claude would prompt
    for every edit unless the flag is here. Only WT's own flag is added or
    removed; any other args the attendee or operator set are kept.
    """
    harness = document.get("harness")
    if not isinstance(harness, dict):
        if not auto_mode:
            return
        harness = document["harness"] = {}
    entry = harness.get("claude-native")
    entry = entry if isinstance(entry, dict) else {}
    current = entry.get("args")
    args = list(current) if isinstance(current, list) else []
    args = [arg for arg in args if arg != _CLAUDE_AUTO_FLAG]
    if auto_mode:
        args.append(_CLAUDE_AUTO_FLAG)
    if args:
        entry["args"] = args
        harness["claude-native"] = entry
    else:
        entry.pop("args", None)
        if entry:
            harness["claude-native"] = entry
        else:
            harness.pop("claude-native", None)
    if not harness:
        document.pop("harness", None)


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

    Whole-file write rather than read-modify-write: this file holds only the
    attendee's OBO credential (never the app service principal). Writing both
    ``[DEFAULT]`` and ``[me]`` with the attendee's OBO token guarantees that
    Python code calling ``WorkspaceClient(profile="DEFAULT")`` inside Omnigent
    never fails with ``profile DEFAULT not found`` while keeping the Omnigent
    plane 100% SP-free.
    """
    parser = configparser.ConfigParser()
    host = config.databricks_host()
    parser["DEFAULT"]["host"] = host
    parser["DEFAULT"]["token"] = obo_token
    profile = config.obo_profile_name()
    if profile.upper() != "DEFAULT":
        parser.add_section(profile)
        parser[profile]["host"] = host
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


@_policy_read_operation
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


@_policy_read_operation
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
