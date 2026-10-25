"""Offline contracts for the Lab 1 prepared guest image."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
LAB1 = ROOT / "guest/training/lab1"
pytestmark = pytest.mark.unit


def _builder():
    path = LAB1 / "build.py"
    assert path.is_file(), "Lab 1 image builder has not been created"
    spec = importlib.util.spec_from_file_location("training_lab1_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake_image_tools(tmp_path: Path) -> tuple[Path, Path, Path]:
    log = tmp_path / "tools.log"
    qemu_img = _write_executable(
        tmp_path / "qemu-img",
        """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

with Path(os.environ["LAB1_TOOL_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(["qemu-img", *sys.argv[1:]]) + "\\n")
if sys.argv[1] == "convert":
    shutil.copyfile(sys.argv[-2], sys.argv[-1])
elif sys.argv[1] == "resize":
    pass
elif sys.argv[1] == "info":
    print(json.dumps({
        "format": "qcow2",
        "virtual-size": 16 * 1024**3,
        "actual-size": Path(sys.argv[-1]).stat().st_size,
        "backing-filename": None,
        "data-file": None,
    }))
else:
    raise SystemExit(2)
""",
    )
    virt_customize = _write_executable(
        tmp_path / "virt-customize",
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

with Path(os.environ["LAB1_TOOL_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(["virt-customize", *sys.argv[1:]]) + "\\n")
image = Path(sys.argv[sys.argv.index("-a") + 1])
with image.open("ab") as handle:
    handle.write(b"\\ncustomized")
""",
    )
    return qemu_img, virt_customize, log


def test_builder_creates_self_contained_image_and_catalog_metadata(tmp_path, monkeypatch):
    builder = _builder()
    source = tmp_path / "noble.img"
    source.write_bytes(b"source-image")
    dvwa_archive = tmp_path / "dvwa.tar.gz"
    dvwa_archive.write_bytes(b"pinned-dvwa")
    expected_dvwa_sha256 = hashlib.sha256(dvwa_archive.read_bytes()).hexdigest()
    destination = tmp_path / "training-dvwa-single.qcow2"
    destination.with_suffix(".qcow2.meta.json").write_text('{"stale": true}\n')
    qemu_img, virt_customize, log = _fake_image_tools(tmp_path)
    monkeypatch.setenv("LAB1_TOOL_LOG", str(log))

    report = builder.build_image(
        source=source,
        destination=destination,
        dvwa_archive=dvwa_archive,
        expected_dvwa_sha256=expected_dvwa_sha256,
        qemu_img=str(qemu_img),
        virt_customize=str(virt_customize),
    )

    assert source.read_bytes() == b"source-image"
    assert destination.read_bytes() == b"source-image\ncustomized"
    assert report["image_id"] == "training-dvwa-single"
    assert report["dvwa"]["sha256"] == expected_dvwa_sha256
    assert report["output"]["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert report["output"]["virtual_size_bytes"] == 16 * 1024**3
    assert report["output"]["backing_filename"] is None
    assert report["output"]["data_file"] is None

    sidecar = json.loads(destination.with_suffix(".qcow2.meta.json").read_text())
    assert sidecar == {
        "arch": "x86_64",
        "build_id": "training-dvwa-single",
        "cloud_init_profile": "ubuntu",
        "default_username": "ubuntu",
        "detected_at": report["built_at"],
        "distro": "ubuntu",
        "family": "debian",
        "inject_network_config": False,
        "inspect_error": None,
        "kernel_release": None,
        "machine_type": "pc",
        "os_variant": "ubuntu24.04",
        "schema_version": 1,
        "source": "user",
        "user_edited_fields": ["cloud_init_profile", "inject_network_config"],
        "version": "24.04",
    }
    invocations = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry[0:2] for entry in invocations] == [
        ["qemu-img", "convert"],
        ["qemu-img", "resize"],
        ["virt-customize", "-a"],
        ["qemu-img", "info"],
    ]
    customize = invocations[2]
    network_index = customize.index("--network")
    install_index = customize.index("--install")
    assert network_index < install_index
    assert "--copy-in" in customize
    assert "--run-command" in customize
    assert expected_dvwa_sha256 in customize[-1]


def test_builder_refuses_unpinned_dvwa_before_writing_output(tmp_path):
    builder = _builder()
    source = tmp_path / "noble.img"
    source.write_bytes(b"source-image")
    archive = tmp_path / "dvwa.tar.gz"
    archive.write_bytes(b"wrong-source")
    destination = tmp_path / "training-dvwa-single.qcow2"

    with pytest.raises(ValueError, match="DVWA archive SHA-256"):
        builder.build_image(
            source=source,
            destination=destination,
            dvwa_archive=archive,
            expected_dvwa_sha256="0" * 64,
        )

    assert not destination.exists()
    assert not destination.with_suffix(".qcow2.meta.json").exists()


def test_builder_reports_external_tool_stderr(tmp_path):
    builder = _builder()
    source = tmp_path / "noble.img"
    source.write_bytes(b"source-image")
    archive = tmp_path / "dvwa.tar.gz"
    archive.write_bytes(b"pinned-dvwa")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    qemu_img = _write_executable(
        tmp_path / "qemu-img",
        "#!/usr/bin/env bash\nprintf 'image conversion exploded\\n' >&2\nexit 9\n",
    )

    with pytest.raises(RuntimeError, match="qemu-img.*image conversion exploded"):
        builder.build_image(
            source=source,
            destination=tmp_path / "training-dvwa-single.qcow2",
            dvwa_archive=archive,
            expected_dvwa_sha256=digest,
            qemu_img=str(qemu_img),
        )


def test_builder_rejects_nested_external_data_file(tmp_path):
    builder = _builder()
    image = tmp_path / "training-dvwa-single.qcow2"
    image.write_bytes(b"qcow")
    qemu_img = _write_executable(
        tmp_path / "qemu-img",
        """#!/usr/bin/env python3
import json
print(json.dumps({
    "format": "qcow2",
    "virtual-size": 16 * 1024**3,
    "format-specific": {"type": "qcow2", "data": {"data-file": "/tmp/external.raw"}},
}))
""",
    )

    with pytest.raises(ValueError, match="external dependency"):
        builder._image_info(str(qemu_img), image)


def _tiny_dvwa_archive(path: Path) -> str:
    tree = path.parent / "DVWA-test"
    (tree / "config").mkdir(parents=True)
    (tree / "hackable/uploads").mkdir(parents=True)
    (tree / "external/phpids/0.6/lib/IDS/tmp").mkdir(parents=True)
    (tree / "login.php").write_text("Damn Vulnerable Web Application", encoding="utf-8")
    (tree / "config/config.inc.php.dist").write_text("<?php", encoding="utf-8")
    with tarfile.open(path, "w:gz") as archive:
        archive.add(tree, arcname=tree.name)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_installer_materializes_static_network_and_observation_services(tmp_path):
    image_root = tmp_path / "root"
    bundle = image_root / "opt/astinuslab-training/lab1"
    bundle.mkdir(parents=True)
    for name in (
        "readiness.sh",
        "firstboot_setup.sh",
        "astinuslab-training-dvwa-setup.service",
        "astinuslab-training-readiness.service",
        "astinuslab-training-observation-clear.service",
    ):
        shutil.copy2(LAB1 / name, bundle / name)
    digest = _tiny_dvwa_archive(bundle / "dvwa.tar.gz")
    env = {**os.environ, "ASTINUSLAB_IMAGE_ROOT": str(image_root)}

    result = subprocess.run(
        [str(LAB1 / "install_image.sh"), digest],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    netplan = (image_root / "etc/netplan/60-astinuslab-training.yaml").read_text()
    assert "10.77.10.10/24" in netplan
    assert "dhcp4: false" in netplan
    assert "accept-ra: false" in netplan
    assert "gateway" not in netplan
    assert (image_root / "etc/cloud/cloud.cfg.d/99-astinuslab-disable-network-config.cfg").read_text() == (
        "network: {config: disabled}\n"
    )
    assert "disable_ipv6 = 1" in (image_root / "etc/sysctl.d/99-astinuslab-training.conf").read_text()
    config = (image_root / "var/www/html/dvwa/config/config.inc.php").read_text()
    assert "default_security_level' ] = 'low'" in config
    assert "disable_authentication' ] = false" in config
    assert (
        image_root / "etc/systemd/system/apache2.service.d/astinuslab-training.conf"
    ).read_text() == "[Service]\nPrivateTmp=false\n"
    assert (image_root / "usr/local/sbin/astinuslab-training-readiness").stat().st_mode & stat.S_IXUSR
    readiness_unit = (image_root / "etc/systemd/system/astinuslab-training-readiness.service").read_text()
    assert "BindsTo=apache2.service mariadb.service" in readiness_unit
    assert "Requires=astinuslab-training-observation-clear.service" in readiness_unit
    assert "ExecStop=/usr/bin/rm -f /var/lib/astinuslab/observations/readiness.json" in readiness_unit
    clear_unit = (image_root / "etc/systemd/system/astinuslab-training-observation-clear.service").read_text()
    assert "Before=astinuslab-training-readiness.service" in clear_unit
    assert "ExecStart=/usr/bin/rm -f /var/lib/astinuslab/observations/readiness.json" in clear_unit
    assert not (image_root / "tmp/astinuslab-lab1-proof").exists()
    mode = stat.S_IMODE((image_root / "var/lib/astinuslab/observations").stat().st_mode)
    assert mode == 0o750


def _fake_readiness_commands(tmp_path: Path, *, curl_ok: bool = True) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "systemctl",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        bin_dir / "mariadb",
        "#!/usr/bin/env bash\nprintf '5\\n'\n",
    )
    _write_executable(
        bin_dir / "curl",
        "#!/usr/bin/env bash\n" + ("printf 'Damn Vulnerable Web Application\\n'\n" if curl_ok else "exit 22\n"),
    )
    _write_executable(
        bin_dir / "ip",
        """#!/usr/bin/env bash
if [[ "$*" == "-4 -o address show scope global" ]]; then
  printf '2: enp1s0    inet 10.77.10.10/24 brd 10.77.10.255 scope global enp1s0\\n'
elif [[ "$*" == "-4 route show default" || "$*" == "-6 -o address show scope global" ]]; then
  exit 0
else
  exit 2
fi
""",
    )
    _write_executable(
        bin_dir / "sysctl",
        "#!/usr/bin/env bash\nprintf '1\\n'\n",
    )
    return bin_dir


def _run_readiness(tmp_path: Path, *, curl_ok: bool) -> subprocess.CompletedProcess[str]:
    observations = tmp_path / "observations"
    observations.mkdir(exist_ok=True)
    leases = tmp_path / "leases"
    leases.mkdir()
    owner = str(os.getuid())
    group = str(os.getgid())
    env = {
        **os.environ,
        "PATH": f"{_fake_readiness_commands(tmp_path, curl_ok=curl_ok)}:{os.environ['PATH']}",
        "ASTINUSLAB_OBSERVATION_DIR": str(observations),
        "ASTINUSLAB_OBSERVATION_OWNER": owner,
        "ASTINUSLAB_OBSERVATION_GROUP": group,
        "ASTINUSLAB_NETWORKD_LEASE_DIR": str(leases),
    }
    return subprocess.run(
        [str(LAB1 / "readiness.sh")],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_readiness_writes_expected_checkpoint_only_after_live_probes(tmp_path):
    result = _run_readiness(tmp_path, curl_ok=True)

    assert result.returncode == 0, result.stderr
    output = tmp_path / "observations/readiness.json"
    assert output.read_text() == '"v1:training-dvwa-single:ready"\n'
    assert stat.S_IMODE(output.stat().st_mode) == 0o640
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o750


def test_readiness_removes_stale_checkpoint_when_http_probe_fails(tmp_path):
    observations = tmp_path / "observations"
    observations.mkdir()
    (observations / "readiness.json").write_text('"v1:training-dvwa-single:ready"\n')

    result = _run_readiness(tmp_path, curl_ok=False)

    assert result.returncode != 0
    assert not (observations / "readiness.json").exists()
