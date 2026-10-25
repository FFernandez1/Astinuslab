"""Exercise remote disk handoff without contacting or acquiring a real guest."""

import gzip
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [pytest.mark.unit, pytest.mark.host]


@pytest.mark.parametrize("configured_root", ["custom", "unset", "empty"])
@pytest.mark.parametrize("transport", ["virtiofs", "missing-shared-file", "unmounted"])
def test_remote_disk_handoff_uses_effective_storage_root(tmp_path, configured_root, transport):
    release = tmp_path / "installed release"
    host_scripts = release / "ops" / "host"
    host_scripts.mkdir(parents=True)
    for name in ("acquire_disk_remote.sh", "publish_disk_to_host.sh"):
        shutil.copy2(ROOT / "ops" / "host" / name, host_scripts / name)
    guest_script = release / "guest" / "scripts" / "acquire_disk.sh"
    guest_script.parent.mkdir(parents=True)
    guest_script.write_text("# Remote execution is stubbed for this handoff test.\n")

    storage_root = tmp_path / "configured evidence" if configured_root == "custom" else release / "storage"
    job_id = "job-storage-regression"
    shared_file = storage_root / "tmp" / job_id / "virtiofs-staging" / "disk.img.gz.partial"
    raw_disk = b"controlled disk image bytes\x00" * 40
    compressed_disk = gzip.compress(raw_disk)
    payload = tmp_path / "remote disk.img.gz"
    payload.write_bytes(compressed_disk)
    if transport == "virtiofs":
        shared_file.parent.mkdir(parents=True)
        shared_file.write_bytes(compressed_disk)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh = fake_bin / "ssh"
    ssh.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'case "${!#}" in\n'
        '  "mountpoint -q "*) [[ "$TEST_MOUNTED" == 1 ]] ;;\n'
        '  "mkdir -p "*) exit 0 ;;\n'
        '  "sudo bash -s -- "*) cat >/dev/null ;;\n'
        '  *) echo "Unexpected SSH command" >&2; exit 99 ;;\n'
        "esac\n"
    )
    scp = fake_bin / "scp"
    scp.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'printf "%s\\n" "${@: -2:1}" "${!#}" >> "$TEST_SCP_LOG"\n'
        'cp -- "$TEST_PAYLOAD" "${!#}"\n'
    )
    ssh.chmod(0o755)
    scp.chmod(0o755)
    scp_log = tmp_path / "scp.log"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_MOUNTED": "0" if transport == "unmounted" else "1",
        "TEST_PAYLOAD": str(payload),
        "TEST_SCP_LOG": str(scp_log),
        "GUEST_USER": "ubuntu",
        "GUEST_MOUNT_PATH": "/mnt/test-out",
    }
    env.pop("ASTINUSLAB_STORAGE_DIR", None)
    if configured_root != "unset":
        env["ASTINUSLAB_STORAGE_DIR"] = str(storage_root) if configured_root == "custom" else ""

    result = subprocess.run(
        [
            "bash",
            str(host_scripts / "acquire_disk_remote.sh"),
            job_id,
            "192.0.2.1",
            "unused-key",
            "ubuntu",
            "24.04",
            "test",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    local_fetch = storage_root / "tmp" / job_id / "disk.img.gz.partial"
    assert local_fetch.read_bytes() == compressed_disk
    assert f"disk_publish_source={local_fetch}" in result.stdout
    final_path = storage_root / "dumps" / job_id / "ubuntu-24.04-kernel-test-disk.img.gz"
    assert final_path.read_bytes() == compressed_disk
    assert f"disk_final_path={final_path}" in result.stdout
    assert f"disk_raw_sha256={hashlib.sha256(raw_disk).hexdigest()}" in result.stdout
    assert (
        hashlib.sha256(compressed_disk).hexdigest()
        in (storage_root / "logs" / f"{job_id}-disk-dump.sha256").read_text()
    )
    if transport == "virtiofs":
        assert not scp_log.exists(), "The configured shared staging file should avoid an SCP transfer"
    else:
        remote_dir = "/tmp/astinuslab-out" if transport == "unmounted" else "/mnt/test-out"
        assert scp_log.read_text().splitlines() == [
            f"ubuntu@192.0.2.1:{remote_dir}/disk.img.gz.partial",
            str(local_fetch),
        ]
    if configured_root == "custom":
        assert not (release / "storage").exists(), "No capture scratch should be written inside the release"
