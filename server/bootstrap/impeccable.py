"""Install upstream Impeccable with its supported installer and reviewed inputs.

The npm launcher, native engine and compiled skill bundle are separate releases.
The local-bundle override is supported upstream; it prevents an event boot from
silently downloading today's latest skills. Native binaries stay out of project
copies and Git, while the upstream skill launcher can find the shared engine on
PATH even in an isolated harness home/worktree.

Each harness gets its own native payload (W1). The installer writes a Claude
skill plus companion agents and a separate Codex skill; they differ in 36 files
and invocation syntax. WT used to keep only the Codex copy and link it into
Claude's home too. Both are now retained and verified against reviewed digests,
and ``routing.json`` records which path and digest serves which provider. Codex
keeps the historic ``impeccable`` path. The Claude payload sits under
``.providers/``, which has no ``SKILL.md``, so it is never linked as a skill of
its own.
"""
import json
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

#: Where each provider's payload lives inside a published skills tree.
PROVIDER_PATHS = {"codex": "impeccable", "claude": ".providers/claude/impeccable"}
#: Claude's companion agents (finish reviewer, documenter, ...), which Claude
#: discovers in an ``agents`` directory rather than inside the skill.
CLAUDE_AGENTS = ".providers/claude/agents"
ROUTING = ".providers/routing.json"
#: Manifest digests per payload. Codex keeps the established key.
PAYLOAD_DIGESTS = {"codex": "content_sha256", "claude": "claude_content_sha256",
                   "claude_agents": "claude_agents_sha256"}


def routing(tree: str | Path) -> dict:
    """The provider routing a published tree claims, or ``{}`` for a legacy tree."""
    try:
        data = json.loads((Path(tree) / ROUTING).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("providers"), dict) else {}


def payload_path(tree: str | Path, provider: str) -> Path | None:
    """The verified payload for ``provider`` in ``tree``; None when it has none.

    Never answers with the other provider's payload: a tree without a Claude
    payload has no Claude Impeccable, which callers report rather than paper over.
    """
    entry = routing(tree).get("providers", {}).get(provider)
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        return None
    path = Path(tree) / entry["path"]
    return path if (path / "SKILL.md").is_file() or provider == "claude_agents" and path.is_dir() else None


def verify_tree(tree: str | Path) -> dict[str, str]:
    """Each routed payload's actual digest; raises when one differs from routing."""
    found = {}
    for provider, entry in routing(tree).get("providers", {}).items():
        path = Path(tree) / entry["path"]
        actual = directory_checksum(path) if path.is_dir() else ""
        if actual != entry.get("sha256"):
            raise RuntimeError(f"Impeccable {provider} payload differs from its routed digest")
        found[provider] = actual
    return found


def write_routing(tree: Path, digests: dict[str, str]) -> None:
    providers = {
        "codex": {"path": PROVIDER_PATHS["codex"], "sha256": digests["codex"]},
        "claude": {"path": PROVIDER_PATHS["claude"], "sha256": digests["claude"]},
    }
    if "claude_agents" in digests:
        providers["claude_agents"] = {"path": CLAUDE_AGENTS, "sha256": digests["claude_agents"]}
    (tree / ROUTING).parent.mkdir(parents=True, exist_ok=True)
    temporary = tree / (ROUTING + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "skill_version": SKILL_VERSION, "providers": providers},
                                    indent=2, sort_keys=True) + "\n")
    temporary.replace(tree / ROUTING)


def _expected(entry: dict, provider: str) -> str:
    key = PAYLOAD_DIGESTS[provider]
    value = entry.get(key)
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimeError(f"Impeccable manifest has no reviewed {provider} digest ({key})")
    return value


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
        installed = {"codex": project / ".agents/skills/impeccable",
                     "claude": project / ".claude/skills/impeccable"}
        digests = {}
        for provider, path in installed.items():
            shutil.rmtree(path / "scripts/bin", ignore_errors=True)
            digests[provider] = directory_checksum(path)
            if digests[provider] != _expected(bundle_entry, provider):
                raise RuntimeError(f"Impeccable installed {provider} skill differs from the reviewed bundle")
        agents = project / ".claude/agents"
        companions = sorted(agents.glob("impeccable-*.md")) if agents.is_dir() else []
        if companions or PAYLOAD_DIGESTS["claude_agents"] in bundle_entry:
            staged_agents = root / "claude-agents"
            staged_agents.mkdir()
            for path in companions:
                shutil.copyfile(path, staged_agents / path.name)
            digests["claude_agents"] = directory_checksum(staged_agents)
            if digests["claude_agents"] != _expected(bundle_entry, "claude_agents"):
                raise RuntimeError("Impeccable Claude companion agents differ from the reviewed bundle")
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
        publish_payloads(Path(target), {
            "codex": installed["codex"], "claude": installed["claude"],
            **({"claude_agents": root / "claude-agents"} if "claude_agents" in digests else {}),
        }, digests)


def publish_payloads(target: Path, sources: dict[str, Path], digests: dict[str, str]) -> None:
    """Replace every provider payload, then the routing that names them.

    Routing is written last, so a reader never sees routing that points at a
    payload it does not describe; until then the previous routing still matches
    nothing and readers fall back to reporting the payload as unverified.
    """
    (target / ROUTING).unlink(missing_ok=True)
    paths = {"codex": PROVIDER_PATHS["codex"], "claude": PROVIDER_PATHS["claude"], "claude_agents": CLAUDE_AGENTS}
    for provider, source in sources.items():
        destination = target / paths[provider]
        shutil.rmtree(destination, ignore_errors=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    write_routing(target, digests)
