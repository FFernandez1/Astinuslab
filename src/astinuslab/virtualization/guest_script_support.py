from __future__ import annotations

import subprocess
import time
from uuid import uuid4

from ..core.config import settings
from ..storage.keystore import get_active_key_paths


def ssh_opts() -> list[str]:
    priv, _ = get_active_key_paths()
    return [
        "-i",
        str(priv),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "BatchMode=yes",
    ]


def _validate_script_filename(filename: str) -> str:
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename or "\x00" in filename:
        raise ValueError("invalid script filename")
    return filename


def exec_script_on_guest(
    guest_ip: str,
    guest_user: str,
    script_bytes: bytes,
    filename: str,
    timeout_s: int,
) -> dict:
    start = time.time()
    local_path = None
    try:
        safe_filename = _validate_script_filename(filename)
        tmp_dir = settings.storage_dir / "tmp" / "adhoc_scripts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        local_path = tmp_dir / f"{uuid4().hex[:10]}_{safe_filename}"
        remote_path = f"/tmp/astinuslab_adhoc_{uuid4().hex[:8]}.sh"  # noqa: S108  # guest-side path, not a host temp file
        local_path.write_bytes(script_bytes)
        target_host = f"{guest_user}@{guest_ip}"
        opts = ssh_opts()
        scp = subprocess.run(
            ["scp"] + opts + [str(local_path), f"{target_host}:{remote_path}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if scp.returncode != 0:
            raise RuntimeError(f"SCP failed: {scp.stderr.strip() or scp.stdout.strip()}")
        result = subprocess.run(
            [
                "ssh",
                *opts,
                target_host,
                f"chmod +x {remote_path} && bash {remote_path}; _rc=$?; rm -f {remote_path}; exit $_rc",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s + 5,
        )
        output = result.stdout.rstrip()
        stderr = result.stderr.strip()
        if stderr:
            output += ("\n" if output else "") + "--- stderr ---\n" + stderr
        return {
            "filename": filename,
            "exit_code": result.returncode,
            "success": result.returncode == 0,
            "output": output.strip(),
            "duration_ms": int((time.time() - start) * 1000),
            "error": None,
        }
    except subprocess.TimeoutExpired:
        return {
            "filename": filename,
            "exit_code": 124,
            "success": False,
            "output": "",
            "duration_ms": int((time.time() - start) * 1000),
            "error": f"Timeout: script exceeded {timeout_s}s",
        }
    except Exception as exc:
        return {
            "filename": filename,
            "exit_code": 1,
            "success": False,
            "output": "",
            "duration_ms": int((time.time() - start) * 1000),
            "error": str(exc),
        }
    finally:
        if local_path is not None:
            local_path.unlink(missing_ok=True)
