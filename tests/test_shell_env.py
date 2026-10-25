"""Deny-by-default attendee shell environment (gap P0-1).

The attendee PTY must never inherit app secrets. The historical regression was
WORKSHOP_PAT (the vended bootstrap credential): it lived in the app env and was
copied through by the old copy-and-subtract strip list. These tests assert the
allowlist construction so no current or future secret can leak in.
"""

import os
import socket

import pytest

from server import users
from server.users import User


@pytest.fixture(autouse=True)
def _fresh_agent_ports(monkeypatch):
    monkeypatch.setattr(users, "_agent_ports", {})


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
        "TMUX_TMPDIR", "OMNIGENT_NO_UPDATE_CHECK",
        "DO_NOT_TRACK", "IMPECCABLE_NO_TELEMETRY",  # Impeccable telemetry off
        "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_REFRESH_INTERVAL_MS",
        "HARNESS_CODEX_GATEWAY_AUTH_REFRESH_INTERVAL_MS",
        "WORKSHOP_APP_URL",
        "DATABRICKS_APP_PORT", "WORKSHOP_TEST_PORT",
        # Conditional (only when configured): WORKSHOP_CATALOG, WORKSHOP_SCHEMA,
        # OBO_PROFILE_NAME — covered in test_obo.py.
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


def test_agents_get_their_own_port_never_the_terminals(monkeypatch):
    """The 25 Oct 2026 incident: an agent defaulted to 8000, found the
    terminal there and killed it to free the port."""
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8100")
    env = User("alice@example.com").shell_env()

    port = env["DATABRICKS_APP_PORT"]
    assert port != "8100"
    assert 8100 <= int(port) <= 8199
    assert env["WORKSHOP_TEST_PORT"] == port
    assert env["WORKSHOP_APP_URL"] == "http://localhost:8100"


def test_the_host_serving_port_is_never_copied_through(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    env = User("alice@example.com").shell_env()

    assert env["DATABRICKS_APP_PORT"] != "8000"
    assert env["WORKSHOP_APP_URL"] == "http://localhost:8000"


def test_a_plane_keeps_its_port_and_planes_never_share_one():
    first = User("alice@example.com").shell_env()["DATABRICKS_APP_PORT"]
    again = User("alice@example.com").shell_env()["DATABRICKS_APP_PORT"]
    omnigent = users.agent_port_env("omnigent")["DATABRICKS_APP_PORT"]

    assert again == first
    assert omnigent != first
    assert users.agent_port_env("omnigent")["DATABRICKS_APP_PORT"] == omnigent


def test_a_port_already_in_use_is_skipped():
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        port = next(p for p in users.AGENT_PORTS if users._bindable(p))
        busy.bind(("0.0.0.0", port))
        busy.listen()
        assert users.agent_port_env()["DATABRICKS_APP_PORT"] != str(port)
    finally:
        busy.close()


def test_the_omnigent_host_gets_a_different_port_from_the_terminal(monkeypatch, tmp_path):
    from server.omnigent_remote import build_host_launch

    user = User("alice@example.com")
    monkeypatch.setattr(user, "home", str(tmp_path / "alice"))
    user.bootstrap_home()
    terminal = user.shell_env()
    _, host, _ = build_host_launch(user, "/opt/bin/omnigent", "https://omnigent.example")

    assert host["DATABRICKS_APP_PORT"] == host["WORKSHOP_TEST_PORT"]
    assert host["DATABRICKS_APP_PORT"] != terminal["DATABRICKS_APP_PORT"]
