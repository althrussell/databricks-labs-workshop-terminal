"""Per-project app manifest (insight contract v2, section 2).

After a deploy screenshot, and again at wrap, WT describes each attendee project
as an ``app.manifest`` event: its name and URL, git revision, stack, the
Databricks resources its config declares, the three-part Unity Catalog names its
code names, the products its code and config evidence, and the screenshots kept
for it.

Everything comes from files in the project, read under strict bounds. Nothing is
read from a conversation, and no keyword in a prompt counts as evidence:

- symlinks are never followed, and a link is never read;
- a file over 512 KB is skipped, and at most 5 MB is read per project;
- only manifests, ``resources/*.yml`` and source files of the listed kinds are read.

The manifest is journal-only (it is not an insight row): it goes through the same
retained emitter as every other attendee event, with a priority type so it keeps
its place under backlog pressure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import config

logger = logging.getLogger("workshop.app_manifest")

MANIFEST_SCHEMA_VERSION = 1
EVENT_TYPE = "app.manifest"
MAX_FILE_BYTES = 512 * 1024
MAX_PROJECT_BYTES = 5 * 1024 * 1024
MAX_SCANNED_FILES = 2000
MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024
MAX_SCREENSHOTS = 3
MAX_STACK = 10
MAX_RESOURCES = 30
MAX_UC_TABLES = 50
MAX_PRODUCTS = 20
GIT_TIMEOUT_SECONDS = 5

#: Directories never walked. Hidden directories (``.workshop`` included) are
#: skipped by the walk and read on their own terms below.
SKIP_DIRS = frozenset({
    "node_modules", "venv", "__pycache__", "dist", "build", "coverage", "site-packages",
})
MANIFEST_NAMES = frozenset({
    "app.yaml", "app.yml", "databricks.yml", "requirements.txt", "package.json", "pyproject.toml",
})
SOURCE_SUFFIXES = (".py", ".sql", ".ts", ".tsx", ".js")

#: Resource keys in an Apps ``app.yaml`` list, mapped to contract kinds.
APP_RESOURCE_KINDS = {
    "sql_warehouse": "sql_warehouse", "serving_endpoint": "serving_endpoint",
    "database": "database", "genie_space": "genie_space", "job": "job",
    "pipeline": "pipeline", "volume": "volume", "secret": "secret",
    "uc_securable": "uc_securable", "vector_search_index": "vector_search_index",
}
#: Collection keys under ``resources:`` in ``databricks.yml``, mapped to kinds.
BUNDLE_RESOURCE_KINDS = {
    "sql_warehouses": "sql_warehouse", "model_serving_endpoints": "serving_endpoint",
    "database_instances": "database", "genie_spaces": "genie_space", "jobs": "job",
    "pipelines": "pipeline", "volumes": "volume", "secrets": "secret",
    "vector_search_indexes": "vector_search_index",
}
#: Resource kind -> canonical product, when a resource alone is the evidence.
RESOURCE_PRODUCTS = {
    "database": "Lakebase", "genie_space": "Genie", "serving_endpoint": "Model Serving",
    "sql_warehouse": "Databricks SQL", "vector_search_index": "Vector Search",
    "job": "Lakeflow Jobs", "pipeline": "Lakeflow Declarative Pipelines",
}

_NAME_PART = r"[A-Za-z_][A-Za-z0-9_]{0,127}"
# A three-part name counts only where SQL or a string literal makes it one. A
# Python attribute chain (``self.cfg.table.name``) has neither, so it is excluded.
_UC_KEYWORD = re.compile(
    rf"\b(?:FROM|JOIN|INTO|UPDATE|TABLE|VIEW|USING)\s+`?({_NAME_PART})`?\.`?({_NAME_PART})`?\.`?({_NAME_PART})`?\b",
    re.IGNORECASE,
)
_UC_LITERAL = re.compile(rf"([\"'`])({_NAME_PART})\.({_NAME_PART})\.({_NAME_PART})\1")
# Dotted names that are code, not data: module paths, web hosts, file names.
_NOT_UC_FIRST = frozenset({
    "os", "sys", "np", "pd", "re", "json", "typing", "logging", "datetime", "pathlib",
    "subprocess", "react", "node", "process", "console", "document", "window", "self",
    "this", "config", "settings", "app", "src", "dist", "tests", "test", "www", "com",
    "org", "io", "net", "http", "https", "github", "databricks", "pyspark", "spark",
})
_NOT_UC_LAST = frozenset({
    "py", "ts", "tsx", "js", "jsx", "json", "md", "csv", "yaml", "yml", "txt", "html",
    "css", "png", "sql", "zip", "gz", "lock", "parquet",
})

_IMPORT_LINE = re.compile(r"^\s*(?:from\s+\S+\s+import\b|import\b|export\b.*\bfrom\b)")
_STACK_PY = re.compile(r"(?im)^\W*(fastapi|flask|streamlit|dash)\b")
_PRODUCT_PATTERNS = (
    ("Genie", re.compile(r"\bw\.genie\.")),
    ("Model Serving", re.compile(r"serving_endpoints\.query|\bai_query\s*\(")),
    ("Vector Search", re.compile(r"databricks[.\-_]?vector[-_]?search", re.IGNORECASE)),
    ("Databricks SQL", re.compile(r"databricks[-_]sql[-_]connector|from\s+databricks\s+import\s+sql\b")),
    ("MLflow", re.compile(r"^\s*(?:import|from)\s+mlflow\b", re.MULTILINE)),
)
_SHOT_NAME = re.compile(
    r"^(?P<stamp>\d{8}T\d{6}Z)-(?P<scope>deployed|local)-(?P<viewport>[a-z]+)-(?P<label>[a-z0-9-]+)\.png$"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _posix(path: str) -> str:
    return path.replace(os.sep, "/")


# --- bounded, symlink-free scan -------------------------------------------------


@dataclass
class ProjectScan:
    files: dict[str, str] = field(default_factory=dict)  # relative posix path -> text
    issues: list[str] = field(default_factory=list)


def _wanted(relative: str) -> bool:
    parts = relative.split("/")
    name = parts[-1]
    if name in MANIFEST_NAMES:
        return True
    if len(parts) == 2 and parts[0] == "resources" and name.endswith((".yml", ".yaml")):
        return True
    return name.endswith(SOURCE_SUFFIXES)


def _read_plain(path: str) -> bytes | None:
    """Bytes of a regular file, opened without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_FILE_BYTES + 1)


def scan_project(root: Path) -> ProjectScan:
    """Read the project's manifests and source files within every bound."""
    scan = ProjectScan()
    if root.is_symlink() or not root.is_dir():
        scan.issues.append("project_unreadable")
        return scan
    total = 0
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not name.startswith(".")
            and not os.path.islink(os.path.join(dirpath, name))
        )
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            relative = _posix(os.path.relpath(full, root))
            if not _wanted(relative):
                continue
            seen += 1
            if seen > MAX_SCANNED_FILES:
                scan.issues.append("file_limit")
                return scan
            try:
                info = os.lstat(full)  # lstat, never stat: a symlink is never followed
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue  # a symlink, device or fifo is never read
            size = info.st_size
            if size > MAX_FILE_BYTES:
                scan.issues.append("file_too_large")
                continue
            if total + size > MAX_PROJECT_BYTES:
                scan.issues.append("project_budget")
                return scan
            data = _read_plain(full)
            if data is None or len(data) > MAX_FILE_BYTES:
                continue
            total += len(data)
            scan.files[relative] = data.decode("utf-8", errors="replace")
    return scan


# --- line evidence ------------------------------------------------------------


def _line_of(text: str, pattern: re.Pattern | str, default: int = 1) -> int:
    regex = re.compile(pattern) if isinstance(pattern, str) else pattern
    for number, line in enumerate(text.splitlines(), start=1):
        if regex.search(line):
            return number
    return default


def _source(relative: str, text: str, pattern: re.Pattern | str) -> str:
    return f"{relative}:{_line_of(text, pattern)}"[:200]


# --- resources and app identity from YAML --------------------------------------


@dataclass
class ConfigFacts:
    resources: list[dict] = field(default_factory=list)
    app_name: str | None = None


def _load_yaml(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _app_yaml_resources(relative: str, text: str, facts: ConfigFacts) -> None:
    document = _load_yaml(text)
    entries = document.get("resources") if isinstance(document, dict) else None
    for item in entries if isinstance(entries, list) else []:
        if not isinstance(item, dict):
            continue
        key = next((k for k in item if k in APP_RESOURCE_KINDS), None)
        if key is None:
            continue
        name = item.get("name") if isinstance(item.get("name"), str) else None
        facts.resources.append({
            "kind": APP_RESOURCE_KINDS[key],
            "name": _clip(name, 200) or None,
            "source": _source(relative, text, rf"^\s*(?:-\s*)?{re.escape(key)}\s*:"),
        })


def _bundle_resources(relative: str, text: str, facts: ConfigFacts) -> None:
    document = _load_yaml(text)
    resources = document.get("resources") if isinstance(document, dict) else None
    if not isinstance(resources, dict):
        return
    apps = resources.get("apps")
    if isinstance(apps, dict) and facts.app_name is None:
        for key, value in apps.items():
            name = value.get("name") if isinstance(value, dict) else None
            facts.app_name = _clip(name or key, 120) or None
            break
    for collection, kind in BUNDLE_RESOURCE_KINDS.items():
        members = resources.get(collection)
        if not isinstance(members, dict):
            continue
        for key, value in members.items():
            name = value.get("name") if isinstance(value, dict) else None
            facts.resources.append({
                "kind": kind,
                "name": _clip(name or key, 200) or None,
                "source": _source(relative, text, rf"^\s*{re.escape(str(key))}\s*:"),
            })


def read_config(scan: ProjectScan) -> ConfigFacts:
    facts = ConfigFacts()
    for relative in sorted(scan.files):
        name = relative.rsplit("/", 1)[-1]
        text = scan.files[relative]
        if name in ("app.yaml", "app.yml"):
            _app_yaml_resources(relative, text, facts)
        elif name == "databricks.yml":
            _bundle_resources(relative, text, facts)
        elif len(relative.split("/")) == 2 and relative.startswith("resources/"):
            _bundle_resources(relative, text, facts)
    seen: set[tuple[str, str | None, str]] = set()
    unique: list[dict] = []
    for resource in facts.resources:
        key = (resource["kind"], resource["name"], resource["source"])
        if key not in seen and len(unique) < MAX_RESOURCES:
            seen.add(key)
            unique.append(resource)
    facts.resources = unique
    return facts


# --- Unity Catalog names --------------------------------------------------------


def _plausible_uc(catalog: str, schema: str, table: str) -> bool:
    return not (
        catalog.lower() in _NOT_UC_FIRST or schema.lower() in _NOT_UC_FIRST
        or table.lower() in _NOT_UC_LAST
    )


def uc_tables(scan: ProjectScan) -> list[dict]:
    """Three-part names read from SQL context or a string literal, first-seen order."""
    found: dict[str, str] = {}
    for relative in sorted(scan.files):
        text = scan.files[relative]
        is_code = relative.endswith((".py", ".ts", ".tsx", ".js"))
        for line_number, line in enumerate(text.splitlines(), start=1):
            if _IMPORT_LINE.match(line):
                continue  # ``from pkg.mod.sub import x`` is an import, not a table
            candidates = [match.groups() for match in _UC_KEYWORD.finditer(line)]
            if is_code:
                candidates += [m.groups()[1:] for m in _UC_LITERAL.finditer(line)]
            for catalog, schema, table in candidates:
                if not _plausible_uc(catalog, schema, table):
                    continue
                name = f"{catalog}.{schema}.{table}"
                if name not in found and len(found) < MAX_UC_TABLES:
                    found[name] = f"{relative}:{line_number}"[:200]
    return [{"name": name[:300], "source": source} for name, source in found.items()]


# --- products and stack ---------------------------------------------------------


def products(scan: ProjectScan, facts: ConfigFacts, tables: list[dict]) -> list[dict]:
    """Canonical product names, each backed by config, code or a resource.

    Keyword mentions in prose never count. A product is listed only from a
    resource kind, an SDK or import shape, or a three-part Unity Catalog name.
    """
    found: dict[str, str] = {}

    def add(product: str, source: str) -> None:
        if product not in found and len(found) < MAX_PRODUCTS:
            found[product] = source[:200]

    for relative in sorted(scan.files):
        if relative.rsplit("/", 1)[-1] in ("app.yaml", "app.yml"):
            add("Databricks Apps", f"{relative}:1")
            break
    for resource in facts.resources:
        product = RESOURCE_PRODUCTS.get(resource["kind"])
        if product:
            add(product, resource["source"])
    for relative in sorted(scan.files):
        text = scan.files[relative]
        for product, pattern in _PRODUCT_PATTERNS:
            if pattern.search(text):
                add(product, _source(relative, text, pattern))
    if tables:
        add("Unity Catalog", tables[0]["source"])
    return [{"product": product, "source": source} for product, source in found.items()]


def stack(scan: ProjectScan) -> list[str]:
    names: list[str] = []

    def add(name: str) -> None:
        if name not in names and len(names) < MAX_STACK:
            names.append(name)

    for relative in sorted(scan.files):
        text = scan.files[relative]
        base = relative.rsplit("/", 1)[-1]
        if base in ("requirements.txt", "pyproject.toml"):
            for match in _STACK_PY.finditer(text):
                add(match.group(1).lower())
        if base == "package.json":
            add("node")
            try:
                data = json.loads(text)
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            if isinstance(deps, dict) and "react" in deps:
                add("react")
        if base.endswith(".py"):
            add("python")
    return names


# --- screenshots ----------------------------------------------------------------


def select_shots(names: list[str]) -> list[str]:
    """The screenshot file names that travel: newest deployed initial, then up to two newest after-N.

    ``names`` are file names in ``{stamp}-{scope}-{viewport}-{label}.png`` form.
    Output is newest first, at most three. ``assets/bin/workshop-export`` keeps a
    copy of this rule, because the script cannot import the server package; a
    test pins the two together.
    """
    parsed = []
    for name in names:
        match = _SHOT_NAME.match(name)
        if match:
            parsed.append((match.group("stamp"), match.group("scope"), match.group("label"), name))
    parsed.sort(key=lambda item: (item[0], item[3]), reverse=True)
    chosen: list[str] = []
    initial = next((p for p in parsed if p[1] == "deployed" and p[2] == "initial"), None)
    if initial:
        chosen.append(initial[3])
    afters = [p for p in parsed if p[2].startswith("after-") and p[3] not in chosen]
    chosen.extend(p[3] for p in afters[:2])
    chosen.sort(key=lambda name: (_SHOT_NAME.match(name).group("stamp"), name), reverse=True)
    return chosen[:MAX_SCREENSHOTS]


def _design_record(project: Path) -> dict:
    try:
        from . import design_workflow

        record = design_workflow.read(project)
    except Exception as exc:  # noqa: BLE001 — a missing record is simply no evidence
        logger.debug("design workflow unreadable for %s: %s", project.name, exc)
        return {}
    return record if isinstance(record, dict) else {}


def screenshots(project: Path, record: dict) -> list[dict]:
    folder = project / ".workshop" / "evidence"
    if folder.is_symlink() or not folder.is_dir():
        return []
    names = []
    for entry in os.scandir(folder):
        if not _SHOT_NAME.match(entry.name) or entry.is_symlink() or not entry.is_file():
            continue
        if entry.stat(follow_symlinks=False).st_size > MAX_SCREENSHOT_BYTES:
            continue
        names.append(entry.name)
    inspect = ((record.get("evidence") or {}).get("inspect") or {})
    by_name: dict[str, dict] = {}
    for item in inspect.get("artifacts") or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            by_name[os.path.basename(item["path"])] = item
    out: list[dict] = []
    for name in select_shots(names):
        path = folder / name
        data = _read_plain(str(path))
        if data is None or len(data) > MAX_SCREENSHOT_BYTES:
            continue
        match = _SHOT_NAME.match(name)
        meta = by_name.get(name, {})
        stamp = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ")
        url = meta.get("url") if isinstance(meta.get("url"), str) else None
        title = meta.get("title") if isinstance(meta.get("title"), str) else None
        out.append({
            "file": f"{project.name}-{name}"[:200],
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "captured_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scope": match.group("scope"),
            "label": match.group("label")[:40],
            "url": _url(url),
            "title": _clip(title, 200) or None,
        })
    return out


def _url(value: object) -> str | None:
    """A deployed app URL without its query or fragment, which can carry tokens."""
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        return None
    return value.split("#", 1)[0].split("?", 1)[0][:500]


# --- revision -------------------------------------------------------------------


def revision(project: Path) -> str | None:
    """The git HEAD of this project only, never of an enclosing repository."""
    git_dir = project / ".git"
    if git_dir.is_symlink() or not git_dir.exists():
        return None
    try:
        done = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--verify", "--quiet", "HEAD"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = done.stdout.strip() if done.returncode == 0 else ""
    return head[:64] if re.fullmatch(r"[0-9a-f]{7,64}", head) else None


# --- the manifest ---------------------------------------------------------------


def build(project: Path, *, captured_at: str | None = None) -> dict | None:
    """The contract's ``app.manifest`` payload for one project, or None."""
    if project.is_symlink() or not project.is_dir():
        return None
    scan = scan_project(project)
    facts = read_config(scan)
    tables = uc_tables(scan)
    record = _design_record(project)
    preview = (((record.get("evidence") or {}).get("build") or {}).get("preview"))
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "project": _clip(project.name, 120),
        "app_name": facts.app_name,
        "url": _url(preview),
        "revision": revision(project),
        "captured_at": captured_at or _now(),
        "stack": stack(scan),
        "resources": facts.resources,
        "uc_tables": tables,
        "products_evidenced": products(scan, facts, tables),
        "screenshots": screenshots(project, record),
    }


def idempotency_key(run_id: str, attendee: str, manifest: dict) -> str:
    """``app-manifest:{run}:{attendee}:{project}:{revision}:{content}``.

    The contract names ``{revision or content-sha}``. Keying on the revision alone
    would let the wrap manifest of a project with a dirty tree or new screenshots
    de-dupe against its deploy-time manifest and be lost, so the content digest is
    always present and the revision rides alongside it.
    """
    stable = {key: value for key, value in manifest.items() if key != "captured_at"}
    digest = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()[:16]
    head = manifest.get("revision") or "norev"
    return f"app-manifest:{run_id}:{attendee}:{manifest['project']}:{head}:{digest}"


def emit(user, project: Path, *, emitter=None) -> dict | None:
    """Build and journal one project's manifest. Never raises into the caller."""
    if not config.insight_v2_enabled():
        return None
    try:
        if emitter is None:
            from .event_emitter import event_emitter as emitter
        manifest = build(project)
        if manifest is None:
            return None
        key = idempotency_key(emitter.run_id, user.email, manifest)
        emitter.emit(EVENT_TYPE, user.email, manifest, idempotency_key=key)
        return manifest
    except Exception as exc:  # noqa: BLE001 — a manifest never costs a deploy or an export
        logger.warning("app manifest not emitted for %s: %s", project.name, exc)
        return None


def emit_for_user(user, *, emitter=None) -> int:
    """Emit a manifest for every project the attendee has. Returns how many."""
    if not config.insight_v2_enabled():
        return 0
    from . import projects

    count = 0
    try:
        root = projects.root(user)
        children = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        return 0
    for child in children:
        if child.is_dir() and not child.is_symlink() and not child.name.startswith("."):
            if emit(user, child, emitter=emitter) is not None:
                count += 1
    return count


__all__ = [
    "EVENT_TYPE",
    "MANIFEST_SCHEMA_VERSION",
    "build",
    "emit",
    "emit_for_user",
    "idempotency_key",
    "select_shots",
]
