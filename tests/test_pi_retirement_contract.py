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
    if path == ROOT / "server" / "bootstrap" / "install.py":
        start = source.index("def _remove_retired_pi_install()")
        end = source.index("\ndef _ready_from", start)
        migration = source[start:end]
        # These are the only retired package names allowed in runtime code.
        assert migration.count('"pi"') == 1
        assert migration.count('"pi-coding-agent"') == 1
        assert migration.count('"pi.install.json"') == 1
        return source[:start] + source[end:]
    if path == ROOT / "server" / "users.py":
        start = source.index("def _remove_retired_binary_links(")
        end = source.index("\n    def _write_databricks_cli_wrapper", start)
        migration = source[start:end]
        # User-home cleanup is quarantined here so the broader runtime scan
        # still rejects every selectable or installable harness reference.
        assert migration.count('"pi"') == 1
        return source[:start] + source[end:]
    return source


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


def test_returning_attendee_loses_retired_home_launcher(monkeypatch, tmp_path):
    from server import config
    from server.users import User

    shared = tmp_path / "shared"
    shared_bin = shared / "bin"
    shared_bin.mkdir(parents=True)
    retired_source = shared_bin / "pi"
    retired_source.write_text("retired")
    (shared_bin / "codex").write_text("supported")
    monkeypatch.setattr(config, "shared_prefix", lambda: str(shared))
    monkeypatch.setattr(config, "users_root", lambda: str(tmp_path / "users"))

    user = User("returning@example.com")
    local_bin = Path(user.home) / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (local_bin / "pi").symlink_to(retired_source)

    user.bootstrap_home()

    assert not (local_bin / "pi").exists()
    assert not (local_bin / "pi").is_symlink()
    assert (local_bin / "codex").is_symlink()
