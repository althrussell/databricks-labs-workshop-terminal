"""Per-user isolation: HOME directories, shell env, and per-user state.

Each attendee gets their own HOME under DATA_ROOT/users/<slug>. Agent CLIs
are installed once under a shared prefix (one copy on disk); only configs
and working files are per-user. The shell env strips every app-level secret
so nothing privileged is readable from inside an attendee terminal.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading

from . import config

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
        self.lock = threading.Lock()
        self.cli_ready: set[str] = set()  # agent ids with configs written
        self.topics: dict[str, float] = {}  # topic -> last seen (terminal keyword spotting)
        self.sessions_launched: dict[str, int] = {}  # agent id -> lifetime count
        self.errors: int = 0  # P1-14: failed session launches / agent errors
        self.last_seen: float = 0.0
        self.first_seen: float = 0.0

    def bootstrap_home(self) -> None:
        for sub in (
            "projects", ".claude", ".codex", ".config",
            os.path.join(".config", "workshop"),  # rotating gateway-token file
            os.path.join(".cache", "tmux"),       # per-user tmux socket dir
        ):
            os.makedirs(os.path.join(self.home, sub), exist_ok=True)
        self._link_shared_binaries()

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
        if not os.path.isdir(shared_bin):
            return
        for name in os.listdir(shared_bin):
            source = os.path.join(shared_bin, name)
            target = os.path.join(local_bin, name)
            if os.path.lexists(target):
                continue
            try:
                os.symlink(os.path.realpath(source), target)
            except OSError:
                pass

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
            # Omnigent's gateway harnesses cache the auth_command result for
            # 15 min by default — the same as our token lifetime, so a token
            # already ≤10 min old at read (rotation interval) could be served
            # up to 10 min past expiry. 4 min keeps worst-case age at 14 min.
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
        return env


class UserManager:
    """Registry of attendees seen by this instance. Process-local by design."""

    def __init__(self):
        self._users: dict[str, User] = {}
        self._lock = threading.Lock()

    def get(self, email: str) -> User:
        with self._lock:
            user = self._users.get(email)
            is_new = user is None
            if is_new:
                user = User(email)
                self._users[email] = user
            distinct = len(self._users)
        if is_new and distinct > 1:
            # P1-11a: a second distinct attendee on one instance breaks the
            # one-workspace-per-attendee credential-isolation model.
            from . import topology

            warning = topology.second_attendee_warning(distinct)
            if warning:
                _topology_log.warning("topology: %s", warning)
        user.bootstrap_home()
        return user

    def peek(self, email: str) -> User | None:
        with self._lock:
            return self._users.get(email)

    def all(self) -> list[User]:
        with self._lock:
            return list(self._users.values())


user_manager = UserManager()
