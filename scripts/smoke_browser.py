#!/usr/bin/env python3
"""Install the prepared browser in a clean container and take a real screenshot (W3).

    python3 scripts/smoke_browser.py stage <work>   # on the host, with a network
    PEX_INTERPRETER=1 python3.11 workshop-terminal.pex scripts/smoke_browser.py run <work> <out-dir>

``stage`` fetches the browser and its runtime packages into ``<work>`` exactly
as bootstrap stages them, each verified against the reviewed manifest, plus a
standalone Python 3.11 for images that have none. ``run`` is the attendee's
container: the release PEX, no system packages, no network. It records what the
bare browser cannot load (``ldd``), runs the real bootstrap step, then renders a
page through the same inspect path agents use and writes the screenshots and a
JSON report to ``<out-dir>``.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import threading
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
try:
    import server  # noqa: F401 - from the release PEX when run inside it
except ImportError:
    sys.path.insert(0, str(ROOT))

# Ubuntu 22.04 (the Apps base) ships no Python; the release targets 3.11.
PYTHON_311 = ("https://github.com/astral-sh/python-build-standalone/releases/download/20261003/"
              "cpython-3.11.17+20261003-x86_64-unknown-linux-gnu-install_only.tar.gz",
              "c624af93ad62a596806bbd2404e1fb80744a407ca7279854445ede16d93858b8")

PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>Outage log</title>
<style>body{font-family:sans-serif;margin:40px;background:#f9f7f4;color:#0b2026}
h1{font-size:40px}button{font:18px monospace;padding:10px 18px;background:#ff3621;color:#fff;border:0}
p{font-family:serif;font-size:22px}</style></head><body>
<h1>Outage log &mdash; rendered by the prepared browser</h1>
<p>Sans, serif and monospace text all need the bundled fonts.</p>
<button onclick="document.querySelector('p').textContent='Clicked: 1 outage logged'">Log an outage</button>
</body></html>"""


def stage(work: Path) -> None:
    import hashlib
    import urllib.request

    from server.bootstrap import browser_runtime, install
    from server.bootstrap.artifacts import ArtifactManifest

    contract = ArtifactManifest.from_path("")
    staging = work / "data" / "shared" / "artifacts"
    total = 0
    for name in [install.BROWSER_ARTIFACT, *browser_runtime.artifact_names()]:
        total += os.path.getsize(contract.fetch(name, staging_dir=str(staging)).path)
    print(f"staged {total} bytes of browser artifacts into {staging}")

    url, expected = PYTHON_311
    archive = work / "python.tar.gz"
    with urllib.request.urlopen(url, timeout=300) as response, open(archive, "wb") as handle:
        shutil.copyfileobj(response, handle)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise SystemExit(f"{url} does not match its pinned sha256")
    with tarfile.open(archive) as bundle:
        # share/ is terminfo and manuals, whose case-only name clashes loop on
        # a case-insensitive (macOS) disk.
        bundle.extractall(work, members=[m for m in bundle if not m.name.startswith("python/share/")],
                          filter="tar")
    archive.unlink()


def _missing(binary: str, env: dict) -> list[str]:
    done = subprocess.run(["ldd", binary], capture_output=True, text=True, env=env)
    return sorted({line.split()[0] for line in done.stdout.splitlines() if "not found" in line})


def _png_colours(path: Path) -> int:
    """Distinct pixel values in an 8-bit RGB(A) PNG, so a blank page cannot pass."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    offset, idat, header = 8, b"", None
    while offset < len(data):
        length, kind = struct.unpack(">I4s", data[offset:offset + 8])
        body = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        offset += 12 + length
    width, height, depth, colour = header[0], header[1], header[2], header[3]
    assert depth == 8 and colour in (2, 6), f"unexpected PNG format {depth}/{colour}"
    channels = 3 if colour == 2 else 4
    raw, stride = zlib.decompress(idat), width * channels
    rows, previous = [], bytearray(stride)
    for y in range(height):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            left = line[x - channels] if x >= channels else 0
            up, corner = previous[x], previous[x - channels] if x >= channels else 0
            if kind == 1:
                line[x] = (line[x] + left) & 0xFF
            elif kind == 2:
                line[x] = (line[x] + up) & 0xFF
            elif kind == 3:
                line[x] = (line[x] + (left + up) // 2) & 0xFF
            elif kind == 4:
                p = left + up - corner
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                line[x] = (line[x] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 0xFF
        rows.append(bytes(line))
        previous = line
    return len({row[i:i + channels] for row in rows for i in range(0, stride, channels)})


def run(work: Path, out: Path) -> None:
    os.environ["DATA_ROOT"] = str(work / "data")
    os.environ.setdefault("DATABRICKS_APP_PORT", "8000")
    out.mkdir(parents=True, exist_ok=True)
    from server import browser_evidence, telemetry
    from server.bootstrap import browser_runtime, install

    # Control Tower's event buffer is not part of this container. No mirror is
    # configured, so bytes come from the staged copies, as on a warm restart.
    events: list[tuple[str, dict]] = []
    telemetry.emit = lambda kind, who, payload=None: events.append((kind, payload or {})) or True

    report: dict = {"image": os.environ.get("SMOKE_IMAGE", ""), "glibc": os.confstr("CS_GNU_LIBC_VERSION")}
    started = time.monotonic()
    install._install_browser()
    report["install_seconds"] = round(time.monotonic() - started, 1)
    state = dict(install._state.get("browser", {}))
    stamp = json.loads((Path(install.config.shared_prefix()) / "browser.install.json").read_text())
    report.update(install_status=state.get("status"), install_source=state.get("source"),
                  install_error=state.get("error"), reason_code=stamp.get("reason_code"),
                  events=[(kind, {k: v for k, v in payload.items() if k != "error"}) for kind, payload in events])
    binary = os.path.join(install.config.shared_prefix(), "browser", install.BROWSER_VERSION,
                          "chrome-headless-shell-linux64", "chrome-headless-shell")
    bare_env = {key: value for key, value in os.environ.items() if key not in {"LD_LIBRARY_PATH", "FONTCONFIG_FILE"}}
    report["missing_without_runtime"] = _missing(binary, bare_env)
    if stamp.get("runtime"):
        report["missing_with_runtime"] = _missing(binary, {**bare_env, **browser_runtime.runtime_env(stamp["runtime"])})
        report["runtime_bytes"] = sum(f.stat().st_size for f in Path(stamp["runtime"]).rglob("*") if f.is_file())
    if state.get("status") != "complete":
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        raise SystemExit(f"browser install failed: {state.get('error')}")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    project = work / "project"
    project.mkdir(exist_ok=True)
    try:
        result = browser_evidence.inspect(browser_evidence.InspectRequest(
            url=f"http://localhost:{server.server_address[1]}/", scope="local", viewports=["desktop"],
            actions=["click:Log an outage"], project=project, settle_seconds=0.5))
    finally:
        server.shutdown()
    shots = []
    for shot in result["artifacts"]:
        source = project / shot["path"]
        target = out / f"{shot['viewport']}-{shot['label']}.png".replace(" ", "-").replace(":", "-")
        shutil.copyfile(source, target)
        shots.append({"file": target.name, "bytes": target.stat().st_size, "title": shot["title"],
                      "colours": _png_colours(target)})
    report.update(screenshots=shots, actions=result["actions"])
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    assert report["missing_with_runtime"] == [], report["missing_with_runtime"]
    assert all(shot["title"] == "Outage log" and shot["colours"] > 16 for shot in shots), shots
    assert any(step.get("visible_change") for step in result["actions"]), "the click changed nothing"


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "stage":
        stage(Path(argv[1]).resolve())
    elif len(argv) == 3 and argv[0] == "run":
        run(Path(argv[1]).resolve(), Path(argv[2]).resolve())
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
