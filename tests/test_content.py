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


def test_presence_lists_attendees(client, as_admin):
    client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    presence = client.get("/api/admin/presence", headers=ADMIN).json()
    emails = [u["email"] for u in presence["users"]]
    assert "alice@example.com" in emails
