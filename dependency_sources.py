#!/usr/bin/env python3
"""Check actual release/upload inputs without resolving packages or changing locks.

Managed laptop transport belongs in command-local settings. Published sources
must use the reviewed public registries even when a laptop mirror supplied the
same hash-pinned bytes locally.
"""
from __future__ import annotations

import argparse
import configparser
from collections.abc import Mapping
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tomllib
from urllib.parse import urlsplit

PUBLIC_PYPI = "https://pypi.org/simple"
PUBLIC_NPM = "https://registry.npmjs.org/"
REQUIRED_INPUTS = (
    "pyproject.toml", "uv.lock", "requirements.in", "requirements.txt",
    "requirements-dev.in", "requirements-dev.txt", "frontend/package.json",
    "frontend/package-lock.json", "app.yaml", "deploy/omnigent-app/pyproject.toml",
    "deploy/omnigent-app/uv.lock", "deploy/omnigent-app/app.yaml",
    "assets/artifacts/manifest.json", "evals/generated_apps/requirements-browser.txt",
)
# These are the existing reviewed toolchain publishers, not alternate indexes.
ARTIFACT_HOSTS = frozenset({"github.com", "raw.githubusercontent.com", "claude.ai", "downloads.claude.ai", "nodejs.org", "registry.npmjs.org"})
CHROME_FOR_TESTING_PREFIX = "/chrome-for-testing-public/"
UBUNTU_ARCHIVE_PREFIX = "/ubuntu/+archive/primary/+files/"
_URL = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REGISTRY_ENV = frozenset({
    "UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_INDEX_PYPI_URL", "UV_EXTRA_INDEX_URL",
    "UV_INDEX", "UV_FIND_LINKS", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS",
    "NPM_CONFIG_REGISTRY", "npm_config_registry", "BUN_CONFIG_REGISTRY",
    "PIP_CONFIG_FILE", "UV_CONFIG_FILE", "NPM_CONFIG_USERCONFIG", "npm_config_userconfig",
})


class DependencySourceError(ValueError):
    """An unreviewed package source would enter a release or upload."""


def _fail(path: str, reason: str) -> None:
    # Never echo URL userinfo or token-bearing values into build logs.
    raise DependencySourceError(f"{path}: {reason}")


def public_url(value: str, *, kind: str) -> bool:
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.username is not None or url.password is not None
                or url.port is not None or url.query or url.fragment or "\\" in value
                or any(ord(character) <= 32 for character in value)):
            return False
        if kind == "python-index":
            return url.hostname == "pypi.org" and url.path.rstrip("/") == "/simple"
        if kind == "python-artifact":
            return url.hostname == "files.pythonhosted.org" and url.path.startswith("/packages/")
        if kind == "npm":
            return url.hostname == "registry.npmjs.org" and url.path.startswith("/")
        if kind == "artifact":
            # Google's official Chrome for Testing bucket only, never all of GCS:
            # the prepared browser agents use to inspect apps (W3).
            if url.hostname == "storage.googleapis.com":
                return url.path.startswith(CHROME_FOR_TESTING_PREFIX)
            # Ubuntu's permanent package archive, for the browser's shared
            # libraries and fonts (the Apps runtime has no package manager).
            if url.hostname == "launchpad.net":
                return url.path.startswith(UBUNTU_ARCHIVE_PREFIX) and url.path.endswith(".deb")
            return url.hostname in ARTIFACT_HOSTS and url.path.startswith("/")
    except (TypeError, ValueError):
        pass
    return False


def _walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _python_requirement(value: str, path: str) -> None:
    if (re.match(r"(?:\.{1,2}/|/|~/)", value)
            or ("@" not in value and not value.startswith("https://")
                and re.match(r"[^;\s]+\.(?:whl|zip|tar\.gz)(?:\s|$)", value))
            or re.match(r"(?!https:)[A-Za-z][A-Za-z0-9+.-]*:", value)):
        _fail(path, "unapproved direct Python requirement")
    if value.lower().startswith("https:") and not public_url(value, kind="python-artifact"):
        _fail(path, "non-public Python artifact")
    if "@" in value:
        target = value.split("@", 1)[1].split(";", 1)[0].strip()
        if not public_url(target, kind="python-artifact"):
            _fail(path, "unapproved direct Python requirement")
    for url in _URL.findall(value):
        if not public_url(url, kind="python-artifact"):
            _fail(path, "non-public Python artifact")
    if re.search(r"(?:git\+|file:|ssh:|git:|^-e\s)", value, re.I):
        _fail(path, "unapproved direct Python requirement")


def _python_lock(text: str, path: str) -> None:
    value = tomllib.loads(text)
    for package in value.get("package", []):
        source = package.get("source", {})
        if (not source or any(key not in {"registry", "virtual", "editable", "directory"} for key in source)
                or any(source.get(key) not in {None, "."} for key in ("virtual", "editable", "directory"))):
            _fail(path, "unapproved Python package source")
        if "registry" in source and not public_url(source["registry"], kind="python-index"):
            _fail(path, "non-public Python registry")
    # Includes every wheel/sdist URL and source metadata rather than merely
    # requiring one public URL next to an otherwise private lock.
    for key, child in _walk(value):
        if key == "url" and not public_url(child, kind="python-artifact"):
            _fail(path, "non-public Python artifact")
        if key in {"git", "path", "directory", "editable"} and child not in (".", None):
            _fail(path, "unapproved Python dependency override")


def _python_project(text: str, path: str, *, config_only: bool = False) -> None:
    value = tomllib.loads(text)
    config = value if config_only else value.get("tool", {}).get("uv", {})
    if config.get("sources") or config.get("find-links"):
        _fail(path, "Python source overrides require review")
    for key, child in _walk(config):
        if key in {"sources", "find-links"} and child:
            _fail(path, "Python source overrides require review")
        if key in {"constraint-dependencies", "override-dependencies", "build-constraint-dependencies", "requires-dist"}:
            for requirement in child:
                if isinstance(requirement, str):
                    _python_requirement(requirement, path)
        if key in {"url", "index-url", "extra-index-url", "default-index"}:
            sources = child if isinstance(child, list) else [child]
            if any(not public_url(source, kind="python-index") for source in sources):
                _fail(path, "non-public Python index")
    project = value.get("project", {})
    requirements = [*project.get("dependencies", []), *value.get("build-system", {}).get("requires", [])]
    requirements.extend(item for group in project.get("optional-dependencies", {}).values() for item in group)
    requirements.extend(item for group in value.get("dependency-groups", {}).values() for item in group if isinstance(item, str))
    for requirement in requirements:
        _python_requirement(requirement, path)


def _requirements(text: str, path: str, files: Mapping[str, bytes], seen: frozenset[str] = frozenset()) -> None:
    if path in seen:
        _fail(path, "cyclic requirements include is not deployable")
    seen = seen | {path}
    # Join hash continuations first; comments (including uv's invocation header)
    # do not select an install source.
    text = re.sub(r"\\\s*\n", " ", text)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("--index-url", "--extra-index-url", "--default-index", "-i ")):
            source = re.split(r"[=\s]+", line, maxsplit=1)[-1]
            if not public_url(source, kind="python-index"):
                _fail(path, "non-public Python index")
        elif line.startswith(("-r", "--requirement", "-c", "--constraint")):
            target = re.sub(r"^(?:--requirement|--constraint|-r|-c)(?:=|\s)*", "", line).strip()
            relative = PurePosixPath(path).parent / target
            if (not target or ":" in target or "\\" in target or relative.is_absolute()
                    or ".." in relative.parts or relative.as_posix() not in files):
                _fail(path, "nested requirements must be present in the validated snapshot")
            # Includes and constraints may use any filename, not just a name
            # that the outer snapshot recognises as a requirements export.
            _requirements(files[relative.as_posix()].decode("utf-8"), relative.as_posix(), files, seen)
        elif line.startswith("-"):
            _fail(path, "unreviewed requirements source option")
        else:
            _python_requirement(line, path)


def _npm_spec(value: str, path: str) -> None:
    if isinstance(value, str) and value.lower().startswith("https:") and not public_url(value, kind="npm"):
        _fail(path, "non-public npm dependency source")
    if (not isinstance(value, str) or re.search(r"(?:git\+|git:|ssh:|file:|link:|workspace:|github:|gitlab:|bitbucket:)", value, re.I)
            or re.match(r"(?:\.{1,2}/|/|~/)", value)
            or re.fullmatch(r"[\w.-]+/[\w.-]+(?:#.*)?", value)
            or re.match(r"(?!https:|npm:)[A-Za-z][A-Za-z0-9+.-]*:", value)):
        _fail(path, "unapproved npm dependency source")
    for url in _URL.findall(value):
        if not public_url(url, kind="npm"):
            _fail(path, "non-public npm dependency source")


def _npm_project(value: dict, path: str) -> None:
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "overrides", "resolutions"):
        for _, child in _walk(value.get(section, {})):
            if isinstance(child, str):
                _npm_spec(child, path)


def _npm_lock(text: str, path: str) -> None:
    value = json.loads(text)
    for key, child in _walk(value):
        if key == "resolved" and not public_url(child, kind="npm"):
            _fail(path, "non-public resolved npm artifact")
        if key == "version" and isinstance(child, str):
            _npm_spec(child, path)
        if key == "link" and child:
            _fail(path, "linked npm package requires review")
    for package in value.get("packages", {}).values():
        _npm_project(package, path)
    _npm_project(value, path)


def _npmrc(text: str, path: str) -> None:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, value = line.partition("=")
        if not separator or not (key == "registry" or key.endswith(":registry")) or value.rstrip("/") != PUBLIC_NPM.rstrip("/"):
            _fail(path, "npm configuration must select public registry only")


def _pip_config(text: str, path: str) -> None:
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(text)
    for section in config.sections():
        for key, value in config.items(section):
            # pip accepts underscores and an optional leading -- in config keys.
            key = key.lower().replace("_", "-").removeprefix("--")
            if key in {"index-url", "extra-index-url"} and not public_url(value, kind="python-index"):
                _fail(path, "non-public Python index")
            if key in {"find-links", "trusted-host"} and value:
                _fail(path, "extra Python sources require review")


def _app_config(text: str, path: str) -> None:
    # Already a frozen WT runtime dependency; parsing YAML also covers flow
    # syntax/aliases used by source upload runners after environment patches.
    import yaml
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        _fail(path, "invalid app configuration")
    if not isinstance(value, dict):
        _fail(path, "invalid app configuration")
    for entry in value.get("env", []):
        name = entry.get("name", "")
        if name not in _REGISTRY_ENV and not (name.startswith("UV_INDEX_") and name.endswith("_URL")):
            continue
        if "valueFrom" in entry:
            _fail(path, "registry settings must be explicit public values")
        source = str(entry.get("value", ""))
        if name in {"PIP_CONFIG_FILE", "UV_CONFIG_FILE", "NPM_CONFIG_USERCONFIG", "npm_config_userconfig"}:
            if source not in {"", "/dev/null"}:
                _fail(path, "deployed package config redirects require review")
        elif name in {"UV_EXTRA_INDEX_URL", "UV_INDEX", "UV_FIND_LINKS", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS"}:
            if source:
                _fail(path, "extra package sources require review")
        elif "REGISTRY" in name.upper():
            if source.rstrip("/") != PUBLIC_NPM.rstrip("/"):
                _fail(path, "deployed npm registry must use the public registry root")
        elif not public_url(source, kind="python-index"):
            _fail(path, "non-public deployed registry setting")


def _artifact_manifest(text: str, path: str) -> None:
    value = json.loads(text)
    for entry in value.get("artifacts", {}).values():
        if not isinstance(entry, dict):
            _fail(path, "invalid artifact entry")
        for key in ("source", "upstream"):
            source = entry.get(key)
            if source is None:
                continue
            if "://" in source:
                if not public_url(source, kind="artifact"):
                    _fail(path, "unapproved toolchain artifact publisher")
            elif key != "source" or "\\" in source or PurePosixPath(source).name != source or source in {"", ".", ".."}:
                _fail(path, "artifact source must be a reviewed repository file")
        if not _SHA256.fullmatch(str(entry.get("sha256", entry.get("content_sha256", "")))):
            _fail(path, "artifact source requires a pinned SHA-256")


def validate_source_files(files: Mapping[str, bytes]) -> None:
    """Validate recognized inputs present in an actual source snapshot/subset."""
    for path, data in files.items():
        name = PurePosixPath(path).name
        parser = None
        if name == "uv.lock":
            parser = _python_lock
        elif name == "pyproject.toml":
            parser = _python_project
        elif name == "uv.toml":
            parser = lambda text, path: _python_project(text, path, config_only=True)
        elif name in {"pip.conf", "pip.ini"}:
            parser = _pip_config
        elif name.startswith("requirements") and name.endswith((".txt", ".in")) or (path.startswith("assets/artifacts/") and name.endswith(".lock")):
            parser = lambda text, path: _requirements(text, path, files)
        elif name == "package-lock.json":
            parser = _npm_lock
        elif name == "package.json":
            parser = lambda text, path: _npm_project(json.loads(text), path)
        elif name == ".npmrc":
            parser = _npmrc
        elif name in {"app.yaml", "app.yml"}:
            parser = _app_config
        elif path.startswith("assets/artifacts/") and name.endswith(".json"):
            parser = _artifact_manifest
        if parser is not None:
            try:
                parser(data.decode("utf-8"), path)
            except DependencySourceError:
                raise
            except (ValueError, TypeError, KeyError, AttributeError, configparser.Error) as error:
                _fail(path, f"invalid dependency input ({type(error).__name__})")


def validate_release_inputs(root: Path, *, require_clean: bool = False) -> None:
    """Validate release checkout inputs, optionally requiring an immutable tree."""
    root = Path(root).resolve()
    missing = [name for name in REQUIRED_INPUTS if not (root / name).is_file()]
    if missing:
        _fail("release", "required dependency inputs are missing: " + ", ".join(missing))
    names = set(REQUIRED_INPUTS)
    names.update(path.relative_to(root).as_posix() for pattern in ("*.lock", "*.json")
                 for path in (root / "assets/artifacts").glob(pattern))
    for directory in (".", "frontend", "deploy/omnigent-app"):
        for config_name in (".npmrc", "uv.toml", "pip.conf", "pip.ini"):
            name = (PurePosixPath(directory) / config_name).as_posix()
            if (root / name).exists():
                names.add(name)
    for name in names:
        target = root / name
        if target.is_symlink():
            _fail(name, "dependency inputs cannot be symlinks")
        for parent in target.parents:
            if parent == root:
                break
            if parent.is_symlink():
                _fail(name, "dependency input ancestors cannot be symlinks")
    validate_source_files({name: (root / name).read_bytes() for name in sorted(names)})
    if require_clean:
        result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                cwd=root, capture_output=True, text=True, check=True)
        if result.stdout.strip():
            _fail("release", "refusing to upload a dirty checkout")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args(argv)
    try:
        validate_release_inputs(args.root, require_clean=args.require_clean)
    except DependencySourceError as error:
        print(error)
        return 1
    print("Public Python/npm release and deployment sources verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
