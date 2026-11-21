"""The workshop-grant-me helper: argument parsing, the request it sends, and the
plain CAN_MANAGE verdict it prints. curl is replaced by a stub on PATH that
records its arguments and answers with a canned body."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "assets" / "bin" / "workshop-grant-me"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run(tmp_path, args, *, answer, status=200):
    home = tmp_path / "home"
    (home / ".config" / "workshop").mkdir(parents=True)
    (home / ".config" / "workshop" / "callback-capability").write_text("cap-token\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "curl-args.txt"
    body = tmp_path / "answer.json"
    body.write_text(json.dumps(answer))
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > '{record}'\n"
        f"cat '{body}'\n"
        f"printf '\\n%s' '{status}'\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "WORKSHOP_USER_EMAIL": "alice@example.com",
        "WORKSHOP_APP_URL": "http://app.test",
    }
    done = subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30
    )
    sent = None
    if record.exists():
        argv = record.read_text().splitlines()
        sent = json.loads(argv[argv.index("-d") + 1])
    return done, sent


def test_a_named_space_that_can_be_managed_reports_yes_and_exits_zero(tmp_path):
    answer = {"resource": {
        "resource_type": "genie-spaces", "resource_id": "01f1e5",
        "state": "handed_off", "permission_level": "CAN_MANAGE", "can_manage": True,
        "message": "CAN_MANAGE granted to alice@example.com",
    }}
    done, sent = _run(tmp_path, ["genie", "01f1e5"], answer=answer)

    assert done.returncode == 0, done.stderr
    assert "CAN_MANAGE: yes" in done.stdout
    assert "genie 01f1e5 is CAN_MANAGE for you" in done.stdout
    assert sent == {"email": "alice@example.com", "resource_type": "genie", "resource_id": "01f1e5"}


def test_a_refused_space_reports_no_with_the_reason_and_exits_nonzero(tmp_path):
    answer = {"resource": {
        "resource_type": "genie-spaces", "resource_id": "mine", "state": "refused",
        "can_manage": False,
        "message": "it was not created by this app's service principal, so it was left alone",
    }}
    done, _ = _run(tmp_path, ["genie", "mine"], answer=answer)

    assert done.returncode == 1
    assert "CAN_MANAGE: no" in done.stdout
    assert "left alone" in done.stdout


def test_a_failed_handoff_without_a_verdict_is_not_read_as_success(tmp_path):
    done, _ = _run(tmp_path, ["genie", "01f1e5"], answer={"enabled": True})

    assert done.returncode == 1
    assert "CAN_MANAGE: no" in done.stdout


def test_an_id_without_a_type_is_refused_before_any_request(tmp_path):
    done, sent = _run(tmp_path, ["", "01f1e5"], answer={})

    assert done.returncode == 2
    assert "resource type before its id" in done.stderr
    assert sent is None


def test_an_id_with_path_characters_never_reaches_the_app(tmp_path):
    done, sent = _run(tmp_path, ["genie", "../apps/x"], answer={})

    assert done.returncode == 2
    assert "not a Databricks id" in done.stderr
    assert sent is None


def test_a_type_alone_keeps_the_list_refresh_request(tmp_path):
    done, sent = _run(tmp_path, ["genie"], answer={"errors": [], "non_uc": {}})

    assert done.returncode == 0, done.stderr
    assert sent == {"email": "alice@example.com", "resource_type": "genie"}
    assert "CAN_MANAGE" not in done.stdout


def test_no_arguments_refreshes_every_type_as_before(tmp_path):
    done, sent = _run(tmp_path, [], answer={"errors": []})

    assert done.returncode == 0, done.stderr
    assert sent == {"email": "alice@example.com"}


def test_a_dot_segment_id_is_refused_locally(tmp_path):
    done, sent = _run(tmp_path, ["genie", ".."], answer={})

    assert done.returncode == 2
    assert sent is None


def test_an_unknown_type_is_refused_locally(tmp_path):
    done, sent = _run(tmp_path, ["widgets", "x1"], answer={})

    assert done.returncode == 2
    assert sent is None


def test_a_refusal_from_the_app_is_reported_and_fails(tmp_path):
    done, _ = _run(tmp_path, ["genie", "01f1e5"], answer={"detail": "forbidden"}, status=403)

    assert done.returncode == 1
    assert "refused the entitlement handoff (HTTP 403)" in done.stderr


def test_a_backoff_deferral_says_why_and_exits_for_a_retry(tmp_path):
    answer = {"enabled": True, "deferred": True, "state": "degraded_backoff",
              "reason": "resource_exhausted", "next_attempt_at": 1_900_000_000.0, "errors": []}
    done, _ = _run(tmp_path, ["genie", "01f1e5"], answer=answer)

    assert done.returncode == 75
    assert "CAN_MANAGE: no. genie 01f1e5" in done.stdout
    assert "asked the app to slow down; retry after" in done.stdout
    assert "?" not in done.stdout


def test_a_rate_limited_resource_exits_for_a_retry(tmp_path):
    answer = {"deferred": True, "next_attempt_at": 1_900_000_000.0, "resource": {
        "resource_type": "genie-spaces", "resource_id": "01f1e5", "state": "rate_limited",
        "can_manage": False, "message": "the platform asked us to slow down"}}
    done, _ = _run(tmp_path, ["genie", "01f1e5"], answer=answer)

    assert done.returncode == 75
    assert "the platform asked us to slow down; retry after" in done.stdout


def test_switched_off_entitlements_are_named_and_do_not_retry(tmp_path):
    done, _ = _run(tmp_path, ["genie", "01f1e5"], answer={"enabled": False})

    assert done.returncode == 1
    assert "entitlements are switched off on this app" in done.stdout
    assert "?" not in done.stdout


def test_an_identity_failure_shows_the_app_error(tmp_path):
    answer = {"enabled": True, "errors": ["cannot verify app service-principal identity"]}
    done, _ = _run(tmp_path, ["genie", "01f1e5"], answer=answer)

    assert done.returncode == 1
    assert "cannot verify app service-principal identity" in done.stdout
    assert "?" not in done.stdout


def test_an_empty_answer_still_names_the_resource(tmp_path):
    done, _ = _run(tmp_path, ["dashboards", "dash1"], answer={})

    assert done.returncode == 1
    assert "CAN_MANAGE: no. dashboards dash1" in done.stdout
    assert "gave no answer" in done.stdout
    assert "?" not in done.stdout
