import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "guest" / "scripts" / "mount_output_share.sh"


def _stub_bin(tmp_path: Path, *, fail_virtiofs: bool, fail_9p: bool) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    log = tmp_path / "mount.log"
    fail_v = "1" if fail_virtiofs else "0"
    fail_9 = "1" if fail_9p else "0"

    (bin_dir / "mount").write_text(
        f"""#!/usr/bin/env bash
echo "$@" >> {log}
FSTYPE=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-t" ]; then FSTYPE="$a"; break; fi
  prev="$a"
done
case "$FSTYPE" in
  virtiofs)
    [ "{fail_v}" = "1" ] && exit 32
    exit 0
    ;;
  9p)
    [ "{fail_9}" = "1" ] && exit 32
    exit 0
    ;;
esac
exit 0
"""
    )
    (bin_dir / "mount").chmod(0o755)

    (bin_dir / "mountpoint").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bin_dir / "mountpoint").chmod(0o755)

    return bin_dir


def _run(bin_dir: Path, mountpoint: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["ASTINUSLAB_SKIP_ROOT_CHECK"] = "1"
    return subprocess.run(
        ["bash", str(SCRIPT), "astinuslab_out", str(mountpoint)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_virtiofs_succeeds_uses_virtiofs(tmp_path):
    bin_dir = _stub_bin(tmp_path, fail_virtiofs=False, fail_9p=False)
    target = tmp_path / "mnt"
    proc = _run(bin_dir, target)
    assert proc.returncode == 0, proc.stderr
    log = (tmp_path / "mount.log").read_text()
    assert "virtiofs" in log
    assert "9p" not in log
    assert "mount_type=virtiofs" in proc.stdout


def test_virtiofs_fails_falls_back_to_9p(tmp_path):
    bin_dir = _stub_bin(tmp_path, fail_virtiofs=True, fail_9p=False)
    target = tmp_path / "mnt"
    proc = _run(bin_dir, target)
    assert proc.returncode == 0, proc.stderr
    log = (tmp_path / "mount.log").read_text()
    assert "virtiofs" in log
    assert "9p" in log
    assert "mount_type=9p" in proc.stdout
    assert "astinuslab_out_9p" in log


def test_both_fail_returns_nonzero(tmp_path):
    bin_dir = _stub_bin(tmp_path, fail_virtiofs=True, fail_9p=True)
    target = tmp_path / "mnt"
    proc = _run(bin_dir, target)
    assert proc.returncode != 0
