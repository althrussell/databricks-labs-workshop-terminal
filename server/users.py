"""Per-user isolation: HOME directories, shell env, and per-user state.

Each attendee gets their own HOME under DATA_ROOT/users/<slug>. Agent CLIs
are installed once under a shared prefix (one copy on disk); only configs
and working files are per-user. The shell env strips every app-level secret
so nothing privileged is readable from inside an attendee terminal.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading

from . import config


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
        self.last_seen: float = 0.0
        self.first_seen: float = 0.0

    def bootstrap_home(self) -> None:
        for sub in ("projects", ".claude", ".codex", ".config"):
            os.makedirs(os.path.join(self.home, sub), exist_ok=True)

    def shell_env(self) -> dict:
        """Env for this user's PTYs — app secrets stripped, identity isolated."""
        env = os.environ.copy()
        # Secrets and CLI-state vars that must never reach an attendee shell.
        for key in (
            "DATABRICKS_TOKEN",
            "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET",
            "DATABRICKS_APP_PORT",
            "CLAUDECODE", "CLAUDE_CODE_SESSION",
            "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
            "NPM_TOKEN", "UV_DEFAULT_INDEX",
        ):
            env.pop(key, None)
        for key in list(env):
            if key.startswith("npm_config_//") or (
                key.startswith("UV_INDEX_") and key.endswith(("_PASSWORD", "_USERNAME"))
            ):
                env.pop(key, None)

        shared_bin = os.path.join(config.shared_prefix(), "bin")
        env.update({
            "HOME": self.home,
            "TERM": "xterm-256color",
            "USER": self.slug,
            "PATH": f"{shared_bin}:{env.get('PATH', '')}",
            "WORKSHOP_USER_EMAIL": self.email,
        })
        return env


class UserManager:
    """Registry of attendees seen by this instance. Process-local by design."""

    def __init__(self):
        self._users: dict[str, User] = {}
        self._lock = threading.Lock()

    def get(self, email: str) -> User:
        with self._lock:
            user = self._users.get(email)
            if user is None:
                user = User(email)
                self._users[email] = user
        user.bootstrap_home()
        return user

    def peek(self, email: str) -> User | None:
        with self._lock:
            return self._users.get(email)

    def all(self) -> list[User]:
        with self._lock:
            return list(self._users.values())


user_manager = UserManager()
