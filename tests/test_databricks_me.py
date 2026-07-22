import configparser
import os
import stat
import subprocess
import time
from pathlib import Path

from .test_obo import make_jwt


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_profile(path: Path, token: str) -> None:
    cfg = configparser.ConfigParser()
    cfg["me"] = {"host": "https://test.cloud.databricks.com", "token": token}
    with path.open("w") as f:
        cfg.write(f)


def _run_helper(
    tmp_path: Path,
    *,
    updated_token: str | None,
    initial_token: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], int]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    home.mkdir()
    fake_bin.mkdir()
    (home / ".config/workshop").mkdir(parents=True)
    (home / ".config/workshop/callback-capability").write_text("capability")
    _write_profile(
        home / ".databrickscfg",
        initial_token or make_jwt(time.time() - 60),
    )

    calls = tmp_path / "databricks-calls"
    updated = tmp_path / "updated.cfg"
    if updated_token is not None:
        _write_profile(updated, updated_token)

    _write_executable(
        fake_bin / "databricks",
        f"""#!/usr/bin/env bash
count=0
[ ! -f "{calls}" ] || count="$(<"{calls}")"
count=$((count + 1))
printf '%s' "$count" >"{calls}"
if [ "$count" -eq 1 ]; then
  echo "401 invalid token" >&2
  exit 1
fi
echo "fresh token accepted"
""",
    )
    if updated_token is None:
        curl_body = "exit 0"
    else:
        curl_body = f'(sleep 0.15; cp "{updated}" "$HOME/.databrickscfg") &'
    _write_executable(
        fake_bin / "curl",
        f"""#!/usr/bin/env bash
{curl_body}
""",
    )

    helper = Path(__file__).parents[1] / "assets/bin/databricks-me"
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "WORKSHOP_USER_EMAIL": "alice@example.com",
        "OBO_REFRESH_TIMEOUT_SECONDS": "0.5",
        "OBO_REFRESH_POLL_SECONDS": "0.05",
    }
    result = subprocess.run(
        ["bash", str(helper), "catalogs", "list"],
        env=env,
        capture_output=True,
        text=True,
        timeout=6,
        check=False,
    )
    return result, int(calls.read_text())


def test_helper_does_not_retry_when_refresh_writes_no_new_token(tmp_path):
    result, calls = _run_helper(tmp_path, updated_token=None)

    assert result.returncode == 1
    assert calls == 1
    assert "401 invalid token" in result.stdout


def test_helper_waits_for_new_fresh_token_before_retry(tmp_path):
    result, calls = _run_helper(
        tmp_path,
        updated_token=make_jwt(time.time() + 3600),
    )

    assert result.returncode == 0
    assert calls == 2
    assert "fresh token accepted" in result.stdout


def test_helper_accepts_changed_opaque_token_after_malformed_prior_token(tmp_path):
    result, calls = _run_helper(
        tmp_path,
        initial_token="header._w.signature",
        updated_token="opaque.not-a-jwt",
    )

    assert result.returncode == 0
    assert calls == 2
    assert "Traceback" not in result.stdout + result.stderr


def test_helper_accepts_changed_malformed_three_segment_as_opaque(tmp_path):
    result, calls = _run_helper(
        tmp_path,
        updated_token="header.%%%%.signature",
    )

    assert result.returncode == 0
    assert calls == 2
    assert "Traceback" not in result.stdout + result.stderr


def test_helper_accepts_changed_three_segment_token_without_exp(tmp_path):
    result, calls = _run_helper(
        tmp_path,
        updated_token="header.e30.signature",
    )

    assert result.returncode == 0
    assert calls == 2
    assert "Traceback" not in result.stdout + result.stderr


def test_helper_rejects_changed_jwt_within_freshness_margin(tmp_path):
    result, calls = _run_helper(
        tmp_path,
        updated_token=make_jwt(time.time() + 30),
    )

    assert result.returncode == 1
    assert calls == 1
    assert "Traceback" not in result.stdout + result.stderr
