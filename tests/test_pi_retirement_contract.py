import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_RETIRED_HARNESS = re.compile(
    r"(?<![A-Za-z0-9_])pi(?![A-Za-z0-9_])", re.IGNORECASE
)
_TEXT_SUFFIXES = {
    ".json",
    ".lock",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}


def _production_files():
    explicit = (
        ROOT / "app.yaml",
        ROOT / "content" / "default_pack.json",
    )
    roots = (
        ROOT / "assets" / "artifacts",
        ROOT / "deploy",
        ROOT / "frontend" / "src",
        ROOT / "scripts",
        ROOT / "server",
    )
    yield from explicit
    for root in roots:
        yield from (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix in _TEXT_SUFFIXES
        )


def _without_retirement_migration(path: Path, source: str) -> str:
    if path != ROOT / "server" / "bootstrap" / "install.py":
        return source
    start = source.index("def _remove_retired_pi_install()")
    end = source.index("\ndef _ready_from", start)
    migration = source[start:end]
    # These are the only retired names allowed in runtime code. They exist
    # solely to remove files left in the persistent prefix by an older release.
    assert migration.count('"pi"') == 1
    assert migration.count('"pi-coding-agent"') == 1
    assert migration.count('"pi.install.json"') == 1
    return source[:start] + source[end:]


def test_retired_pi_harness_cannot_return_to_runtime_or_artifacts():
    offenders = []
    for path in _production_files():
        source = _without_retirement_migration(path, path.read_text())
        if _RETIRED_HARNESS.search(source):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_retired_pi_release_contract_cannot_return():
    source = "\n".join(path.read_text() for path in _production_files())

    for retired_name in (
        "PI_CLI_VERSION",
        "pi_npm_package",
        "@earendil-works/pi-coding-agent",
    ):
        # The scoped npm package is intentionally reconstructed from path
        # segments in the one-time cleanup, so its installable spelling must
        # never return to source, manifests, locks, or deployment configuration.
        assert retired_name not in source
