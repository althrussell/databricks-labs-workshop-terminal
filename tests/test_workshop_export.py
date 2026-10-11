"""Portable source, namespace isolation, upload/read-back and failure recovery."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("workshop_export", str(ROOT / "assets/bin/workshop-export"))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WORKSHOP_CATALOG", "attendee_catalog")
    monkeypatch.setenv("WORKSHOP_SCHEMA", "assigned_schema")
    monkeypatch.setenv("OBO_PROFILE_NAME", "me")
    for project in ("repair-desk", "garden"):
        root = tmp_path / "projects" / project
        (root / "src").mkdir(parents=True)
        (root / "src/main.py").write_text(f"print({project!r})\n")
        (root / "README.md").write_text(f"# {project}\nUnfinished workshop work.\n")
        (root / "uv.lock").write_text("dependency-lock\n")
    return tmp_path / "projects"


@pytest.fixture
def fake_cli(monkeypatch):
    remote = {}
    calls = []
    state = {"volume": False, "fail_upload": False, "corrupt_download": False,
             "schema_privileges": set(), "volume_privileges": {"ALL_PRIVILEGES"},
             "fail_grant": False}

    def run(argv, **kwargs):
        calls.append(argv)
        args = argv[1:]
        code, output = 0, "{}"
        if args[:2] == ["current-user", "me"]:
            output = json.dumps({"userName": "coding-sp"})
        elif args[:2] == ["grants", "get-effective"]:
            output = json.dumps({"privilege_assignments": [{"principal": "coding-sp",
                "privileges": [{"privilege": value} for value in state[args[2] + "_privileges"]]}]})
        elif args[:2] == ["grants", "update"]:
            if state["fail_grant"]:
                code, output = 1, "PERMISSION_DENIED"
            else:
                change = json.loads(args[args.index("--json") + 1])["changes"][0]
                assert change["principal"] == "coding-sp"
                state[args[2] + "_privileges"].update(change["add"])
        elif args[:2] == ["volumes", "read"] and not state["volume"]:
            code, output = 1, "RESOURCE_DOES_NOT_EXIST"
        elif args[:2] == ["volumes", "create"]:
            state["volume"] = True
        elif args[:2] == ["fs", "cp"]:
            src, dest = args[2:]
            if src.startswith("dbfs:"):
                Path(dest).write_bytes(b"corrupt" if state["corrupt_download"] else remote[src])
            elif state["fail_upload"]:
                code, output = 1, "PERMISSION_DENIED bearer test-secret-should-never-print"
            else:
                remote[dest] = Path(src).read_bytes()
        return subprocess.CompletedProcess(argv, code, output if not code else "", output if code else "")

    monkeypatch.setattr(helper.subprocess, "run", run)
    return calls, remote, state


def test_all_projects_include_current_source_and_exclude_runtime_credentials(source, fake_cli):
    project = source / "repair-desk"
    for relative in (".env", ".env.production", ".databrickscfg", ".npmrc", "private.key",
                     "node_modules/pkg/index.js", ".git/config", ".agents/skills/tool/SKILL.md",
                     ".venv/bin/python", "dist/bundle.js"):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private-or-generated")
    (project / ".env.example").write_text("DATABRICKS_HOST=\n")
    outside = source.parent / "private"
    outside.write_text("outside-project-secret")
    (project / "src/linked.py").symlink_to(outside)
    (project / "uncommitted.ts").write_text("export const current = true;\n")
    result = helper.export()
    with zipfile.ZipFile(result["local_zip"]) as archive:
        members = set(archive.namelist())
        assert "workshop-code/repair-desk/uncommitted.ts" in members
        assert "workshop-code/repair-desk/.env.example" in members
        assert "workshop-code/garden/uv.lock" in members
        assert "workshop-code/garden/README.md" in members
        assert all(archive.read(name) not in (b"private-or-generated", b"outside-project-secret") for name in members)
        assert archive.read("workshop-code/repair-desk/src/main.py") == b"print('repair-desk')\n"
    calls, remote, _ = fake_cli
    assert result["projects"] == ["garden", "repair-desk"]
    assert result["verified"] is True
    assert result["volume_path"].startswith("/Volumes/attendee_catalog/assigned_schema/workshop_exports/code/")
    assert remote["dbfs:" + result["volume_path"]] == Path(result["local_zip"]).read_bytes()
    assert all(call[0] == "databricks" for call in calls)
    mutations = [call for call in calls if call[1:3] == ["grants", "update"]]
    assert len(mutations) == 1
    assert mutations[0][3:5] == ["schema", "attendee_catalog.assigned_schema"]
    assert json.loads(mutations[0][mutations[0].index("--json") + 1]) == {
        "changes": [{"principal": "coding-sp", "add": ["CREATE_VOLUME", "USE_SCHEMA"]}]}


def test_one_project_and_reruns_keep_separate_archives(source, fake_cli):
    first = helper.export(str(source / "garden"))
    (source / "garden/src/main.py").write_text("new uncommitted version\n")
    second = helper.export(str(source / "garden"))
    assert first["volume_path"] != second["volume_path"]
    with zipfile.ZipFile(first["local_zip"]) as old, zipfile.ZipFile(second["local_zip"]) as new:
        assert old.read("workshop-code/garden/src/main.py") != new.read("workshop-code/garden/src/main.py")
        assert not any("repair-desk" in name for name in new.namelist())
    assert len(fake_cli[1]) == 2
    assert len([call for call in fake_cli[0] if call[1:3] == ["volumes", "create"]]) == 1


@pytest.mark.parametrize("missing", ["WORKSHOP_CATALOG", "WORKSHOP_SCHEMA"])
def test_missing_namespace_never_guesses_or_mutates(source, fake_cli, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(helper.ExportError, match="assigned WORKSHOP_CATALOG and WORKSHOP_SCHEMA"):
        helper.export()
    assert fake_cli[0] == []
    assert not (source.parent / ".cache/workshop/exports").exists()


def test_outside_source_is_rejected(source, fake_cli):
    with pytest.raises(helper.ExportError, match="inside your ~/projects"):
        helper.export(str(source.parent))
    assert fake_cli[0] == []


def test_auth_failure_retains_zip_without_echoing_credentials(source, fake_cli):
    fake_cli[2]["fail_upload"] = True
    with pytest.raises(helper.ExportError, match="Local ZIP retained") as error:
        helper.export()
    assert "test-secret" not in str(error.value)
    saved = list((source.parent / ".cache/workshop/exports").glob("*.zip"))
    assert len(saved) == 1 and zipfile.is_zipfile(saved[0])
    assert fake_cli[1] == {}


def test_mismatched_upload_is_not_reported_as_verified(source, fake_cli):
    fake_cli[2]["corrupt_download"] = True
    with pytest.raises(helper.ExportError, match="did not match the local ZIP"):
        helper.export()


def test_non_obo_uses_existing_default_identity(source, fake_cli, monkeypatch):
    monkeypatch.delenv("OBO_PROFILE_NAME")
    helper.export()
    assert all(call[0] == "databricks" for call in fake_cli[0])


def test_existing_access_needs_no_grants(source, fake_cli):
    fake_cli[2]["schema_privileges"] = {"ALL_PRIVILEGES"}
    helper.export()
    assert not any(call[1:3] == ["grants", "update"] for call in fake_cli[0])


def test_existing_volume_adds_only_own_read_write_access(source, fake_cli):
    fake_cli[2].update(volume=True, schema_privileges={"USE_SCHEMA", "CREATE_VOLUME"},
                       volume_privileges=set())
    helper.export()
    mutations = [call for call in fake_cli[0] if call[1:3] == ["grants", "update"]]
    assert len(mutations) == 1
    assert mutations[0][3:5] == ["volume", "attendee_catalog.assigned_schema.workshop_exports"]
    assert json.loads(mutations[0][mutations[0].index("--json") + 1]) == {
        "changes": [{"principal": "coding-sp", "add": ["READ_VOLUME", "WRITE_VOLUME"]}]}


def test_missing_manage_retains_zip_without_fallback_or_upload(source, fake_cli):
    fake_cli[2]["fail_grant"] = True
    with pytest.raises(helper.ExportError, match="Local ZIP retained"):
        helper.export()
    assert fake_cli[1] == {}
    assert all(call[0] == "databricks" for call in fake_cli[0])
    assert not any(call[1:3] == ["volumes", "create"] for call in fake_cli[0])


def test_success_prints_specific_volume_and_download_steps(source, fake_cli, monkeypatch, capsys):
    monkeypatch.setattr(helper.sys, "argv", ["workshop-export"])
    assert helper.main() == 0
    output = capsys.readouterr().out
    assert "attendee_catalog.assigned_schema.workshop_exports" in output
    assert "For workshop-code-" in output
    assert "File options → Download file" in output
    assert "before the workshop environment is deleted" in output
    assert "read-back matches" in output


def test_removed_promote_and_export_pack_load():
    from server.content import ContentPack
    from server.bootstrap.install import FORK_SKILLS

    assert not (ROOT / "assets/skills/promote").exists()
    assert "promote" not in FORK_SKILLS
    assert "workshop-export" in FORK_SKILLS
    pack = ContentPack.model_validate(json.loads((ROOT / "content/default_pack.json").read_text()))
    assert {n.id for n in pack.nuggets} >= {"export-project", "wrap-take-it-with-you"}
    assert not any("promote" in n.id for n in pack.nuggets)
