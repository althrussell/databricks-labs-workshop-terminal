"""The agent's CLI identity on the Omnigent plane.

The Omnigent host exports an attendee-only ``DATABRICKS_CONFIG_FILE`` so the
runner's SDK fallback can never assume the app service principal. Everything
below the host inherits it, which would leave the agent able to read and unable
to create. The generated wrapper redirects CLI invocations — and only CLI
invocations — back to the workshop config.
"""

import os
import stat
import subprocess
from pathlib import Path


def _bootstrap(tmp_path: Path, monkeypatch) -> tuple[object, Path, Path]:
    """A bootstrapped user whose shared install holds a fake databricks CLI."""
    from server import config
    from server.users import User

    shared = tmp_path / "shared"
    (shared / "bin").mkdir(parents=True)
    real = shared / "bin" / "databricks"
    real.write_text(
        "#!/bin/sh\n"
        'echo "cfg=${DATABRICKS_CONFIG_FILE:-<unset>}"\n'
        'echo "profile=${DATABRICKS_CONFIG_PROFILE:-<unset>}"\n'
        'echo "args=$*"\n'
    )
    real.chmod(real.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(config, "shared_prefix", lambda: str(shared))

    user = User("alice@example.com")
    monkeypatch.setattr(user, "home", str(tmp_path / "alice"))
    user.bootstrap_home()
    return user, Path(user.home) / ".local" / "bin" / "databricks", real


def _run(wrapper: Path, home: str, env: dict[str, str], *args: str):
    return subprocess.run(
        [str(wrapper), *args],
        capture_output=True,
        text=True,
        env={"HOME": home, "PATH": "/usr/bin:/bin", **env},
    )


def test_wrapper_replaces_the_shared_symlink(tmp_path, monkeypatch):
    _user, wrapper, real = _bootstrap(tmp_path, monkeypatch)

    # _link_shared_binaries symlinks the shared CLI in first; the wrapper has to
    # win, or the agent keeps inheriting the attendee-only config.
    assert not wrapper.is_symlink()
    assert os.access(wrapper, os.X_OK)
    assert os.path.realpath(real) in wrapper.read_text()


def test_omnigent_plane_cli_resolves_the_workshop_config(tmp_path, monkeypatch):
    user, wrapper, _real = _bootstrap(tmp_path, monkeypatch)
    omnigent_cfg = str(
        Path(user.home) / ".config" / "workshop" / "omnigent-databrickscfg"
    )

    result = _run(
        wrapper,
        user.home,
        {"DATABRICKS_CONFIG_FILE": omnigent_cfg, "DATABRICKS_CONFIG_PROFILE": "me"},
        "current-user",
        "me",
    )

    assert result.returncode == 0, result.stderr
    # Redirected to the workshop config with no profile pin, which is exactly
    # what the same command resolves to in a Workshop Terminal shell: [DEFAULT],
    # the app service principal, able to create.
    assert f"cfg={Path(user.home) / '.databrickscfg'}" in result.stdout
    assert "profile=<unset>" in result.stdout
    assert "args=current-user me" in result.stdout


def test_a_claude_native_pane_still_resolves_the_workshop_config(
    tmp_path, monkeypatch
):
    """Upstream strips ``DATABRICKS_CONFIG_PROFILE`` from native Claude panes.

    Measured in the deployed 0.10.0 wheel: ``runner/native/orchestration.py``
    launches the Claude terminal with ``env_unset = ["DATABRICKS_CONFIG_PROFILE",
    "CLAUDECODE"]`` (plus ``ANTHROPIC_API_KEY`` under an ``apiKeyHelper``, which
    is our configuration), and ``inner/terminal.py`` applies that strip after
    merging the spec env, so the variable is gone unconditionally. A wrapper that keyed
    the redirect on the profile instead of the config file would therefore work
    everywhere except the one surface most attendees actually use.
    """
    user, wrapper, _real = _bootstrap(tmp_path, monkeypatch)
    omnigent_cfg = str(
        Path(user.home) / ".config" / "workshop" / "omnigent-databrickscfg"
    )

    result = _run(
        wrapper,
        user.home,
        {"DATABRICKS_CONFIG_FILE": omnigent_cfg},
        "current-user",
        "me",
    )

    assert result.returncode == 0, result.stderr
    assert f"cfg={Path(user.home) / '.databrickscfg'}" in result.stdout
    assert "profile=<unset>" in result.stdout


def test_workshop_terminal_environment_is_untouched(tmp_path, monkeypatch):
    user, wrapper, _real = _bootstrap(tmp_path, monkeypatch)

    result = _run(wrapper, user.home, {}, "jobs", "list")

    # No DATABRICKS_CONFIG_FILE means a bare WT shell, where unified auth already
    # resolves ~/.databrickscfg. The wrapper must not invent one.
    assert result.returncode == 0, result.stderr
    assert "cfg=<unset>" in result.stdout


def test_an_attendees_own_config_choice_is_respected(tmp_path, monkeypatch):
    user, wrapper, _real = _bootstrap(tmp_path, monkeypatch)
    chosen = str(tmp_path / "my-own.cfg")

    result = _run(
        wrapper,
        user.home,
        {"DATABRICKS_CONFIG_FILE": chosen, "DATABRICKS_CONFIG_PROFILE": "mine"},
    )

    # The redirect is keyed to the Omnigent-plane path specifically. Anyone who
    # points the variable somewhere themselves keeps what they set.
    assert f"cfg={chosen}" in result.stdout
    assert "profile=mine" in result.stdout


def test_explicit_profile_flag_still_reaches_the_cli(tmp_path, monkeypatch):
    user, wrapper, _real = _bootstrap(tmp_path, monkeypatch)
    omnigent_cfg = str(
        Path(user.home) / ".config" / "workshop" / "omnigent-databrickscfg"
    )

    result = _run(
        wrapper,
        user.home,
        {"DATABRICKS_CONFIG_FILE": omnigent_cfg},
        "--profile",
        "me",
        "current-user",
        "me",
    )

    # databricks-me passes --profile explicitly, and a flag outranks the
    # environment — so attendee-identity reads keep working through the wrapper.
    assert "args=--profile me current-user me" in result.stdout
    assert f"cfg={Path(user.home) / '.databrickscfg'}" in result.stdout


def test_bootstrap_survives_a_real_binary_at_the_wrapper_path(tmp_path, monkeypatch):
    from server import config
    from server.users import User

    shared = tmp_path / "shared"
    (shared / "bin").mkdir(parents=True)
    real = shared / "bin" / "databricks"
    # The shipped CLI is a Go binary, not a shell script. On the first bootstrap
    # the wrapper path holds a symlink to it, and reading that as UTF-8 text
    # raised UnicodeDecodeError — a ValueError, so it escaped the OSError guard
    # in _write_generated and 500'd every session create, host readiness probe
    # and diagnostics call on a deployed instance.
    real.write_bytes(b"\x7fELF\x02\x01\x01\x00" + bytes(range(0x80, 0x100)) * 4)
    real.chmod(real.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(config, "shared_prefix", lambda: str(shared))
    user = User("carol@example.com")
    monkeypatch.setattr(user, "home", str(tmp_path / "carol"))

    user.bootstrap_home()

    wrapper = Path(user.home) / ".local" / "bin" / "databricks"
    assert not wrapper.is_symlink()
    assert os.path.realpath(real) in wrapper.read_text()


def test_no_wrapper_without_a_shared_cli(tmp_path, monkeypatch):
    from server import config
    from server.users import User

    shared = tmp_path / "shared"
    (shared / "bin").mkdir(parents=True)
    monkeypatch.setattr(config, "shared_prefix", lambda: str(shared))
    user = User("bob@example.com")
    monkeypatch.setattr(user, "home", str(tmp_path / "bob"))

    user.bootstrap_home()

    # A wrapper that execs a path which does not exist is worse than no wrapper.
    assert not (Path(user.home) / ".local" / "bin" / "databricks").exists()
