import configparser
import os
import stat
import threading

from server import cli_config
from server.users import User


def _cfg_token(path):
    parser = configparser.ConfigParser()
    with open(path) as handle:
        parser.read_file(handle)
    return parser["DEFAULT"]["token"]


def test_atomic_core_credential_update_works_with_non_reentrant_user_lock(
    monkeypatch, tmp_path
):
    user = User("atomic@example.com")
    user.home = str(tmp_path)
    user.lock = threading.Lock()
    monkeypatch.setattr(cli_config.config, "databricks_host", lambda: "https://x.test")
    os.makedirs(tmp_path / ".config" / "workshop")
    os.makedirs(tmp_path / ".claude")
    os.makedirs(tmp_path / ".codex")
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    (tmp_path / ".codex" / "config.toml").write_text("")

    thread = threading.Thread(target=cli_config.update_tokens, args=(user, "token-a"))
    thread.start()
    thread.join(2)

    assert not thread.is_alive(), "credential update recursed into user.lock"
    assert _cfg_token(tmp_path / ".databrickscfg") == "token-a"
    assert (tmp_path / ".config" / "workshop" / "gateway-token").read_text() == "token-a\n"


def test_concurrent_core_updates_never_expose_partial_or_mismatched_files(
    monkeypatch, tmp_path
):
    user = User("concurrent@example.com")
    user.home = str(tmp_path)
    user.lock = threading.Lock()
    monkeypatch.setattr(cli_config.config, "databricks_host", lambda: "https://x.test")
    os.makedirs(tmp_path / ".config" / "workshop")
    os.makedirs(tmp_path / ".claude")
    os.makedirs(tmp_path / ".codex")
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    (tmp_path / ".codex" / "config.toml").write_text("")
    cli_config.update_tokens(user, "seed")
    errors = []
    stop = threading.Event()

    def read_loop():
        while not stop.is_set():
            try:
                cfg = _cfg_token(tmp_path / ".databrickscfg")
                gateway = (
                    tmp_path / ".config" / "workshop" / "gateway-token"
                ).read_text().strip()
                if not cfg or not gateway:
                    errors.append("empty")
            except (OSError, configparser.Error, KeyError) as error:
                errors.append(str(error))

    reader = threading.Thread(target=read_loop)
    reader.start()
    writers = [
        threading.Thread(target=cli_config.update_tokens, args=(user, f"token-{i}"))
        for i in range(20)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(2)
    stop.set()
    reader.join(2)

    cfg_path = tmp_path / ".databrickscfg"
    token_path = tmp_path / ".config" / "workshop" / "gateway-token"
    assert errors == []
    assert _cfg_token(cfg_path) == token_path.read_text().strip()
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert not list(tmp_path.rglob("*.tmp"))


def test_slow_first_configure_cannot_roll_back_newer_rotation(
    monkeypatch, tmp_path
):
    user = User("interleaving@example.com")
    user.home = str(tmp_path)
    monkeypatch.setattr(cli_config.config, "databricks_host", lambda: "https://x.test")
    monkeypatch.setattr(cli_config.config, "omnigent_enabled", lambda: False)
    monkeypatch.setattr(cli_config, "gateway_host", lambda: "")
    discovery_entered = threading.Event()
    release_discovery = threading.Event()

    def slow_discovery(token):
        if token == "old-token":
            discovery_entered.set()
            assert release_discovery.wait(2)
        return set()

    monkeypatch.setattr(cli_config, "_discover_serving_endpoints", slow_discovery)
    configure = threading.Thread(
        target=cli_config.configure_all, args=(user, "old-token")
    )
    configure.start()
    assert discovery_entered.wait(1)

    cli_config.update_tokens(user, "new-token")
    release_discovery.set()
    configure.join(2)

    assert not configure.is_alive()
    assert _cfg_token(tmp_path / ".databrickscfg") == "new-token"
    assert (
        tmp_path / ".config" / "workshop" / "gateway-token"
    ).read_text().strip() == "new-token"
