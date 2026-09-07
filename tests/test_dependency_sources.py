"""Hosted release locks must never depend on laptop-only package proxies."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESOLUTION_LOCKS = (
    ROOT / "uv.lock",
    ROOT / "deploy" / "omnigent-app" / "uv.lock",
    ROOT / "frontend" / "package-lock.json",
)
LOCKS = RESOLUTION_LOCKS + tuple(
    sorted((ROOT / "assets" / "artifacts").glob("omnigent-*.lock"))
)
INTERNAL_HOSTS = (
    "pypi-proxy.dev.databricks.com",
    "npm-proxy.dev.databricks.com",
)


def test_hosted_locks_use_only_public_package_sources():
    for path in LOCKS:
        text = path.read_text(encoding="utf-8")
        assert not any(host in text for host in INTERNAL_HOSTS), path

    for path in RESOLUTION_LOCKS[:2]:
        text = path.read_text(encoding="utf-8")
        assert any(
            registry in text
            for registry in (
                'registry = "https://pypi.org/simple"',
                'registry = "https://pypi.org/simple/"',
            )
        ), path
        assert "https://files.pythonhosted.org/packages/" in text, path

    npm_lock = RESOLUTION_LOCKS[2].read_text(encoding="utf-8")
    assert "https://registry.npmjs.org/" in npm_lock
