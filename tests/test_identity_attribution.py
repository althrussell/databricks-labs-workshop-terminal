"""Which principal each CLI surface resolves to, recorded per plane.

The gap this closes: an agent deployed an app, and afterwards nobody could say
which identity did it. A successful command logs nothing, and transcripts never
leave the box.
"""

import json
import stat
import time
from pathlib import Path


def _user_with_fake_cli(tmp_path, monkeypatch, script: str):
    from server import config
    from server.users import User

    shared = tmp_path / "shared"
    (shared / "bin").mkdir(parents=True)
    real = shared / "bin" / "databricks"
    real.write_text(script)
    real.chmod(real.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(config, "shared_prefix", lambda: str(shared))

    user = User("alice@example.com")
    monkeypatch.setattr(user, "home", str(tmp_path / "alice"))
    user.bootstrap_home()
    # databricks-me is the second surface; both resolve through ~/.local/bin.
    me = Path(user.home) / ".local" / "bin" / "databricks-me"
    me.write_text(
        f'#!/bin/sh\nexec "{Path(user.home) / ".local" / "bin" / "databricks"}" '
        '--profile me "$@"\n'
    )
    me.chmod(me.stat().st_mode | stat.S_IXUSR)
    return user


_REPORT_IDENTITY = """#!/bin/sh
# Report a different principal per profile, the way the real CLI would.
case " $* " in
  *" --profile me "*) echo '{"userName": "alice@example.com"}' ;;
  *) echo '{"applicationId": "app-sp-client-id"}' ;;
esac
"""


def test_resolve_records_both_surfaces_on_both_planes(tmp_path, monkeypatch):
    from server import config, identity

    monkeypatch.setenv("OMNIGENT_APP_URL", "https://omni.example.com")
    monkeypatch.setattr(config, "omnigent_app_url", lambda: "https://omni.example.com")
    user = _user_with_fake_cli(tmp_path, monkeypatch, _REPORT_IDENTITY)

    result = identity.resolve(user)

    assert result["attendee"] == "alice@example.com"
    assert set(result["planes"]) == {"workshop_terminal", "omnigent"}
    for plane, surfaces in result["planes"].items():
        # The whole point of the wrapper: the two planes agree. `databricks`
        # builds as the service principal, `databricks-me` reads as the attendee,
        # in a Workshop Terminal shell and inside Omnigent alike.
        assert surfaces["databricks"] == "app-sp-client-id", plane
        assert surfaces["databricks-me"] == "alice@example.com", plane


def test_a_broken_surface_is_recorded_rather_than_raised(tmp_path, monkeypatch):
    from server import config, identity

    monkeypatch.setattr(config, "omnigent_app_url", lambda: "")
    user = _user_with_fake_cli(
        tmp_path,
        monkeypatch,
        '#!/bin/sh\necho "Error: no credentials configured" >&2\nexit 1\n',
    )

    result = identity.resolve(user)

    surfaces = result["planes"]["workshop_terminal"]
    assert surfaces["databricks"].startswith("error:")
    assert "no credentials configured" in surfaces["databricks"]


def test_observe_is_backgrounded_cached_and_emits(tmp_path, monkeypatch):
    from server import config, identity
    from server.event_emitter import event_emitter

    monkeypatch.setattr(config, "omnigent_app_url", lambda: "")
    user = _user_with_fake_cli(tmp_path, monkeypatch, _REPORT_IDENTITY)
    identity._snapshots.clear()

    emitted = []
    monkeypatch.setattr(
        event_emitter,
        "emit",
        lambda event_type, attendee, payload=None, **kw: emitted.append(
            (event_type, attendee, payload)
        ),
    )

    resolved = []
    original = identity.resolve
    monkeypatch.setattr(
        identity,
        "resolve",
        lambda u: (resolved.append(u.email), original(u))[1],
    )

    identity.observe(user)
    for _ in range(200):
        if identity.snapshot(user.email):
            break
        time.sleep(0.02)

    assert identity.snapshot(user.email)["planes"]["workshop_terminal"]
    assert [event for event in emitted if event[0] == "identity.resolved"]

    # A second launch inside the TTL reuses the answer rather than paying for
    # two more CLI round trips on the attendee's session-start path.
    identity.observe(user)
    time.sleep(0.1)
    assert resolved == ["alice@example.com"]


def test_the_omnigent_plane_is_measured_with_the_hosts_own_environment(
    tmp_path, monkeypatch
):
    """Reconstructing the env here would hide exactly the drift this catches."""
    from server import config, identity

    monkeypatch.setattr(config, "omnigent_app_url", lambda: "https://omni.example.com")
    user = _user_with_fake_cli(
        tmp_path,
        monkeypatch,
        '#!/bin/sh\nprintf \'{"userName": "%s"}\\n\' "${DATABRICKS_CONFIG_FILE:-none}"\n',
    )

    environments = identity._plane_environments(user)
    result = identity.resolve(user)

    # The environment handed to the process is the host's own, attendee-only one.
    assert environments["omnigent"]["DATABRICKS_CONFIG_FILE"].endswith(
        "omnigent-databrickscfg"
    )
    # And the wrapper redirects the CLI off it, which is what lets the agent
    # create things inside Omnigent. Both facts have to hold at once.
    assert result["planes"]["omnigent"]["databricks"].endswith(".databrickscfg")
    assert not result["planes"]["omnigent"]["databricks"].endswith(
        "omnigent-databrickscfg"
    )
    assert result["planes"]["workshop_terminal"]["databricks"] == "none"


def test_snapshot_is_json_serialisable_for_the_event(tmp_path, monkeypatch):
    from server import config, identity

    monkeypatch.setattr(config, "omnigent_app_url", lambda: "")
    user = _user_with_fake_cli(tmp_path, monkeypatch, _REPORT_IDENTITY)

    json.dumps(identity.resolve(user))
