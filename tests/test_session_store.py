"""P1-11: session-metadata journal + reconnect-after-restart ghosts."""

import json
import os

import pytest

from server.session_store import SessionMetadataStore
from server.sessions import SessionManager


@pytest.fixture()
def store(tmp_path):
    return SessionMetadataStore(str(tmp_path / "journal.json"))


# ── SessionMetadataStore ──────────────────────────────────────────────────────


def _meta(sid, owner="alice@example.com", **over):
    m = {"id": sid, "owner_email": owner, "agent_id": "bash", "label": "Terminal",
         "created_at": 1.0, "last_activity": 2.0, "exited": False,
         "scrollback_tail": "hello"}
    m.update(over)
    return m


def test_upsert_and_load_roundtrip(store):
    store.upsert(_meta("s1"))
    store.upsert(_meta("s2", owner="bob@example.com"))
    data = store.load()
    assert set(data) == {"s1", "s2"}
    assert data["s1"]["owner_email"] == "alice@example.com"


def test_live_session_metadata_does_not_persist_terminal_output():
    from server.sessions import Session

    session = Session("alice@example.com", "bash", "Terminal", -1, -1)
    session.scrollback.append("secret terminal output")

    metadata = session.metadata()

    assert "scrollback_tail" not in metadata
    assert "secret terminal output" not in repr(metadata)


def test_upsert_without_id_is_ignored(store):
    store.upsert({"owner_email": "alice@example.com"})
    assert store.load() == {}


def test_mark_exited_and_remove(store):
    store.upsert(_meta("s1"))
    store.mark_exited("s1", "closed")
    assert store.load()["s1"]["exited"] is True
    assert store.load()["s1"]["exit_reason"] == "closed"
    store.remove("s1")
    assert store.load() == {}


def test_prior_live_sessions_only_returns_unexited_as_ghosts(store):
    store.upsert(_meta("live1"))
    store.upsert(_meta("ended", exited=True))
    ghosts = store.prior_live_sessions()
    assert [g["id"] for g in ghosts] == ["live1"]
    # Surfaced as ended-on-restart, not reattachable.
    assert ghosts[0]["exited"] is True
    assert ghosts[0]["exit_reason"] == "server_restarted"


def test_writes_are_atomic_and_leave_no_temp_files(store, tmp_path):
    store.upsert(_meta("s1"))
    # Only the journal file remains — the temp file was renamed away.
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".session-journal-")]
    assert leftovers == []
    # And the file is valid JSON.
    with open(store.path) as fh:
        assert "s1" in json.load(fh)
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def test_corrupt_journal_loads_as_empty(store):
    with open(store.path, "w") as fh:
        fh.write("{ this is not json")
    assert store.load() == {}


def test_writes_are_fail_soft(tmp_path):
    # Point the journal at a directory: os.replace onto it fails, but upsert
    # must swallow the error rather than break the calling session.
    bad = SessionMetadataStore(str(tmp_path))
    bad.upsert(_meta("s1"))  # must not raise
    assert bad.load() == {}  # unreadable (it's a dir) → empty


# ── SessionManager integration (no PTYs) ──────────────────────────────────────


def test_manager_surfaces_prior_live_sessions_after_restart(store):
    # Simulate the pre-restart process having journaled a live session.
    store.upsert(_meta("s1", owner="alice@example.com"))
    store.upsert(_meta("s2", owner="alice@example.com", last_activity=5.0))
    store.upsert(_meta("s3", owner="bob@example.com"))

    # New process boots and attaches the same journal.
    mgr = SessionManager()
    mgr.configure_store(store)

    alice = mgr.prior_for("alice@example.com")
    assert [g["id"] for g in alice] == ["s2", "s1"]  # newest activity first
    assert all(g["exited"] and g["exit_reason"] == "server_restarted" for g in alice)
    assert [g["id"] for g in mgr.prior_for("bob@example.com")] == ["s3"]

    # The journal is cleared after load so the same ghosts aren't re-surfaced
    # on the next restart; freshly created sessions repopulate it.
    assert store.load() == {}


def test_legacy_prior_scrollback_is_not_returned(client, store):
    from .conftest import ALICE
    from server.sessions import session_manager

    store.upsert(
        _meta(
            "s1",
            owner="alice@example.com",
            scrollback_tail="legacy secret output",
        )
    )
    previous_prior = session_manager._prior
    previous_store = session_manager._store
    try:
        session_manager.configure_store(store)
        payload = client.get("/api/sessions", headers=ALICE).json()["prior_sessions"][0]
        assert payload["exit_reason"] == "server_restarted"
        assert "scrollback_tail" not in payload
        assert "legacy secret output" not in repr(payload)
    finally:
        session_manager._prior = previous_prior
        session_manager._store = previous_store


def test_prior_ghost_acknowledgement_is_owner_scoped(client):
    from .conftest import ALICE, BOB
    from server.sessions import session_manager

    previous_prior = session_manager._prior
    session_manager._prior = {
        "alice@example.com": [_meta("ghost-1", exited=True, exit_reason="server_restarted")]
    }
    try:
        denied = client.delete("/api/sessions/prior/ghost-1", headers=BOB)
        assert denied.status_code == 404
        assert len(client.get("/api/sessions", headers=ALICE).json()["prior_sessions"]) == 1

        acknowledged = client.delete("/api/sessions/prior/ghost-1", headers=ALICE)
        assert acknowledged.status_code == 200
        assert client.get("/api/sessions", headers=ALICE).json()["prior_sessions"] == []
    finally:
        session_manager._prior = previous_prior


def test_manager_without_store_is_inert(store):
    mgr = SessionManager()  # default: no journal
    assert mgr.prior_for("alice@example.com") == []
    # Persist hooks are safe no-ops without a store.
    mgr._persist_exit("nope", "closed")


def test_create_journals_live_session_and_terminate_removes_it(client, store):
    # Attach the journal to the real (global) manager used by the API, then
    # drive the actual PTY create/terminate path.
    from .conftest import ALICE
    from server.sessions import session_manager

    prev = session_manager._store
    session_manager._store = store
    try:
        resp = client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
        sid = resp.json()["session"]["id"]
        # The live session is journaled (so a restart would surface it).
        journaled = store.load()
        assert sid in journaled and journaled[sid]["exited"] is False
        assert journaled[sid]["owner_email"] == "alice@example.com"

        # Deliberately closing it drops it from the journal — not a restart ghost.
        assert client.delete(f"/api/sessions/{sid}", headers=ALICE).status_code == 200
        assert sid not in store.load()
    finally:
        session_manager._store = prev
