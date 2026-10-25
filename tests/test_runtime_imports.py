"""Server code imports only what the release PEX ships.

The release PEX holds the runtime dependency closure from ``uv.lock``, not the
dev group. A server import of a dev-only package passes every test and then
fails on a real seat: httpx arrives with FastAPI's TestClient, and
``verify-write`` raised ``ModuleNotFoundError: No module named 'httpx'`` on
every seat in the 25 Oct live run.

Each import is resolved to the distribution that owns its file, so a namespace
package such as ``databricks`` is judged by the part actually imported
(``databricks.sdk`` is runtime; ``databricks.agents`` is not).
"""
from __future__ import annotations

import ast
import importlib.util
from importlib import metadata
from pathlib import Path
import re
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "databricks-workshop-terminal"
LOCAL = {"server"}


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_closure() -> set[str]:
    """Distribution names reachable from the project's runtime dependencies."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {_normal(package["name"]): package for package in lock["package"]}

    def edges(dependency: dict) -> list[tuple[str, str | None]]:
        return [(_normal(dependency["name"]), None)] + [
            (_normal(dependency["name"]), extra) for extra in dependency.get("extra", [])]

    stack = [edge for dependency in packages[PROJECT].get("dependencies", []) for edge in edges(dependency)]
    seen: set[tuple[str, str | None]] = set()
    while stack:
        name, extra = stack.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        package = packages[name]
        dependencies = (package.get("dependencies", []) if extra is None
                        else package.get("optional-dependencies", {}).get(extra, []))
        stack.extend(edge for dependency in dependencies for edge in edges(dependency))
    return {name for name, _ in seen}


def _owners() -> dict[str, str]:
    owners: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = _normal(distribution.metadata["Name"] or "")
        for file in distribution.files or ():
            owners[str(Path(distribution.locate_file(file)).resolve())] = name
    return owners


def _optional(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """An import guarded by ``except ImportError`` is optional by design."""
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Try) and any(
            handler.type is not None and any(
                isinstance(name, ast.Name) and name.id in {"ImportError", "ModuleNotFoundError"}
                for name in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]))
            for handler in node.handlers):
            return True
    return False


def imported_modules(source: str) -> set[str]:
    """Absolute module names a source imports, at any depth, minus guarded optional imports."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    names: set[str] = set()
    for node in ast.walk(tree):
        if _optional(node, parents):
            continue
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # ``from a import b`` may name submodule ``a.b``; prefer it when it exists.
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                try:
                    names.add(full if importlib.util.find_spec(full) is not None else node.module)
                except (ImportError, ValueError):
                    names.add(node.module)
    return names


def violations(source: str, closure: set[str], owners: dict[str, str]) -> list[str]:
    found = []
    for module in sorted(imported_modules(source)):
        top = module.split(".")[0]
        if top in sys.stdlib_module_names or top in LOCAL or top == "__future__":
            continue
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            found.append(f"{module} (not installed)")
            continue
        files = [spec.origin] if spec.origin not in (None, "built-in", "frozen") else []
        files += [str(Path(location) / "__init__.py") for location in spec.submodule_search_locations or []]
        owner = next((owners[path] for path in (str(Path(f).resolve()) for f in files) if path in owners), None)
        if owner not in closure:
            found.append(f"{module} (from {owner or 'unknown distribution'}, not a runtime dependency)")
    return found


def test_server_modules_import_only_runtime_dependencies():
    closure, owners = runtime_closure(), _owners()
    problems = {}
    for path in sorted((ROOT / "server").rglob("*.py")):
        found = violations(path.read_text(), closure, owners)
        if found:
            problems[str(path.relative_to(ROOT))] = found
    assert problems == {}, "server code imports packages the release PEX does not ship: " + repr(problems)


def test_the_guard_catches_a_dev_only_import():
    closure, owners = runtime_closure(), _owners()
    assert "httpx" not in closure and "requests" in closure and "databricks-sdk" in closure
    lazy = "def verify():\n    import httpx\n    return httpx\n"
    assert violations(lazy, closure, owners) == ["httpx (from httpx, not a runtime dependency)"]
    guarded = "try:\n    import httpx\nexcept ImportError:\n    httpx = None\n"
    assert violations(guarded, closure, owners) == []
    assert violations("import requests\nfrom databricks.sdk import WorkspaceClient\nimport json\n", closure, owners) == []
