#!/usr/bin/env python3
"""Simulate a full Control Tower deployment of the Workshop Terminal app.

This goes beyond ``deploy_dev.py`` (source import + deploy) by reproducing the
Control-Tower contract documented in ``docs/control-tower-implementation.md``,
including the OBO dual-profile feature (§8) and entitlement provisioning (§9):

  1. Import source to the deploying user's workspace home.
  2. Patch the *uploaded* app.yaml bytes (never the git copy) with the attendee,
     OBO/entitlement settings, admin group, and reviewed release pins.
  3. Create the app, declaring user_api_scopes (catalog.*:read + sql) on the app
     resource (the OBO scope ceiling). NOTE: there is no `unity-catalog` scope —
     use the granular `catalog.catalogs:read` / `catalog.schemas:read` /
     `catalog.tables:read` scopes (list UC metadata) + `sql` (query data).
  4. Provision the per-attendee catalog: labuser OWNER + ALL_PRIVILEGES/MANAGE, app SP
     catalog-scoped MANAGE + USE_CATALOG + CREATE_SCHEMA (§9).
  5. Deploy and require the app to report healthy direct app-identity OAuth.
  6. Print an acceptance summary.

Event deployment defaults to no static PAT and fails closed on missing OBO
scopes. Permission assumptions are reported explicitly and produce a nonzero
exit instead of silently claiming readiness. Use --dry-run/--validate to print
the secret-free mutation plan without calling workspace APIs.

  export DATABRICKS_CONFIG_PROFILE=labs
  python scripts/deploy_ct_sim.py --name workshop-terminal-ct \
    --skills-ref v1.2.3 \
    --anthropic-model databricks-claude-sonnet-5 \
    --codex-model databricks-gpt-5-6-codex

Requires: databricks-sdk.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import requests
import yaml

EXCLUDE = ["node_modules*", ".venv*", "__pycache__*", ".git*", "frontend/node_modules*", "*.pyc", "uploads*"]
REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_SCOPES = "catalog.catalogs:read,catalog.schemas:read,catalog.tables:read,sql"
BASELINE_SCOPES = frozenset(DEFAULT_SCOPES.split(","))
EXACT_DEFAULTS = {
    "claude_code_version": "2.1.216",
    "codex_cli_version": "0.147.0",
    "databricks_cli_version": "1.11.0",
    "omnigent_version": "0.9.0",
    "node_version": "24.18.1",
    "pi_cli_version": "0.83.0",
}
SEMVER_PATTERN = (
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:"
    r"(?:0|[1-9][0-9]*)"
    r"|(?:[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r")(?:\.(?:"
    r"(?:0|[1-9][0-9]*)"
    r"|(?:[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
SEMVER_RE = re.compile(rf"^{SEMVER_PATTERN}$")
VERSION_TAG_RE = re.compile(rf"^v{SEMVER_PATTERN}$")
MODEL_ENDPOINT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
FLOATING_MODEL_WORDS = frozenset({"latest", "stable", "default", "auto", "current"})


def _log(step: str, msg: str) -> None:
    print(f"[ct-sim] {step:<14} {msg}", flush=True)


def _parse_args(argv=None, *, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="workshop-terminal-ct")
    parser.add_argument("--attendee", default="",
                        help="labuser email used for OBO + entitlement grants "
                             "(required for --dry-run; otherwise defaults to deployer)")
    parser.add_argument("--catalog", default="workshop_ct_sim",
                        help="WORKSHOP_CATALOG to provision (labuser owner + ALL PRIVILEGES)")
    parser.add_argument("--catalog-existing-owner", default="")
    parser.add_argument("--catalog-existing-creator", default="")
    parser.add_argument("--catalog-existing-type", default="")
    parser.add_argument("--catalog-existing-isolation-mode", default="")
    parser.add_argument("--catalog-existing-storage-root", default="")
    parser.add_argument(
        "--attendee-token-env",
        default="WORKSHOP_ATTENDEE_TOKEN",
        help="external CT OAuth exchange result used for attendee acceptance",
    )
    parser.add_argument(
        "--scopes",
        default=DEFAULT_SCOPES,
        help="OBO user_api_scopes declared on the app resource (comma-separated). "
             "No 'unity-catalog' scope exists — use the granular 'catalog.*:read' scopes.")
    parser.add_argument(
        "--with-emergency-pat",
        action="store_true",
        help="explicitly inject WORKSHOP_PAT from the environment (degraded emergency fallback)",
    )
    parser.add_argument("--no-obo", action="store_true", help="leave ENABLE_OBO off")
    parser.add_argument(
        "--insight-capture",
        action="store_true",
        help="set WORKSHOP_INSIGHT_CAPTURE=true (behavioural signal, agent discovery, wrap summary)",
    )
    parser.add_argument("--no-entitlements", action="store_true", help="leave ENABLE_ENTITLEMENTS off")
    parser.add_argument(
        "--non-event-mode",
        action="store_true",
        help="allow disabling event-required OBO/entitlements for non-event development",
    )
    parser.add_argument(
        "--profile",
        default="",
        help="Databricks CLI profile (or set DATABRICKS_CONFIG_PROFILE)",
    )
    parser.add_argument("--admin-group", default="platform_admins")
    parser.add_argument("--skills-ref", required=True,
                        help="reviewed databricks-agent-skills tag or commit SHA (branch tips rejected)")
    parser.add_argument("--anthropic-model", required=True,
                        help="exact reviewed Anthropic serving endpoint")
    parser.add_argument("--codex-model", required=True,
                        help="exact reviewed Codex serving endpoint")
    parser.add_argument(
        "--model-profile",
        default="",
        help="WORKSHOP_MODEL_PROFILE: the event's cost posture. Empty means "
             "balanced, which is what every event ran before this existed. "
             "'economy' caps Claude's Opus slot at Sonnet for a large free "
             "event; 'frontier' promotes the everyday driver to Opus.",
    )
    parser.add_argument("--claude-code-version", default=EXACT_DEFAULTS["claude_code_version"])
    parser.add_argument("--codex-cli-version", default=EXACT_DEFAULTS["codex_cli_version"])
    parser.add_argument("--databricks-cli-version", default=EXACT_DEFAULTS["databricks_cli_version"])
    parser.add_argument("--omnigent-version", default=EXACT_DEFAULTS["omnigent_version"])
    parser.add_argument("--node-version", default=EXACT_DEFAULTS["node_version"])
    parser.add_argument("--pi-cli-version", default=EXACT_DEFAULTS["pi_cli_version"])
    parser.add_argument(
        "--gateway-host",
        default="",
        help="DATABRICKS_GATEWAY_HOST for the AI Gateway. Prefer the "
             "workspace-hosted form https://<workspace>.cloud.databricks.com/ai-gateway. "
             "Set it to put the event's traffic through the governed gateway: on "
             "AWS the workspace id cannot be derived from a dbc- hostname, so "
             "nothing is auto-constructed and every CLI silently uses the "
             "serving-endpoints fallback instead.",
    )
    parser.add_argument(
        "--toolchain-mirror",
        default="",
        help="OPTIONAL /Volumes/<catalog>/<schema>/<volume> holding the staged "
             "toolchain. Set it to reproduce the event-day path where boot pulls "
             "artifacts from workspace-local storage instead of the internet. "
             "Empty leaves the mirror off, which is the default everywhere.",
    )
    parser.add_argument(
        "--toolchain-mirror-group",
        default="",
        help="group granted READ_VOLUME on the mirror and joined by the app SP. "
             "Group-based rather than per-SP because a grant issued to an SP "
             "minted seconds earlier races the bootstrap thread that needs it.",
    )
    parser.add_argument(
        "--toolchain-mirror-strict",
        action="store_true",
        help="fail an artifact outright when the mirror cannot serve it. Only "
             "for rehearsing an air-gapped event, where reaching the internet is "
             "itself the failure.",
    )
    parser.add_argument(
        "--skip-mirror-stage",
        action="store_true",
        help="assume the mirror is already staged and only verify it. For "
             "repeat runs against a volume that has not changed.",
    )
    parser.add_argument("--dry-run", "--validate", dest="dry_run", action="store_true",
                        help="print a secret-free settings/grant plan without API mutations")
    args = parser.parse_args(argv)
    _validate_args(parser, args, environ)
    return args


def _parse_scopes(value):
    return [scope.strip() for scope in value.split(",") if scope.strip()]


def _applied_scopes_include(requested, applied):
    return set(requested).issubset(set(applied))


def _require_applied_scopes(app, requested):
    applied = list(getattr(app, "user_api_scopes", None) or [])
    missing = sorted(set(requested) - set(applied))
    if missing:
        raise RuntimeError(
            "app is missing requested OBO scopes: " + ",".join(missing)
        )
    return applied


def _validate_args(parser, args, environ):
    args.profile = (
        args.profile.strip()
        or str(environ.get("DATABRICKS_CONFIG_PROFILE", "")).strip()
    )
    if not args.profile:
        parser.error("--profile or DATABRICKS_CONFIG_PROFILE is required")
    if not args.non_event_mode and (args.no_obo or args.no_entitlements):
        parser.error(
            "--no-obo/--no-entitlements require explicit --non-event-mode; "
            "event validation requires both features"
        )
    if not args.non_event_mode and args.with_emergency_pat:
        parser.error(
            "--with-emergency-pat requires --non-event-mode; event acceptance "
            "requires direct app-identity OAuth"
        )
    scopes = _parse_scopes(args.scopes)
    missing_scopes = sorted(BASELINE_SCOPES - set(scopes))
    if not args.non_event_mode and missing_scopes:
        parser.error(
            "event OBO scopes are missing baseline values: "
            + ",".join(missing_scopes)
        )
    args.toolchain_mirror = args.toolchain_mirror.strip().rstrip("/")
    args.toolchain_mirror_group = args.toolchain_mirror_group.strip()
    if args.toolchain_mirror:
        parts = args.toolchain_mirror.split("/")
        if (
            not args.toolchain_mirror.startswith("/Volumes/")
            or len(parts) != 5
            or not all(parts[2:5])
        ):
            parser.error(
                "--toolchain-mirror must be an absolute "
                "/Volumes/<catalog>/<schema>/<volume> path"
            )
        if not args.toolchain_mirror_group:
            parser.error("--toolchain-mirror requires --toolchain-mirror-group")
    elif args.toolchain_mirror_group or args.toolchain_mirror_strict:
        # Strict without a mirror would fail every artifact, and a reader group
        # without one grants access to nothing. Both mean the operator believes a
        # mirror is configured when none is, which is the exact confusion the
        # feature's reporting exists to prevent.
        parser.error(
            "--toolchain-mirror-group/--toolchain-mirror-strict require "
            "--toolchain-mirror"
        )
    ref = args.skills_ref
    if not (re.fullmatch(r"[0-9A-Fa-f]{40}", ref) or VERSION_TAG_RE.fullmatch(ref)):
        parser.error(
            "--skills-ref must be a full 40-hex commit SHA or strict "
            "version tag (vMAJOR.MINOR.PATCH with optional prerelease/build)"
        )
    for field in (
        "claude_code_version",
        "codex_cli_version",
        "databricks_cli_version",
        "omnigent_version",
        "node_version",
        "pi_cli_version",
    ):
        value = getattr(args, field)
        if not SEMVER_RE.fullmatch(value):
            parser.error(
                f"--{field.replace('_', '-')} must be an exact semantic version"
            )
    for field in ("anthropic_model", "codex_model"):
        value = getattr(args, field)
        words = set(re.split(r"[._-]+", value.lower()))
        if (
            not MODEL_ENDPOINT_RE.fullmatch(value)
            or not any(char.isdigit() for char in value)
            or words & FLOATING_MODEL_WORDS
        ):
            parser.error(
                f"--{field.replace('_', '-')} must be an explicit non-floating "
                "model endpoint name"
            )
    _validate_model_profile(parser, args.model_profile)
    _validate_gateway_host(parser, args.gateway_host)
    if args.dry_run and not args.attendee.strip():
        parser.error("--attendee is required for --dry-run/--validate")


# Spelled out rather than imported from server.models, because this script runs
# as `python scripts/deploy_ct_sim.py` — sys.path[0] is scripts/, so the server
# package is not importable, and an operator would meet that as a traceback at
# the moment they tried to deploy. test_deploy_ct_sim asserts this list matches
# server.models.PROFILES, which is the check that keeps the copy honest.
MODEL_PROFILES = ("balanced", "economy", "frontier")


def _validate_model_profile(parser, value):
    """Reject a profile name this release does not implement.

    The terminal deliberately falls back to ``balanced`` on an unknown name so a
    typo cannot stop a workshop, and reports the substitution as an amber
    readiness check. That is the right behaviour at runtime and the wrong one
    here: a deploy is where someone is still watching, so an event asking for a
    posture we cannot supply should fail loudly rather than run at a cost the
    operator did not choose.
    """
    value = (value or "").strip().lower()
    if not value:
        return
    if value not in MODEL_PROFILES:
        parser.error(
            "--model-profile must be one of: "
            + ", ".join(MODEL_PROFILES)
            + " (or empty for the default)"
        )


# Mirrors the trusted-suffix allowlist Omnigent gates on before it will route a
# base URL as the AI Gateway, so a typo is caught here rather than becoming an
# event that quietly spends outside gateway policy and usage tracking.
GATEWAY_TRUSTED_SUFFIXES = (
    ".cloud.databricks.com",
    ".azuredatabricks.net",
    ".gcp.databricks.com",
)


def _validate_gateway_host(parser, value):
    """Reject a gateway host Omnigent would decline to route.

    Empty is allowed and means "not set": Workshop Terminal then falls back to
    ``<host>/serving-endpoints``, which serves every model an attendee needs but
    bypasses gateway policy, usage tracking and rate limits. That degradation is
    reported as the soft ``model_gateway`` readiness check.

    The workspace-hosted form (``https://<workspace>.cloud.databricks.com/ai-gateway``)
    is preferred over a dedicated ``ai-gateway`` subdomain: on the subdomain form
    Omnigent cannot infer the workspace hostname and falls back to resolving
    ``~/.databrickscfg``, and if that fails it drops the openai-completions
    provider the chat-completions-only models need.
    """
    value = (value or "").strip()
    if not value:
        return
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname:
        parser.error("--gateway-host must be an https URL")
    if not any(hostname.endswith(suffix) for suffix in GATEWAY_TRUSTED_SUFFIXES):
        parser.error(
            "--gateway-host must be a Databricks-owned host ending in one of: "
            + ", ".join(GATEWAY_TRUSTED_SUFFIXES)
        )
    labels = hostname.split(".")
    path = (parsed.path or "").rstrip("/")
    # Strict about the path because the terminal appends the provider suffix
    # itself (/anthropic for Claude, /codex/v1 for the OpenAI-completions models
    # GLM routes through). A host handed over with one already attached yields a
    # double-suffixed base URL that resolves to nothing, and the DNS-label form
    # would otherwise sail past the label check carrying it.
    if path not in ("", "/ai-gateway"):
        parser.error(
            "--gateway-host must be the gateway root, not a provider URL: the "
            "terminal appends /anthropic and /codex/v1 itself"
        )
    if "ai-gateway" not in labels and path != "/ai-gateway":
        parser.error(
            "--gateway-host must either carry an 'ai-gateway' DNS label or end "
            "in the /ai-gateway path, or Omnigent will not route it as the gateway"
        )


def main(argv=None, *, environ=None) -> int:
    args = _parse_args(argv, environ=environ)
    enable_obo = not args.no_obo
    enable_ent = not args.no_entitlements
    scopes = _parse_scopes(args.scopes) if enable_obo else []

    if args.dry_run:
        settings = _event_settings_from_args(args, args.attendee.strip(), "")
        _print_dry_run(_dry_run_plan(
            name=args.name,
            attendee=args.attendee.strip(),
            catalog=args.catalog,
            scopes=scopes,
            settings=settings,
            admin_group=args.admin_group,
            profile=args.profile,
            emergency_pat=args.with_emergency_pat,
            catalog_provenance=_catalog_provenance_from_args(args),
            toolchain_mirror=args.toolchain_mirror,
            toolchain_mirror_group=args.toolchain_mirror_group,
            toolchain_mirror_strict=args.toolchain_mirror_strict,
            skip_mirror_stage=args.skip_mirror_stage,
        ))
        return 0

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.apps import App, AppDeployment
    from databricks.sdk.service.workspace import ImportFormat

    w = _make_workspace_client(WorkspaceClient, args.profile)
    me_record = w.current_user.me()
    me = me_record.user_name
    attendee = args.attendee.strip() or me
    target = f"/Workspace/Users/{me}/apps/{args.name}"
    _log(
        "identity",
        f"profile={args.profile}; deploying as {me} -> app '{args.name}' "
        f"(attendee/labuser={attendee})",
    )

    # Emergency-only static credential. Never mint one here: the event default is
    # direct app-identity OAuth, and fallback material must be explicitly supplied.
    workshop_pat = ""
    if args.with_emergency_pat:
        workshop_pat = str((environ or os.environ).get("WORKSHOP_PAT", "")).strip()
        if not workshop_pat:
            raise RuntimeError(
                "--with-emergency-pat requires WORKSHOP_PAT in the environment"
            )
        _log("credential", "DEGRADED: injecting explicit emergency WORKSHOP_PAT")

    settings = _event_settings_from_args(args, attendee, workshop_pat)

    # --- 1+2. import source, editing the uploaded app.yaml in place ---
    _log("import", f"uploading source to {target}")
    n_files = 0
    uploaded_app_yaml = None
    for root, dirs, files in os.walk(REPO_ROOT):
        rel_root = os.path.relpath(root, REPO_ROOT)
        dirs[:] = [
            d for d in dirs
            if not any(fnmatch.fnmatch(os.path.normpath(os.path.join(rel_root, d)), p) for p in EXCLUDE)
        ]
        for name in files:
            rel = os.path.normpath(os.path.join(rel_root, name))
            if any(fnmatch.fnmatch(rel, p) for p in EXCLUDE):
                continue
            with open(os.path.join(root, name), "rb") as f:
                content = f.read()
            if rel == "app.yaml":
                content = _patch_app_yaml(content, settings)
                uploaded_app_yaml = content
            ws_path = f"{target}/{rel}".replace("\\", "/")
            w.workspace.mkdirs(os.path.dirname(ws_path))
            w.workspace.upload(ws_path, io.BytesIO(content), format=ImportFormat.AUTO, overwrite=True)
            n_files += 1
    _log("import", f"uploaded {n_files} files")

    # --- 3. create app, declaring OBO scopes on the app resource ---
    app = _create_app(w, App, args.name, scopes, fail_closed=True)
    if enable_obo:
        _require_applied_scopes(app, scopes)
    if uploaded_app_yaml is None:
        raise RuntimeError("source bundle did not contain app.yaml")
    uploaded_app_yaml = _patch_uploaded_app_yaml_with_sp_id(
        w, target, ImportFormat.AUTO, uploaded_app_yaml, app
    )
    sp_id = getattr(app, "service_principal_client_id", None)
    if not sp_id:
        raise RuntimeError("app did not expose service_principal_client_id")
    _log("create", f"app SP client id = {sp_id}")

    group_status = _configure_admin_group(w, args.admin_group, sp_id, me_record)

    # --- 4. provision the per-attendee catalog (§9) ---
    catalog_ok = True
    if enable_ent:
        catalog_ok = _provision_catalog(
            w,
            args.catalog,
            attendee,
            sp_id,
            provenance=_catalog_provenance_from_args(args),
        )

    # --- 4b. stage and authorise the toolchain mirror, if the event uses one ---
    # Ahead of the deploy on purpose: bootstrap begins seconds after the container
    # starts, and a mirror that is not yet readable simply misses, sending the app
    # to the internet for ~430 MiB with nothing in the logs to explain the delay.
    mirror_status = None
    if args.toolchain_mirror:
        mirror_status = _provision_mirror(
            w,
            args.toolchain_mirror,
            args.toolchain_mirror_group,
            sp_id,
            stage=not args.skip_mirror_stage,
        )

    # --- 5. deploy, then prove the runtime acquired direct OAuth ---
    _log("deploy", "deploying source bundle ...")
    deployment = w.apps.deploy_and_wait(
        app_name=args.name, app_deployment=AppDeployment(source_code_path=target)
    )
    app = w.apps.get(args.name)
    state = deployment.status.state if deployment.status else "unknown"
    _log("deploy", f"deployment state = {state}")
    deployer_token = _authorization_bearer(w.config.authenticate())
    attendee_token = (
        deployer_token
        if attendee == me
        else str((environ or os.environ).get(args.attendee_token_env, "")).strip()
    )
    acceptance = _post_deploy_acceptance(
        app.url,
        attendee=attendee,
        attendee_token=attendee_token,
        request=requests.request,
        now=time.time(),
        workshop_pat_present=bool(workshop_pat),
    )
    direct_oauth_ok = acceptance["ok"]
    _log(
        "credential",
        "healthy direct app-identity OAuth"
        if direct_oauth_ok
        else "UNHEALTHY: direct app-identity OAuth not validated after deploy",
    )
    if acceptance["blocker"]:
        _log("acceptance", f"BLOCKED: {acceptance['blocker']}")

    applied_scopes = (
        _require_applied_scopes(app, scopes)
        if enable_obo
        else list(getattr(app, "user_api_scopes", None) or [])
    )
    _print_summary(args, me, attendee, app, sp_id, enable_obo, enable_ent,
                   bool(workshop_pat), applied_scopes, direct_oauth_ok,
                   catalog_ok, group_status, mirror_status)
    # A degraded mirror is normally a slow boot, not a broken one, so it does not
    # fail the run. Under strict it is the opposite: an artifact the volume cannot
    # serve is an install error rather than a fallback, so an unready mirror is a
    # genuine event blocker and has to be reported as one.
    mirror_ok = (
        mirror_status is None
        or not args.toolchain_mirror_strict
        or all(
            mirror_status[key]
            for key in ("staged", "granted", "sp_in_group", "verified")
        )
    )
    assumptions_ok = (
        direct_oauth_ok
        and catalog_ok
        and mirror_ok
        and group_status["app_sp_member"]
        and group_status["deployer_member"]
        and (not enable_obo or _applied_scopes_include(scopes, applied_scopes))
    )
    if not assumptions_ok:
        _log("result", "INCOMPLETE: one or more event-readiness assumptions failed")
        return 1
    return 0


def _event_settings(
    *,
    attendee,
    catalog,
    scopes,
    admin_group,
    skills_ref,
    anthropic_model,
    codex_model,
    model_profile,
    claude_code_version,
    codex_cli_version,
    databricks_cli_version,
    omnigent_version,
    node_version,
    pi_cli_version,
    gateway_host,
    workshop_pat,
    enable_obo=True,
    enable_entitlements=True,
    insight_capture=False,
    toolchain_mirror="",
    toolchain_mirror_strict=False,
):
    return {
        "WORKSHOP_PAT": workshop_pat,
        "WORKSHOP_APP_SP_ID": "",
        "WORKSHOP_ATTENDEE_EMAIL": attendee,
        # Part of the Control Tower contract (§14), so this simulation has to be
        # able to set it. Off unless asked for, matching the real default: the
        # decision belongs to whoever wrote the event's registration terms.
        "WORKSHOP_INSIGHT_CAPTURE": str(insight_capture).lower(),
        "WORKSHOP_CATALOG": catalog if enable_entitlements else "",
        "ENABLE_OBO": str(enable_obo).lower(),
        "ENABLE_ENTITLEMENTS": str(enable_entitlements).lower(),
        "OBO_SCOPES": scopes if enable_obo else "",
        "ADMIN_GROUP": admin_group,
        "SKILLS_REF": skills_ref,
        "ANTHROPIC_MODEL": anthropic_model,
        "CODEX_MODEL": codex_model,
        # Empty is the reviewed default rather than an omission: it selects the
        # posture every event ran before profiles existed, and the two pins above
        # still override the roles they name.
        "WORKSHOP_MODEL_PROFILE": model_profile,
        "CLAUDE_CODE_VERSION": claude_code_version,
        "CODEX_CLI_VERSION": codex_cli_version,
        "DATABRICKS_CLI_VERSION": databricks_cli_version,
        "OMNIGENT_VERSION": omnigent_version,
        "NODE_VERSION": node_version,
        "PI_CLI_VERSION": pi_cli_version,
        # Empty is a supported state, not a hole: without it every CLI falls back
        # to <host>/serving-endpoints, which serves every model the model-set
        # variants use. What it costs is gateway policy, usage tracking and rate
        # limits, so a real event should still set it.
        "DATABRICKS_GATEWAY_HOST": gateway_host,
        # Empty is the reviewed default: the mirror is a speed optimisation, and
        # an event that does not stage one boots exactly as it always has, from
        # the internet, against the same repo-owned checksums.
        "WORKSHOP_TOOLCHAIN_MIRROR_PATH": toolchain_mirror,
        "WORKSHOP_TOOLCHAIN_MIRROR_STRICT": str(
            bool(toolchain_mirror) and toolchain_mirror_strict
        ).lower(),
    }


def _event_settings_from_args(args, attendee, workshop_pat):
    return _event_settings(
        attendee=attendee,
        catalog=args.catalog,
        scopes=args.scopes,
        admin_group=args.admin_group,
        skills_ref=args.skills_ref,
        anthropic_model=args.anthropic_model,
        codex_model=args.codex_model,
        model_profile=args.model_profile.strip().lower(),
        claude_code_version=args.claude_code_version,
        codex_cli_version=args.codex_cli_version,
        databricks_cli_version=args.databricks_cli_version,
        omnigent_version=args.omnigent_version,
        node_version=args.node_version,
        pi_cli_version=args.pi_cli_version,
        gateway_host=args.gateway_host.strip(),
        workshop_pat=workshop_pat,
        enable_obo=not args.no_obo,
        enable_entitlements=not args.no_entitlements,
        insight_capture=args.insight_capture,
        toolchain_mirror=args.toolchain_mirror,
        toolchain_mirror_strict=args.toolchain_mirror_strict,
    )


def _patch_app_yaml(content, settings):
    """Replace only named env scalar values while preserving all other bytes."""
    text = content.decode("utf-8")
    original = yaml.safe_load(text)
    env = original.get("env") if isinstance(original, dict) else None
    if not isinstance(env, list):
        raise ValueError("app.yaml must contain an env list")
    names = [item.get("name") for item in env if isinstance(item, dict)]
    duplicate_names = sorted({
        name for name in names if name and names.count(name) > 1
    })
    if duplicate_names:
        raise ValueError(
            "app.yaml contains duplicate env names: " + ", ".join(duplicate_names)
        )
    missing = sorted(set(settings) - set(names))
    if missing:
        raise ValueError(f"app.yaml missing required event settings: {', '.join(missing)}")

    lines = text.splitlines(keepends=True)
    env_index, env_indent = _find_env_section(lines)
    section_end = _section_end(lines, env_index + 1, env_indent)
    blocks = _env_blocks(lines, env_index + 1, section_end)
    replacements = {}
    for name, value in settings.items():
        start, end = blocks.get(name, (None, None))
        if start is None:
            raise ValueError(f"could not locate textual env block for {name}")
        value_lines = [
            index
            for index in range(start, end)
            if re.match(r"^[ \t]*value[ \t]*:", lines[index])
        ]
        if len(value_lines) != 1:
            raise ValueError(f"env block {name} must contain exactly one scalar value")
        index = value_lines[0]
        replacements[index] = _replace_yaml_scalar_line(lines[index], str(value))
    for index, replacement in replacements.items():
        lines[index] = replacement

    patched = "".join(lines).encode("utf-8")
    result = yaml.safe_load(patched)
    result_env = result.get("env") if isinstance(result, dict) else None
    result_values = {
        item.get("name"): item.get("value")
        for item in (result_env or [])
        if isinstance(item, dict)
    }
    incorrect = [
        name
        for name, value in settings.items()
        if result_values.get(name) != str(value)
    ]
    if incorrect:
        raise ValueError(
            "patched app.yaml failed env validation: " + ", ".join(sorted(incorrect))
        )
    return patched


def _patch_uploaded_app_yaml_with_sp_id(w, target, import_format, content, app):
    """Patch the post-create numeric app SP ID into the uploaded app.yaml."""
    service_principal_id = str(
        getattr(app, "service_principal_id", None) or ""
    ).strip()
    if not re.fullmatch(r"[0-9]+", service_principal_id):
        raise RuntimeError(
            "app create/get response did not expose numeric service_principal_id"
        )
    patched = _patch_app_yaml(
        content, {"WORKSHOP_APP_SP_ID": service_principal_id}
    )
    w.workspace.upload(
        f"{target}/app.yaml",
        io.BytesIO(patched),
        format=import_format,
        overwrite=True,
    )
    _log("create", f"app SP numeric SCIM id = {service_principal_id}")
    return patched


def _find_env_section(lines):
    matches = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        match = re.match(r"^(?P<indent>[ ]*)env[ \t]*:[ \t]*(?:#.*)?$", body)
        if match:
            matches.append((index, len(match.group("indent"))))
    if len(matches) != 1:
        raise ValueError("app.yaml must contain exactly one textual env section")
    return matches[0]


def _line_indent(line):
    stripped = line.lstrip(" ")
    return len(line) - len(stripped)


def _section_end(lines, start, parent_indent):
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _line_indent(lines[index]) <= parent_indent:
            return index
    return len(lines)


def _env_blocks(lines, start, end):
    item_pattern = re.compile(r"^(?P<indent>[ ]*)-[ \t]+name[ \t]*:[ \t]*(?P<name>.*)$")
    items = []
    for index in range(start, end):
        body = lines[index].rstrip("\r\n")
        match = item_pattern.match(body)
        if not match:
            continue
        parsed = yaml.safe_load("name: " + match.group("name"))
        name = parsed.get("name") if isinstance(parsed, dict) else None
        if not isinstance(name, str):
            raise ValueError("env item name must be a string")
        items.append((index, len(match.group("indent")), name))
    blocks = {}
    for position, (item_start, indent, name) in enumerate(items):
        item_end = end
        for next_start, next_indent, _ in items[position + 1:]:
            if next_indent == indent:
                item_end = next_start
                break
        blocks[name] = (item_start, item_end)
    return blocks


def _replace_yaml_scalar_line(line, value):
    newline = ""
    if line.endswith("\r\n"):
        line, newline = line[:-2], "\r\n"
    elif line.endswith("\n"):
        line, newline = line[:-1], "\n"
    match = re.match(r"^(?P<prefix>[ \t]*value[ \t]*:[ \t]*)(?P<body>.*)$", line)
    if not match:
        raise ValueError("env value must be a scalar line")
    body = match.group("body")
    comment_index = _yaml_comment_index(body)
    before_comment = body if comment_index is None else body[:comment_index]
    scalar_end = len(before_comment.rstrip(" \t"))
    suffix = before_comment[scalar_end:]
    if comment_index is not None:
        suffix += body[comment_index:]
    return (
        match.group("prefix")
        + json.dumps(value, ensure_ascii=False)
        + suffix
        + newline
    )


def _yaml_comment_index(value):
    quote = None
    escaped = False
    for index, char in enumerate(value):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return index
    return None


def _quoted(value):
    text = str(value)
    if not text or "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError("catalog and principal identifiers must be non-empty single-line values")
    return f"`{text.replace('`', '``')}`"


def _catalog_sql_plan(catalog, attendee, sp_id, *, create=True):
    cat = _quoted(catalog)
    user = _quoted(attendee)
    sp = _quoted(sp_id)
    statements = [
        f"GRANT ALL PRIVILEGES ON CATALOG {cat} TO {user}",
        f"GRANT MANAGE ON CATALOG {cat} TO {user}",
        f"GRANT MANAGE, USE CATALOG, CREATE SCHEMA ON CATALOG {cat} TO {sp}",
        f"ALTER CATALOG {cat} OWNER TO {user}",
    ]
    if create:
        statements.insert(
            0,
            f"CREATE CATALOG IF NOT EXISTS {cat} "
            "COMMENT 'Workshop CT-sim per-attendee catalog'",
        )
    return statements


def _dry_run_plan(
    *,
    name,
    attendee,
    catalog,
    scopes,
    settings,
    admin_group,
    profile,
    emergency_pat,
    catalog_provenance=None,
    toolchain_mirror="",
    toolchain_mirror_group="",
    toolchain_mirror_strict=False,
    skip_mirror_stage=False,
):
    safe_settings = dict(settings)
    safe_settings["WORKSHOP_APP_SP_ID"] = "<resolved-after-app-create>"
    if safe_settings.get("WORKSHOP_PAT"):
        safe_settings["WORKSHOP_PAT"] = "<redacted-emergency-pat>"
    return {
        "mode": "validate",
        "mutates_workspace": False,
        "app_name": name,
        "profile": profile,
        "attendee": attendee,
        "credential_mode": (
            "degraded_emergency_pat" if emergency_pat else "direct_app_identity_oauth_no_pat"
        ),
        "patched_settings": safe_settings,
        "user_api_scopes": list(scopes),
        "admin_group_plan": {
            "group": admin_group,
            "app_service_principal_membership": "required",
            "deployer_membership": "required_and_verified_at_apply",
        },
        "catalog_sql": _catalog_sql_plan(catalog, attendee, "<app-service-principal-client-id>"),
        "catalog_existing_provenance": catalog_provenance,
        "workspace_grants": [],
        "toolchain_mirror_plan": _mirror_plan(
            toolchain_mirror,
            toolchain_mirror_group,
            toolchain_mirror_strict,
            skip_mirror_stage,
        ),
    }


def _mirror_plan(volume_path, group_name, strict, skip_stage):
    """The mirror side of the plan, spelled out before anything is mutated.

    Present even when disabled: "enabled: false" is the difference between an
    event that chose the internet and one that meant to stage a volume and
    silently did not, and only the first should ever pass a pre-flight read.
    """
    if not volume_path:
        return {"enabled": False, "boot_source": "internet"}
    catalog, schema, volume = volume_path.split("/")[2:5]
    full_name = f"{catalog}.{schema}.{volume}"
    return {
        "enabled": True,
        "volume": volume_path,
        "reader_group": group_name,
        "strict": strict,
        "boot_source": "volume_only" if strict else "volume_then_internet",
        "stage": "skipped_by_operator" if skip_stage else "stage_then_verify",
        "sql": [
            f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}",
            f"CREATE VOLUME IF NOT EXISTS {full_name}",
            f"GRANT USE CATALOG ON CATALOG {catalog} TO `{group_name}`",
            f"GRANT USE SCHEMA ON SCHEMA {catalog}.{schema} TO `{group_name}`",
            f"GRANT READ VOLUME ON VOLUME {full_name} TO `{group_name}`",
        ],
        "group_membership": {
            "group": group_name,
            "member": "<app-service-principal-client-id>",
            "why": (
                "granted to a group rather than the SP directly: an SP minted "
                "seconds before deploy races the bootstrap thread that needs it"
            ),
        },
        "ordering": "staged, granted and verified before the app is deployed",
    }


def _print_dry_run(plan):
    print(json.dumps(plan, indent=2, sort_keys=True))


def _make_workspace_client(workspace_client_cls, profile):
    return workspace_client_cls(profile=profile)


def _create_app(w, App, name, scopes, *, fail_closed=True):
    kwargs = {"name": name}
    kwargs["user_api_scopes"] = list(scopes)
    try:
        w.apps.create_and_wait(App(**kwargs))
        _log("create", f"app created (user_api_scopes={scopes})")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "already exists" in msg.lower():
            _log("create", "app exists — updating scopes + redeploying")
            try:
                w.apps.update(name, App(**kwargs))
            except Exception as ue:  # noqa: BLE001
                if fail_closed:
                    raise
                _log("create", f"WARN could not update user_api_scopes: {ue}")
        elif fail_closed:
            raise
        else:
            _log("create", f"WARN create with scopes failed: {msg.splitlines()[0]}")
            w.apps.create_and_wait(App(name=name))
    return w.apps.get(name)


def _scim_literal(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _configure_admin_group(w, group_name, sp_id, me_record):
    """Ensure the app SP is in ADMIN_GROUP and report the deployer's assumption."""
    groups = list(w.groups.list(
        filter=f'displayName eq "{_scim_literal(group_name)}"',
    ))
    if len(groups) != 1:
        raise RuntimeError(
            f"ADMIN_GROUP {group_name!r} must resolve to exactly one workspace group"
        )
    group = w.groups.get(groups[0].id)
    service_principals = list(w.service_principals.list(
        filter=f'applicationId eq "{_scim_literal(sp_id)}"',
    ))
    if len(service_principals) != 1:
        raise RuntimeError("app service principal could not be resolved for ADMIN_GROUP")
    member_id = str(service_principals[0].id)
    existing_ids = {
        str(getattr(member, "value", ""))
        for member in (getattr(group, "members", None) or [])
    }
    if member_id not in existing_ids:
        w.api_client.do(
            "PATCH",
            f"/api/2.0/preview/scim/v2/Groups/{group.id}",
            body={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{
                    "op": "add",
                    "path": "members",
                    "value": [{"value": member_id}],
                }],
            },
        )
    deployer_groups = {
        getattr(group_ref, "display", None)
        for group_ref in (getattr(me_record, "groups", None) or [])
    }
    deployer_member = group_name in deployer_groups
    _log("admin-group", f"app SP is a member of {group_name}")
    if deployer_member:
        _log("admin-group", f"deployer is a member of {group_name}")
    else:
        _log("admin-group", f"WARN deployer is not reported as a member of {group_name}")
    return {
        "name": group_name,
        "app_sp_member": True,
        "deployer_member": deployer_member,
    }


def _provision_mirror(w, volume_path, group_name, sp_id, *, stage=True):
    """Stand up the toolchain mirror the way Control Tower does, then prove it.

    Ordering is the whole point: staging and the reader grant both have to be
    settled *before* the app deploys, because the bootstrap thread starts within
    seconds of the container coming up and a mirror that is not ready yet is
    indistinguishable from one that does not exist -- the app quietly downloads
    from the internet and the operator sees a slow boot with no explanation.

    Reported rather than raised on failure. A broken mirror is a performance
    regression, not a correctness one, so it must not take an otherwise healthy
    event deploy down with it.
    """
    # Imported two ways because this file is reached two ways: run as
    # `python scripts/deploy_ct_sim.py`, sys.path[0] is scripts/ and the sibling
    # is a top-level module; imported by the tests, the repo root is on the path
    # and it is scripts.ct_mirror. Failing either way would surface as a
    # traceback mid-deploy, which is the worst possible moment to find out.
    try:
        from scripts import ct_mirror
    except ImportError:
        import ct_mirror

    report = {
        "path": volume_path,
        "group": group_name,
        "staged": False,
        "granted": False,
        "sp_in_group": False,
        "verified": False,
        "detail": "",
    }
    try:
        _, catalog, schema, volume = ct_mirror.parse_volume(volume_path)
        full_name = f"{catalog}.{schema}.{volume}"
        wh = _pick_warehouse(w)
        if not wh:
            report["detail"] = "no warehouse available to create the volume or grant"
            _log("mirror", f"WARN {report['detail']}")
            return report

        _sql(w, wh, f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")
        _sql(w, wh, f"CREATE VOLUME IF NOT EXISTS {full_name}")

        if stage:
            _log("mirror", f"staging manifest artifacts into {volume_path} ...")
            staged = ct_mirror.stage(
                w, volume_path, progress=lambda message: _log("mirror", message)
            )
            report["staged"] = staged["status"] == "staged"
            if not report["staged"]:
                names = ", ".join(f["artifact"] for f in staged["failures"])
                report["detail"] = f"staging failed for: {names}"
                _log("mirror", f"WARN {report['detail']}")
            else:
                _log(
                    "mirror",
                    f"staged {staged['uploaded']} new, {staged['skipped']} already present",
                )
        else:
            report["staged"] = True
            _log("mirror", "skipping stage at operator request; verifying only")

        # READ_VOLUME alone is not enough: Unity Catalog needs the traversal
        # privileges on both parents before the volume grant means anything.
        for statement in (
            f"GRANT USE CATALOG ON CATALOG {catalog} TO `{group_name}`",
            f"GRANT USE SCHEMA ON SCHEMA {catalog}.{schema} TO `{group_name}`",
            f"GRANT READ VOLUME ON VOLUME {full_name} TO `{group_name}`",
        ):
            _sql(w, wh, statement)
        report["granted"] = True
        _log("mirror", f"{group_name} holds READ VOLUME on {full_name}")

        report["sp_in_group"] = _add_sp_to_group(w, group_name, sp_id)
        if not report["sp_in_group"]:
            report["detail"] = f"app SP could not be added to {group_name}"
            _log("mirror", f"WARN {report['detail']}")

        verified = ct_mirror.verify(w, volume_path, reader_group=group_name)
        report["verified"] = verified["exit_code"] == 0
        if not report["verified"]:
            report["detail"] = (
                f"verify reported {verified['status']}; missing="
                f"{verified.get('missing')} corrupt={verified.get('corrupt')}"
            )
            _log("mirror", f"WARN {report['detail']}")
        else:
            _log("mirror", f"verified: {verified['artifact_count']} artifacts current")
    except Exception as error:  # noqa: BLE001 - never fail the deploy over a mirror
        report["detail"] = f"{type(error).__name__}: {error}"
        _log("mirror", f"WARN mirror provisioning incomplete: {report['detail']}")
    return report


def _account_directory(w):
    """An account-scoped client for the same account, or ``None``.

    The mirror's reader group is normally created by Control Tower through the
    account directory, so it does not appear in workspace SCIM at all -- a
    workspace typically lists only ``users``, ``admins`` and any local clones.
    Looking there alone silently finds nothing and reports the app SP as
    unaddable, which costs the mirror rather than failing loudly.
    """
    account_id = str(getattr(w.config, "account_id", "") or "").strip()
    if not account_id:
        return None
    host = str(getattr(w.config, "host", "") or "")
    if "azuredatabricks.net" in host:
        accounts_host = "https://accounts.azuredatabricks.net"
    elif "gcp.databricks.com" in host:
        accounts_host = "https://accounts.gcp.databricks.com"
    else:
        accounts_host = "https://accounts.cloud.databricks.com"
    try:
        from databricks.sdk import AccountClient

        return AccountClient(host=accounts_host, account_id=account_id)
    except Exception:  # noqa: BLE001 - no account access is a miss, not a failure
        return None


def _join_group(client, group_name, sp_id):
    """Add the SP to ``group_name`` in one directory; ``False`` if not found there."""
    from databricks.sdk.service import iam

    groups = list(
        client.groups.list(filter=f'displayName eq "{_scim_literal(group_name)}"')
    )
    if len(groups) != 1:
        return False
    group = client.groups.get(groups[0].id)
    principals = list(
        client.service_principals.list(
            filter=f'applicationId eq "{_scim_literal(sp_id)}"'
        )
    )
    if len(principals) != 1:
        return False
    member_id = str(principals[0].id)
    existing = {
        str(getattr(member, "value", ""))
        for member in (getattr(group, "members", None) or [])
    }
    if member_id in existing:
        return True
    client.groups.patch(
        id=group.id,
        operations=[
            iam.Patch(op=iam.PatchOp.ADD, value={"members": [{"value": member_id}]})
        ],
        schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
    )
    return True


def _add_sp_to_group(w, group_name, sp_id):
    """Put the app SP in the mirror's reader group; ``False`` if it could not be.

    Tries the workspace directory first, then the account one. Which directory
    holds the group depends on who created it -- Control Tower makes an account
    group, a hand-rolled setup often makes a workspace group -- and the caller
    has no way to know, so both are searched rather than guessed at.
    """
    if _join_group(w, group_name, sp_id):
        return True
    account = _account_directory(w)
    if account is None:
        return False
    try:
        return _join_group(account, group_name, sp_id)
    except Exception:  # noqa: BLE001 - never fail a deploy over the mirror
        return False


def _pick_warehouse(w):
    """First STARTING/RUNNING/STOPPED warehouse (prefer serverless), or None."""
    whs = list(w.warehouses.list())
    if not whs:
        return None
    whs.sort(key=lambda x: (not getattr(x, "enable_serverless_compute", False),))
    return whs[0].id


def _sql(w, wh, stmt):
    r = w.statement_execution.execute_statement(
        statement=stmt,
        warehouse_id=wh,
        wait_timeout="50s",
    )
    status = getattr(r, "status", None)
    state = getattr(status, "state", None)
    state_value = getattr(state, "value", state)
    state_name = "unknown" if state_value is None else str(state_value)
    if state_name.rsplit(".", 1)[-1].upper() != "SUCCEEDED":
        error = getattr(status, "error", None)
        detail = getattr(error, "message", None)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"SQL statement did not succeed (state={state_name}){suffix}"
        )
    return r


def _provision_catalog(w, catalog, attendee, sp_id, *, provenance=None):
    """CT §9: per-attendee catalog, labuser OWNER + ALL PRIVILEGES, app SP USE/CREATE.

    Tries the SDK create; on a Default-Storage metastore (no storage root) the
    REST create is rejected, so we fall back to a SQL ``CREATE CATALOG`` via a
    warehouse (which resolves default storage). Grants are issued via SQL too —
    the typed ``grants.update`` enum mis-serializes on some SDK builds.
    """
    wh = _pick_warehouse(w)
    if not wh:
        _log("catalog", "WARN no warehouse to create catalog and issue scoped grants")
        return False
    try:
        existing = _catalog_info(w, catalog)
        if existing is not None:
            if not provenance:
                raise RuntimeError(
                    "existing catalog reuse requires explicit CT provenance"
                )
            _require_catalog_provenance(existing, provenance)
            _log("catalog", f"reusing existing dedicated catalog {catalog}")
        for statement in _catalog_sql_plan(
            catalog, attendee, sp_id, create=existing is None
        ):
            _sql(w, wh, statement)
        _verify_catalog_access(w, catalog, attendee, sp_id, provenance)
        _log(
            "catalog",
            "attendee OWNER + ALL PRIVILEGES + MANAGE; app SP catalog-scoped "
            "MANAGE + USE CATALOG + CREATE SCHEMA",
        )
        return True
    except Exception as e:  # noqa: BLE001
        _log("catalog", f"WARN catalog provisioning incomplete: {e}")
        return False


def _catalog_info(w, catalog):
    try:
        value = w.catalogs.get(catalog)
        return value if getattr(value, "name", None) == catalog else None
    except Exception as error:  # noqa: BLE001 - SDK exception type varies by version
        code = str(getattr(error, "error_code", "")).upper()
        message = str(error).lower()
        if code in {"RESOURCE_DOES_NOT_EXIST", "NOT_FOUND"} or any(
            text in message for text in ("does not exist", "not found")
        ):
            return None
        raise


def _metadata_value(value):
    raw = getattr(value, "value", value)
    return None if raw is None else str(raw)


def _require_catalog_provenance(info, provenance):
    fields = {
        "owner": "owner",
        "creator": "created_by",
        "catalog_type": "catalog_type",
        "isolation_mode": "isolation_mode",
        "storage_root": "storage_root",
    }
    if set(provenance) != set(fields):
        raise RuntimeError("existing catalog provenance requires all fields")
    for expected_name, actual_name in fields.items():
        actual = _metadata_value(getattr(info, actual_name, None))
        expected_value = provenance[expected_name]
        if expected_name == "storage_root" and expected_value is None:
            if actual is not None:
                raise RuntimeError(
                    "existing catalog provenance mismatch for storage_root"
                )
            continue
        expected = str(expected_value or "")
        if actual is None:
            raise RuntimeError(
                f"existing catalog API metadata missing {expected_name}"
            )
        if not expected:
            raise RuntimeError(
                f"existing catalog provenance missing {expected_name}"
            )
        if actual != expected:
            raise RuntimeError(
                f"existing catalog provenance mismatch for {expected_name}"
            )


def _verify_catalog_access(w, catalog, attendee, sp_id, provenance):
    info = w.catalogs.get(catalog)
    if _metadata_value(getattr(info, "owner", None)) != attendee:
        raise RuntimeError("catalog owner read-back did not match attendee")
    if provenance:
        preserved = dict(provenance)
        preserved["owner"] = attendee
        _require_catalog_provenance(info, preserved)
    result = w.grants.get(securable_type="catalog", full_name=catalog)
    grants = {}
    for assignment in getattr(result, "privilege_assignments", None) or []:
        grants[str(getattr(assignment, "principal", ""))] = {
            _metadata_value(privilege).upper().replace(" ", "_")
            for privilege in (getattr(assignment, "privileges", None) or [])
        }
    attendee_required = {"ALL_PRIVILEGES", "MANAGE"}
    app_required = {"MANAGE", "USE_CATALOG", "CREATE_SCHEMA"}
    if not attendee_required.issubset(grants.get(attendee, set())):
        raise RuntimeError("attendee catalog grants failed read-back verification")
    if not app_required.issubset(grants.get(sp_id, set())):
        raise RuntimeError("app SP catalog grants failed read-back verification")


def _catalog_provenance_from_args(args):
    raw = {
        "owner": args.catalog_existing_owner.strip(),
        "creator": args.catalog_existing_creator.strip(),
        "catalog_type": args.catalog_existing_type.strip(),
        "isolation_mode": args.catalog_existing_isolation_mode.strip(),
        "storage_root": args.catalog_existing_storage_root.strip(),
    }
    if not any(raw.values()):
        return None
    missing = [name for name, value in raw.items() if not value]
    if missing:
        raise ValueError(
            "existing catalog reuse requires all provenance fields or none"
        )
    values = dict(raw)
    if raw["storage_root"].casefold() == "null":
        values["storage_root"] = None
    return values


def _verify_direct_oauth(w, app_url, *, attempts=1, interval=0, sleep=time.sleep):
    """Require the deployed app to report recently validated direct OAuth."""
    for attempt in range(max(1, int(attempts))):
        try:
            response = requests.get(
                f"{str(app_url).rstrip('/')}/api/admin/presence",
                headers=w.config.authenticate(),
                timeout=30,
            )
            payload = response.json() if response.status_code == 200 else {}
            credential = (
                payload.get("credential", {}) if isinstance(payload, dict) else {}
            )
            if (
                credential.get("state") == "rotating"
                and credential.get("source") == "app_identity_oauth"
                and credential.get("healthy") is True
            ):
                return True
        except (requests.RequestException, ValueError, AttributeError):
            pass
        if attempt + 1 < max(1, int(attempts)):
            sleep(max(0, interval))
    return False


def _authorization_bearer(headers):
    value = str((headers or {}).get("Authorization") or "")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def _post_deploy_acceptance(
    app_url,
    *,
    attendee,
    attendee_token,
    request=requests.request,
    now,
    workshop_pat_present,
):
    """Seed attendee OBO, reconcile, then require the complete deep gate."""
    if workshop_pat_present:
        return {
            "ok": False,
            "blocker": "WORKSHOP_PAT is forbidden by event acceptance",
        }
    if not attendee_token:
        return {
            "ok": False,
            "blocker": (
                "external Control Tower attendee OAuth exchange is required "
                "to seed /api/config and OBO"
            ),
        }
    base = str(app_url).rstrip("/")
    headers = {"Authorization": f"Bearer {attendee_token}"}
    try:
        config_response = request(
            "GET", f"{base}/api/config", headers=headers, timeout=30
        )
        config_payload = (
            config_response.json() if config_response.status_code == 200 else {}
        )
        if config_response.status_code in {401, 403}:
            return {
                "ok": False,
                "blocker": (
                    "external Control Tower attendee OAuth exchange could not "
                    "authenticate to /api/config"
                ),
            }
        credential = (
            config_payload.get("credential", {})
            if isinstance(config_payload, dict)
            else {}
        )
        last_success = credential.get("last_successful_at")
        credential_ok = (
            credential.get("state") == "rotating"
            and credential.get("source") == "app_identity_oauth"
            and credential.get("healthy") is True
            and isinstance(last_success, (int, float))
            and 0 <= now - float(last_success) <= 600
        )
        if not credential_ok:
            return {"ok": False, "blocker": "direct OAuth status is not fresh"}
        reconcile_response = request(
            "POST",
            f"{base}/api/entitlements/reconcile",
            headers=headers,
            json={"email": attendee},
            timeout=30,
        )
        if reconcile_response.status_code != 200:
            return {"ok": False, "blocker": "entitlement reconcile failed"}
        ready_response = request(
            "GET", f"{base}/readyz", headers=headers, timeout=30
        )
        ready_payload = ready_response.json() if ready_response.status_code == 200 else {}
        if not (
            ready_response.status_code == 200
            and isinstance(ready_payload, dict)
            and ready_payload.get("ready") is True
            and ready_payload.get("status") == "ready"
        ):
            return {"ok": False, "blocker": "/readyz did not become green"}
        return {"ok": True, "blocker": None}
    except (requests.RequestException, ValueError, AttributeError):
        return {"ok": False, "blocker": "post-deploy app acceptance request failed"}


def _print_summary(
    args,
    me,
    attendee,
    app,
    sp_id,
    enable_obo,
    enable_ent,
    vended,
    applied_scopes,
    direct_oauth_ok,
    catalog_ok,
    group_status,
    mirror_status=None,
):
    print("\n" + "=" * 72)
    print("CT-SIM DEPLOYMENT SUMMARY")
    print("=" * 72)
    print(f"  app name        : {args.name}")
    print(f"  profile         : {args.profile}")
    print(f"  app url         : {app.url}")
    print(f"  deployed by     : {me}")
    print(f"  attendee/labuser: {attendee}")
    print(f"  app SP client id: {sp_id}")
    print(f"  app SP numeric id: {app.service_principal_id}")
    print(f"  emergency PAT   : {'yes (degraded fallback)' if vended else 'no'}")
    scope_str = ",".join(applied_scopes) if applied_scopes else "(none applied — defaults iam.* only)"
    print(f"  OBO             : {'ENABLED, user_api_scopes=' + scope_str if enable_obo else 'off'}")
    print(f"  entitlements    : {'ENABLED, catalog=' + args.catalog if enable_ent else 'off'}")
    print(f"  direct OAuth    : {'healthy' if direct_oauth_ok else 'UNHEALTHY'}")
    print(f"  catalog grants  : {'configured' if catalog_ok else 'INCOMPLETE'}")
    print(f"  ADMIN_GROUP     : {group_status['name']}")
    print(f"    app SP member : {group_status['app_sp_member']}")
    print(f"    deployer member: {group_status['deployer_member']}")
    if mirror_status is None:
        print("  toolchain mirror: off (artifacts download from the internet)")
    else:
        ready = all(
            mirror_status[key]
            for key in ("staged", "granted", "sp_in_group", "verified")
        )
        print(f"  toolchain mirror: {'READY' if ready else 'INCOMPLETE'}")
        print(f"    volume        : {mirror_status['path']}")
        print(f"    reader group  : {mirror_status['group']}")
        print(f"    staged/verified: {mirror_status['staged']}/{mirror_status['verified']}")
        print(f"    app SP in group: {mirror_status['sp_in_group']}")
        if mirror_status["detail"]:
            print(f"    detail        : {mirror_status['detail']}")
        if not ready:
            print("    -> boot will fall back to the internet for anything unstaged")
    print("\nAcceptance checks (open the app URL in a browser first to mint an OBO token):")
    print(f"  curl -s {app.url}/api/config -H 'Authorization: Bearer <admin>' | jq '.credential,.obo,.entitlements'")
    print("  In a terminal session:")
    print("    databricks current-user me            # -> the app service principal")
    print("    databricks-me current-user me         # -> the attendee (OBO)")
    print(f"    databricks-me catalogs list           # -> includes {args.catalog}")
    print("\nNOTE: workspace 'User authorization' must be enabled and consent granted")
    print("      for the OBO token to be forwarded; otherwise obo.present stays false.")
    print("=" * 72)


if __name__ == "__main__":
    sys.exit(main())
