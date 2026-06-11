"""Brag certificate and idle nudges."""

import os
import subprocess

from .conftest import ALICE


def _seed_repo(home: str) -> None:
    repo = os.path.join(home, "projects", "demo")
    if os.path.isdir(os.path.join(repo, ".git")):
        return  # already seeded by an earlier test (HOMEs persist per session)
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, "pipeline.py"), "w") as f:
        f.write("print('hello')\n" * 40)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "."],
                ["git", "commit", "-q", "-m", "build"]):
        subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True)


def test_certificate_downloads_pdf_with_stats(client, monkeypatch):
    from server.users import user_manager

    monkeypatch.setenv("WORKSHOP_PAT", "dapi-test-token")
    client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    _seed_repo(user_manager.get("alice@example.com").home)

    resp = client.get("/api/certificate", params={"name": "Ada Lovelace"}, headers=ALICE)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF")
    assert len(resp.content) > 1500


def test_certificate_requires_name(client):
    resp = client.get("/api/certificate", params={"name": "   "}, headers=ALICE)
    assert resp.status_code == 422


def test_stats_gathering(client, monkeypatch):
    from server import stats
    from server.users import user_manager

    monkeypatch.delenv("WORKSHOP_PAT", raising=False)  # census skipped cleanly
    client.post("/api/sessions", json={"agent_id": "bash"}, headers=ALICE)
    user = user_manager.get("alice@example.com")
    _seed_repo(user.home)
    user.topics["lakebase"] = 1.0

    data = stats.gather(user)
    assert data["terminal_sessions"] >= 1
    assert data["code"]["projects"] == 1
    assert data["code"]["commits"] == 1
    assert data["code"]["lines"] >= 40
    assert data["topics"] == ["lakebase"]
    assert data["resources"] == {}  # no credential -> census skipped


def test_idle_nudges_rank_first(client):
    from server.content import content_service

    content_service.set_phase("build")
    active = content_service.nuggets_for(set(), set(), idle_minutes=0)
    assert not any(n["nudge"] for n in active)

    idle = content_service.nuggets_for(set(), set(), idle_minutes=6)
    nudges = [n for n in idle if n["nudge"]]
    assert nudges, "idle_5m cards should fire at 6 minutes idle"
    unpinned = [n for n in idle if not n["pinned"]]
    assert unpinned[0]["nudge"] is True  # nudges outrank everything unpinned

    deep_idle = content_service.nuggets_for(set(), set(), idle_minutes=11)
    assert any(n["id"] == "stuck-tip" and n["nudge"] for n in deep_idle)


def test_wrap_phase_has_certificate_card(client):
    from server.content import content_service

    content_service.set_phase("wrap")
    cards = content_service.nuggets_for(set(), set())
    cert = [n for n in cards if n["id"] == "wrap-certificate"]
    assert cert and cert[0]["pinned"] and cert[0]["link"]["url"] == "#certificate"
