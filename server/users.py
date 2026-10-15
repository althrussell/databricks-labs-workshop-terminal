"""Per-user isolation: HOME directories, shell env, and per-user state.

Each attendee gets their own HOME under DATA_ROOT/users/<slug>. Agent CLIs
are installed once under a shared prefix (one copy on disk); only configs
and working files are per-user. The shell env strips every app-level secret
so nothing privileged is readable from inside an attendee terminal.
"""

from __future__ import annotations

import copy
import hashlib
import math
import logging
import os
import re
import tempfile
import threading

from . import config
from .capture_journal import CaptureHeldError

# Deny-by-default allowlist for attendee PTY environments (gap P0-1). Only
# non-secret, shell-functional host vars are copied through; PATH/TERM/HOME/USER
# and the per-user identity are set explicitly in shell_env(). Everything else
# (vended credentials incl. WORKSHOP_PAT, registry tokens, CLI state, app
# internals) is dropped by construction — it is never added here.
_SHELL_ENV_ALLOWLIST = frozenset({
    "LANG", "LANGUAGE", "TZ", "SHELL", "COLORTERM",
})
_SHELL_ENV_ALLOWLIST_PREFIXES = ("LC_",)

_topology_log = logging.getLogger("workshop.topology")

# Decides whether the Omnigent card joins an existing conversation or starts a
# new one. It reads the attendee's own mirrored bearer and asks the App, because
# the alternative — running the attach and treating a non-zero exit as "nothing
# to join" — cannot tell an empty control plane from a Ctrl-C out of a session
# the attendee had deliberately joined, and would silently fork a new session on
# every such exit. Every failure is "no joinable session", so an expired token or
# an unreachable App degrades to the create path instead of to an error.
#
# ``failed`` is excluded rather than every status but a known-good allowlist:
# restarting this app fails every session it was running runners for, so after
# any redeploy an attendee's whole history reads ``failed`` and a picker over it
# would offer nothing but corpses. Excluding by denylist keeps a status we have
# not seen yet joinable, which is the right default for "they have a live
# conversation". Deliberately resuming a failed one is what ``omnigent resume``
# is for.
_LIVE_SESSIONS_PROBE = '''#!/usr/bin/env python3
"""Exit 0 when this attendee already has a session worth joining."""

import json
import os
import sys
import urllib.request

url = os.environ.get("OMNIGENT_APP_URL", "").strip().rstrip("/")
if not url:
    sys.exit(1)
try:
    with open(os.path.join(os.path.expanduser("~"), ".omnigent", "auth_tokens.json")) as f:
        token = json.load(f)[url]["token"]
    request = urllib.request.Request(
        url + "/v1/sessions", headers={"Authorization": "Bearer " + token}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        sessions = json.load(response).get("data") or []
except Exception:
    sys.exit(1)
joinable = [
    s for s in sessions if not s.get("archived") and s.get("status") != "failed"
]
# A named build must never inherit the newest conversation for another build.
# Explicit remote resumption pins a matching workspace; unselected legacy
# launches retain their existing most-recent behavior.
if os.environ.get("WORKSHOP_PROJECT_NAME"):
    workspace = os.path.realpath(os.getcwd())
    joinable = [s for s in joinable if s.get("agent_name") == "polly"
                and s.get("workspace") == workspace
                and isinstance(s.get("id"), str)]
    if joinable:
        latest = max(joinable, key=lambda s: s.get("updated_at") or s.get("created_at") or "")
        print(latest["id"])
sys.exit(0 if joinable else 1)
'''


def email_slug(email: str) -> str:
    """Stable, filesystem-safe slug: local part + short hash of the full email."""
    local = re.sub(r"[^a-z0-9]+", "-", email.split("@")[0].lower()).strip("-") or "user"
    digest = hashlib.sha256(email.encode()).hexdigest()[:8]
    return f"{local}-{digest}"


class User:
    def __init__(self, email: str):
        self.email = email
        self.slug = email_slug(email)
        self.home = os.path.join(config.users_root(), self.slug)
        # Serializes all per-user config writes. Lock-held helpers avoid
        # recursion, so a plain Lock also catches accidental nested acquisition.
        self.lock = threading.Lock()
        # Content preparation can call config helpers that acquire self.lock.
        # Keep its lock separate; profile/brief refreshes share it as well.
        self.content_lock = threading.RLock()
        self._credential_revision = 0
        self._bootstrapped = False
        self.cli_ready: set[str] = set()  # agent ids with configs written
        self.topics: dict[str, float] = {}  # topic -> last seen (terminal keyword spotting)
        # topic -> lifetime hit count. `topics` answers "did they touch it",
        # which reads the same for a passing mention and an hour of work; the
        # counter is what lets a brief say which one they actually went deep on.
        self.topic_hits: dict[str, int] = {}
        self.sessions_launched: dict[str, int] = {}  # agent id -> lifetime count
        self.errors: int = 0  # P1-14: failed session launches / agent errors
        self.last_seen: float = 0.0
        self.first_seen: float = 0.0
        self._capture_lock = threading.RLock()
        self._capture_journal = None
        self._capture_recovered = False
        self._capture_baseline = _empty_user_snapshot()

    def bootstrap_home(self) -> None:
        if self._bootstrapped:
            return
        with self.lock:
            if self._bootstrapped:
                return
            for sub in (
                "projects", ".claude", ".codex", ".config", ".omnigent",
                os.path.join(".config", "workshop"),  # rotating gateway-token file
                os.path.join(".cache", "tmux"),       # per-user tmux socket dir
            ):
                os.makedirs(os.path.join(self.home, sub), exist_ok=True)
            self._link_shared_binaries()
            self._write_databricks_cli_wrapper()
            self._write_omnigent_databricks_config()
            self._write_omnigent_helper()
            self._bootstrapped = True

    def _link_shared_binaries(self) -> None:
        """Symlink shared CLI binaries into ~/.local/bin.

        Claude Code's health check (`claude doctor`) expects its binary at
        $HOME/.local/bin/claude; with per-user HOMEs the shared install must
        be linked into each user's home or every session shows a broken-install
        warning. Linking everything in the shared bin also keeps `databricks`,
        `codex`, `node` etc. resolvable without relying on PATH ordering.
        """
        shared_bin = os.path.join(config.shared_prefix(), "bin")
        local_bin = os.path.join(self.home, ".local", "bin")
        os.makedirs(local_bin, exist_ok=True)
        retired = self._remove_retired_binary_links(local_bin)
        if not os.path.isdir(shared_bin):
            return
        for name in os.listdir(shared_bin):
            # Bootstrap installation runs in the background, so a returning
            # attendee can reach this loop before shared-prefix cleanup. Never
            # recreate a retired launcher during that race.
            if name in retired:
                continue
            source = os.path.join(shared_bin, name)
            target = os.path.join(local_bin, name)
            if os.path.islink(target):
                previous = os.path.realpath(target)
                if not previous.startswith(os.path.realpath(config.shared_prefix()) + os.sep):
                    continue  # attendee-owned launcher
                if os.readlink(target) == source:
                    continue
                os.unlink(target)
            elif os.path.lexists(target):
                continue
            try:
                # Follow the stable shared launcher, including an atomic upgrade.
                os.symlink(source, target)
            except OSError:
                if not os.path.exists(target):
                    raise

    def refresh_launchers(self) -> None:
        """Reconcile launchers installed after this HOME was first opened."""
        with self.lock:
            self._link_shared_binaries()
            self._write_databricks_cli_wrapper()
            self._write_omnigent_helper()

    @staticmethod
    def _remove_retired_binary_links(local_bin: str) -> frozenset[str]:
        """Remove user-home launchers left by retired shared binaries."""
        retired = frozenset({"pi"})
        for name in retired:
            target = os.path.join(local_bin, name)
            if os.path.islink(target) or os.path.isfile(target):
                os.unlink(target)
        return retired

    def _write_databricks_cli_wrapper(self) -> None:
        """Give the agent's CLI the same identity in Omnigent as in a WT shell.

        The Omnigent host runs with ``DATABRICKS_CONFIG_FILE`` pointed at an
        attendee-only config, and everything below it inherits that variable —
        native terminals through ``caller_process`` and harness subprocesses
        through the runner's own environment. That inheritance is the whole
        point: it is what stops the runner's SDK fallback from silently
        authenticating as the app service principal when the mirrored token has
        expired. The side effect is that the agent's CLI inherits it too, and
        the attendee's scoped token can only read, so asking the agent to build
        something returns 403 rather than creating it.

        This narrows the rule to the process that needs narrowing. A CLI
        invocation from inside the Omnigent plane is redirected back to
        ``~/.databrickscfg``, which is the identity the same command already has
        in a Workshop Terminal shell; the runner's SDK calls never pass through
        argv and keep resolving the attendee. The redirect is conditional on the
        variable holding exactly the Omnigent-plane path, so an attendee who
        points it somewhere themselves is left alone.

        Parity is for the CLI only. Code that builds a Databricks SDK client
        directly still resolves the attendee-only config and still cannot
        create — the same trade, and the reason the agent instructions point at
        the CLI for anything that writes.
        """
        real = os.path.join(config.shared_prefix(), "bin", "databricks")
        if not os.path.exists(real):
            # No CLI in the shared install yet (dev boxes, partial bootstrap).
            # Leaving _link_shared_binaries' symlink in place beats installing a
            # wrapper that execs a path which does not exist.
            return
        path = os.path.join(self.home, ".local", "bin", "databricks")
        content = (
            "#!/bin/sh\n"
            "# Generated by the workshop terminal — do not edit.\n"
            "set -u\n"
            'if [ "${DATABRICKS_CONFIG_FILE:-}" = '
            f'"{self._omnigent_databrickscfg_path()}" ]; then\n'
            '  DATABRICKS_CONFIG_FILE="$HOME/.databrickscfg"\n'
            "  export DATABRICKS_CONFIG_FILE\n"
            "  unset DATABRICKS_CONFIG_PROFILE\n"
            "fi\n"
            f'exec "{real}" "$@"\n'
        )
        self._write_generated(path, content)

    def _write_omnigent_helper(self) -> None:
        """Install the stable TUI entrypoint used by the Omnigent catalog card.

        Both branches name the agent, because only a named agent uploads the
        local agent bundle. Measured on a live deployment: an argument-free
        ``omnigent run --server <url>`` is a thin client, and while it can join
        a conversation the server already holds, the first prompt that needs a
        new session dies inside the TUI with "Sessions API fresh session
        creation requires a local agent bundle". That is reachable by any
        attendee whose second launch follows a conversation whose runner is
        gone — an upstream error box, in the product, with no way forward.
        ``omnigent polly --server <url> -c`` is the shape that keeps both
        properties: the bundle is uploaded, so a session can always be created,
        and ``--continue`` resumes the most recent conversation for the agent,
        which is what makes a conversation started in the App's UI the same
        conversation the terminal shows.

        Continuation is still conditional, because ``-c`` needs something to
        continue: against a fresh control plane — every attendee's first launch
        — there is nothing, so an argument-free invocation probes first and
        creates plainly when the attendee has none. ``polly`` is the bundled
        orchestrator a bare ``omnigent`` already launches for a Claude
        credential, so that create path is the behavior the card had before the
        App existed.

        The probe fails closed to creating: a missing token, an unreachable
        server, or no probe at all lands on the previous always-create behavior
        rather than on an error. Its stderr is discarded so that a probe which
        cannot execute at all degrades silently instead of printing shell
        diagnostics into the attendee's terminal.

        Any argument suppresses the probe. An optional first positional selects
        a different agent (for example ``codex``) so a catalog card can launch
        something other than stock ``polly``; it is only read as an agent when
        it does not look like a flag, so ``workshop-omnigent --help`` still
        reaches the CLI. Flags reach the create path because a human typing them
        is driving the CLI directly, not asking to resume a conversation.

        The local branch is handled first and stays byte-for-byte
        ``exec omnigent "$@"``. The Control Tower contract pins that behaviour
        for an empty URL, and consuming a positional before that branch would
        change it.
        """
        probe = os.path.join(
            self.home, ".local", "bin", "workshop-omnigent-live-sessions"
        )
        self._write_generated(probe, _LIVE_SESSIONS_PROBE)
        path = os.path.join(self.home, ".local", "bin", "workshop-omnigent")
        content = (
            "#!/bin/sh\n"
            "set -eu\n"
            'if [ -z "${OMNIGENT_APP_URL:-}" ]; then\n'
            '  exec omnigent "$@"\n'
            "fi\n"
            "unset DATABRICKS_TOKEN DATABRICKS_CLIENT_ID "
            "DATABRICKS_CLIENT_SECRET DATABRICKS_HOST\n"
            'export DATABRICKS_CONFIG_FILE="$HOME/.config/workshop/'
            'omnigent-databrickscfg"\n'
            f"export DATABRICKS_CONFIG_PROFILE={config.obo_profile_name()}\n"
            'if [ "$#" -eq 0 ] && SESSION_ID=$( '
            '"$HOME/.local/bin/workshop-omnigent-live-sessions" '
            "2>/dev/null); then\n"
            '  if [ -n "${WORKSHOP_PROJECT_NAME:-}" ] && [ -n "$SESSION_ID" ]; then\n'
            '    exec omnigent polly --server "$OMNIGENT_APP_URL" --resume "$SESSION_ID"\n'
            '  fi\n'
            '  exec omnigent polly --server "$OMNIGENT_APP_URL" -c\n'
            "fi\n"
            "AGENT=polly\n"
            'case "${1:-}" in\n'
            "  ''|-*) ;;\n"
            '  *) AGENT="$1"; shift ;;\n'
            "esac\n"
            'exec omnigent "$AGENT" --server "$OMNIGENT_APP_URL" "$@"\n'
        )
        self._write_generated(path, content)

    @staticmethod
    def _write_generated(path: str, content: str) -> None:
        # Compare bytes, and only as many as the generated file has. What sits
        # here on the first bootstrap is _link_shared_binaries' symlink to the
        # multi-megabyte Databricks CLI: decoding that as text raises
        # UnicodeDecodeError, which is a ValueError and so escaped the guard
        # below, taking bootstrap_home — and with it every session launch on the
        # instance — down with it.
        expected = content.encode()
        try:
            with open(path, "rb") as existing:
                if existing.read(len(expected) + 1) == expected:
                    os.chmod(path, 0o755)
                    return
        except OSError:
            pass
        _atomic_write(path, content, 0o755)

    def _write_omnigent_databricks_config(self) -> None:
        """Create the Omnigent-plane config so the path always resolves.

        Left empty here and populated by OBO capture with the attendee's
        profile alone. The property that matters is what it never contains: the
        app service principal. Whatever this file can resolve is an identity the
        runner's SDK fallback can silently assume, so the worst case has to be
        "acts as the attendee". CLI callers are redirected away from it by the
        wrapper above, which is how the agent still creates things.
        """
        path = self._omnigent_databrickscfg_path()
        if os.path.exists(path):
            os.chmod(path, 0o600)
            return
        _atomic_write(path, "", 0o600)

    def _omnigent_databrickscfg_path(self) -> str:
        return os.path.join(self.home, ".config", "workshop", "omnigent-databrickscfg")

    def shell_env(self) -> dict:
        """Env for this user's PTYs, built deny-by-default (gap P0-1).

        We start from an EMPTY environment and copy only an explicit allowlist
        of non-secret host vars, then set the per-user identity. The previous
        copy-and-subtract approach leaked WORKSHOP_PAT because newly-added
        secrets were not also added to the strip list; an allowlist cannot leak
        a variable nobody allowed.

        The agent CLIs do not need app secrets in the shell: cli_config /
        user_content read gateway, model, and MCP settings from the *app's*
        environment and bake them into per-user config files
        (~/.databrickscfg, ~/.claude.json) that the agent reads. DATABRICKS_HOST
        is deliberately absent so unified auth resolves the rotated credentials
        from ~/.databrickscfg rather than a tokenless "env" strategy.
        """
        src = os.environ
        env: dict[str, str] = {
            key: value
            for key, value in src.items()
            if key in _SHELL_ENV_ALLOWLIST or key.startswith(_SHELL_ENV_ALLOWLIST_PREFIXES)
        }

        # User-local bin first (symlinks into the shared install — claude
        # expects to resolve from $HOME/.local/bin), shared bin as fallback
        # for binaries installed after this user's home was bootstrapped.
        local_bin = os.path.join(self.home, ".local", "bin")
        shared_bin = os.path.join(config.shared_prefix(), "bin")
        base_path = src.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        env.update({
            "HOME": self.home,
            "TERM": "xterm-256color",
            "USER": self.slug,
            "PATH": f"{local_bin}:{shared_bin}:{base_path}",
            "WORKSHOP_USER_EMAIL": self.email,
            "DATABRICKS_CONFIG_PROFILE": "DEFAULT",
            # Omnigent runs its terminals on private per-instance sockets
            # (tmux -S), but attendees can also run bare tmux in a bash
            # session — whose default socket dir /tmp/tmux-<uid> would merge
            # every attendee (shared uid) into ONE tmux server. Per-user
            # socket dir keeps that path isolated too.
            "TMUX_TMPDIR": os.path.join(self.home, ".cache", "tmux"),
            # The omnigent install is shared across attendees — never
            # self-update (mirrors DISABLE_AUTOUPDATER for claude).
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            # Re-read the app OAuth bearer file every four minutes so long-lived
            # agents promptly adopt SDK refreshes; a 401 also forces refresh.
            "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_REFRESH_INTERVAL_MS": "240000",
            "HARNESS_CODEX_GATEWAY_AUTH_REFRESH_INTERVAL_MS": "240000",
            # Loopback URL the per-user CLI helpers (databricks-me,
            # workshop-grant-me) call back into the app with. Derived from the
            # app's serving port; never exposes the raw DATABRICKS_APP_PORT.
            "WORKSHOP_APP_URL": f"http://localhost:{src.get('DATABRICKS_APP_PORT', '8000').strip() or '8000'}",
        })
        # Non-secret namespace hints so the agent/skills create objects inside
        # the attendee's catalog (inherited grants make them usable as `me`).
        # The OBO token itself never enters the shell — deny-by-default covers it.
        catalog = config.workshop_catalog()
        if catalog:
            env["WORKSHOP_CATALOG"] = catalog
        schema = config.workshop_schema()
        if schema:
            env["WORKSHOP_SCHEMA"] = schema
        if config.obo_enabled():
            env["OBO_PROFILE_NAME"] = config.obo_profile_name()
        remote_url = config.omnigent_app_url()
        if remote_url:
            env["OMNIGENT_APP_URL"] = remote_url
        return env


def _empty_user_snapshot() -> dict:
    return {"topics": {}, "topic_hits": {}, "sessions_launched": {}, "errors": 0,
            "first_seen": 0.0, "last_seen": 0.0}


def _finite_nonnegative(value) -> bool:
    return (type(value) is int and value >= 0
            or type(value) is float and math.isfinite(value) and value >= 0)


def validate_user_snapshot(raw) -> dict:
    if (not isinstance(raw, dict) or set(raw) != set(_empty_user_snapshot())
            or type(raw.get("errors")) is not int or raw["errors"] < 0
            or not _finite_nonnegative(raw.get("first_seen"))
            or not _finite_nonnegative(raw.get("last_seen"))):
        raise CaptureHeldError("retained attendee counters are invalid")
    for name in ("topics", "topic_hits", "sessions_launched"):
        if not isinstance(raw.get(name), dict):
            raise CaptureHeldError("retained attendee counter map is invalid")
        for key, value in raw[name].items():
            if (not isinstance(key, str) or not key
                    or (not _finite_nonnegative(value) if name == "topics"
                        else type(value) is not int or value < 0)):
                raise CaptureHeldError("retained attendee counter value is invalid")
    return raw


def user_snapshot(user: User) -> dict:
    return validate_user_snapshot({"topics": dict(user.topics), "topic_hits": dict(user.topic_hits),
                                  "sessions_launched": dict(user.sessions_launched),
                                  "errors": user.errors, "first_seen": user.first_seen,
                                  "last_seen": user.last_seen})


def merge_user_snapshot(existing: dict, current: dict, baseline: dict) -> dict:
    """Add each process's new counters once, retaining concurrent committed work."""
    for raw in (existing, current, baseline):
        validate_user_snapshot(raw)
    merged = copy.deepcopy(existing)
    for name in ("topic_hits", "sessions_launched"):
        for key, value in current[name].items():
            merged[name][key] = existing[name].get(key, 0) + max(0, value - baseline[name].get(key, 0))
    merged["errors"] += max(0, current["errors"] - baseline["errors"])
    for topic, timestamp in current["topics"].items():
        merged["topics"][topic] = max(merged["topics"].get(topic, 0), timestamp)
    starts = [value for value in (existing["first_seen"], current["first_seen"]) if value]
    merged["first_seen"] = min(starts) if starts else 0.0
    merged["last_seen"] = max(existing["last_seen"], current["last_seen"])
    return merged


def _apply_user_snapshot(user: User, raw: dict) -> None:
    for name, value in validate_user_snapshot(raw).items():
        setattr(user, name, copy.deepcopy(value))


def _capture_users(state, owner: str) -> dict:
    rows = state.setdefault("users", {})
    if not isinstance(rows, dict) or any(email != owner for email in rows):
        raise CaptureHeldError("retained attendee inventory does not match its owner")
    for raw in rows.values():
        validate_user_snapshot(raw)
    return rows


class UserManager:
    """Live attendees plus retained derived metadata for the configured owner."""

    def __init__(self):
        self._users: dict[str, User] = {}
        self._lock = threading.Lock()
        self._capture_journal = None
        self._capture_owner = ""
        self._require_ready = None

    def require_capture_ready(self, callback) -> None:
        """Block new attendee work while production recovery is unavailable."""
        self._require_ready = callback

    def reset_for_tests(self) -> None:
        with self._lock:
            self._users.clear()
            self._capture_journal = None
            self._capture_owner = ""
            self._require_ready = None

    def configure_capture(self, journal, owner: str) -> None:
        """Recover the known owner headlessly, without creating a HOME or OBO token."""
        owner = owner.strip().lower()
        try:
            if not config.valid_attendee_email(owner) or journal.binding["owner"] != owner:
                raise CaptureHeldError("capture attendee owner does not match its retained binding")
            def initialize(state):
                rows = _capture_users(state, owner)
                return copy.deepcopy(rows.setdefault(owner, _empty_user_snapshot()))
            saved = journal.transaction(initialize)
        except CaptureHeldError as error:
            journal.mark_held(error)
            raise
        with self._lock:
            user = self._users.get(owner) or User(owner)
            _apply_user_snapshot(user, saved)
            user._capture_journal = journal
            user._capture_baseline = copy.deepcopy(saved)
            user._capture_recovered = bool(saved["first_seen"] or journal.read().get("stats"))
            self._users[owner] = user
            self._capture_journal, self._capture_owner = journal, owner

    @staticmethod
    def checkpoint_state(state, user: User, current: dict, baseline: dict) -> dict:
        rows = _capture_users(state, user.email)
        merged = merge_user_snapshot(rows.get(user.email, _empty_user_snapshot()), current, baseline)
        rows[user.email] = merged
        return copy.deepcopy(merged)

    @staticmethod
    def accept_checkpoint(user: User, current: dict, saved: dict) -> None:
        # Output observation can continue while the remote journal commits.
        # Preserve those new in-memory increments for the next checkpoint.
        latest = user_snapshot(user)
        _apply_user_snapshot(user, merge_user_snapshot(saved, latest, current))
        user._capture_baseline = copy.deepcopy(saved)

    def checkpoint(self, user: User) -> None:
        if self._require_ready is not None:
            self._require_ready()
        journal = user._capture_journal
        if journal is None or user.email != self._capture_owner:
            return
        with user._capture_lock:
            current, baseline = user_snapshot(user), copy.deepcopy(user._capture_baseline)
            try:
                saved = journal.transaction(lambda state: self.checkpoint_state(state, user, current, baseline))
            except CaptureHeldError as error:
                journal.mark_held(error)
                raise
            self.accept_checkpoint(user, current, saved)

    def get(self, email: str) -> User:
        if self._require_ready is not None:
            self._require_ready()
        with self._lock:
            user = self._users.get(email)
            is_new = user is None
            if is_new:
                user = User(email)
                self._users[email] = user
            distinct = len(self._users)
        if is_new and distinct > 1:
            from . import topology
            warning = topology.second_attendee_warning(distinct)
            if warning:
                _topology_log.warning("topology: %s", warning)
        user.bootstrap_home()
        from . import obo
        obo.obo_manager.user_ready(user)
        return user

    def peek(self, email: str) -> User | None:
        with self._lock:
            return self._users.get(email)

    def all(self) -> list[User]:
        with self._lock:
            return list(self._users.values())


user_manager = UserManager()


def _atomic_write(path: str, content: str, mode: int) -> None:
    """Durably replace a generated user file without partial readers."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
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
