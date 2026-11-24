"""Read-only POST routes are not capture writers, so their failures never block the seal.

CT rehearsal R7 (23 Nov 2026): the agent read its freshly deployed App's logs twice while the App's log stream
was not up; each /api/apps/logs call answered 502, WT recorded both as failed writers, and the seat's closeout
stayed INCOMPLETE with accepted_writer_failed until teardown.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server.closeout_http import produces_capture

from .test_closeout_api import retained, seal  # noqa: F401 - closeout fixtures

ALICE = {"X-Forwarded-Email": "alice@example.com"}


@pytest.mark.parametrize("path", ["/api/apps/logs", "/api/browser/status"])
def test_read_only_posts_are_not_capture_writers(path):
    assert produces_capture({"type": "http", "path": path, "method": "POST"}) is False


def test_capture_writing_posts_are_still_writers():
    for path in ("/api/discovery", "/api/tools/app-deploy/event", "/api/wizard", "/api/help/raise"):
        assert produces_capture({"type": "http", "path": path, "method": "POST"}) is True, path


def test_a_failing_app_logs_read_never_blocks_the_seal(client, as_admin, retained, monkeypatch):
    import server.main as main

    def no_stream(body, request):
        raise HTTPException(status_code=502, detail="Log stream failed for 'notes-app': not up yet")

    monkeypatch.setattr(main, "_callback_identity", no_stream)
    for _ in range(2):
        response = client.post("/api/apps/logs", json={"app_name": "notes-app"}, headers=ALICE)
        assert response.status_code == 502
    assert retained.journal.read().get("closeout", {}).get("writer_outcomes", []) == []
    assert seal(client, retained)["phase"] == "SEALED"
