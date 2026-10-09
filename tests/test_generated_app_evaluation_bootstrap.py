"""The external observer must work with the release's real SPA route precedence."""
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from evals.generated_apps.runtime import wt_evaluation_bootstrap as bootstrap


def test_observer_get_precedes_spa_without_reordering_release_routes(monkeypatch, tmp_path):
    from server import main, config, sessions
    app = FastAPI()
    @app.get("/release-api")
    def release(): return {"release": True}
    @app.get("/{path:path}")
    def spa(path): return {"spa": path}
    original = list(app.router.routes)
    (tmp_path / "wt_evaluation.py").write_text('''from fastapi import APIRouter
router=APIRouter(prefix="/api/admin/evaluation")
@router.get("/sessions/{session_id}/messages")
def messages(session_id): return {"observer": session_id}
''')
    monkeypatch.setattr(main, "app", app)
    monkeypatch.setenv("WT_EVALUATION_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_VERSION", "2.1.283")
    monkeypatch.setenv("CODEX_CLI_VERSION", "0.157.1")
    import sys
    # Factory registration should not leak a synthetic observer into other tests.
    monkeypatch.setitem(sys.modules, "server.evaluation", None)
    monkeypatch.setattr(config, "evaluation_observation_binding", None)
    actual = bootstrap.application()
    assert actual is app and app.router.routes[-len(original):] == original
    client = TestClient(app)
    assert client.get("/api/admin/evaluation/sessions/owned/messages").json() == {"observer": "owned"}
    assert client.get("/release-api").json() == {"release": True}
    assert client.get("/ordinary-frontend").json() == {"spa": "ordinary-frontend"}
