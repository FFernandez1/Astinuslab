"""Execute the published Bash setup against real HTTP authentication/CSRF."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "lang,authenticated", [("en", False), ("es", False), ("fr", False), ("ca", False), ("en", True)]
)
def test_documented_helper_reaches_validation_without_testclient_cookies(monkeypatch, tmp_path, lang, authenticated):
    if not shutil.which("curl"):
        pytest.skip("curl is needed to execute the documented Bash example")
    from astinuslab.core import security
    from astinuslab.main import app

    token = "documentation-test-token"
    monkeypatch.setattr(
        security,
        "settings",
        SimpleNamespace(
            auth_enabled=authenticated,
            admin_api_token_hash="sha256:" + hashlib.sha256(token.encode()).hexdigest() if authenticated else None,
            api_port=8000,
            browser_allowed_origins=[],
        ),
    )
    suffix = "" if lang == "en" else "." + lang
    source = (ROOT / f"docs/usage/examples{suffix}.md").read_text()
    helper = next(block for block in re.findall(r"```bash\n(.*?)```", source, re.S) if "api_curl()" in block)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            # Empty job input deliberately reaches validation but cannot start a VM.
            command = (
                helper
                + '\napi_curl -sS -w "\\nHTTP_CODE=%{http_code}\\n" -X POST "$BASE_URL/api/v1/jobs" -H "Content-Type: application/json" -d \'{}\'\n'
            )
            env = {
                **os.environ,
                "BASE_URL": f"http://127.0.0.1:{port}",
                "TMPDIR": str(tmp_path),
                "ASTINUSLAB_TOKEN": token if authenticated else "",
            }
            response = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True, timeout=10)
            assert response.returncode == 22, response.stderr
            assert "HTTP_CODE=422" in response.stdout, response.stdout
            assert "CSRF token required" not in response.stdout
            assert not list(tmp_path.glob("astinuslab-cookies.*")), "temporary cookies must be removed"
        finally:
            server.should_exit = True
            worker.join(timeout=5)
            assert not worker.is_alive()
