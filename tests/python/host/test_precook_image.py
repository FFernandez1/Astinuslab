import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops" / "host" / "precook_image.sh"


def _resolve(*args: str) -> dict[str, str]:
    out = subprocess.run(
        [str(SCRIPT), *args, "--resolve-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def test_kali_filename_autodetects_attacker():
    r = _resolve("kali-linux-2025.4-qemu-amd64.qcow2")
    assert r["role"] == "attacker"
    assert "prebake_attacker_image.sh" in r["command"]
    assert r["output"].endswith("kali-linux-2025.4-qemu-amd64-attacker-base.qcow2")


def test_ubuntu_filename_autodetects_target():
    r = _resolve("ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img")
    assert r["role"] == "target"
    assert "prepare_target_image.sh" in r["command"]


def test_attacker_catalog_id_resolves_attacker():
    r = _resolve("kali")
    assert r["role"] == "attacker"
    assert "images/definitions/" in r["path"]


def test_target_catalog_id_resolves_target():
    r = _resolve("ubuntu-24.04")
    assert r["role"] == "target"
    assert "prepare_target_image.sh" in r["command"]
    assert "images/definitions/" in r["path"]


def test_role_override_forces_target_on_kali_name():
    r = _resolve("kali-linux-2025.4-qemu-amd64.qcow2", "--role", "target")
    assert r["role"] == "target"


def test_role_override_forces_attacker_on_ubuntu_name():
    r = _resolve("ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img", "--role", "attacker")
    assert r["role"] == "attacker"


def test_attacker_base_name_is_idempotent():
    # Re-precooking an already-baked name must not double the suffix.
    r = _resolve("kali-linux-2025.4-attacker-base.qcow2", "--role", "attacker")
    assert r["output"].endswith("kali-linux-2025.4-attacker-base.qcow2")


def test_resolve_reports_key_line():
    r = _resolve("kali-linux-2025.4-qemu-amd64.qcow2")
    assert "key" in r
