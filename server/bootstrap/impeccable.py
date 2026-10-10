"""Install upstream Impeccable with its supported installer and reviewed inputs.

The npm launcher, native engine and compiled skill bundle are separate releases.
The local-bundle override is supported upstream; it prevents an event boot from
silently downloading today's latest skills. Native binaries stay out of project
copies and Git, while the upstream skill launcher can find the shared engine on
PATH even in an isolated harness home/worktree.
"""
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

from .artifacts import directory_checksum

LAUNCHER_VERSION = "4.1.0"
ENGINE_VERSION = "0.1.14"
SKILL_VERSION = "4.5.2"
ARTIFACTS = {
    "impeccable_npm_launcher": LAUNCHER_VERSION,
    "impeccable_engine_linux_x64": ENGINE_VERSION,
    "impeccable_skill_bundle": SKILL_VERSION,
}


def install_skill(target: str, prefix: str, fetch, env: dict, *, platform: str = "linux-x64") -> None:
    inputs = {}
    for name, version in ARTIFACTS.items():
        path, entry = fetch(name)
        if entry["version"] != version:
            raise RuntimeError(f"{name}: configured and reviewed versions differ")
        inputs[name] = (Path(path), entry)
    with tempfile.TemporaryDirectory(prefix=".impeccable-", dir=prefix) as temporary:
        root = Path(temporary)
        launcher_archive, _ = inputs["impeccable_npm_launcher"]
        with tarfile.open(launcher_archive) as archive:
            archive.extractall(root / "launcher", filter="data")
        bundle_archive, bundle_entry = inputs["impeccable_skill_bundle"]
        bundle = root / "bundle"
        with zipfile.ZipFile(bundle_archive) as archive:
            for member in archive.infolist():
                resolved = (bundle / member.filename).resolve()
                if not resolved.is_relative_to(bundle.resolve()):
                    raise RuntimeError("Impeccable bundle contains an escaping path")
            archive.extractall(bundle)
        engine, _ = inputs["impeccable_engine_linux_x64"]
        # Prestage the verified engine where the supported installer expects it.
        # This avoids its otherwise separate network fetch of that same binary.
        for provider in (".claude", ".agents"):
            skill = bundle / provider / "skills/impeccable"
            if (skill / "scripts/VERSION").read_text().strip() != ENGINE_VERSION:
                raise RuntimeError("Impeccable skill bundle expects a different engine")
            binary = skill / "scripts/bin" / platform / "impeccable"
            binary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(engine, binary)
            binary.chmod(0o755)
        project = root / "project"
        project.mkdir()
        engine_run = root / "engine"
        shutil.copyfile(engine, engine_run)
        engine_run.chmod(0o755)
        install_env = {**env, "IMPECCABLE_BIN": str(engine_run),
                       "IMPECCABLE_BUNDLE_PATH": str(bundle)}
        launcher = root / "launcher/package/cli/bin/cli.js"
        result = subprocess.run(
            [str(Path(prefix) / "bin/node"), str(launcher), "install",
             "--providers=claude,codex", "--scope=project", "--no-hooks", "--yes"],
            cwd=project, env=install_env, capture_output=True, text=True, timeout=120,
        )
        if result.returncode:
            raise RuntimeError(f"Impeccable installer failed: {result.stderr[-500:]}")
        installed = project / ".agents/skills/impeccable"
        shutil.rmtree(installed / "scripts/bin", ignore_errors=True)
        if directory_checksum(installed) != bundle_entry["content_sha256"]:
            raise RuntimeError("Impeccable installed skill differs from the reviewed bundle")
        runtime = Path(prefix) / "impeccable"
        runtime.mkdir(exist_ok=True)
        # Retain the reviewed npm launcher and engine. No dependency lifecycle
        # script, optional npm engine package, or unpinned download runs here.
        shutil.copytree(root / "launcher/package", runtime / "launcher", dirs_exist_ok=True)
        shutil.copyfile(engine, runtime / "engine")
        (runtime / "engine").chmod(0o755)
        wrapper = Path(prefix) / "bin/impeccable"
        wrapper.write_text(
            "#!/bin/sh\nexport IMPECCABLE_BIN=" + shlex.quote(str(runtime / "engine"))
            + "\nexec " + shlex.quote(str(Path(prefix) / "bin/node")) + " "
            + shlex.quote(str(runtime / "launcher/cli/bin/cli.js")) + ' "$@"\n'
        )
        wrapper.chmod(0o755)
        destination = Path(target) / "impeccable"
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(installed, destination)
