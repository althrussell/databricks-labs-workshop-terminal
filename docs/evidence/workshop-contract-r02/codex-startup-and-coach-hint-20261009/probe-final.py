"""Disposable Linux daemon probe: synthetic HOME, no model calls or credentials."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.request

from codex_artifacts import install_native_alias
import codex_home

ROOT = Path(__file__).resolve().parent
REPORT = {"status": "starting", "model_requests": 0, "control_tower_mutations": 0}
ARTIFACTS = json.loads((ROOT / "pins.json").read_text())

def fetch(name):
    pin = ARTIFACTS[name]
    destination = ROOT / (name + ".tgz")
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == pin["sha256"]:
        return str(destination)
    digest = hashlib.sha256()
    with urllib.request.urlopen(pin["source"], timeout=60) as source, destination.open("wb") as output:
        while chunk := source.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
    assert digest.hexdigest() == pin["sha256"]
    return str(destination)

def exercise():
    try:
        launcher = fetch("codex_npm_launcher_package")
        native = fetch("codex_native_package_linux_x64")
        binary = install_native_alias(launcher, native, str(ROOT / "native"), "0.157.1")
        assert hashlib.sha256(Path(binary).read_bytes()).hexdigest() == ARTIFACTS["codex_native_package_linux_x64"]["executable_sha256"]
        REPORT.update(status="probing", version=subprocess.check_output([binary, "--version"], text=True).strip(), cells=[])
        for name, home, configured in [
            ("long_home_wt_alias", ROOT / "data/wt-eval-qcap-1009-6b90/users/labuser-1-e2e66dc8", True),
        ]:
            (home / ".codex").mkdir(parents=True, exist_ok=True)
            cwd = home / "projects"
            cwd.mkdir(exist_ok=True)
            # Same provider/auth/config shape as WT; the address is deliberately
            # unresolvable and no turn is submitted. Nothing can invoke a model.
            if configured:
                (home / ".codex/config.toml").write_text('''model = "system.ai.gpt-5-6-terra"
model_provider = "databricks"
web_search = "disabled"
approval_policy = "never"
sandbox_mode = "danger-full-access"
[model_providers.databricks]
name = "Databricks Model Serving"
base_url = "https://example.invalid/serving-endpoints/responses"
wire_api = "responses"
request_max_retries = 1
stream_max_retries = 1
[model_providers.databricks.auth]
command = "cat"
args = ["/dev/null"]
timeout_ms = 5000
refresh_interval_ms = 240000
''')
            env = {"HOME": str(home), "USER": "synthetic-diagnostic", "TERM": "xterm-256color", "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "CODEX_MANAGED_BY_NPM": "1"}
            if name == "long_home_short_alias":
                alias = Path("/tmp/wt-cx-" + hashlib.sha256(str(home).encode()).hexdigest()[:32])
                try:
                    alias.symlink_to(home, target_is_directory=True)
                except FileExistsError:
                    assert alias.resolve() == home.resolve()
                env["HOME"] = str(alias)
            elif name == "long_home_wt_alias":
                env["HOME"] = codex_home.shell_home(str(home))
            started = time.monotonic()
            try:
                result = subprocess.run([binary, "app-server", "daemon", "start"], env=env, cwd=cwd, capture_output=True, text=True, timeout=35)
                row = {"name": name, "home_bytes": len(env["HOME"].encode()), "original_home_bytes": len(str(home).encode()), "control_socket_bytes": len((env["HOME"] + "/.codex/app-server-control/app-server-control.sock").encode()), "alias_resolves_to_original": Path(env["HOME"]).resolve() == home.resolve(), "elapsed_s": round(time.monotonic() - started, 3), "exit_code": result.returncode, "stdout": result.stdout[-16000:], "stderr": result.stderr[-16000:]}
            except subprocess.TimeoutExpired:
                row = {"name": name, "timeout": True, "elapsed_s": round(time.monotonic() - started, 3)}
            # Collect only logs from this synthetic credential-free daemon.
            row["daemon_stderr"] = [{"name": str(p.relative_to(home)), "tail": p.read_text(errors="replace")[-16000:]} for p in (home / ".codex/app-server-daemon").glob("*.log")]
            cleanup = subprocess.run([binary, "app-server", "daemon", "stop"], env=env, cwd=cwd, capture_output=True, text=True, timeout=20)
            row["stop_exit_code"] = cleanup.returncode
            REPORT["cells"].append(row)
        REPORT["status"] = "complete"
    except Exception as error:
        REPORT.update(status="failed", error_type=type(error).__name__, detail=str(error))

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/probe":
            self.send_error(404)
            return
        body = json.dumps(REPORT).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        pass

threading.Thread(target=exercise, daemon=True).start()
http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("DATABRICKS_APP_PORT", "8000"))), Handler).serve_forever()
