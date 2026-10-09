"""Long Apps HOME paths must not prevent the pinned Codex daemon connecting."""
import hashlib
import os
import tempfile

import pytest

from server.codex_home import shell_home as codex_shell_home


def long_home(tmp_path, name="attendee"):
    path = tmp_path / ("nested-runtime-" * 8) / name
    path.mkdir(parents=True)
    return path


def alias_for(path):
    digest = hashlib.sha256(os.fsencode(os.path.abspath(path))).hexdigest()[:32]
    return "/tmp/wt-cx-" + digest


def test_short_home_needs_no_alias():
    assert codex_shell_home("/app/users/alice") == "/app/users/alice"


def test_long_home_alias_preserves_config_and_native_log_identity(tmp_path):
    original = long_home(tmp_path)
    (original / ".codex/sessions").mkdir(parents=True)
    (original / ".codex/config.toml").write_text("original config")
    expected = alias_for(original)
    try:
        alias = codex_shell_home(str(original))
        assert alias == expected
        assert len(os.fsencode(alias + "/.codex/app-server-control/app-server-control.sock")) < 100
        assert os.path.realpath(alias) == str(original)
        assert open(alias + "/.codex/config.toml").read() == "original config"
        with open(alias + "/.codex/sessions/native.jsonl", "w") as handle:
            handle.write("native record")
        assert (original / ".codex/sessions/native.jsonl").read_text() == "native record"
        assert codex_shell_home(str(original)) == alias
    finally:
        os.unlink(expected)


def test_same_attendee_in_different_runtime_homes_never_shares_an_alias(tmp_path):
    first = long_home(tmp_path, "runtime-a/alice")
    second = long_home(tmp_path, "runtime-b/alice")
    try:
        assert codex_shell_home(str(first)) != codex_shell_home(str(second))
    finally:
        os.unlink(alias_for(first))
        os.unlink(alias_for(second))


@pytest.mark.parametrize("kind", ["directory", "wrong_symlink"])
def test_unexpected_alias_entry_is_rejected_without_replacing_it(tmp_path, kind):
    original = long_home(tmp_path)
    alias = alias_for(original)
    if kind == "directory":
        os.mkdir(alias)
    else:
        os.symlink(str(tmp_path), alias)
    try:
        with pytest.raises(RuntimeError, match="not owned"):
            codex_shell_home(str(original))
        if kind == "directory":
            assert os.path.isdir(alias) and not os.path.islink(alias)
        else:
            assert os.readlink(alias) == str(tmp_path)
    finally:
        if kind == "directory":
            os.rmdir(alias)
        else:
            os.unlink(alias)


def test_apps_alias_is_private_and_keeps_the_original_home(monkeypatch):
    from server import codex_home
    with tempfile.TemporaryDirectory(prefix="cx-", dir="/tmp") as root:
        monkeypatch.setattr(codex_home, "_APPS_SOURCE_ROOT", root)
        original = os.path.join(root, "nested-runtime-" * 8, "attendee")
        os.makedirs(original)
        alias = codex_shell_home(original)
        assert alias.startswith(root + "/.cx/")
        assert os.stat(root + "/.cx").st_mode & 0o077 == 0
        assert os.path.realpath(alias) == os.path.realpath(original)
        assert len(os.fsencode(alias + "/.codex/app-server-control/app-server-control.sock")) < 104


def test_apps_alias_refuses_an_exposed_root_without_chmod(monkeypatch):
    from server import codex_home
    with tempfile.TemporaryDirectory(prefix="cx-", dir="/tmp") as root:
        monkeypatch.setattr(codex_home, "_APPS_SOURCE_ROOT", root)
        original = os.path.join(root, "nested-runtime-" * 8, "attendee")
        os.makedirs(original)
        os.mkdir(root + "/.cx", mode=0o755)
        with pytest.raises(RuntimeError, match="not private"):
            codex_shell_home(original)
        assert os.stat(root + "/.cx").st_mode & 0o077 != 0
