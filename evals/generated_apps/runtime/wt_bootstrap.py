from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

CHUNK_SIZE = 1024 * 1024
MAX_ATTEMPTS = 30


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def workspace_host() -> str:
    host = required("DATABRICKS_HOST").rstrip("/")
    # Databricks Apps injects a bare hostname today, while local/default SDK
    # configurations commonly include https://. urllib requires a scheme.
    if "://" not in host:
        host = f"https://{host}"
    return host


def oauth_token() -> str:
    host = workspace_host()
    client_id = required("DATABRICKS_CLIENT_ID")
    client_secret = required("DATABRICKS_CLIENT_SECRET")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = Request(
        f"{host}/oidc/v1/token",
        data=urlencode(
            {"grant_type": "client_credentials", "scope": "all-apis"}
        ).encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "workshop-terminal-bootstrap",
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        payload = json.loads(response.read(1024 * 1024))
    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError("Databricks OAuth response did not contain an access token")
    return token


def download_pex(token: str, target: Path) -> None:
    host = workspace_host()
    volume_path = required("WORKSHOP_PACKAGE_VOLUME_PATH")
    expected_digest = required("WORKSHOP_PACKAGE_SHA256")
    expected_size = int(required("WORKSHOP_PACKAGE_SIZE_BYTES"))
    if not volume_path.startswith("/Volumes/"):
        raise RuntimeError("package path must be an absolute Unity Catalog Volume path")
    url = f"{host}/api/2.0/fs/files{quote(volume_path, safe='/')}"
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/octet-stream",
            "User-Agent": "workshop-terminal-bootstrap",
        },
    )
    partial = target.with_suffix(".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with urlopen(request, timeout=300) as response, partial.open("wb") as output:
            while chunk := response.read(CHUNK_SIZE):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                if size > expected_size:
                    raise RuntimeError("package download exceeded its manifest size")
        actual_digest = digest.hexdigest()
        if size != expected_size or actual_digest != expected_digest:
            raise RuntimeError(
                "package verification failed: "
                f"expected {expected_size}/{expected_digest}, "
                f"got {size}/{actual_digest}"
            )
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def main() -> None:
    digest = required("WORKSHOP_PACKAGE_SHA256")
    target = Path("/tmp") / f"workshop-terminal-{digest}.pex"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            download_pex(oauth_token(), target)
            print(
                f"Verified Workshop Terminal package {digest} from Unity Catalog",
                flush=True,
            )
            # Normal attendee use must run the product's own entry point.
            # Evaluation instrumentation is an explicit disposable-test mode.
            if os.environ.get("WORKSHOP_EVALUATION_ENABLED") == "true":
                source = str(Path(__file__).resolve().parent)
                os.environ["WT_EVALUATION_SOURCE_ROOT"] = source
                os.environ["PEX_EXTRA_SYS_PATH"] = source
                os.environ["PEX_MODULE"] = "wt_evaluation_bootstrap:main"
                os.environ["PYTHONPATH"] = source
            else:
                os.environ["PEX_MODULE"] = "server.otel_bootstrap:main"
            os.execv(sys.executable, [sys.executable, str(target)])
        except Exception as error:
            print(
                f"Workshop Terminal package bootstrap attempt {attempt}/"
                f"{MAX_ATTEMPTS} failed: {type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(min(attempt * 2, 10))


if __name__ == "__main__":
    main()
