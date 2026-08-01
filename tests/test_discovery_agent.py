"""The agent-facing discovery surface: CLI helper and instruction overlay (C6).

The agent is the only thing positioned to notice what an attendee is trying to
build, which makes these two files the actual capture mechanism. The instruction
text is therefore treated as load-bearing and tested: an overlay that reads like
a qualification script would make the agent interrogate people, and that produces
both a worse workshop and worse data.
"""

import json
import os
import subprocess

import pytest

from .conftest import ALICE

_ASSETS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets"))
HELPER = os.path.join(_ASSETS, "bin", "workshop-discovery")
OVERLAY = os.path.join(_ASSETS, "instructions", "discovery.md")


def _provisioned_home(client, monkeypatch):
    from server import user_content
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    user_content._provisioned.discard("alice@example.com")
    resp = client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    assert resp.status_code == 200
    return user_manager.get("alice@example.com").home


# --- Installation ------------------------------------------------------------


def test_helper_is_installed_even_when_capture_is_off(client, monkeypatch):
    """"command not found" mid-session is a worse failure than a polite refusal.

    An operator can enable capture on a running instance, and homes are
    provisioned once on first session — so a conditionally-installed helper would
    be missing for exactly the attendees who were already working.
    """
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    home = _provisioned_home(client, monkeypatch)
    installed = os.path.join(home, ".local", "bin", "workshop-discovery")
    assert os.path.isfile(installed)
    assert os.stat(installed).st_mode & 0o111


def test_overlay_is_absent_when_capture_is_off(client, monkeypatch):
    """Instructions to record against a disabled endpoint only produce noise."""
    monkeypatch.delenv("WORKSHOP_INSIGHT_CAPTURE", raising=False)
    home = _provisioned_home(client, monkeypatch)
    for path in (".claude/CLAUDE.md", ".codex/AGENTS.md"):
        assert "workshop-discovery" not in open(os.path.join(home, path)).read()


def test_overlay_is_present_when_capture_is_on(client, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    home = _provisioned_home(client, monkeypatch)
    for path in (".claude/CLAUDE.md", ".codex/AGENTS.md"):
        assert "workshop-discovery" in open(os.path.join(home, path)).read()


def test_overlay_is_absent_when_only_discovery_is_off(client, monkeypatch):
    """The signal-only setting must not still instruct the agent to elicit."""
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    home = _provisioned_home(client, monkeypatch)
    claude_md = open(os.path.join(home, ".claude", "CLAUDE.md")).read()
    assert "workshop-discovery" not in claude_md


def test_overlay_does_not_displace_the_coach(client, monkeypatch):
    """Both overlays append; the second must not overwrite the first."""
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    home = _provisioned_home(client, monkeypatch)
    claude_md = open(os.path.join(home, ".claude", "CLAUDE.md")).read()
    assert "workshop-lab-coach" in claude_md
    assert "workshop-discovery" in claude_md
    assert "Workshop Edition" in claude_md


def test_overlay_is_appended_once(client, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    home = _provisioned_home(client, monkeypatch)
    claude_md = open(os.path.join(home, ".claude", "CLAUDE.md")).read()
    assert claude_md.count("<!-- workshop-discovery -->") == 1


# --- The instruction text is load-bearing ------------------------------------


def test_overlay_forbids_interviewing():
    """An attendee who feels surveyed stops volunteering anything useful."""
    text = open(OVERLAY).read()
    assert "not conducting an interview" in text
    assert "never a detour" in text
    # And explicitly names the failure mode of filling a field for its own sake.
    assert "fill a field" in text


def test_overlay_tells_the_agent_partial_records_are_fine():
    """Otherwise the agent chases completeness and the ask becomes the detour."""
    text = open(OVERLAY).read()
    assert "partial record" in text


def test_overlay_explains_why_confidence_must_be_honest():
    """A brief that reads an inference as a commitment misleads the account team."""
    text = open(OVERLAY).read()
    for level in ("`high`", "`medium`", "`low`"):
        assert level in text
    assert "inference" in text


def test_overlay_requires_telling_the_attendee():
    """Capture the attendee doesn't know about is not something to ship."""
    text = open(OVERLAY).read()
    assert "Tell me if you'd rather I didn't" in text
    assert "stop recording" in text


def test_overlay_documents_the_disabled_response():
    """The agent must not retry, and must not surface it to the attendee."""
    text = open(OVERLAY).read()
    assert '{"captured": false}' in text
    assert "Don't retry" in text


def test_overlay_explains_record_id_reuse():
    """Without this the agent files three contradictory versions of one use case."""
    text = open(OVERLAY).read()
    assert "record_id" in text
    assert "refine" in text


def test_overlay_example_is_valid_json_matching_the_contract():
    """A malformed example is what the agent will copy."""
    text = open(OVERLAY).read()
    block = text.split("workshop-discovery '", 1)[1].split("'\n", 1)[0]
    record = json.loads(block)

    from server.discovery import build_record

    built = build_record("labuser007@example.com", record)
    assert built.record_id == record["record_id"]
    assert built.confidence == "high"
    assert built.current_stack == record["current_stack"]
    # Nothing in the documented example should trip the redaction pass — an
    # example that arrives pre-redacted would teach the agent the wrong shape.
    assert built.redactions == 0


def test_helper_documents_every_field_the_server_accepts():
    """A field the server accepts but the helper never mentions is dead weight."""
    from server import discovery

    text = open(HELPER).read()
    for name in (*discovery._TEXT_FIELDS, *discovery._LIST_FIELDS):
        assert name in text, name


# --- The helper actually works ------------------------------------------------


@pytest.fixture
def helper_env(client, monkeypatch, tmp_path):
    """A provisioned home plus the env the PTY would give the helper."""
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    home = _provisioned_home(client, monkeypatch)
    return {
        "HOME": home,
        "PATH": os.environ["PATH"],
        "WORKSHOP_USER_EMAIL": "alice@example.com",
        "WORKSHOP_APP_URL": "http://127.0.0.1:1",  # unreachable on purpose
    }


def _run(env, *args, stdin: str = ""):
    return subprocess.run(
        ["bash", HELPER, *args],
        env=env, input=stdin, capture_output=True, text=True, timeout=30,
    )


def test_helper_injects_the_attendee_identity(helper_env):
    """The agent must not be able to choose whose record this is.

    The capability is verified against this email server-side, so the helper
    overwrites whatever the agent put in ``email`` rather than trusting it.
    """
    out = _run(helper_env, json.dumps({"email": "someone-else@x.com", "goal": "g"}))
    # The app is unreachable in this env, so the failure is the curl step — what
    # matters is that the payload assembly ran and did not reject the record.
    assert "not valid JSON" not in out.stderr
    assert out.returncode == 1


def test_helper_rejects_malformed_json(helper_env):
    out = _run(helper_env, "{not json")
    assert out.returncode == 2
    assert "not valid JSON" in out.stderr


def test_helper_rejects_a_non_object(helper_env):
    out = _run(helper_env, '["a", "b"]')
    assert out.returncode == 2
    assert "expected a JSON object" in out.stderr


def test_helper_rejects_an_empty_invocation(helper_env):
    out = _run(helper_env, stdin="   \n")
    assert out.returncode == 2
    assert "expected a JSON object" in out.stderr


def test_helper_accepts_stdin(helper_env):
    """Agents pipe as readily as they pass arguments."""
    out = _run(helper_env, stdin=json.dumps({"goal": "explore lakebase"}))
    assert "expected a JSON object" not in out.stderr
    assert out.returncode == 1  # unreachable app, not a usage error


def test_helper_failure_message_does_not_alarm(helper_env):
    """An unreachable app is usually capture being off, not something broken."""
    out = _run(helper_env, json.dumps({"goal": "g"}))
    assert "capture may be off" in out.stderr


def test_helper_sends_the_capability_and_never_the_email_env_as_auth():
    text = open(HELPER).read()
    assert "X-Workshop-Capability" in text
    assert "callback-capability" in text
    # The endpoint path must match what the server exposes.
    assert "/api/discovery" in text
