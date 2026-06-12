"""Deny-by-default attendee shell environment (gap P0-1).

The attendee PTY must never inherit app secrets. The historical regression was
WORKSHOP_PAT (the vended bootstrap credential): it lived in the app env and was
copied through by the old copy-and-subtract strip list. These tests assert the
allowlist construction so no current or future secret can leak in.
"""

import os

from server.users import User


# Representative secrets / app-internal vars that MUST NOT reach an attendee
# shell. WORKSHOP_PAT is the one that actually leaked (P0-1).
_FORBIDDEN = [
    "WORKSHOP_PAT",
    "DATABRICKS_TOKEN",
    "DATABRICKS_HOST",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "NPM_TOKEN",
    "DATABRICKS_APP_PORT",
    "SOME_FUTURE_SECRET",  # the whole point: unknown vars are denied, not allowed
]


def test_no_secret_or_unknown_var_reaches_the_shell(monkeypatch):
    for key in _FORBIDDEN:
        monkeypatch.setenv(key, "leak-me")
    env = User("alice@example.com").shell_env()
    for key in _FORBIDDEN:
        assert key not in env, f"{key} leaked into the attendee shell env"


def test_only_allowlisted_and_explicit_keys_present(monkeypatch):
    monkeypatch.setenv("WORKSHOP_PAT", "dapi-secret")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    env = User("alice@example.com").shell_env()
    explicit = {
        "HOME", "TERM", "USER", "PATH",
        "WORKSHOP_USER_EMAIL", "DATABRICKS_CONFIG_PROFILE",
    }
    allowed_exact = {"LANG", "LANGUAGE", "TZ", "SHELL", "COLORTERM"}
    for key in env:
        assert (
            key in explicit or key in allowed_exact or key.startswith("LC_")
        ), f"unexpected key in attendee shell env: {key}"


def test_identity_and_paths_are_set(monkeypatch):
    user = User("alice@example.com")
    env = user.shell_env()
    assert env["HOME"] == user.home
    assert env["USER"] == user.slug
    assert env["WORKSHOP_USER_EMAIL"] == "alice@example.com"
    assert env["DATABRICKS_CONFIG_PROFILE"] == "DEFAULT"
    assert env["TERM"] == "xterm-256color"
    # User-local bin resolves first; a base PATH is always present.
    assert env["PATH"].startswith(os.path.join(user.home, ".local", "bin"))
    assert "/bin" in env["PATH"]


def test_locale_passes_through(monkeypatch):
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    monkeypatch.setenv("LC_CTYPE", "en_GB.UTF-8")
    env = User("alice@example.com").shell_env()
    assert env["LANG"] == "en_GB.UTF-8"
    assert env["LC_CTYPE"] == "en_GB.UTF-8"
