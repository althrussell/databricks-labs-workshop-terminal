"""Contract and behaviour tests for the optional UC Volume artifact mirror.

The property every one of these protects is the same: a mirror that cannot serve
an artifact must cost a *slow* boot, never a broken or unverified one -- and it
must never do so silently.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
import yaml

from scripts import ct_mirror
from server import config as runtime_config
from server.bootstrap import artifacts as artifacts_module
from server.bootstrap import mirror as mirror_module
from server.bootstrap.artifacts import ArtifactManifest, ArtifactManifestError


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b"reviewed toolchain bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


# -- fakes ------------------------------------------------------------------


class FakeFiles:
    def __init__(self, blobs: dict[str, bytes] | None = None, error=None):
        self.blobs = dict(blobs or {})
        self.error = error
        self.downloads: list[str] = []
        self.uploads: list[str] = []
        self.deletes: list[str] = []
        self.lists: list[str] = []

    def download(self, path):
        self.downloads.append(path)
        if self.error is not None:
            raise self.error
        if path not in self.blobs:
            raise FileNotFoundError(f"404 {path} does not exist")
        return type("Response", (), {"contents": io.BytesIO(self.blobs[path])})()

    def upload(self, path, contents, overwrite=False):
        self.uploads.append(path)
        self.blobs[path] = contents.read()

    def delete(self, path):
        self.deletes.append(path)
        self.blobs.pop(path, None)

    def list_directory_contents(self, path):
        self.lists.append(path)
        prefix = path.rstrip("/") + "/"
        for key, value in sorted(self.blobs.items()):
            if key.startswith(prefix) and "/" not in key[len(prefix):]:
                yield type(
                    "DirectoryEntry",
                    (),
                    {
                        "name": key[len(prefix):],
                        "path": key,
                        "is_directory": False,
                        "file_size": len(value),
                    },
                )()


class FakeClient:
    def __init__(self, files: FakeFiles):
        self.files = files


def _manifest(tmp_path: Path, source: str, *, digest: str = DIGEST) -> ArtifactManifest:
    """A one-artifact contract, so the tests do not depend on real pins."""
    return ArtifactManifest(
        str(tmp_path / "manifest.json"),
        {
            "artifacts": {
                "widget": {
                    "version": "1.0.0",
                    "source": source,
                    "sha256": digest,
                }
            },
            "source": "default",
        },
    )


def _mirror(files: FakeFiles, *, strict: bool = False, **kwargs):
    return mirror_module.ToolchainMirror(
        "/Volumes/main/central/toolchain",
        FakeClient(files),
        strict=strict,
        sleep=lambda _seconds: None,
        **kwargs,
    )


# -- app.yaml contract ------------------------------------------------------


def _app_env() -> dict:
    config = yaml.safe_load((ROOT / "app.yaml").read_text())
    return {item["name"]: item.get("value", "") for item in config["env"]}


def test_app_yaml_ships_the_mirror_disabled_so_the_change_lands_inert():
    """Empty means download from source, which is exactly today's behaviour.

    This is what lets the WT and Control Tower changes ship in either order.
    """
    env = _app_env()

    assert env["WORKSHOP_TOOLCHAIN_MIRROR_PATH"] == ""
    assert env["WORKSHOP_TOOLCHAIN_MIRROR_STRICT"] == "false"


def test_mirror_path_is_patched_in_place_rather_than_bound_as_a_resource():
    """``valueFrom`` would make the volume mandatory and kill the dual model."""
    config = yaml.safe_load((ROOT / "app.yaml").read_text())
    entry = next(
        item for item in config["env"]
        if item["name"] == "WORKSHOP_TOOLCHAIN_MIRROR_PATH"
    )

    assert "valueFrom" not in entry


# -- config validation ------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "/tmp/toolchain", "Volumes/main/c/v", "/Volumes/main", "/Volumes/a/b"],
)
def test_a_path_that_cannot_name_a_volume_disables_the_mirror(monkeypatch, raw):
    monkeypatch.setenv("WORKSHOP_TOOLCHAIN_MIRROR_PATH", raw)

    assert runtime_config.toolchain_mirror_path() == ""


def test_a_valid_volume_path_is_accepted_and_trailing_slashes_are_dropped(monkeypatch):
    monkeypatch.setenv("WORKSHOP_TOOLCHAIN_MIRROR_PATH", "/Volumes/main/central/tc/")

    assert runtime_config.toolchain_mirror_path() == "/Volumes/main/central/tc"


def test_a_rejected_path_is_reported_rather_than_looking_unconfigured(monkeypatch):
    """The two must never be indistinguishable.

    A typo that silently disables the mirror is the failure this feature is
    supposed to prevent, so it has to survive normalisation to empty.
    """
    monkeypatch.setenv("WORKSHOP_TOOLCHAIN_MIRROR_PATH", "/tmp/not-a-volume")
    monkeypatch.delenv("WORKSHOP_TOOLCHAIN_MIRROR_STRICT", raising=False)

    status = mirror_module.configuration_status()

    assert status["configured"] is True
    assert status["path"] == ""
    assert "absolute /Volumes" in status["error"]


def test_no_mirror_configured_reports_no_error(monkeypatch):
    monkeypatch.delenv("WORKSHOP_TOOLCHAIN_MIRROR_PATH", raising=False)

    status = mirror_module.configuration_status()

    assert status == {
        "configured": False,
        "path": "",
        "strict": False,
        "error": None,
    }


def test_strict_mode_refuses_to_start_on_a_malformed_path(monkeypatch):
    monkeypatch.setenv("WORKSHOP_TOOLCHAIN_MIRROR_PATH", "/tmp/not-a-volume")
    monkeypatch.setenv("WORKSHOP_TOOLCHAIN_MIRROR_STRICT", "true")

    with pytest.raises(mirror_module.ToolchainMirrorError):
        mirror_module.from_environment()


def test_the_mirror_needs_the_scrubbed_singleton_not_an_ambient_client(monkeypatch):
    """``initialize_app_identity`` deletes the client secret from the process.

    An ambient ``WorkspaceClient()`` built in the bootstrap thread therefore has
    nothing left to authenticate with, so a missing singleton must disable the
    mirror rather than produce a client that 401s on every artifact.
    """
    monkeypatch.setenv("WORKSHOP_TOOLCHAIN_MIRROR_PATH", "/Volumes/main/central/tc")
    monkeypatch.delenv("WORKSHOP_TOOLCHAIN_MIRROR_STRICT", raising=False)
    monkeypatch.setattr("server.credentials.workspace_client", lambda: None)

    assert mirror_module.from_environment() is None


# -- fetch behaviour --------------------------------------------------------


def test_a_mirror_hit_is_used_and_reported_as_coming_from_the_volume(tmp_path):
    files = FakeFiles({f"/Volumes/main/central/toolchain/{DIGEST}": PAYLOAD})
    contract = _manifest(tmp_path, "https://example.invalid/widget.tgz")

    fetched = contract.fetch(
        "widget", staging_dir=str(tmp_path / "stage"), mirror=_mirror(files)
    )

    assert fetched.source == "volume"
    assert Path(fetched.path).read_bytes() == PAYLOAD
    assert files.downloads == [f"/Volumes/main/central/toolchain/{DIGEST}"]


def test_an_unstaged_artifact_falls_back_to_the_manifest_source(tmp_path, monkeypatch):
    """A pin the volume has not been re-staged for is simply a key that is not
    there. That artifact takes the internet path; nothing else is affected."""
    files = FakeFiles({})
    monkeypatch.setattr(
        artifacts_module, "urlopen", lambda *a, **k: io.BytesIO(PAYLOAD)
    )
    contract = _manifest(tmp_path, "https://example.invalid/widget.tgz")

    fetched = contract.fetch(
        "widget", staging_dir=str(tmp_path / "stage"), mirror=_mirror(files)
    )

    assert fetched.source == "network"
    assert Path(fetched.path).read_bytes() == PAYLOAD


def test_a_corrupt_blob_falls_back_instead_of_installing_bad_bytes(
    tmp_path, monkeypatch
):
    """The checksum gate is identical on both paths, so the worst a tampered or
    truncated mirror can do is send one app to the internet."""
    files = FakeFiles({f"/Volumes/main/central/toolchain/{DIGEST}": b"tampered"})
    monkeypatch.setattr(
        artifacts_module, "urlopen", lambda *a, **k: io.BytesIO(PAYLOAD)
    )
    contract = _manifest(tmp_path, "https://example.invalid/widget.tgz")

    fetched = contract.fetch(
        "widget", staging_dir=str(tmp_path / "stage"), mirror=_mirror(files)
    )

    assert fetched.source == "network"
    assert Path(fetched.path).read_bytes() == PAYLOAD


def test_a_corrupt_blob_never_survives_under_the_reusable_staged_name(
    tmp_path, monkeypatch
):
    """Verification happens before the rename, so a later boot cannot reuse it."""
    staging = tmp_path / "stage"
    files = FakeFiles({f"/Volumes/main/central/toolchain/{DIGEST}": b"tampered"})
    monkeypatch.setattr(
        artifacts_module, "urlopen", lambda *a, **k: io.BytesIO(PAYLOAD)
    )
    contract = _manifest(tmp_path, "https://example.invalid/widget.tgz")

    contract.fetch("widget", staging_dir=str(staging), mirror=_mirror(files))

    assert not any(path.read_bytes() == b"tampered" for path in staging.iterdir())


def test_strict_mode_fails_closed_rather_than_reaching_the_internet(
    tmp_path, monkeypatch
):
    """Air-gapped events: reaching the internet is itself the failure."""
    monkeypatch.setattr(
        artifacts_module,
        "urlopen",
        lambda *a, **k: pytest.fail("strict mode must not download"),
    )
    contract = _manifest(tmp_path, "https://example.invalid/widget.tgz")

    with pytest.raises(ArtifactManifestError, match="toolchain mirror"):
        contract.fetch(
            "widget",
            staging_dir=str(tmp_path / "stage"),
            mirror=_mirror(FakeFiles({}), strict=True),
        )


def test_without_a_mirror_the_fetch_behaves_exactly_as_it_did_before(
    tmp_path, monkeypatch
):
    files = FakeFiles({})
    monkeypatch.setattr(
        artifacts_module, "urlopen", lambda *a, **k: io.BytesIO(PAYLOAD)
    )
    contract = _manifest(tmp_path, "https://example.invalid/widget.tgz")

    fetched = contract.fetch("widget", staging_dir=str(tmp_path / "stage"))

    assert fetched.source == "network"
    assert files.downloads == []


def test_a_permission_error_is_retried_before_the_grant_is_given_up_on():
    """Control Tower adds each new app SP to the reader group at deploy and boot
    starts moments later, so a 403 can just be a grant still propagating."""
    files = FakeFiles({}, error=PermissionError("403 permission denied"))
    client = _mirror(files, retries=3, backoff=0)

    assert client.fetch(DIGEST, "/dev/null") is False
    assert len(files.downloads) == 3


def test_an_absent_blob_is_not_retried():
    """A miss is an answer, not a transient failure -- retrying just delays boot."""
    files = FakeFiles({})
    client = _mirror(files, retries=3, backoff=0)

    assert client.fetch(DIGEST, "/dev/null") is False
    assert len(files.downloads) == 1


def test_a_staged_copy_is_reused_instead_of_refetched(tmp_path, monkeypatch):
    """Content-addressed staging: restarts stop re-downloading, and the staging
    directory stops growing by a whole toolchain on every boot."""
    staging = tmp_path / "stage"
    files = FakeFiles({f"/Volumes/main/central/toolchain/{DIGEST}": PAYLOAD})
    contract = _manifest(tmp_path, "https://example.invalid/widget.tgz")

    first = contract.fetch("widget", staging_dir=str(staging), mirror=_mirror(files))
    second = contract.fetch("widget", staging_dir=str(staging), mirror=_mirror(files))

    assert first.path == second.path
    assert second.source == "cached"
    assert len(list(staging.iterdir())) == 1


def test_mirror_only_callers_never_silently_reach_the_internet(tmp_path, monkeypatch):
    """The claude binary's fallback is the installer, which downloads the same
    bytes. Fetching them here too would double the step rather than replace it."""
    monkeypatch.setattr(
        artifacts_module,
        "urlopen",
        lambda *a, **k: pytest.fail("allow_network=False must not download"),
    )
    contract = _manifest(tmp_path, "https://example.invalid/widget.tgz")

    with pytest.raises(ArtifactManifestError):
        contract.fetch(
            "widget",
            staging_dir=str(tmp_path / "stage"),
            mirror=_mirror(FakeFiles({})),
            allow_network=False,
        )


# -- provenance reporting ---------------------------------------------------


def test_status_names_the_artifacts_that_still_came_over_the_internet(monkeypatch):
    """A configured mirror that is not being used looks identical to success
    until event day. ``bypassed`` is what makes it visible before then."""
    from server.bootstrap import install

    monkeypatch.setenv("WORKSHOP_TOOLCHAIN_MIRROR_PATH", "/Volumes/main/central/tc")
    monkeypatch.setattr(
        install, "_artifact_sources", {"node_linux_x64": "network", "uv_binary": "volume"}
    )

    report = install.toolchain_mirror_status()

    assert report["configured"] is True
    assert report["bypassed"] is True
    assert report["from_network"] == ["node_linux_x64"]
    assert report["served"] == 1


def test_a_fully_served_boot_is_not_reported_as_bypassed(monkeypatch):
    from server.bootstrap import install

    monkeypatch.setenv("WORKSHOP_TOOLCHAIN_MIRROR_PATH", "/Volumes/main/central/tc")
    monkeypatch.setattr(
        install, "_artifact_sources", {"node_linux_x64": "volume", "uv_binary": "cached"}
    )

    report = install.toolchain_mirror_status()

    assert report["bypassed"] is False
    assert report["from_network"] == []


def test_a_step_reports_the_volume_only_when_every_artifact_came_from_it(monkeypatch):
    from server.bootstrap import install

    monkeypatch.setattr(
        install, "_artifact_sources", {"a": "volume", "b": "network"}
    )

    assert install._install_source("staged", "a") == "volume"
    assert install._install_source("staged", "a", "b") == "staged"
    assert install._install_source("staged") == "staged"


# -- the stager -------------------------------------------------------------


def test_only_checksummed_https_artifacts_are_mirrorable():
    """The skills clone is provenanced by commit, and the installer scripts and
    Omnigent lock already ship in the image -- none are blobs to stage."""
    names = set(ct_mirror.mirrorable_artifacts())

    assert "claude_binary" in names
    assert "databricks_agent_skills" not in names
    assert "omnigent_lock" not in names
    assert "claude_installer" not in names


@pytest.mark.parametrize(
    "bad", ["", "/Volumes/main", "/tmp/toolchain", "/Volumes/a/b/c/d", "relative"]
)
def test_the_stager_rejects_anything_that_is_not_a_volume_path(bad):
    with pytest.raises(ct_mirror.MirrorError):
        ct_mirror.parse_volume(bad)


def test_blobs_are_stored_flat_and_content_addressed():
    """Flat, not sharded: one list call is what keeps Validate to seconds."""
    assert ct_mirror.parse_volume("/Volumes/main/c/v")[0] == "/Volumes/main/c/v"


def test_staging_skips_what_is_already_present_so_a_pin_bump_uploads_one_file(
    tmp_path, monkeypatch
):
    volume = "/Volumes/main/central/toolchain"
    entries = {
        "kept": {"version": "1", "source": "https://x.invalid/a", "sha256": DIGEST},
        "new": {
            "version": "2",
            "source": "https://x.invalid/b",
            "sha256": hashlib.sha256(b"second").hexdigest(),
        },
    }
    monkeypatch.setattr(ct_mirror, "mirrorable_artifacts", lambda _p="": entries)
    monkeypatch.setattr(ct_mirror, "_fetch_verified", lambda entry, name: b"second")
    files = FakeFiles({f"{volume}/{DIGEST}": PAYLOAD})

    report = ct_mirror.stage(FakeClient(files), volume)

    assert report["status"] == "staged"
    assert report["uploaded"] == 1
    assert report["skipped"] == 1
    assert files.uploads == [
        f"{volume}/{entries['new']['sha256']}",
        f"{volume}/index.json",
    ]


def test_a_partial_stage_still_records_sizes_for_what_did_land(tmp_path, monkeypatch):
    """Observed for real: three npm artifacts were unreachable, so the run failed.
    Withholding the index would leave the seven that succeeded unsized, silently
    costing verify its truncation check until the upstream outage cleared."""
    volume = "/Volumes/main/central/toolchain"
    reachable = {"version": "1", "source": "https://x.invalid/a", "sha256": DIGEST}
    blocked = {
        "version": "1",
        "source": "https://x.invalid/b",
        "sha256": hashlib.sha256(b"blocked").hexdigest(),
    }
    monkeypatch.setattr(
        ct_mirror,
        "mirrorable_artifacts",
        lambda _p="": {"reachable": reachable, "blocked": blocked},
    )

    def fetch(entry, name):
        if name == "blocked":
            raise OSError("Connection refused")
        return PAYLOAD

    monkeypatch.setattr(ct_mirror, "_fetch_verified", fetch)
    files = FakeFiles({})

    report = ct_mirror.stage(FakeClient(files), volume)

    assert report["status"] == "failed"
    assert [f["artifact"] for f in report["failures"]] == ["blocked"]
    index = json.loads(files.blobs[f"{volume}/index.json"])
    assert index["complete"] is False
    assert [entry["artifact"] for entry in index["artifacts"]] == ["reachable"]
    assert index["artifacts"][0]["size"] == len(PAYLOAD)


def test_a_blob_with_no_recorded_size_passes_but_says_it_was_not_size_checked(
    monkeypatch,
):
    """A green Validate must not imply more assurance than was actually bought."""
    volume = "/Volumes/main/central/toolchain"
    entries = {"widget": {"version": "1", "source": "https://x/a", "sha256": DIGEST}}
    monkeypatch.setattr(ct_mirror, "mirrorable_artifacts", lambda _p="": entries)
    files = FakeFiles({f"{volume}/{DIGEST}": PAYLOAD})
    client = FakeClient(files)
    client.volumes = type("V", (), {"read": lambda _self, _n: None})()

    report = ct_mirror.verify(client, volume)

    assert report["status"] == "current"
    assert report["index_present"] is False
    assert report["index_complete"] is None
    assert report["unsized"] == ["widget"]
    assert "not size-checked" in report["artifacts"][0]["detail"]


def test_a_transient_network_failure_is_retried_rather_than_losing_the_artifact(
    monkeypatch,
):
    """One reset connection partway through a 430 MiB run should not cost an
    artifact and force a whole re-run."""
    entry = {"version": "1", "source": "https://x.invalid/a", "sha256": DIGEST}
    calls = []

    def flaky(_entry, name):
        calls.append(name)
        if len(calls) < 3:
            raise OSError("Connection reset by peer")
        return PAYLOAD

    monkeypatch.setattr(ct_mirror, "_fetch_verified", flaky)
    monkeypatch.setattr(ct_mirror.time, "sleep", lambda _s: None)

    payload = ct_mirror._fetch_with_retry(
        entry, "widget", attempts=3, progress=lambda _m: None
    )

    assert payload == PAYLOAD
    assert len(calls) == 3


def test_a_checksum_mismatch_is_not_retried(monkeypatch):
    """The bytes upstream are wrong, or the pin is. Neither improves with a
    second attempt, and retrying turns a clear failure into a slow one."""
    entry = {"version": "1", "source": "https://x.invalid/a", "sha256": DIGEST}
    calls = []

    def wrong(_entry, name):
        calls.append(name)
        raise ct_mirror.MirrorError("upstream checksum mismatch for widget")

    monkeypatch.setattr(ct_mirror, "_fetch_verified", wrong)
    monkeypatch.setattr(ct_mirror.time, "sleep", lambda _s: None)

    with pytest.raises(ct_mirror.MirrorError, match="checksum mismatch"):
        ct_mirror._fetch_with_retry(
            entry, "widget", attempts=3, progress=lambda _m: None
        )

    assert len(calls) == 1


def test_one_unreachable_artifact_does_not_abort_the_others(tmp_path, monkeypatch):
    """Exactly the case that hit a real run: npm was blocked, everything else
    was reachable, and the reachable ones had to still land."""
    volume = "/Volumes/main/central/toolchain"
    entries = {
        f"art{index}": {
            "version": "1",
            "source": f"https://x.invalid/{index}",
            "sha256": hashlib.sha256(f"body{index}".encode()).hexdigest(),
        }
        for index in range(5)
    }
    monkeypatch.setattr(ct_mirror, "mirrorable_artifacts", lambda _p="": entries)
    monkeypatch.setattr(ct_mirror.time, "sleep", lambda _s: None)

    def fetch(entry, name):
        if name == "art2":
            raise OSError("Connection refused")
        return f"body{name[-1]}".encode()

    monkeypatch.setattr(ct_mirror, "_fetch_verified", fetch)
    files = FakeFiles({})

    report = ct_mirror.stage(FakeClient(files), volume, jobs=4, attempts=2)

    assert report["status"] == "failed"
    assert report["uploaded"] == 4
    assert [f["artifact"] for f in report["failures"]] == ["art2"]
    # The four reachable blobs are on the volume, so a re-run only fetches one.
    assert sum(1 for key in files.blobs if key != f"{volume}/index.json") == 4


def test_concurrent_staging_reports_every_artifact_exactly_once(monkeypatch):
    """Shared counters and lists are mutated from several worker threads."""
    volume = "/Volumes/main/central/toolchain"
    entries = {
        f"art{index:02d}": {
            "version": "1",
            "source": f"https://x.invalid/{index}",
            "sha256": hashlib.sha256(f"body{index}".encode()).hexdigest(),
        }
        for index in range(24)
    }
    monkeypatch.setattr(ct_mirror, "mirrorable_artifacts", lambda _p="": entries)
    monkeypatch.setattr(
        ct_mirror, "_fetch_verified", lambda entry, name: f"body{int(name[3:])}".encode()
    )
    files = FakeFiles({})

    report = ct_mirror.stage(FakeClient(files), volume, jobs=8)

    assert report["status"] == "staged"
    assert report["uploaded"] == 24
    reported = [item["artifact"] for item in report["artifacts"]]
    assert reported == sorted(entries)
    index = json.loads(files.blobs[f"{volume}/index.json"])
    assert len(index["artifacts"]) == 24


def test_progress_never_contaminates_the_json_report(capsys, monkeypatch):
    """Control Tower parses stdout. Progress has to stay on stderr or the
    button gets a JSON decode error instead of a result."""
    volume = "/Volumes/main/central/toolchain"
    entries = {"widget": {"version": "1", "source": "https://x/a", "sha256": DIGEST}}
    monkeypatch.setattr(ct_mirror, "mirrorable_artifacts", lambda _p="": entries)
    monkeypatch.setattr(ct_mirror, "_fetch_verified", lambda entry, name: PAYLOAD)
    monkeypatch.setattr(ct_mirror, "_client", lambda: FakeClient(FakeFiles({})))

    exit_code = ct_mirror.main(["stage", "--volume", volume])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["status"] == "staged"
    assert "staged widget" in captured.err


def test_quiet_silences_progress_but_not_the_report(capsys, monkeypatch):
    volume = "/Volumes/main/central/toolchain"
    entries = {"widget": {"version": "1", "source": "https://x/a", "sha256": DIGEST}}
    monkeypatch.setattr(ct_mirror, "mirrorable_artifacts", lambda _p="": entries)
    monkeypatch.setattr(ct_mirror, "_fetch_verified", lambda entry, name: PAYLOAD)
    monkeypatch.setattr(ct_mirror, "_client", lambda: FakeClient(FakeFiles({})))

    ct_mirror.main(["stage", "--volume", volume, "--quiet"])
    captured = capsys.readouterr()

    assert json.loads(captured.out)["status"] == "staged"
    assert captured.err == ""


def test_staging_refuses_upstream_bytes_that_do_not_match_the_pin(monkeypatch):
    entry = {"version": "1", "source": "https://x.invalid/a", "sha256": DIGEST}
    monkeypatch.setattr(ct_mirror, "urlopen", lambda *a, **k: io.BytesIO(b"wrong"))

    with pytest.raises(ct_mirror.MirrorError, match="checksum mismatch"):
        ct_mirror._fetch_verified(entry, "widget")


def test_verify_downloads_no_artifact_bytes_on_the_fast_path(monkeypatch):
    volume = "/Volumes/main/central/toolchain"
    entries = {"widget": {"version": "1", "source": "https://x/a", "sha256": DIGEST}}
    monkeypatch.setattr(ct_mirror, "mirrorable_artifacts", lambda _p="": entries)
    index = {
        "schema_version": 1,
        "wt_commit": "abc123",
        "artifacts": [{"artifact": "widget", "sha256": DIGEST, "size": len(PAYLOAD)}],
    }
    files = FakeFiles({
        f"{volume}/{DIGEST}": PAYLOAD,
        f"{volume}/index.json": json.dumps(index).encode(),
    })
    client = FakeClient(files)
    client.volumes = type("V", (), {"read": lambda _self, _n: None})()

    report = ct_mirror.verify(client, volume)

    assert report["status"] == "current"
    assert report["exit_code"] == 0
    assert report["staged_from_commit"] == "abc123"
    # The index, and nothing else. No artifact bytes crossed the wire.
    assert files.downloads == [f"{volume}/index.json"]
    assert len(files.lists) == 1


def test_verify_catches_a_truncated_upload_by_size(monkeypatch):
    """Presence alone is too weak: an interrupted upload leaves a partial object
    under exactly the right key, and size comes back in the same list call."""
    volume = "/Volumes/main/central/toolchain"
    entries = {"widget": {"version": "1", "source": "https://x/a", "sha256": DIGEST}}
    monkeypatch.setattr(ct_mirror, "mirrorable_artifacts", lambda _p="": entries)
    index = {
        "artifacts": [{"artifact": "widget", "sha256": DIGEST, "size": len(PAYLOAD)}]
    }
    files = FakeFiles({
        f"{volume}/{DIGEST}": PAYLOAD[:4],
        f"{volume}/index.json": json.dumps(index).encode(),
    })
    client = FakeClient(files)
    client.volumes = type("V", (), {"read": lambda _self, _n: None})()

    report = ct_mirror.verify(client, volume)

    assert report["status"] == "drift"
    assert report["exit_code"] == 1
    assert report["corrupt"] == ["widget"]


def test_verify_fails_the_gate_when_a_bumped_pin_was_never_staged(monkeypatch):
    """The whole point of the pre-deploy gate: turn a silent event-day slowdown
    into a loud release-time failure."""
    volume = "/Volumes/main/central/toolchain"
    entries = {"widget": {"version": "2", "source": "https://x/a", "sha256": DIGEST}}
    monkeypatch.setattr(ct_mirror, "mirrorable_artifacts", lambda _p="": entries)
    client = FakeClient(FakeFiles({}))
    client.volumes = type("V", (), {"read": lambda _self, _n: None})()

    report = ct_mirror.verify(client, volume)

    assert report["exit_code"] == 1
    assert report["missing"] == ["widget"]


def test_verify_reports_a_reader_group_without_read_volume_as_misconfigured(
    monkeypatch,
):
    volume = "/Volumes/main/central/toolchain"
    monkeypatch.setattr(ct_mirror, "mirrorable_artifacts", lambda _p="": {})
    client = FakeClient(FakeFiles({}))
    client.volumes = type("V", (), {"read": lambda _self, _n: None})()
    client.grants = type(
        "G",
        (),
        {
            "get_effective": lambda _self, **_kw: type(
                "R", (), {"privilege_assignments": []}
            )()
        },
    )()

    report = ct_mirror.verify(client, volume, reader_group="wt_toolchain_readers")

    assert report["status"] == "misconfigured"
    assert report["exit_code"] == 1
    assert "READ_VOLUME" in report["grants"]["error"]


def test_prune_leaves_files_it_did_not_write_alone():
    """Deleting by content address is safe; deleting by guesswork is not."""
    volume = "/Volumes/main/central/toolchain"
    stale = "b" * 64
    files = FakeFiles({
        f"{volume}/{DIGEST}": PAYLOAD,
        f"{volume}/{stale}": b"old release",
        f"{volume}/index.json": b"{}",
        f"{volume}/somebody-elses-file.txt": b"hands off",
    })

    removed = ct_mirror._prune_blobs(FakeClient(files), volume, {DIGEST})

    assert removed == [stale]
    assert f"{volume}/somebody-elses-file.txt" in files.blobs
    assert f"{volume}/index.json" in files.blobs
