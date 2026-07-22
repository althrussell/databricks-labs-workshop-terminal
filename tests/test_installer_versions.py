import json
import os
import select
import subprocess
import sys
import threading

import pytest

from server.bootstrap import install


class _SyntheticArtifactContract:
    def __init__(self, root):
        self.root = root
        self._entries = {}
        self.ai_dev_kit = {
            "version": install.AI_DEV_KIT_REF,
            "source": install.AI_DEV_KIT_REPO,
            "sha256": "9" * 64,
            "commit": "a" * 40,
            "content_sha256": "b" * 64,
        }

    def entry(self, name):
        if name == "ai_dev_kit":
            self.ai_dev_kit["version"] = install.AI_DEV_KIT_REF
            self.ai_dev_kit["source"] = install.AI_DEV_KIT_REPO
            return self.ai_dev_kit
        if name in {
            "codex_npm_launcher_package",
            "codex_native_package_linux_x64",
        }:
            if name in self._entries:
                return self._entries[name]
            from tests.test_codex_artifacts import make_codex_tarballs

            launcher, native = make_codex_tarballs(self.root)
            self._entries["codex_npm_launcher_package"] = {
                "version": install.CODEX_VERSION,
                "source": str(launcher),
                "sha256": install._file_checksum(launcher),
            }
            self._entries["codex_native_package_linux_x64"] = {
                "version": install.CODEX_VERSION,
                "source": str(native),
                "sha256": install._file_checksum(native),
                "executable_sha256": __import__("hashlib")
                .sha256(b"reviewed-native")
                .hexdigest(),
            }
            return self._entries[name]
        path = self.root / name
        if name in {"omnigent_wheelhouse", "python_3_12_runtime"}:
            path.mkdir(exist_ok=True)
            relative = (
                "omnigent.whl"
                if name == "omnigent_wheelhouse"
                else "bin/python3.12"
            )
            artifact = path / relative
            artifact.parent.mkdir(parents=True, exist_ok=True)
            if not artifact.exists():
                artifact.write_bytes(
                    b"wheel" if name == "omnigent_wheelhouse" else b"python"
                )
            entry = {
                "version": "test",
                "source": str(path),
                "kind": "directory",
                "content_sha256": install._directory_checksum(path),
            }
            if name == "python_3_12_runtime":
                entry["executable_relative_path"] = relative
            return entry
        payload = (
            b"omnigent==0.5.1 --hash=sha256:" + b"a" * 64 + b"\n"
            if name == "omnigent_lock"
            else name.encode()
        )
        path.write_bytes(path.read_bytes() if path.exists() else payload)
        checksum = install._file_checksum(path)
        if name == "tmux_linux_x64":
            checksum = install.TMUX_STATIC_SHA256
        if name == "node_linux_x64":
            checksum = "69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec"
        if name == "claude_binary":
            binary_name = "claude"
            checksum = install._file_checksum(
                os.path.join(install.config.shared_prefix(), "bin", binary_name)
            ) or checksum
        entry = {
            "version": "test",
            "source": str(path),
            "sha256": checksum,
        }
        if name == "omnigent_lock":
            entry["lock_sha256"] = checksum
        return entry


@pytest.fixture(autouse=True)
def restore_installer_state(monkeypatch, tmp_path):
    contract = _SyntheticArtifactContract(tmp_path / "artifacts")
    contract.root.mkdir()
    monkeypatch.setattr(install, "_artifact_contract", lambda: contract)
    monkeypatch.setattr(
        install,
        "_verified_artifact",
        lambda name: (contract.entry(name)["source"], contract.entry(name)),
    )
    with install._state_lock:
        saved = dict(install._state)
        install._state.clear()
    yield contract
    with install._state_lock:
        install._state.clear()
        install._state.update(saved)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("2.1.216 (Claude Code)", "2.1.216"),
        ("codex-cli 0.144.6", "0.144.6"),
        ("Databricks CLI v1.8.0", "1.8.0"),
        ("omnigent 0.5.1", "0.5.1"),
    ],
)
def test_version_output_parser(output, expected):
    assert install._parse_version(output) == expected


def test_official_installer_commands_are_exactly_versioned():
    assert install._claude_install_argv() == [
        "bash",
        "-s",
        install.CLAUDE_VERSION,
    ]
    assert install._databricks_installer_url() == (
        "https://raw.githubusercontent.com/databricks/setup-cli/"
        f"v{install.DATABRICKS_CLI_VERSION}/install.sh"
    )


def test_synthetic_codex_artifacts_are_stable_across_repeated_reads(
    restore_installer_state,
):
    launcher_first = restore_installer_state.entry("codex_npm_launcher_package")
    native_first = restore_installer_state.entry("codex_native_package_linux_x64")

    assert restore_installer_state.entry("codex_npm_launcher_package") == launcher_first
    assert restore_installer_state.entry("codex_native_package_linux_x64") == native_first


def test_status_exposes_secret_free_expected_and_actual_release_manifest():
    install._set(
        "claude",
        "complete",
        expected_version="2.1.216",
        actual_version="2.1.215",
    )

    manifest = install.status()["release_manifest"]

    assert {
        key: manifest["claude"][key]
        for key in ("enabled", "expected", "actual", "match")
    } == {
        "enabled": True,
        "expected": "2.1.216",
        "actual": "2.1.215",
        "match": False,
    }
    assert {
        "source",
        "started_at",
        "completed_at",
        "duration_ms",
        "expected_checksum",
        "actual_checksum",
    } <= set(manifest["claude"])
    assert "token" not in repr(manifest).lower()


def test_setup_steps_record_source_timing_and_verification_fields(monkeypatch):
    times = iter([100.0, 104.25])
    monkeypatch.setattr(install.time, "time", lambda: next(times))

    install._set(
        "claude",
        "running",
        expected_version="2.1.216",
        source="network",
        expected_checksum="abc",
    )
    install._set(
        "claude",
        "complete",
        actual_version="2.1.216",
        actual_checksum="abc",
    )

    step = install.status()["steps"]["claude"]
    assert step["source"] == "network"
    assert step["started_at"] == 100.0
    assert step["completed_at"] == 104.25
    assert step["duration_ms"] == 4250
    assert step["expected_version"] == "2.1.216"
    assert step["actual_version"] == "2.1.216"
    assert step["expected_checksum"] == "abc"
    assert step["actual_checksum"] == "abc"


def test_new_pending_boot_clears_prior_run_provenance(monkeypatch):
    monkeypatch.setattr(install.time, "time", lambda: 100.0)
    install._set("skills", "running", source="network")
    install._set("skills", "complete", actual_version="v1", actual_checksum="abc")

    install._set("skills", "pending")

    step = install.status()["steps"]["skills"]
    assert step["source"] is None
    assert step["started_at"] is None
    assert step["completed_at"] is None
    assert step["duration_ms"] is None


def test_codex_existing_version_mismatch_forces_exact_reinstall(
    monkeypatch, tmp_path
):
    prefix = tmp_path
    codex = prefix / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text("old")
    versions = iter(["0.1.0", install.CODEX_VERSION])
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install, "_read_cli_version", lambda _: next(versions))
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Result()
    monkeypatch.setattr(
        install.subprocess,
        "run",
        fake_run,
    )

    install._install_codex()

    assert any(
        isinstance(argv, list)
        and "install" in argv
        and not any("@openai/codex@" in value for value in argv)
        for argv in calls
    )
    step = install.status()["steps"]["codex"]
    assert step["actual_version"] == install.CODEX_VERSION
    assert step["source"] == "staged"


def test_matching_version_with_arbitrary_checksum_is_not_prewarmed(
    monkeypatch, tmp_path
):
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text("binary")
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(tmp_path))
    monkeypatch.setattr(
        install, "_read_cli_version", lambda _: install.CODEX_VERSION
    )
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *args, **kwargs: type(
            "Result", (), {"returncode": 1, "stdout": "", "stderr": "blocked"}
        )(),
    )
    monkeypatch.setattr(install.time, "sleep", lambda _: None)

    install._install_codex()

    assert install.status()["steps"]["codex"]["source"] != "prewarmed"


def test_codex_installs_both_staged_packages_and_verifies_vendor_native_binary(
    monkeypatch, tmp_path
):
    from tests.test_codex_artifacts import make_codex_tarballs

    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "codex").write_bytes(b"old-launcher")
    launcher_package, native_package = make_codex_tarballs(tmp_path)
    native_bytes = b"reviewed-native"
    launcher_entry = {
        "sha256": install._file_checksum(launcher_package),
        "source": str(launcher_package),
    }
    native_entry = {
        "sha256": install._file_checksum(native_package),
        "source": str(native_package),
        "executable_sha256": __import__("hashlib").sha256(native_bytes).hexdigest(),
    }
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(
        install,
        "_verified_artifact",
        lambda name: (
            (str(launcher_package), launcher_entry)
            if name == "codex_npm_launcher_package"
            else (str(native_package), native_entry)
        ),
    )
    versions = iter(["0.1.0", install.CODEX_VERSION])
    monkeypatch.setattr(install, "_read_cli_version", lambda _path: next(versions))
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        assert _kwargs["env"]["npm_config_offline"] == "true"
        assert _kwargs["env"]["npm_config_cache"] == str(
            prefix / "npm-offline-cache"
        )
        (prefix / "bin" / "codex").write_bytes(b"js-launcher")
        launcher_root = prefix / "lib/node_modules/@openai/codex"
        (launcher_root / "bin").mkdir(parents=True)
        (launcher_root / "bin/codex.js").write_bytes(b"js-launcher")
        (launcher_root / "package.json").write_bytes(b"launcher-metadata")
        (launcher_root / "README.md").write_bytes(b"launcher-sidecar")
        return Result()

    monkeypatch.setattr(install.subprocess, "run", fake_run)

    install._install_codex()

    npm_call = next(call for call in calls if "install" in call)
    assert "--offline" in npm_call
    assert "--omit=optional" in npm_call
    assert str(launcher_package) in npm_call
    assert str(native_package) not in npm_call
    stamp = json.loads((prefix / "codex.install.json").read_text())
    assert stamp["launcher_package_sha256"] == launcher_entry["sha256"]
    assert stamp["native_package_sha256"] == native_entry["sha256"]
    assert stamp["launcher_sha256"] == install._file_checksum(prefix / "bin/codex")
    assert stamp["native_sha256"] == native_entry["executable_sha256"]
    launcher_root = prefix / "lib/node_modules/@openai/codex"
    alias_root = launcher_root / "node_modules/@openai/codex-linux-x64"
    assert stamp["launcher_tree_sha256"] == install._directory_checksum(
        launcher_root
    )
    assert stamp["native_tree_sha256"] == install._directory_checksum(alias_root)
    assert install._codex_install_reusable(
        str(prefix), launcher_entry, native_entry
    )

    native_path = prefix / stamp["native_relative_path"]
    assert "node_modules/@openai/codex-linux-x64/vendor/" in str(native_path)
    native_path.write_bytes(b"tampered-native")
    assert not install._codex_install_reusable(
        str(prefix), launcher_entry, native_entry
    )


def test_codex_prewarm_rejects_launcher_and_native_sidecar_tampering(
    monkeypatch, tmp_path
):
    from tests.test_codex_artifacts import make_codex_tarballs

    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    launcher_package, native_package = make_codex_tarballs(tmp_path)
    launcher_entry = {
        "source": str(launcher_package),
        "sha256": install._file_checksum(launcher_package),
    }
    native_entry = {
        "source": str(native_package),
        "sha256": install._file_checksum(native_package),
        "executable_sha256": __import__("hashlib").sha256(
            b"reviewed-native"
        ).hexdigest(),
    }
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(
        install,
        "_verified_artifact",
        lambda name: (
            (str(launcher_package), launcher_entry)
            if name == "codex_npm_launcher_package"
            else (str(native_package), native_entry)
        ),
    )
    monkeypatch.setattr(
        install, "_read_cli_version", lambda _path: install.CODEX_VERSION
    )

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(_argv, **_kwargs):
        (prefix / "bin/codex").write_bytes(b"launcher")
        launcher = prefix / "lib/node_modules/@openai/codex"
        (launcher / "bin").mkdir(parents=True)
        (launcher / "bin/codex.js").write_bytes(b"launcher")
        (launcher / "package.json").write_bytes(b"metadata")
        return Result()

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    install._install_codex()
    assert install._codex_install_reusable(
        str(prefix), launcher_entry, native_entry
    )

    launcher_sidecar = prefix / "lib/node_modules/@openai/codex/package.json"
    launcher_original = launcher_sidecar.read_bytes()
    launcher_sidecar.write_bytes(b"tampered")
    assert not install._codex_install_reusable(
        str(prefix), launcher_entry, native_entry
    )
    launcher_sidecar.write_bytes(launcher_original)

    native_sidecar = (
        prefix
        / "lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64"
        / "vendor/x86_64-unknown-linux-musl/codex-resources/bwrap"
    )
    native_sidecar.unlink()
    assert not install._codex_install_reusable(
        str(prefix), launcher_entry, native_entry
    )


def test_tmux_reuses_only_checksum_verified_persistent_install(
    monkeypatch, tmp_path
):
    tmux = tmp_path / "bin" / "tmux"
    tmux.parent.mkdir()
    tmux.write_bytes(b"verified-tmux")
    checksum = install._file_checksum(tmux)
    (tmp_path / "tmux.install.json").write_text(json.dumps({
        "archive_sha256": install.TMUX_STATIC_SHA256,
        "binary_sha256": checksum,
    }))
    calls = []

    class Result:
        returncode = 0
        stdout = "tmux 3.6b"
        stderr = ""

    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(tmp_path))
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda argv, **kwargs: calls.append(argv) or Result(),
    )

    install._install_tmux()

    assert not any(argv and argv[0] == "curl" for argv in calls)
    step = install.status()["steps"]["tmux"]
    assert step["source"] == "prewarmed"
    assert step["expected_checksum"] == install.TMUX_STATIC_SHA256
    assert step["actual_checksum"] == checksum


def test_omnigent_installs_from_complete_offline_supply_chain(
    monkeypatch, tmp_path
):
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    uv = tmp_path / "uv"
    python_runtime = tmp_path / "python-runtime"
    python = python_runtime / "bin/python3.12"
    lock = tmp_path / "omnigent.lock"
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    uv.write_bytes(b"reviewed-uv")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"reviewed-python-3.12")
    (python_runtime / "lib/python3.12/os.py").parent.mkdir(parents=True)
    (python_runtime / "lib/python3.12/os.py").write_bytes(b"stdlib")
    lock.write_text(
        "omnigent==0.5.1 --hash=sha256:" + "a" * 64 + "\n"
        "omnigent-client==0.5.1 --hash=sha256:" + "b" * 64 + "\n"
    )
    (wheelhouse / "omnigent-0.5.1-py3-none-any.whl").write_bytes(b"wheel")
    entries = {
        "uv_binary": {
            "source": str(uv),
            "sha256": install._file_checksum(uv),
        },
        "python_3_12_runtime": {
            "source": str(python_runtime),
            "content_sha256": install._directory_checksum(python_runtime),
            "executable_relative_path": "bin/python3.12",
        },
        "omnigent_lock": {
            "source": str(lock),
            "sha256": install._file_checksum(lock),
            "lock_sha256": install._file_checksum(lock),
        },
        "omnigent_wheelhouse": {
            "source": str(wheelhouse),
            "content_sha256": install._directory_checksum(wheelhouse),
        },
    }
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(
        install,
        "_verified_artifact",
        lambda name: (entries[name]["source"], entries[name]),
    )
    monkeypatch.setattr(install, "_read_cli_version", lambda _path: install.OMNIGENT_VERSION)
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        assert argv[0] == str(uv)
        assert kwargs["env"]["UV_OFFLINE"] == "1"
        assert kwargs["env"]["UV_NO_INDEX"] == "1"
        assert kwargs["env"]["UV_PYTHON_DOWNLOADS"] == "never"
        if argv[1] == "venv":
            venv_python = prefix / "omnigent-venv/bin/python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_bytes(b"venv-python")
            site = (
                prefix
                / "omnigent-venv/lib/python3.12/site-packages/omnigent"
                / "__init__.py"
            )
            site.parent.mkdir(parents=True)
            site.write_bytes(b"omnigent-package")
            (site.parent.parent / "dependency.py").write_bytes(b"dependency")
        else:
            omnigent = prefix / "omnigent-venv/bin/omnigent"
            omnigent.write_bytes(b"omnigent-launcher")
        return Result()

    monkeypatch.setattr(install.subprocess, "run", fake_run)

    install._install_omnigent()

    venv_call, pip_call = [call[0] for call in calls]
    assert venv_call == [
        str(uv), "venv", "--clear", "--python", str(python),
        str(prefix / "omnigent-venv"),
    ]
    assert "--offline" in pip_call
    assert "--no-index" in pip_call
    assert "--require-hashes" in pip_call
    assert str(wheelhouse) in pip_call
    assert str(lock) in pip_call
    stamp = json.loads((prefix / "omnigent.install.json").read_text())
    assert stamp["uv_sha256"] == entries["uv_binary"]["sha256"]
    assert stamp["python_runtime_sha256"] == entries["python_3_12_runtime"]["content_sha256"]
    assert stamp["lock_sha256"] == entries["omnigent_lock"]["sha256"]
    assert stamp["wheelhouse_sha256"] == entries["omnigent_wheelhouse"]["content_sha256"]
    assert stamp["venv_sha256"] == install._directory_checksum(
        prefix / "omnigent-venv"
    )
    assert install._omnigent_install_reusable(str(prefix), entries)

    site = prefix / "omnigent-venv/lib/python3.12/site-packages/dependency.py"
    original_site = site.read_bytes()
    site.write_bytes(b"tampered dependency")
    assert not install._omnigent_install_reusable(str(prefix), entries)
    site.write_bytes(original_site)
    assert install._omnigent_install_reusable(str(prefix), entries)

    (python_runtime / "lib/python3.12/os.py").write_bytes(b"tampered stdlib")
    assert not install._omnigent_install_reusable(str(prefix), entries)

    uv.write_bytes(b"tampered")
    assert not install._omnigent_install_reusable(str(prefix), entries)


def test_omnigent_missing_staged_supply_chain_sets_installer_error(
    monkeypatch, tmp_path
):
    from server.bootstrap.artifacts import ArtifactManifestError

    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(tmp_path))
    monkeypatch.setattr(
        install,
        "_verified_artifact",
        lambda _name: (_ for _ in ()).throw(
            ArtifactManifestError("offline supply chain incomplete")
        ),
    )

    install._install_omnigent()

    step = install.status()["steps"]["omnigent"]
    assert step["status"] == "error"
    assert "offline supply chain incomplete" in step["error"]


def test_all_release_candidate_defaults_are_exact():
    assert install.CLAUDE_VERSION == "2.1.216"
    assert install.CODEX_VERSION == "0.144.6"
    assert install.DATABRICKS_CLI_VERSION == "1.8.0"
    assert install.OMNIGENT_VERSION == "0.5.1"


def test_ai_dev_kit_fetch_records_exact_ref_and_resolved_commit(
    monkeypatch, tmp_path, restore_installer_state
):
    prefix = tmp_path / "prefix"
    vendored = tmp_path / "vendored"
    vendored.mkdir()
    commit = "a" * 40

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            clone_dir = prefix / "ai-dev-kit"
            (clone_dir / "databricks-skills" / "fetched-skill").mkdir(
                parents=True
            )
            restore_installer_state.ai_dev_kit["commit"] = commit
            restore_installer_state.ai_dev_kit["content_sha256"] = (
                install._directory_checksum(clone_dir / "databricks-skills")
            )
            return Result()
        if "rev-parse" in argv:
            return Result(stdout=f"{commit}\n")
        return Result()

    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install, "_ASSETS_SKILLS", str(vendored))
    monkeypatch.setattr(install, "AI_DEV_KIT_REF", "v1.2.3")
    monkeypatch.setattr(install.subprocess, "run", fake_run)

    install._install_skills()

    manifest = install.status()["release_manifest"]["ai_dev_kit"]
    assert {
        key: manifest[key]
        for key in (
            "enabled",
            "expected",
            "actual",
            "match",
            "resolved_commit",
            "checksum",
            "source",
        )
    } == {
        "enabled": True,
        "expected": "v1.2.3",
        "actual": "v1.2.3",
        "match": True,
        "resolved_commit": commit,
        "checksum": manifest["checksum"],
        "source": "network",
    }
    assert len(manifest["checksum"]) == 64
    assert {
        "started_at",
        "completed_at",
        "duration_ms",
        "expected_checksum",
        "actual_checksum",
    } <= set(manifest)


def test_ai_dev_kit_checksum_covers_only_copied_skill_directories(
    monkeypatch, tmp_path, restore_installer_state
):
    prefix = tmp_path / "prefix"
    vendored = tmp_path / "vendored"
    vendored.mkdir()
    commit = "f" * 40

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            upstream = prefix / "ai-dev-kit" / "databricks-skills"
            skill = upstream / "fetched-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("verified")
            (upstream / "README.md").write_text("repository metadata")
            restore_installer_state.ai_dev_kit["commit"] = commit
            restore_installer_state.ai_dev_kit["content_sha256"] = (
                install._directory_checksum(upstream, {"fetched-skill"})
            )
            return Result()
        if "rev-parse" in argv:
            return Result(stdout=f"{commit}\n")
        return Result()

    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install, "_ASSETS_SKILLS", str(vendored))
    monkeypatch.setattr(install, "AI_DEV_KIT_REF", commit)
    monkeypatch.setattr(install.subprocess, "run", fake_run)

    install._install_skills()

    manifest = install.status()["release_manifest"]["ai_dev_kit"]
    assert manifest["source"] == "network"
    assert (prefix / "skills" / "fetched-skill" / "SKILL.md").read_text() == "verified"
    assert manifest["checksum"] == install._directory_checksum(
        prefix / "ai-dev-kit" / "databricks-skills",
        {"fetched-skill"},
    )


def test_ai_dev_kit_fetch_failure_marks_vendored_fallback_not_installed_ref(
    monkeypatch, tmp_path, restore_installer_state
):
    prefix = tmp_path / "prefix"
    vendored = tmp_path / "vendored"
    (vendored / "fallback-skill").mkdir(parents=True)

    class Result:
        returncode = 1
        stdout = ""
        stderr = "offline"

    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install, "_ASSETS_SKILLS", str(vendored))
    monkeypatch.setattr(install, "AI_DEV_KIT_REF", "v1.2.3")
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: Result())

    install._install_skills()

    assert install.status()["steps"]["skills"]["status"] == "complete"
    manifest = install.status()["release_manifest"]["ai_dev_kit"]
    assert manifest["expected"] == "v1.2.3"
    assert manifest["actual"] is None
    assert manifest["match"] is False
    assert manifest["source"] == "vendored_fallback"
    assert manifest["resolved_commit"] is None


def test_ai_dev_kit_valid_persistent_stamp_skips_clone(
    monkeypatch, tmp_path, restore_installer_state
):
    prefix = tmp_path / "prefix"
    vendored = tmp_path / "vendored"
    upstream = prefix / "ai-dev-kit" / "databricks-skills" / "kit-skill"
    installed = prefix / "skills" / "kit-skill"
    upstream.mkdir(parents=True)
    installed.mkdir(parents=True)
    vendored.mkdir()
    (upstream / "SKILL.md").write_text("verified")
    (installed / "SKILL.md").write_text("verified")
    commit = "b" * 40
    checksum = install._directory_checksum(prefix / "ai-dev-kit" / "databricks-skills")
    stamp = {
        "repo": install.AI_DEV_KIT_REPO,
        "ref": "v2.0.0",
        "resolved_commit": commit,
        "content_checksum": checksum,
        "artifact_sha256": restore_installer_state.ai_dev_kit["sha256"],
    }
    (prefix / "ai-dev-kit.install.json").write_text(json.dumps(stamp))
    calls = []

    class Result:
        returncode = 0
        stdout = f"{commit}\n"
        stderr = ""

    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install, "_ASSETS_SKILLS", str(vendored))
    monkeypatch.setattr(install, "AI_DEV_KIT_REF", "v2.0.0")
    restore_installer_state.ai_dev_kit.update({
        "commit": commit,
        "content_sha256": checksum,
    })
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda argv, **kwargs: calls.append(argv) or Result(),
    )

    install._install_skills()

    assert not any(argv[:2] == ["git", "clone"] for argv in calls)
    manifest = install.status()["release_manifest"]["ai_dev_kit"]
    assert manifest["match"] is True
    assert manifest["source"] == "prewarmed"
    assert manifest["resolved_commit"] == commit
    assert manifest["checksum"] == checksum


def test_ai_dev_kit_tampered_persistent_content_forces_reclone(
    monkeypatch, tmp_path, restore_installer_state
):
    prefix = tmp_path / "prefix"
    vendored = tmp_path / "vendored"
    upstream = prefix / "ai-dev-kit" / "databricks-skills" / "kit-skill"
    installed = prefix / "skills" / "kit-skill"
    upstream.mkdir(parents=True)
    installed.mkdir(parents=True)
    vendored.mkdir()
    (upstream / "SKILL.md").write_text("expected")
    (installed / "SKILL.md").write_text("tampered")
    commit = "c" * 40
    checksum = install._directory_checksum(prefix / "ai-dev-kit" / "databricks-skills")
    (prefix / "ai-dev-kit.install.json").write_text(json.dumps({
        "repo": install.AI_DEV_KIT_REPO,
        "ref": "v2.0.0",
        "resolved_commit": commit,
        "content_checksum": checksum,
        "artifact_sha256": restore_installer_state.ai_dev_kit["sha256"],
    }))
    calls = []

    class Result:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["git", "clone"]:
            clone = prefix / "ai-dev-kit"
            (clone / "databricks-skills" / "kit-skill").mkdir(parents=True)
            (clone / "databricks-skills" / "kit-skill" / "SKILL.md").write_text(
                "fresh"
            )
            restore_installer_state.ai_dev_kit["content_sha256"] = (
                install._directory_checksum(clone / "databricks-skills")
            )
        return Result(f"{commit}\n" if "rev-parse" in argv else "")

    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install, "_ASSETS_SKILLS", str(vendored))
    monkeypatch.setattr(install, "AI_DEV_KIT_REF", "v2.0.0")
    restore_installer_state.ai_dev_kit["commit"] = commit
    monkeypatch.setattr(install.subprocess, "run", fake_run)

    install._install_skills()

    assert any(argv[:2] == ["git", "clone"] for argv in calls)
    assert (installed / "SKILL.md").read_text() == "fresh"
    assert install.status()["steps"]["skills"]["source"] == "network"


def test_prewarm_status_proves_reusable_disk_content_without_runtime_state(
    monkeypatch, tmp_path, restore_installer_state
):
    prefix = tmp_path / "prefix"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    expected_versions = {
        "node": install.NODE_VERSION,
        "claude": install.CLAUDE_VERSION,
        "codex": install.CODEX_VERSION,
        "databricks": install.DATABRICKS_CLI_VERSION,
        "omnigent": install.OMNIGENT_VERSION,
    }
    for name in (*expected_versions, "tmux"):
        (bin_dir / name).write_bytes(f"{name}-binary".encode())
    native_entry = restore_installer_state.entry(
        "codex_native_package_linux_x64"
    )
    native = (
            prefix / "lib/node_modules/@openai/codex/node_modules"
            / "@openai/codex-linux-x64"
            / "vendor/x86_64-unknown-linux-musl/bin/codex"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"reviewed-native")
    tmux_checksum = install._file_checksum(bin_dir / "tmux")
    (prefix / "tmux.install.json").write_text(json.dumps({
        "archive_sha256": install.TMUX_STATIC_SHA256,
        "binary_sha256": tmux_checksum,
    }))

    upstream = prefix / "ai-dev-kit" / "databricks-skills" / "kit-skill"
    installed = prefix / "skills" / "kit-skill"
    upstream.mkdir(parents=True)
    installed.mkdir(parents=True)
    (upstream / "SKILL.md").write_text("same")
    (installed / "SKILL.md").write_text("same")
    commit = "d" * 40
    skills_checksum = install._directory_checksum(
        prefix / "ai-dev-kit" / "databricks-skills"
    )
    (prefix / "ai-dev-kit.install.json").write_text(json.dumps({
        "repo": install.AI_DEV_KIT_REPO,
        "ref": "v2.0.0",
        "resolved_commit": commit,
        "content_checksum": skills_checksum,
        "artifact_sha256": restore_installer_state.ai_dev_kit["sha256"],
    }))

    class Result:
        returncode = 0
        stdout = f"{commit}\n"
        stderr = ""

    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install.config, "omnigent_enabled", lambda: True)
    monkeypatch.setattr(install, "AI_DEV_KIT_REF", "v2.0.0")
    restore_installer_state.ai_dev_kit.update({
        "commit": commit,
        "content_sha256": skills_checksum,
    })
    for name, artifact_name in {
        "node": (
            "node_linux_arm64"
            if install.platform.machine().lower() in {"aarch64", "arm64"}
            else "node_linux_x64"
        ),
        "claude": "claude_installer",
        "databricks": "databricks_cli_archive_linux_x64",
    }.items():
        artifact = restore_installer_state.entry(artifact_name)
        checksum = install._file_checksum(bin_dir / name)
        (prefix / f"{name}.install.json").write_text(json.dumps({
            "artifact_sha256": artifact["sha256"],
            "binary_sha256": checksum,
        }))
    launcher_entry = restore_installer_state.entry(
        "codex_npm_launcher_package"
    )
    launcher_root = prefix / "lib/node_modules/@openai/codex"
    alias_root = (
        launcher_root / "node_modules/@openai/codex-linux-x64"
    )
    (launcher_root / "package.json").write_bytes(b"launcher")
    (prefix / "codex.install.json").write_text(json.dumps({
        "launcher_package_sha256": launcher_entry["sha256"],
        "native_package_sha256": native_entry["sha256"],
        "launcher_sha256": install._file_checksum(bin_dir / "codex"),
        "native_sha256": install._file_checksum(native),
        "native_relative_path": os.path.relpath(native, prefix),
        "launcher_tree_sha256": install._directory_checksum(launcher_root),
        "native_tree_sha256": install._directory_checksum(alias_root),
        "launcher_tree_relative_path": os.path.relpath(launcher_root, prefix),
        "native_tree_relative_path": os.path.relpath(alias_root, prefix),
    }))
    omnigent_entries = {
        name: restore_installer_state.entry(name)
        for name in (
            "uv_binary",
            "python_3_12_runtime",
            "omnigent_wheelhouse",
            "omnigent_lock",
        )
    }
    venv_site = prefix / "omnigent-venv/lib/python3.12/site-packages/omnigent.py"
    venv_site.parent.mkdir(parents=True)
    venv_site.write_bytes(b"omnigent")
    venv_python = prefix / "omnigent-venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"python")
    (prefix / "omnigent.install.json").write_text(json.dumps({
        "uv_sha256": omnigent_entries["uv_binary"]["sha256"],
        "python_runtime_sha256": omnigent_entries["python_3_12_runtime"]["content_sha256"],
        "lock_sha256": omnigent_entries["omnigent_lock"]["sha256"],
        "wheelhouse_sha256": omnigent_entries["omnigent_wheelhouse"]["content_sha256"],
        "binary_sha256": install._file_checksum(bin_dir / "omnigent"),
        "venv_sha256": install._directory_checksum(prefix / "omnigent-venv"),
    }))
    monkeypatch.setattr(
        install,
        "_read_cli_version",
        lambda path: expected_versions.get(__import__("os").path.basename(path)),
    )
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: Result())
    with install._state_lock:
        install._state["skills"] = {"status": "error", "source": "network"}

    proof = install.prewarm_status()

    assert [
        name
        for name, entry in proof["manifest"]["binaries"].items()
        if not entry["reusable"]
    ] == []
    assert proof["manifest"]["ai_dev_kit"]["reusable"] is True
    assert proof["reusable"] is True
    assert proof["manifest"]["ai_dev_kit"] == {
        "expected_ref": "v2.0.0",
        "actual_ref": "v2.0.0",
        "resolved_commit": commit,
        "expected_checksum": skills_checksum,
        "actual_checksum": skills_checksum,
            "source": "persistent",
        "reusable": True,
    }
    assert proof["manifest"]["expected_binaries"] == [
        "claude",
        "codex",
        "databricks",
        "node",
        "omnigent",
        "tmux",
    ]
    assert all(
        entry["reusable"]
        for entry in proof["manifest"]["binaries"].values()
    )
    assert all(
        entry["source"] == "persistent"
        and entry["expected"]
        and entry["actual"]
        and entry["actual"] == entry["expected"]
        and entry["actual_checksum"]
        for entry in proof["manifest"]["binaries"].values()
    )


def test_prewarm_status_fails_closed_for_tampered_skills(monkeypatch, tmp_path):
    prefix = tmp_path / "prefix"
    upstream = prefix / "ai-dev-kit" / "databricks-skills" / "kit-skill"
    installed = prefix / "skills" / "kit-skill"
    upstream.mkdir(parents=True)
    installed.mkdir(parents=True)
    (upstream / "SKILL.md").write_text("reviewed")
    (installed / "SKILL.md").write_text("tampered")
    checksum = install._directory_checksum(
        prefix / "ai-dev-kit" / "databricks-skills"
    )
    (prefix / "ai-dev-kit.install.json").write_text(json.dumps({
        "repo": install.AI_DEV_KIT_REPO,
        "ref": "v2.0.0",
        "resolved_commit": "e" * 40,
        "content_checksum": checksum,
    }))
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install.config, "omnigent_enabled", lambda: False)
    monkeypatch.setattr(install, "AI_DEV_KIT_REF", "v2.0.0")

    proof = install.prewarm_status()

    assert proof["reusable"] is False
    assert proof["manifest"]["ai_dev_kit"]["reusable"] is False


def test_prewarm_status_fails_closed_when_disk_verification_errors(
    monkeypatch, tmp_path
):
    prefix = tmp_path / "prefix"
    upstream = prefix / "ai-dev-kit" / "databricks-skills" / "kit-skill"
    installed = prefix / "skills" / "kit-skill"
    upstream.mkdir(parents=True)
    installed.mkdir(parents=True)
    (upstream / "SKILL.md").write_text("same")
    (installed / "SKILL.md").write_text("same")
    checksum = install._directory_checksum(
        prefix / "ai-dev-kit" / "databricks-skills"
    )
    (prefix / "ai-dev-kit.install.json").write_text(json.dumps({
        "repo": install.AI_DEV_KIT_REPO,
        "ref": "v2.0.0",
        "resolved_commit": "e" * 40,
        "content_checksum": checksum,
    }))
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install.config, "omnigent_enabled", lambda: False)
    monkeypatch.setattr(install, "AI_DEV_KIT_REF", "v2.0.0")
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            install.subprocess.TimeoutExpired("git", 30)
        ),
    )

    proof = install.prewarm_status()

    assert proof["reusable"] is False
    assert proof["manifest"]["ai_dev_kit"]["reusable"] is False


def test_bootstrap_file_lock_serializes_processes(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(shared))
    env = os.environ.copy()
    env["DATA_ROOT"] = str(tmp_path)
    code = (
        "from server.bootstrap import install\n"
        "print('attempting', flush=True)\n"
        "with install._install_file_lock(exclusive=True):\n"
        "    print('child-acquired', flush=True)\n"
    )
    with install._install_file_lock(exclusive=True):
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=os.getcwd(),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "attempting"
        readable, _, _ = select.select([process.stdout], [], [], 0.2)
        assert readable == []

    stdout, stderr = process.communicate(timeout=2)
    assert stdout.strip() == "child-acquired"
    assert stderr == ""
    assert process.returncode == 0


def test_atomic_stamp_writes_use_unique_temp_files(monkeypatch, tmp_path):
    target = tmp_path / "stamp.json"
    replaced_from = []
    real_replace = os.replace
    barrier = threading.Barrier(2)

    def tracked_replace(source, destination):
        barrier.wait(timeout=2)
        replaced_from.append(source)
        real_replace(source, destination)

    monkeypatch.setattr(install.os, "replace", tracked_replace)
    errors = []

    def write(value):
        try:
            install._write_json_atomic(str(target), {"value": value})
        except Exception as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    threads = [threading.Thread(target=write, args=(value,)) for value in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert len(set(replaced_from)) == 2
    assert json.loads(target.read_text()) in ({"value": 1}, {"value": 2})
    assert list(tmp_path.glob(".stamp.json.*.tmp")) == []


def test_background_bootstrap_returns_before_waiting_for_install_lock(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(tmp_path))
    monkeypatch.setattr(install.config, "omnigent_enabled", lambda: False)
    started = threading.Event()
    release = threading.Event()

    class BlockingLock:
        def __enter__(self):
            started.set()
            release.wait(timeout=2)

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        install,
        "_install_file_lock",
        lambda **kwargs: BlockingLock(),
    )
    completed = threading.Event()
    monkeypatch.setattr(install, "_install_node", lambda: completed.set())
    monkeypatch.setattr(install, "_install_claude", lambda: None)
    monkeypatch.setattr(install, "_install_codex", lambda: None)
    monkeypatch.setattr(install, "_install_databricks_cli", lambda: None)
    monkeypatch.setattr(install, "_install_skills", lambda: None)

    install.run_in_background()

    assert started.wait(timeout=1)
    assert completed.is_set() is False
    release.set()
    assert completed.wait(timeout=1)


@pytest.mark.parametrize(
    ("installer_name", "step"),
    [
        ("_install_node", "node"),
        ("_install_claude", "claude"),
        ("_install_codex", "codex"),
        ("_install_tmux", "tmux"),
        ("_install_omnigent", "omnigent"),
        ("_install_databricks_cli", "databricks"),
        ("_install_skills", "skills"),
    ],
)
def test_every_installer_records_artifact_validation_failure(
    monkeypatch, installer_name, step
):
    from server.bootstrap.artifacts import ArtifactManifestError

    monkeypatch.setattr(
        install,
        "_verified_artifact",
        lambda _name: (_ for _ in ()).throw(
            ArtifactManifestError(f"{step} reviewed artifact missing")
        ),
    )
    monkeypatch.setattr(
        install,
        "_artifact_contract",
        lambda: (_ for _ in ()).throw(
            ArtifactManifestError(f"{step} reviewed artifact missing")
        ),
    )

    getattr(install, installer_name)()

    status = install.status()["steps"][step]
    assert status["status"] == "error"
    assert f"{step} reviewed artifact missing" in status["error"]
    assert status["started_at"] is not None


def test_parallel_installer_runner_consumes_future_exceptions():
    def explode():
        raise RuntimeError("worker exploded")

    install._set("codex", "pending")

    install._run_parallel_installers([("codex", explode)], max_workers=1)

    status = install.status()["steps"]["codex"]
    assert status["status"] == "error"
    assert status["error"] == "worker exploded"


def test_failed_skills_refresh_never_exposes_mixed_installed_content(
    monkeypatch, tmp_path, restore_installer_state
):
    prefix = tmp_path / "prefix"
    vendored = tmp_path / "vendored"
    vendored.mkdir()
    installed = prefix / "skills" / "kit-skill"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("old-complete")

    class Result:
        returncode = 0
        stdout = f"{'f' * 40}\n"
        stderr = ""

    real_copytree = install.shutil.copytree

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            upstream = prefix / "ai-dev-kit" / "databricks-skills" / "kit-skill"
            upstream.mkdir(parents=True)
            (upstream / "SKILL.md").write_text("new")
            restore_installer_state.ai_dev_kit.update({
                "commit": "f" * 40,
                "content_sha256": install._directory_checksum(
                    prefix / "ai-dev-kit" / "databricks-skills"
                ),
            })
        return Result()

    def fail_new_overlay(source, target, *args, **kwargs):
        if "databricks-skills" in str(source):
            raise OSError("copy interrupted")
        return real_copytree(source, target, *args, **kwargs)

    monkeypatch.setattr(install.config, "shared_prefix", lambda: str(prefix))
    monkeypatch.setattr(install, "_ASSETS_SKILLS", str(vendored))
    monkeypatch.setattr(install, "AI_DEV_KIT_REF", "v2.0.0")
    monkeypatch.setattr(install.subprocess, "run", fake_run)
    monkeypatch.setattr(install.shutil, "copytree", fail_new_overlay)

    install._install_skills()

    assert (installed / "SKILL.md").read_text() == "old-complete"
    assert install.status()["release_manifest"]["ai_dev_kit"]["match"] is False
