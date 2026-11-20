"""The per-project app manifest (server/app_manifest.py, contract v2 section 2).

The manifest is the account team's evidence of what an attendee built, so the
tests pin three things: what counts as evidence (and what does not), the bounds
that keep a hostile or huge project from costing the seat, and the screenshot
metadata that must match the files uploaded beside it.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from server import app_manifest
from server.event_emitter import EventEmitter

from .schema_assert import assert_schema

SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "workshop-insight-events.schema.json").read_text())
RUN_ID = "11111111-2222-3333-4444-555555555555"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 8


class _User:
    def __init__(self, email: str = "labuser001@x.com"):
        self.email = email


@pytest.fixture(autouse=True)
def _no_design_record(monkeypatch):
    """The design workflow record is tested separately; these tests read files only."""
    monkeypatch.setattr(app_manifest, "_design_record", lambda project: {})


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "feature-store"
    root.mkdir()
    (root / "app.yaml").write_text(
        "command: [\"uvicorn\", \"app:app\"]\n"
        "resources:\n"
        "  - name: feature-db\n"
        "    database:\n"
        "      instance_name: fraud-lakebase\n"
        "      database_name: features\n"
        "  - name: shared-warehouse\n"
        "    sql_warehouse:\n"
        "      id: 0123456789abcdef\n"
        "      permission: CAN_USE\n"
    )
    (root / "databricks.yml").write_text(
        "bundle:\n  name: feature-store\n"
        "resources:\n"
        "  apps:\n"
        "    feature_app:\n"
        "      name: feature-store-app\n"
        "  jobs:\n"
        "    nightly_refresh:\n"
        "      name: nightly refresh\n"
    )
    (root / "app.py").write_text(
        "import mlflow\n"
        "from databricks.vector_search.client import VectorSearchClient\n"
        "def load(spark):\n"
        "    return spark.read.table(\"main_demo.fraud.account_features\")\n"
        "def attribute_chain(self):\n"
        "    return self.config.main.sales.orders\n"
        "# Genie and Lakebase are only mentioned in this comment\n"
    )
    (root / "queries.sql").write_text(
        "SELECT * FROM main_demo.fraud.scores;\n"
        "INSERT INTO main_demo.fraud.alerts SELECT 1;\n"
    )
    (root / "requirements.txt").write_text("fastapi==0.142.2\ndatabricks-sql-connector==4.0.0\n")
    (root / "package.json").write_text('{"dependencies": {"react": "18.3.1"}}')
    return root


def _manifest(project: Path) -> dict:
    manifest = app_manifest.build(project, captured_at="2026-07-30T12:00:00Z")
    assert manifest is not None
    return manifest


# --- resources ----------------------------------------------------------------


def test_resources_come_from_app_yaml_and_databricks_yml_with_line_sources(project):
    resources = _manifest(project)["resources"]

    by_kind = {r["kind"]: r for r in resources}
    assert by_kind["database"]["name"] == "feature-db"
    assert by_kind["database"]["source"] == "app.yaml:4"
    assert by_kind["sql_warehouse"]["source"] == "app.yaml:8"
    assert by_kind["job"]["name"] == "nightly refresh"
    assert by_kind["job"]["source"] == "databricks.yml:8"


def test_the_app_name_comes_from_the_bundle_apps_block(project):
    assert _manifest(project)["app_name"] == "feature-store-app"


def test_resource_count_is_capped_at_thirty(project):
    lines = ["resources:"]
    for n in range(40):
        lines += [f"  - name: db-{n}", "    database:", "      database_name: d"]
    (project / "app.yaml").write_text("\n".join(lines) + "\n")

    assert len(_manifest(project)["resources"]) == 30


# --- Unity Catalog names -------------------------------------------------------


def test_three_part_names_in_sql_context_and_string_literals_count(project):
    names = {t["name"] for t in _manifest(project)["uc_tables"]}

    assert "main_demo.fraud.account_features" in names   # Python string literal
    assert "main_demo.fraud.scores" in names             # SQL FROM
    assert "main_demo.fraud.alerts" in names             # SQL INTO


def test_python_attribute_chains_and_imports_never_count_as_tables(project):
    names = {t["name"] for t in _manifest(project)["uc_tables"]}

    assert not any(n.startswith(("self.", "config.")) for n in names)
    assert not any("vector_search" in n for n in names)
    assert not any(n.startswith("os.") for n in names)


def test_module_and_file_names_that_look_three_part_are_not_tables(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "app.py").write_text(
        "import os\n"
        "path = os.path.join('a', 'b')\n"
        "name = 'react.dom.client'\n"
        "report = 'exports.v1.csv'\n"
        "from mypkg.utils.helpers import run\n"
        "spark.read.table('sales.reporting.orders')\n"
    )

    names = {t["name"] for t in _manifest(root)["uc_tables"]}

    assert names == {"sales.reporting.orders"}
    assert "mypkg.utils.helpers" not in names
    assert not any(n.startswith("react.") or n.startswith("os.") for n in names)


def test_uc_names_are_capped_at_fifty(project):
    (project / "many.sql").write_text(
        "\n".join(f"SELECT 1 FROM main_demo.bulk.t{n};" for n in range(80)) + "\n"
    )

    assert len(_manifest(project)["uc_tables"]) == 50


# --- products: evidence, not keywords ------------------------------------------


def test_products_are_listed_from_resources_config_and_imports(project):
    products = {p["product"]: p["source"] for p in _manifest(project)["products_evidenced"]}

    assert products["Databricks Apps"] == "app.yaml:1"
    assert products["Lakebase"] == "app.yaml:4"
    assert products["Databricks SQL"]  # resource or the connector requirement
    assert products["Vector Search"].startswith("app.py:")
    assert products["MLflow"] == "app.py:1"
    assert products["Unity Catalog"]
    assert products["Lakeflow Jobs"] == "databricks.yml:8"


def test_a_keyword_in_a_comment_is_not_evidence(project):
    products = {p["product"] for p in _manifest(project)["products_evidenced"]}

    assert "Genie" not in products, "a comment that names Genie is not a genie_space call"


def test_genie_is_evidenced_by_the_sdk_call(project):
    (project / "genie.py").write_text("answer = w.genie.start_conversation(space_id='x')\n")

    products = {p["product"] for p in _manifest(project)["products_evidenced"]}

    assert "Genie" in products


def test_product_list_is_capped_at_twenty(project):
    assert len(_manifest(project)["products_evidenced"]) <= 20


# --- stack ---------------------------------------------------------------------


def test_stack_comes_from_manifests_and_source_kinds(project):
    stack = _manifest(project)["stack"]

    assert "fastapi" in stack
    assert "react" in stack
    assert "python" in stack


# --- bounds: symlinks, size, budget ----------------------------------------------


def test_a_symlink_is_never_followed_or_read(project, tmp_path):
    secret = tmp_path / "outside.py"
    secret.write_text("SELECT * FROM secret_cat.private.creds\n")
    (project / "linked.py").symlink_to(secret)

    scan = app_manifest.scan_project(project)

    assert "linked.py" not in scan.files
    assert "secret_cat.private.creds" not in json.dumps(_manifest(project))


def test_a_file_over_512_kb_is_skipped_and_recorded(project):
    (project / "big.py").write_text("# " + "x" * (513 * 1024) + "\nSELECT * FROM main_demo.big.t\n")

    scan = app_manifest.scan_project(project)

    assert "big.py" not in scan.files
    assert "file_too_large" in scan.issues


def test_a_project_is_read_only_up_to_its_byte_budget(project, monkeypatch):
    monkeypatch.setattr(app_manifest, "MAX_PROJECT_BYTES", 300)

    scan = app_manifest.scan_project(project)

    assert "project_budget" in scan.issues
    assert sum(len(text.encode()) for text in scan.files.values()) <= 300


def test_only_listed_file_kinds_are_read(project):
    (project / "notes.md").write_text("SELECT * FROM main_demo.docs.only_in_markdown\n")
    (project / "image.png").write_bytes(PNG)

    scan = app_manifest.scan_project(project)

    assert "notes.md" not in scan.files
    assert "image.png" not in scan.files


def test_hidden_and_vendored_directories_are_not_walked(project):
    (project / "node_modules").mkdir()
    (project / "node_modules" / "dep.js").write_text("SELECT 1 FROM main_demo.vendor.x\n")
    (project / ".hidden").mkdir()
    (project / ".hidden" / "h.py").write_text("SELECT 1 FROM main_demo.hidden.x\n")

    names = {t["name"] for t in _manifest(project)["uc_tables"]}

    assert not any(n.startswith(("main_demo.vendor", "main_demo.hidden")) for n in names)


# --- screenshots ---------------------------------------------------------------


def _shot(folder: Path, name: str, data: bytes = PNG) -> bytes:
    (folder / name).write_bytes(data)
    return data


def test_screenshot_metadata_matches_the_file_bytes(project):
    folder = project / ".workshop" / "evidence"
    folder.mkdir(parents=True)
    initial = _shot(folder, "20260730T113012Z-deployed-desktop-initial.png")

    [shot] = _manifest(project)["screenshots"]

    assert shot["file"] == "feature-store-20260730T113012Z-deployed-desktop-initial.png"
    assert shot["sha256"] == hashlib.sha256(initial).hexdigest()
    assert shot["bytes"] == len(initial)
    assert shot["captured_at"] == "2026-07-30T11:30:12Z"
    assert shot["scope"] == "deployed"
    assert shot["label"] == "initial"


def test_screenshots_keep_the_newest_initial_and_two_newest_after_shots(project):
    folder = project / ".workshop" / "evidence"
    folder.mkdir(parents=True)
    for name in (
        "20260730T113012Z-deployed-desktop-initial.png",
        "20260730T113500Z-deployed-desktop-after-1.png",
        "20260730T114000Z-local-desktop-after-2.png",
        "20260730T115000Z-deployed-desktop-after-3.png",
        "notes.txt",
    ):
        if name.endswith(".png"):
            _shot(folder, name)
        else:
            (folder / name).write_text("not a shot")

    labels = [s["label"] for s in _manifest(project)["screenshots"]]

    assert labels == ["after-3", "after-2", "initial"]


def test_a_screenshot_over_two_megabytes_is_left_out(project):
    folder = project / ".workshop" / "evidence"
    folder.mkdir(parents=True)
    _shot(folder, "20260730T113012Z-deployed-desktop-initial.png", PNG + b"0" * (2 * 1024 * 1024))

    assert _manifest(project)["screenshots"] == []


def test_screenshot_url_and_title_come_from_the_design_record(project, monkeypatch):
    folder = project / ".workshop" / "evidence"
    folder.mkdir(parents=True)
    _shot(folder, "20260730T113012Z-deployed-desktop-initial.png")
    monkeypatch.setattr(app_manifest, "_design_record", lambda p: {"evidence": {"inspect": {"artifacts": [
        {"kind": "screenshot", "path": ".workshop/evidence/20260730T113012Z-deployed-desktop-initial.png",
         "url": "https://feature-store.example.invalid/", "title": "Feature store"},
    ]}}})

    [shot] = _manifest(project)["screenshots"]

    assert shot["url"] == "https://feature-store.example.invalid/"
    assert shot["title"] == "Feature store"


def test_select_shots_agrees_with_the_export_script_copy(tmp_path):
    loader = importlib.machinery.SourceFileLoader(
        "workshop_export_under_test",
        str(Path(__file__).resolve().parents[1] / "assets" / "bin" / "workshop-export"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    script = importlib.util.module_from_spec(spec)
    loader.exec_module(script)
    names = [
        "20260730T113012Z-deployed-desktop-initial.png",
        "20260730T113500Z-deployed-desktop-after-1.png",
        "20260730T114000Z-local-desktop-after-2.png",
        "20260730T115000Z-deployed-desktop-after-3.png",
        "20260729T100000Z-deployed-desktop-initial.png",
        "not-a-shot.png",
    ]

    assert app_manifest.select_shots(names) == script.select_shots(names)


# --- revision and URL -----------------------------------------------------------


def test_url_drops_its_query_and_fragment(project, monkeypatch):
    monkeypatch.setattr(app_manifest, "_design_record", lambda p: {"evidence": {"build": {
        "preview": "https://feature-store.example.invalid/app?token=abc#x"}}})

    assert _manifest(project)["url"] == "https://feature-store.example.invalid/app"


def test_a_project_without_its_own_git_repository_has_no_revision(project):
    assert _manifest(project)["revision"] is None


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_revision_is_the_projects_own_head(project):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=project, check=True, env=env)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True,
                          check=True).stdout.strip()

    assert _manifest(project)["revision"] == head


# --- identity and emission -------------------------------------------------------


def test_the_idempotency_key_follows_the_contract_shape_and_ignores_capture_time(project):
    first = _manifest(project)
    later = app_manifest.build(project, captured_at="2026-11-20T09:00:00Z")

    key = app_manifest.idempotency_key(RUN_ID, "labuser001@x.com", first)

    assert key.startswith(f"app-manifest:{RUN_ID}:labuser001@x.com:feature-store:norev:")
    assert key == app_manifest.idempotency_key(RUN_ID, "labuser001@x.com", later)


def test_the_idempotency_key_changes_when_the_content_changes(project):
    before = _manifest(project)
    (project / "more.py").write_text("SELECT 1 FROM main_demo.new.table_x\n")
    after = _manifest(project)

    assert app_manifest.idempotency_key(RUN_ID, "a@x.com", before) != \
        app_manifest.idempotency_key(RUN_ID, "a@x.com", after)


def test_nothing_is_emitted_with_the_flag_off(project, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.delenv("WORKSHOP_INSIGHT_V2", raising=False)
    emitter = EventEmitter(run_id=RUN_ID, workspace_id="ws1", ingest_url="", ingest_token="")

    assert app_manifest.emit(_User(), project, emitter=emitter) is None
    assert app_manifest.emit_for_user(_User(), emitter=emitter) == 0
    assert emitter.pending() == 0


def test_with_the_flag_on_a_manifest_is_journaled_as_app_manifest(project, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "true")
    emitter = EventEmitter(run_id=RUN_ID, workspace_id="ws1", ingest_url="", ingest_token="")

    manifest = app_manifest.emit(_User(), project, emitter=emitter)

    assert manifest is not None
    sent: list[dict] = []
    emitter.drain(lambda ev: sent.append(ev) or True)
    [event] = sent
    assert event["type"] == "app.manifest"
    assert event["attendee"] == "labuser001@x.com"
    assert event["idempotency_key"].startswith("app-manifest:")
    assert_schema(event, SCHEMA)


def test_emit_for_user_visits_each_visible_project(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_INSIGHT_CAPTURE", "true")
    monkeypatch.setenv("WORKSHOP_INSIGHT_V2", "true")
    emitter = EventEmitter(run_id=RUN_ID, workspace_id="ws1", ingest_url="", ingest_token="")
    root = tmp_path / "projects"
    for name in ("alpha", "beta"):
        (root / name).mkdir(parents=True)
        (root / name / "app.py").write_text("x = 1\n")
    (root / ".hidden").mkdir()
    (root / "linked").symlink_to(root / "alpha")
    from server import projects as projects_module

    monkeypatch.setattr(projects_module, "root", lambda user: root)

    assert app_manifest.emit_for_user(_User(), emitter=emitter) == 2


def test_host_literals_are_not_tables_and_if_not_exists_still_counts(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "app.py").write_text(
        'HOST = "api.example.com"\n'
        'url = "sales.reporting.internal"\n'
    )
    (root / "setup.sql").write_text("CREATE TABLE IF NOT EXISTS main.fraud.x (id INT);\n")

    names = {t["name"] for t in _manifest(root)["uc_tables"]}

    assert names == {"main.fraud.x"}


def test_a_re_emit_with_only_a_new_capture_time_is_the_same_logical_event(project):
    emitter = EventEmitter(run_id=RUN_ID, workspace_id="ws1", ingest_url="", ingest_token="")
    first = app_manifest.build(project, captured_at="2026-11-20T10:00:00Z")
    again = app_manifest.build(project, captured_at="2026-11-20T11:00:00Z")
    key = app_manifest.idempotency_key(RUN_ID, "labuser001@x.com", first)
    assert key == app_manifest.idempotency_key(RUN_ID, "labuser001@x.com", again)

    emitter.emit(app_manifest.EVENT_TYPE, "labuser001@x.com", first, idempotency_key=key)
    emitter.emit(app_manifest.EVENT_TYPE, "labuser001@x.com", again, idempotency_key=key)

    assert emitter.pending() == 1


def test_free_text_in_the_manifest_is_redacted_and_ipv6_needs_a_digit():
    from server.discovery import redact_pii

    assert "ada@example.com" not in app_manifest._scrub("Inbox for ada@example.com")
    assert redact_pii("Foo::cadd()") == ("Foo::cadd()", 0)
    assert redact_pii("host 2001:db8::1 here")[0] == "host [redacted-ip] here"


def test_a_screenshot_between_the_source_and_screenshot_limits_is_read_whole(project):
    folder = project / ".workshop" / "evidence"
    folder.mkdir(parents=True)
    data = PNG + b"0" * (700 * 1024)
    _shot(folder, "20260730T113012Z-deployed-desktop-initial.png", data)

    [shot] = _manifest(project)["screenshots"]

    assert shot["bytes"] == len(data)
    assert shot["sha256"] == hashlib.sha256(data).hexdigest()
