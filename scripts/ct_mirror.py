#!/usr/bin/env python3
"""Stage, verify and prune the Workshop Terminal toolchain mirror.

Control Tower runs this as the **deployer service principal** -- the only
principal with write access to the mirror volume -- from the same checkout it is
about to deploy. It lives in this repo rather than Control Tower's because
``assets/artifacts/manifest.json`` is repo-owned: the thing that stages has to be
version-locked to the thing that verifies.

    ct_mirror.py verify --volume /Volumes/main/central/toolchain [--deep]
    ct_mirror.py stage  --volume /Volumes/main/central/toolchain
    ct_mirror.py resync --volume /Volumes/main/central/toolchain [--prune]
    ct_mirror.py prune  --volume /Volumes/main/central/toolchain

Like ``ct_verify.py``, every run prints exactly one JSON object on stdout and
exits ``0`` on success, ``1`` on drift or failure and ``2`` on invalid input, so
Control Tower can drive it straight from an operator button and render the
result. Bearer tokens are never printed.

Blobs are content-addressed and **flat** at ``{volume}/{sha256}``. Flat rather
than sharded into ``{sha256[:2]}/`` subdirectories: at roughly a dozen objects,
sharding would turn the Validate check from one list call into a walk over up to
256 prefixes. Content-addressing is also what removes any need to coordinate
releases with Control Tower -- a bumped pin is simply a key that is not there
yet, so that one artifact falls back to the internet instead of matching a stale
file and failing its checksum.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit
from urllib.request import urlopen

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from server.bootstrap.artifacts import load_manifest  # noqa: E402

SCHEMA_VERSION = 1
INDEX_NAME = "index.json"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1024 * 1024


class MirrorError(RuntimeError):
    """Invalid input -- exit code 2."""


# -- manifest ---------------------------------------------------------------


def mirrorable_artifacts(manifest_path: str = "") -> dict[str, dict]:
    """Manifest entries the mirror can serve, keyed by artifact name.

    Only fetched, checksummed files: the skills repository is a git clone
    provenanced by commit rather than a blob digest, and the installer scripts
    and Omnigent lock already ship inside the image.
    """
    artifacts = load_manifest(manifest_path)["artifacts"]
    return {
        name: entry
        for name, entry in sorted(artifacts.items())
        if urlsplit(str(entry.get("source") or "")).scheme == "https"
        and _SHA256.fullmatch(str(entry.get("sha256") or ""))
    }


def _repo_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", REPO_ROOT, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


# -- volume addressing ------------------------------------------------------


def parse_volume(volume_path: str) -> tuple[str, str, str, str]:
    """``(normalised path, catalog, schema, volume)`` for a ``/Volumes`` address."""
    path = str(volume_path or "").rstrip("/")
    parts = path.split("/")
    if not path.startswith("/Volumes/") or len(parts) != 5 or not all(parts[2:5]):
        raise MirrorError(
            "--volume must be an absolute /Volumes/<catalog>/<schema>/<volume> "
            f"path, got {volume_path!r}"
        )
    return path, parts[2], parts[3], parts[4]


# -- Files API helpers ------------------------------------------------------


def _client():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def _list_blobs(client, volume_path: str) -> dict[str, int | None]:
    """One list call: every object in the volume root, mapped to its byte size."""
    blobs: dict[str, int | None] = {}
    for entry in client.files.list_directory_contents(volume_path):
        if getattr(entry, "is_directory", False):
            continue
        name = str(getattr(entry, "name", "") or "").strip("/")
        if not name:
            name = str(getattr(entry, "path", "")).rstrip("/").rsplit("/", 1)[-1]
        if name:
            blobs[name] = getattr(entry, "file_size", None)
    return blobs


def _download_bytes(client, remote: str) -> bytes:
    response = client.files.download(remote)
    contents = getattr(response, "contents", response)
    try:
        return contents.read()
    finally:
        close = getattr(contents, "close", None)
        if callable(close):
            close()


def _read_index(client, volume_path: str) -> dict:
    try:
        payload = json.loads(
            _download_bytes(client, f"{volume_path}/{INDEX_NAME}").decode("utf-8")
        )
    except Exception:  # noqa: BLE001 - an absent or corrupt index is not fatal
        return {}
    return payload if isinstance(payload, dict) else {}


def _index_sizes(index: dict) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for entry in index.get("artifacts", []) or []:
        if not isinstance(entry, dict):
            continue
        sha = str(entry.get("sha256") or "")
        size = entry.get("size")
        if _SHA256.fullmatch(sha) and isinstance(size, int) and size >= 0:
            sizes[sha] = size
    return sizes


# -- staging ----------------------------------------------------------------


def _fetch_verified(entry: dict, name: str) -> bytes:
    """Download from upstream and refuse anything that is not the pinned bytes."""
    source = str(entry["source"])
    buffer = io.BytesIO()
    digest = hashlib.sha256()
    with urlopen(source, timeout=300) as response:
        for chunk in iter(lambda: response.read(_CHUNK), b""):
            buffer.write(chunk)
            digest.update(chunk)
    if digest.hexdigest() != entry["sha256"]:
        raise MirrorError(f"upstream checksum mismatch for {name}: {source}")
    return buffer.getvalue()


def _is_retryable(error: BaseException) -> bool:
    """Whether another attempt could plausibly succeed.

    A checksum mismatch could not -- the bytes upstream are wrong, or the pin
    is, and hammering the URL turns a clear failure into a slow one. Everything
    else on a long multi-hundred-megabyte run is assumed transient, because the
    realistic causes (a reset connection, a 5xx, a timeout) all are.
    """
    return not isinstance(error, MirrorError)


def _fetch_with_retry(entry: dict, name: str, *, attempts: int, progress) -> bytes:
    delay = 2.0
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return _fetch_verified(entry, name)
        except Exception as error:  # noqa: BLE001 - classify, then re-raise or wait
            if attempt >= attempts or not _is_retryable(error):
                raise
            progress(
                f"  {name}: {type(error).__name__} on attempt {attempt}/{attempts}, "
                f"retrying in {delay:.0f}s"
            )
            time.sleep(delay)
            delay *= 2
    raise MirrorError(f"retries exhausted for {name}")  # pragma: no cover - unreachable


def stage(
    client,
    volume_path: str,
    *,
    manifest_path: str = "",
    overwrite: bool = False,
    prune_after: bool = False,
    operation: str = "stage",
    jobs: int = 4,
    attempts: int = 3,
    progress=None,
) -> dict:
    """Upload every current-manifest blob that is not already present.

    Idempotent by construction: keys are checksums, so a re-run after a pin bump
    uploads only the new artifact and skips the rest. ``overwrite`` re-fetches
    and rewrites everything regardless, which is what the Force resync button
    needs when a blob is suspected corrupt.

    A failed artifact never aborts the batch: the run reports which ones failed
    and exits non-zero, and because the keys are checksums, re-running after
    fixing the cause uploads only what is still missing.
    """
    path, _, _, _ = parse_volume(volume_path)
    artifacts = mirrorable_artifacts(manifest_path)
    present = _list_blobs(client, path)
    note = progress or (lambda _message: None)

    results = []
    index_entries = []
    failures = []
    uploaded = skipped = 0
    pending = []
    for name, entry in sorted(artifacts.items()):
        sha = str(entry["sha256"])
        if not overwrite and sha in present:
            skipped += 1
            results.append(
                {
                    "artifact": name,
                    "sha256": sha,
                    "action": "present",
                    "size": present.get(sha),
                }
            )
            index_entries.append(
                {
                    "artifact": name,
                    "sha256": sha,
                    "size": present.get(sha),
                    "version": str(entry.get("version") or ""),
                }
            )
            continue
        pending.append((name, entry))

    note(
        f"{len(artifacts)} mirrorable artifacts: {skipped} already present, "
        f"{len(pending)} to fetch"
    )

    def transfer(item):
        name, entry = item
        payload = _fetch_with_retry(entry, name, attempts=attempts, progress=note)
        client.files.upload(
            f"{path}/{entry['sha256']}", io.BytesIO(payload), overwrite=True
        )
        return name, entry, len(payload)

    if pending:
        lock = threading.Lock()
        done = 0
        # Concurrent because these are independent downloads from unrelated hosts
        # and the run is otherwise several minutes of mostly-idle waiting. Modest
        # by default: the point is to overlap latency, not to hammer npm.
        workers = max(1, min(int(jobs), len(pending)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stage") as pool:
            futures = {pool.submit(transfer, item): item[0] for item in pending}
            for future in as_completed(futures):
                name = futures[future]
                with lock:
                    done += 1
                    position = f"[{done}/{len(pending)}]"
                try:
                    name, entry, size = future.result()
                except Exception as error:  # noqa: BLE001 - report, do not abort
                    sha = str(artifacts[name]["sha256"])
                    note(f"{position} FAILED {name}: {error}")
                    with lock:
                        failures.append(
                            {"artifact": name, "sha256": sha, "error": str(error)}
                        )
                        results.append(
                            {"artifact": name, "sha256": sha, "action": "failed"}
                        )
                    continue
                note(f"{position} staged {name} ({size / 1048576:.1f} MiB)")
                with lock:
                    uploaded += 1
                    results.append(
                        {
                            "artifact": name,
                            "sha256": str(entry["sha256"]),
                            "action": "uploaded",
                            "size": size,
                        }
                    )
                    index_entries.append(
                        {
                            "artifact": name,
                            "sha256": str(entry["sha256"]),
                            "size": size,
                            "version": str(entry.get("version") or ""),
                        }
                    )
    results.sort(key=lambda item: item["artifact"])

    pruned: list[str] = []
    prune_error = None
    if prune_after and not failures:
        try:
            pruned = _prune_blobs(client, path, set(artifacts_shas(artifacts)))
        except Exception as error:  # noqa: BLE001
            prune_error = str(error)

    index = {
        "schema_version": SCHEMA_VERSION,
        "staged_at": time.time(),
        "wt_commit": _repo_commit(),
        "manifest_artifact_count": len(artifacts),
        # False when some artifact could not be staged. The index still lists the
        # ones that made it, because it only ever describes blobs observed on the
        # volume -- withholding it after a partial run would leave those blobs
        # unsized, quietly costing verify its truncation check for as long as the
        # upstream failure persists.
        "complete": not failures,
        "artifacts": sorted(index_entries, key=lambda item: item["artifact"]),
    }
    index_error = None
    try:
        client.files.upload(
            f"{path}/{INDEX_NAME}",
            io.BytesIO(json.dumps(index, sort_keys=True, indent=2).encode()),
            overwrite=True,
        )
    except Exception as error:  # noqa: BLE001
        index_error = str(error)

    ok = not failures and index_error is None and prune_error is None
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "status": "staged" if ok else "failed",
        "exit_code": 0 if ok else 1,
        "volume": path,
        "wt_commit": index["wt_commit"],
        "uploaded": uploaded,
        "skipped": skipped,
        "pruned": pruned,
        "artifacts": results,
        "failures": failures,
        "index_error": index_error,
        "prune_error": prune_error,
    }


def artifacts_shas(artifacts: dict[str, dict]) -> list[str]:
    return [str(entry["sha256"]) for entry in artifacts.values()]


def _prune_blobs(client, path: str, keep: set[str]) -> list[str]:
    removed = []
    for name in sorted(_list_blobs(client, path)):
        if name == INDEX_NAME or name in keep:
            continue
        if not _SHA256.fullmatch(name):
            # Not something this tool wrote; leaving it alone is cheaper than
            # being wrong about someone else's file.
            continue
        client.files.delete(f"{path}/{name}")
        removed.append(name)
    return removed


def prune(client, volume_path: str, *, manifest_path: str = "") -> dict:
    """Delete blobs the current manifest no longer references.

    Storage hygiene, not correctness: a stale blob is never read, because
    lookups are keyed by the checksum the running release expects. Worth roughly
    430 MiB per superseded release.
    """
    path, _, _, _ = parse_volume(volume_path)
    artifacts = mirrorable_artifacts(manifest_path)
    try:
        removed = _prune_blobs(client, path, set(artifacts_shas(artifacts)))
    except Exception as error:  # noqa: BLE001
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": "prune",
            "status": "failed",
            "exit_code": 1,
            "volume": path,
            "pruned": [],
            "error": str(error),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "prune",
        "status": "pruned",
        "exit_code": 0,
        "volume": path,
        "pruned": removed,
        "error": None,
    }


# -- verification -----------------------------------------------------------


def _grant_report(client, catalog: str, schema: str, full_name: str, group: str) -> dict:
    """Effective privileges ``group`` holds on the volume, inheritance included.

    One ``get_effective`` call rather than three ``get`` calls, because
    ``USE_CATALOG`` and ``USE_SCHEMA`` on the parents are exactly what effective
    privileges already account for.
    """
    report = {
        "reader_group": group,
        "checked": False,
        "ok": False,
        "privileges": [],
        "error": None,
    }
    if not group:
        report["error"] = "no --reader-group given, so the grant was not checked"
        return report
    report["checked"] = True
    try:
        # The literal "VOLUME", not SecurableType.VOLUME: the SDK serialises that
        # enum as "SECURABLETYPE.VOLUME", which Unity Catalog rejects.
        effective = client.grants.get_effective(
            securable_type="VOLUME",
            full_name=full_name,
            principal=group,
        )
    except Exception as error:  # noqa: BLE001
        report["error"] = str(error)
        return report
    privileges = set()
    for assignment in getattr(effective, "privilege_assignments", None) or []:
        for privilege in getattr(assignment, "privileges", None) or []:
            value = getattr(privilege, "privilege", privilege)
            privileges.add(str(getattr(value, "value", value)))
    report["privileges"] = sorted(privileges)
    report["ok"] = "READ_VOLUME" in privileges or "ALL_PRIVILEGES" in privileges
    if not report["ok"]:
        report["error"] = (
            f"{group} does not hold READ_VOLUME on {full_name} "
            "(check USE_CATALOG / USE_SCHEMA on the parents)"
        )
    return report


def verify(
    client,
    volume_path: str,
    *,
    manifest_path: str = "",
    reader_group: str = "",
    deep: bool = False,
) -> dict:
    """Confirm the volume can serve the current manifest. Fast by default.

    The fast path downloads no artifact bytes: one list call establishes both
    presence and size, and size is compared against the sidecar index. That
    catches the realistic corruption mode, an interrupted upload leaving a
    partial object under the right key.

    It cannot catch a blob of exactly the right length with wrong contents --
    but nothing needs it to. Every artifact's sha256 is verified against the
    repo-owned manifest at boot, on the mirror path exactly as on the internet
    path, so a corrupt mirror can only ever cost a fallback. ``--deep`` re-hashes
    everything and is a manual diagnostic, not what the button calls.
    """
    path, catalog, schema, volume = parse_volume(volume_path)
    full_name = f"{catalog}.{schema}.{volume}"
    artifacts = mirrorable_artifacts(manifest_path)

    volume_ok = True
    volume_error = None
    try:
        client.volumes.read(full_name)
    except Exception as error:  # noqa: BLE001
        volume_ok = False
        volume_error = str(error)

    try:
        present = _list_blobs(client, path)
    except Exception as error:  # noqa: BLE001
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": "verify",
            "status": "unreachable",
            "exit_code": 1,
            "volume": path,
            "securable": full_name,
            "deep": deep,
            "volume_ok": volume_ok,
            "volume_error": volume_error or str(error),
            "error": str(error),
            "artifacts": [],
            "missing": [],
            "grants": _grant_report(client, catalog, schema, full_name, reader_group),
        }

    index = _read_index(client, path)
    expected_sizes = _index_sizes(index)

    results = []
    missing = []
    corrupt = []
    unsized: list[str] = []
    for name, entry in artifacts.items():
        sha = str(entry["sha256"])
        item = {
            "artifact": name,
            "version": str(entry.get("version") or ""),
            "sha256": sha,
            "present": sha in present,
            "size": present.get(sha),
            "expected_size": expected_sizes.get(sha),
            "ok": False,
            "detail": None,
        }
        if sha not in present:
            item["detail"] = "not staged"
            missing.append(name)
        elif (
            item["expected_size"] is not None
            and item["size"] is not None
            and item["size"] != item["expected_size"]
        ):
            item["detail"] = (
                f"size {item['size']} does not match the staged "
                f"{item['expected_size']} — likely a truncated upload"
            )
            corrupt.append(name)
        elif item["present"] and item["expected_size"] is None and not deep:
            # Present, but with nothing to size it against. Passing is right --
            # boot re-hashes every artifact regardless -- but saying so keeps the
            # operator from reading a green Validate as more assurance than it is.
            item["ok"] = True
            item["detail"] = "present; not size-checked (no staged size on record)"
            unsized.append(name)
        elif deep:
            digest = hashlib.sha256()
            try:
                digest.update(_download_bytes(client, f"{path}/{sha}"))
            except Exception as error:  # noqa: BLE001
                item["detail"] = str(error)
                corrupt.append(name)
            else:
                if digest.hexdigest() == sha:
                    item["ok"] = True
                else:
                    item["detail"] = "content does not hash to its key"
                    corrupt.append(name)
        else:
            item["ok"] = True
        results.append(item)

    grants = _grant_report(client, catalog, schema, full_name, reader_group)
    ok = bool(volume_ok and not missing and not corrupt and (grants["ok"] or not reader_group))
    if missing or corrupt:
        status = "drift"
    elif not volume_ok or (reader_group and not grants["ok"]):
        status = "misconfigured"
    else:
        status = "current"
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "verify",
        "status": status,
        "exit_code": 0 if ok else 1,
        "volume": path,
        "securable": full_name,
        "deep": deep,
        "volume_ok": volume_ok,
        "volume_error": volume_error,
        "error": None,
        # Which checkout produced what is on the volume, so a failure reads as
        # "three artifacts behind, staged from commit abc123" instead of a bare
        # pass or fail.
        "staged_from_commit": index.get("wt_commit"),
        "staged_at": index.get("staged_at"),
        "index_present": bool(index),
        # False after a partial stage: some blob upstream refused to download, so
        # the volume is short and the sidecar says so rather than implying a full
        # set. Distinct from a missing index, which means no stage ever finished.
        "index_complete": bool(index.get("complete")) if index else None,
        "unsized": unsized,
        "expected_from_commit": _repo_commit(),
        "artifact_count": len(artifacts),
        "artifacts": results,
        "missing": missing,
        "corrupt": corrupt,
        "grants": grants,
    }


# -- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("verify", "stage", "resync", "prune")
    )
    parser.add_argument("--volume", required=True)
    parser.add_argument(
        "--manifest",
        default="",
        help="optional source-override manifest, as ARTIFACT_MANIFEST_PATH",
    )
    parser.add_argument(
        "--reader-group",
        default="",
        help="group that must hold READ_VOLUME, checked by verify",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="re-download and re-hash every blob (minutes; manual diagnostic only)",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="with resync, also delete blobs the current manifest does not name",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="concurrent artifact transfers during stage/resync (default 4)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="tries per artifact before giving up on it (default 3). A checksum "
             "mismatch is never retried",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress per-artifact progress. stdout is the JSON report either "
             "way; progress goes to stderr",
    )
    args = parser.parse_args(argv)

    # stderr, so a caller parsing stdout still gets exactly one JSON object while
    # a human running a multi-minute stage can see that it is alive.
    def progress(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr, flush=True)

    try:
        parse_volume(args.volume)
        client = _client()
        if args.command == "verify":
            report = verify(
                client,
                args.volume,
                manifest_path=args.manifest,
                reader_group=args.reader_group,
                deep=args.deep,
            )
        elif args.command == "stage":
            report = stage(
                client,
                args.volume,
                manifest_path=args.manifest,
                jobs=args.jobs,
                attempts=args.attempts,
                progress=progress,
            )
        elif args.command == "resync":
            report = stage(
                client,
                args.volume,
                manifest_path=args.manifest,
                overwrite=True,
                prune_after=args.prune,
                operation="resync",
                jobs=args.jobs,
                attempts=args.attempts,
                progress=progress,
            )
        else:
            report = prune(client, args.volume, manifest_path=args.manifest)
    except MirrorError as error:
        report = {
            "schema_version": SCHEMA_VERSION,
            "operation": args.command,
            "status": "invalid_input",
            "exit_code": 2,
            "error": str(error),
        }
    except Exception as error:  # noqa: BLE001 - a button needs JSON, not a traceback
        report = {
            "schema_version": SCHEMA_VERSION,
            "operation": args.command,
            "status": "failed",
            "exit_code": 1,
            "error": f"{type(error).__name__}: {error}",
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    _print_human_summary(report, progress)
    return int(report["exit_code"])


def _print_human_summary(report: dict, note) -> None:
    """A one-line verdict on stderr, so an operator need not read the JSON."""
    status = report.get("status")
    note(f"{report.get('operation')}: {status}")
    for failure in report.get("failures") or []:
        note(f"  failed  {failure['artifact']}: {failure['error']}")
    for name in report.get("missing") or []:
        note(f"  missing {name}")
    for name in report.get("corrupt") or []:
        note(f"  corrupt {name}")
    if report.get("error"):
        note(f"  error   {report['error']}")
    if status in {"failed", "drift"}:
        note("re-run to pick up only what is still missing; keys are checksums")


if __name__ == "__main__":
    sys.exit(main())
