"""Content pack validation, phases, triggers, broadcasts."""

from .conftest import ALICE

PACK = {
    "version": 1,
    "phases": ["intro", "build"],
    "shell": {
        "links": [{"label": "Academy", "url": "https://academy.example.com", "icon": "graduation-cap"}],
        "features": {"nuggets_pane": True},
    },
    "nuggets": [
        {"id": "n-intro", "title": "Intro", "markdown": "hello", "phases": ["intro"]},
        {"id": "n-build", "title": "Build", "markdown": "build", "phases": ["build"]},
        {"id": "n-claude", "title": "Claude tip", "markdown": "tip",
         "phases": ["build"], "triggers": ["claude_active"]},
        {"id": "n-pinned", "title": "Pinned", "markdown": "pin", "pinned": True},
    ],
}

ADMIN = {"X-Forwarded-Email": "op@example.com"}


def test_pack_roundtrip_and_phase_filtering(client, as_admin):
    resp = client.post("/api/admin/content-pack", json=PACK, headers=ADMIN)
    assert resp.status_code == 200

    client.post("/api/admin/phase", json={"phase": "intro"}, headers=ADMIN)
    nuggets = client.get("/api/nuggets", headers=ALICE).json()
    ids = {n["id"] for n in nuggets["nuggets"]}
    assert "n-intro" in ids and "n-pinned" in ids
    assert "n-build" not in ids

    client.post("/api/admin/phase", json={"phase": "build"}, headers=ADMIN)
    nuggets = client.get("/api/nuggets", headers=ALICE).json()
    ids = {n["id"] for n in nuggets["nuggets"]}
    assert "n-build" in ids
    # claude not running for alice -> trigger unsatisfied
    assert "n-claude" not in ids

    # Pinned nuggets sort first.
    assert nuggets["nuggets"][0]["id"] == "n-pinned"


def test_trigger_satisfied_by_active_agent(client, as_admin):
    client.post("/api/admin/content-pack", json=PACK, headers=ADMIN)
    client.post("/api/admin/phase", json={"phase": "build"}, headers=ADMIN)

    from server.content import content_service

    eligible = {n["id"] for n in content_service.nuggets_for({"claude_active"})}
    assert "n-claude" in eligible


def test_invalid_pack_422(client, as_admin):
    bad = {"version": 1, "nuggets": [{"id": "x"}]}  # missing title/markdown
    resp = client.post("/api/admin/content-pack", json=bad, headers=ADMIN)
    assert resp.status_code == 422


def test_unknown_phase_422(client, as_admin):
    client.post("/api/admin/content-pack", json=PACK, headers=ADMIN)
    resp = client.post("/api/admin/phase", json={"phase": "afterparty"}, headers=ADMIN)
    assert resp.status_code == 422


def test_broadcast_reaches_events_without_being_retained(client, as_admin):
    """A default broadcast is a toast: it fires once and is not replayed.

    Retaining every notice meant an attendee who reloaded an hour later got
    "5 minutes left" again. Only a pinned banner describes a condition worth
    surviving a reload; see the banner test below.
    """
    with client.websocket_connect("/ws/events", headers=ALICE) as ws:
        resp = client.post(
            "/api/admin/broadcast",
            json={"message": "Break time", "level": "info", "ttl_s": 60},
            headers=ADMIN,
        )
        assert resp.status_code == 200
        import json

        msg = json.loads(ws.receive_text())
        assert msg["t"] == "broadcast" and msg["message"] == "Break time"
        assert msg["surface"] == "toast"

    cfg = client.get("/api/config", headers=ALICE).json()
    assert cfg["broadcast"] is None


def test_pinned_banner_survives_reload_and_can_be_cleared(client, as_admin):
    resp = client.post(
        "/api/admin/broadcast",
        json={
            "message": "Wi-Fi: guest / labs2026",
            "level": "info",
            "ttl_s": 600,
            "surface": "banner",
            "durability": "sticky",
        },
        headers=ADMIN,
    )
    assert resp.status_code == 200
    cfg = client.get("/api/config", headers=ALICE).json()
    assert cfg["broadcast"]["message"] == "Wi-Fi: guest / labs2026"

    cleared = client.post(
        "/api/admin/broadcast",
        json={"message": "", "clear": True},
        headers=ADMIN,
    )
    assert cleared.status_code == 200
    cfg = client.get("/api/config", headers=ALICE).json()
    assert cfg["broadcast"] is None


def test_shell_config_served_to_attendees(client, as_admin):
    client.post("/api/admin/content-pack", json=PACK, headers=ADMIN)
    cfg = client.get("/api/config", headers=ALICE).json()
    assert cfg["shell"]["links"][0]["label"] == "Academy"


def test_presence_lists_attendees(client, as_admin, launchable_agents):
    client.post("/api/sessions", json={"agent_id": "claude"}, headers=ALICE)
    presence = client.get("/api/admin/presence", headers=ADMIN).json()
    emails = [u["email"] for u in presence["users"]]
    assert "alice@example.com" in emails


def test_manual_build_phase_still_auto_advances_to_wrap_once(client, as_admin):
    from server.content import content_service

    pack_with_wrap = {
        **PACK,
        "phases": ["intro", "build", "wrap"],
    }
    client.post("/api/admin/content-pack", json=pack_with_wrap, headers=ADMIN)
    client.post("/api/admin/phase", json={"phase": "build"}, headers=ADMIN)
    assert content_service.phase == "build"

    resp = client.post(
        "/api/admin/broadcast",
        json={"message": "30 minutes left: the workshop ends at 17:00."},
        headers=ADMIN,
    )
    assert resp.status_code == 200
    assert content_service.phase == "wrap"



WRAP_PACK = {**PACK, "phases": ["intro", "setup", "build", "wrap"]}


def test_an_operators_phase_holds_against_auto_advance(client, as_admin, monkeypatch):
    """A facilitator's intro lasted about one card fetch on any seat with an
    open terminal: the flag that should have protected it was never set."""
    import types

    from server.content import content_service
    from server.sessions import session_manager

    client.post("/api/admin/content-pack", json=WRAP_PACK, headers=ADMIN)
    client.post("/api/admin/phase", json={"phase": "intro"}, headers=ADMIN)
    session = types.SimpleNamespace(agent_id="claude", last_io=0.0, last_activity=0.0)
    monkeypatch.setattr(session_manager, "list_for", lambda _email: [session])

    assert content_service.phase_explicitly_set is True
    assert client.get("/api/nuggets", headers=ALICE).json()["phase"] == "intro"
    assert content_service.advance_to_build_if_default() is False
    assert content_service.phase == "intro"


def test_the_countdown_still_wraps_an_operator_phase(client, as_admin):
    from server.content import content_service

    client.post("/api/admin/content-pack", json=WRAP_PACK, headers=ADMIN)
    client.post("/api/admin/phase", json={"phase": "setup"}, headers=ADMIN)

    client.post("/api/admin/broadcast", headers=ADMIN, json={
        "message": "30 minutes left: the workshop ends at 17:00.", "level": "info", "ttl_s": 60})

    assert content_service.phase == "wrap"


def test_the_wrap_up_export_still_wraps_an_operator_phase(client, as_admin, monkeypatch):
    from server import insight_summary
    from server.content import content_service

    client.post("/api/admin/content-pack", json=WRAP_PACK, headers=ADMIN)
    client.post("/api/admin/phase", json={"phase": "intro"}, headers=ADMIN)
    monkeypatch.setattr(insight_summary, "summarise_in_background", lambda *a, **k: None)

    client.post("/api/admin/export", json={"reason": "wrap_up"}, headers=ADMIN)

    assert content_service.phase == "wrap"


def test_auto_advance_runs_until_an_operator_chooses_and_a_fresh_seat_starts_unset(client, as_admin):
    from server.content import ContentService, content_service

    client.post("/api/admin/content-pack", json=WRAP_PACK, headers=ADMIN)
    content_service.set_phase("intro")

    assert content_service.phase_explicitly_set is False
    assert content_service.advance_to_build_if_default() is True
    assert content_service.phase == "build"
    assert ContentService().phase_explicitly_set is False


IDLE_PACK = {
    "version": 1,
    "phases": ["intro", "build", "wrap"],
    "nuggets": [
        {"id": "n-idle", "title": "Stuck?", "markdown": "x", "phases": ["build"], "triggers": ["idle_5m"]},
        {"id": "n-build", "title": "Build", "markdown": "b", "phases": ["build"]},
    ],
}


def _nugget_ids(client) -> set[str]:
    return {n["id"] for n in client.get("/api/nuggets", headers=ALICE).json()["nuggets"]}


def test_keepalive_pings_do_not_hold_idle_at_zero(client, as_admin, monkeypatch):
    """The page pings every 20 s and each ping refreshed last-seen, so idle
    nudges never showed while the tab was open. Presence must not change."""
    import time
    import types

    from server import artifacts
    from server.sessions import session_manager
    from server.users import user_manager

    client.post("/api/admin/content-pack", json=IDLE_PACK, headers=ADMIN)
    client.post("/api/admin/phase", json={"phase": "build"}, headers=ADMIN)
    monkeypatch.setattr(artifacts, "scan_new_tool_targets", lambda *_a: [])
    now = time.time()
    user = user_manager.get("alice@example.com")
    user.first_seen, user.last_active = now - 3600, now - 600
    user.last_seen = now  # the page's keepalive, just now
    session = types.SimpleNamespace(agent_id="codex", last_io=now - 600, last_activity=now, to_dict=lambda: {})
    monkeypatch.setattr(session_manager, "list_for", lambda _email: [session])

    assert "n-idle" in _nugget_ids(client)
    presence = client.get("/api/admin/presence", headers=ADMIN).json()
    alice = next(u for u in presence["users"] if u["email"] == "alice@example.com")
    assert alice["online"] is True and alice["last_seen"] == now

    session.last_io = time.time()  # they typed, or the agent printed
    assert "n-idle" not in _nugget_ids(client)


def test_an_omni_only_seat_moves_to_build_on_its_first_worker_tool_call(client, as_admin, monkeypatch):
    """Omnigent opens in its own tab and creates no WT session, so its seats
    stayed in intro until wrap. Its workers' tool calls are already scanned."""
    import time

    import server.main as main
    from server import artifacts
    from server.content import content_service
    from server.sessions import session_manager
    from server.users import user_manager

    client.post("/api/admin/content-pack", json=IDLE_PACK, headers=ADMIN)
    content_service.set_phase("intro")
    monkeypatch.setattr(session_manager, "list_for", lambda _email: [])
    user = user_manager.get("alice@example.com")
    user.last_active = 0.0
    main._transcript_topic_scan_at.clear()
    monkeypatch.setattr(artifacts, "scan_new_tool_targets", lambda *_a: [])

    assert client.get("/api/nuggets", headers=ALICE).json()["phase"] == "intro"

    main._transcript_topic_scan_at.clear()
    monkeypatch.setattr(artifacts, "scan_new_tool_targets", lambda *_a: ["databricks apps deploy outage-board"])
    before = time.time()

    assert client.get("/api/nuggets", headers=ALICE).json()["phase"] == "build"
    assert user.last_active >= before
    main._transcript_topic_scan_at.clear()


def test_omni_codex_worker_rollouts_count_as_building_activity(tmp_path, monkeypatch):
    """F-010: Omnigent's Codex worker keeps its rollouts in a private
    per-conversation CODEX_HOME under the temp dir and passes shell commands as
    an argv list, so the activity scan saw nothing and the seat stayed in intro."""
    import json as _json

    from server import artifacts

    home = tmp_path / "home"
    home.mkdir()
    worker_root = tmp_path / "omnigent-codex-homes-1000"
    sessions = worker_root / "omnigent-codex-home-abc" / "sessions" / "2026" / "10" / "29"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-10-29T08-55-00.jsonl"
    rollout.write_text("\n".join(_json.dumps(line) for line in (
        {"type": "session_meta", "payload": {"id": "s1"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
                                              "arguments": _json.dumps({"command": ["bash", "-lc", "databricks tables list workshop_demo food_agribusiness"]})}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan",
                                              "arguments": _json.dumps({"plan": [{"step": "x"}]})}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "view_image",
                                              "arguments": _json.dumps({"detail": "high"})}},
    )) + "\n")
    monkeypatch.setattr(artifacts, "_omnigent_codex_home_roots", lambda: [str(worker_root)])

    targets = artifacts.scan_new_tool_targets(str(home), {})

    assert "shell bash -lc databricks tables list workshop_demo food_agribusiness" in targets
    assert "view_image" in targets  # a call with no target still counts
    assert not any(t.startswith("update_plan") for t in targets)
    # The insight harvest never reads worker homes (their prompts are polly's).
    assert not any(str(worker_root) in p for p in artifacts._transcript_files(str(home)))
