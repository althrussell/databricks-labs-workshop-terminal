"""Operation-scoped work reduction keeps source/currency and deep-integrity gates."""
import hashlib
import os
from pathlib import Path

import pytest

from server import cli_config, config, model_policy
from server.bootstrap import install
from server.bootstrap.proof import FileDigestProof
from server.capture_journal import CaptureHeldError, CaptureStorageError
from server.users import User
from .test_capture_startup import WorkspaceFiles, capture_modules  # noqa: F401
from .test_model_policy_capture import attach, another_store, request
from .test_installer_versions import restore_installer_state, _lay_down_prewarmed_prefix  # noqa: F401


class CountedFiles(WorkspaceFiles):
    def __init__(self):
        super().__init__()
        self.list_calls = 0

    def list(self, path):
        self.list_calls += 1
        return super().list(path)


def test_policy_config_operation_reuses_one_source_snapshot_and_rechecks_at_completion(monkeypatch, tmp_path):
    files = CountedFiles()
    attach(files)
    model_policy.store.apply(request(7))
    monkeypatch.setenv("DATABRICKS_GATEWAY_HOST", "https://test.cloud.databricks.com/ai-gateway")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    user = User("labuser+199@awsbricks.com")
    user.bootstrap_home()
    files.list_calls = 0
    cli_config.configure_all(user, "test-token")
    assert files.list_calls == 2
    files.list_calls = 0
    cli_config.refresh_model_configs(user)
    assert files.list_calls == 2
    # Independent reads/operations must see the remote journal every time.
    model_policy.store.snapshot()
    model_policy.store.snapshot()
    assert files.list_calls == 4


def test_concurrent_retained_policy_change_invalidates_operation_and_is_not_cached():
    files = CountedFiles()
    attach(files)
    model_policy.store.apply(request(7))
    other, _ = another_store(files)
    with pytest.raises(model_policy.ModelPolicyConfigurationError, match="changed during"):
        with model_policy.store.read_operation() as snapshot:
            assert snapshot.revision == 7
            assert model_policy.store.snapshot().revision == 7
            other.apply(request(8))
    assert model_policy.store.snapshot().revision == 8


def test_source_rebinding_invalidates_operation_even_with_identical_policy():
    files = CountedFiles()
    attach(files)
    model_policy.store.apply(request(7))
    other, journal = another_store(files)
    assert other.snapshot().revision == 7
    with pytest.raises(model_policy.ModelPolicyConfigurationError, match="changed during"):
        with model_policy.store.read_operation():
            model_policy.store.configure_capture(journal)
            model_policy.store.snapshot()
    assert model_policy.store.snapshot().revision == 7


def test_sticky_journal_fault_is_not_hidden_by_a_policy_operation():
    files = CountedFiles()
    retained = attach(files)
    model_policy.store.apply(request(7))
    with pytest.raises(CaptureHeldError, match="retained integrity failed"):
        with model_policy.store.read_operation():
            retained.journal.mark_held("retained integrity failed")
            model_policy.store.snapshot()


def test_racing_revision_clears_cli_ready_and_next_operation_rebuilds(monkeypatch, tmp_path):
    files = CountedFiles()
    attach(files)
    model_policy.store.apply(request(7))
    other, _ = another_store(files)
    monkeypatch.setenv("DATABRICKS_GATEWAY_HOST", "https://test.cloud.databricks.com/ai-gateway")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    user = User("labuser+199@awsbricks.com")
    user.bootstrap_home()
    cli_config.configure_all(user, "test-token")
    original = cli_config.configure_codex
    def race(*args, **kwargs):
        original(*args, **kwargs)
        other.apply(request(8))
    monkeypatch.setattr(cli_config, "configure_codex", race)
    with pytest.raises(model_policy.ModelPolicyConfigurationError, match="changed during"):
        cli_config.refresh_model_configs(user)
    assert user.cli_ready == set()
    monkeypatch.setattr(cli_config, "configure_codex", original)
    cli_config.configure_all(user, "test-token")
    assert "claude" in user.cli_ready and model_policy.store.snapshot().revision == 8


def test_final_storage_read_failure_clears_ready_and_next_operation_rebuilds(monkeypatch, tmp_path):
    files = CountedFiles()
    attach(files)
    model_policy.store.apply(request(7))
    monkeypatch.setenv("DATABRICKS_GATEWAY_HOST", "https://test.cloud.databricks.com/ai-gateway")
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))
    user = User("labuser+199@awsbricks.com")
    user.bootstrap_home()
    original = files.list
    files.list_calls = 0

    def fail_final_read(path):
        if files.list_calls == 1:
            raise OSError("temporary retained-store outage")
        return original(path)

    monkeypatch.setattr(files, "list", fail_final_read)
    with pytest.raises(CaptureStorageError, match="listing failed"):
        cli_config.configure_all(user, "test-token")
    assert user.cli_ready == set()
    monkeypatch.setattr(files, "list", original)
    cli_config.configure_all(user, "test-token")
    assert "claude" in user.cli_ready


def test_file_digest_reuse_is_per_operation_and_final_generation_checked(tmp_path):
    path = tmp_path / "binary"
    path.write_bytes(b"reviewed-binary")
    reads = []
    def hash_file(name):
        reads.append(name)
        return hashlib.sha256(Path(name).read_bytes()).hexdigest()
    proof = FileDigestProof(hash_file)
    first = proof.checksum(path)
    assert proof.checksum(path) == first
    assert len(reads) == 1 and proof.unchanged()
    assert FileDigestProof(hash_file).checksum(path) == first
    assert len(reads) == 2  # independent deep proof always re-reads bytes
    path.write_bytes(b"tampered-binary")
    assert not proof.unchanged()
    assert proof.checksum(path) == ""


def test_same_size_and_restored_mtime_cannot_reuse_old_digest(tmp_path):
    path = tmp_path / "binary"
    path.write_bytes(b"first")
    original = path.stat()
    proof = FileDigestProof(lambda name: hashlib.sha256(Path(name).read_bytes()).hexdigest())
    assert proof.checksum(path)
    path.write_bytes(b"other")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert proof.checksum(path) == "" and not proof.unchanged()


def test_symlink_retarget_and_mutation_during_hash_fail_closed(tmp_path):
    first, second, link = (tmp_path / name for name in ("one", "two", "link"))
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    link.symlink_to(first)
    proof = FileDigestProof(lambda name: hashlib.sha256(Path(name).read_bytes()).hexdigest())
    assert proof.checksum(link)
    link.unlink()
    link.symlink_to(second)
    assert proof.checksum(link) == "" and not proof.unchanged()
    def mutate(name):
        Path(name).write_bytes(b"modified during hash")
        return "a" * 64
    changed = FileDigestProof(mutate)
    assert changed.checksum(first) == "" and not changed.unchanged()


def test_deep_proof_hashes_each_binary_once_and_repeats_on_next_probe(monkeypatch, tmp_path, restore_installer_state):
    _lay_down_prewarmed_prefix(monkeypatch, tmp_path, restore_installer_state)
    names = ["node_linux_x64", "node_linux_arm64", "claude_installer", "claude_binary",
             "codex_npm_launcher_package", "codex_native_package_linux_x64", "databricks_cli_archive_linux_x64",
             "uv_binary", "python_3_12_runtime", "omnigent_lock", "tmux_linux_x64", "databricks_agent_skills"]
    entries = {name: restore_installer_state.entry(name) for name in names}
    monkeypatch.setattr(restore_installer_state, "entry", lambda name: entries[name])
    original = install._file_checksum
    reads = {}
    def counted(path):
        name = os.path.abspath(path)
        reads[name] = reads.get(name, 0) + 1
        return original(path)
    monkeypatch.setattr(install, "_file_checksum", counted)
    assert install.prewarm_status()["reusable"] is True
    prefix = Path(config.shared_prefix())
    for name in ["node", "claude", "codex", "databricks", "omnigent", "tmux"]:
        assert reads[str(prefix / "bin" / name)] == 1
    assert install.prewarm_status()["reusable"] is True
    for name in ["node", "claude", "codex", "databricks", "omnigent", "tmux"]:
        assert reads[str(prefix / "bin" / name)] == 2
