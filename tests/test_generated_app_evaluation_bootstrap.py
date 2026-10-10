"""The external observer must work with the release's real SPA route precedence."""
import os
import pytest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from evals.generated_apps.runtime import wt_evaluation_bootstrap as bootstrap


@pytest.mark.parametrize("evaluation", ["true", "false", ""])
def test_package_launcher_loads_evaluation_only_when_explicitly_enabled(monkeypatch, evaluation):
    from evals.generated_apps.runtime import wt_bootstrap as launcher

    class Executed(BaseException):
        pass

    monkeypatch.setenv("WORKSHOP_PACKAGE_SHA256", "a" * 64)
    monkeypatch.setenv("WORKSHOP_EVALUATION_ENABLED", evaluation)
    monkeypatch.setenv("PEX_MODULE", "offline-original")
    monkeypatch.setenv("PEX_EXTRA_SYS_PATH", "offline-original")
    monkeypatch.setenv("WT_EVALUATION_SOURCE_ROOT", "offline-original")
    monkeypatch.setenv("PYTHONPATH", "offline-original")
    monkeypatch.setattr(launcher, "oauth_token", lambda: "offline-test-token")
    monkeypatch.setattr(launcher, "download_pex", lambda *_args: None)
    def execute(_python, _args):
        assert os.environ["PEX_MODULE"] == (
            "wt_evaluation_bootstrap:main" if evaluation == "true" else "server.otel_bootstrap:main"
        )
        raise Executed
    monkeypatch.setattr(launcher.os, "execv", execute)
    # A successful exec never returns; bypass the startup retry handler.
    with pytest.raises(Executed):
        launcher.main()


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
