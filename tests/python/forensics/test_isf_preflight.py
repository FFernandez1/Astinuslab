from types import SimpleNamespace

import pytest

from astinuslab.forensics.isf_preflight import (
    PreflightError,
    _check_tool,
    check_host_prerequisites,
)


def test_check_tool_present(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert _check_tool("rpm2cpio") == "/usr/bin/rpm2cpio"


def test_check_tool_missing_raises(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(PreflightError) as exc:
        _check_tool("rpm2cpio", install_hint="sudo apt install rpm2cpio")
    assert "rpm2cpio" in str(exc.value)
    assert "sudo apt install rpm2cpio" in str(exc.value)


def test_check_host_prerequisites_all_present(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "astinuslab.forensics.isf_preflight._check_libvirt",
        lambda uri: "1.2.3",
    )
    monkeypatch.setattr(
        "astinuslab.forensics.isf_preflight.check_dwarf2json_tool",
        lambda: SimpleNamespace(ready=True, code="ready", detail="dwarf2json is ready."),
    )
    info = check_host_prerequisites(libvirt_uri="qemu:///system")
    assert info["rpm2cpio"] == "/usr/bin/rpm2cpio"
    assert info["virsh"] == "/usr/bin/virsh"
    assert info["libvirt"] == "1.2.3"
    assert info["dwarf2json"] == "ready"


def test_check_host_prerequisites_missing_tool(monkeypatch):
    def fake_which(name):
        return None if name == "rpm2cpio" else f"/usr/bin/{name}"

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr(
        "astinuslab.forensics.isf_preflight._check_libvirt",
        lambda uri: "1.2.3",
    )
    with pytest.raises(PreflightError, match="rpm2cpio"):
        check_host_prerequisites(libvirt_uri="qemu:///system")


def test_unready_dwarf2json_is_reported_without_blocking_provider_backed_isf_builds(monkeypatch):
    """An unready dwarf2json must not fail preflight for the whole ISF-only job.

    The provider chain tries local_cache and prebuilt first, and both deliver a ready
    ISF without invoking dwarf2json.  Failing here would deny operators a capability
    that has no dependency on the missing tool.  The later providers (debuginfod and
    host_fetch) return a vmlinux and do require dwarf2json; those paths re-verify it
    immediately before use and report its readiness code when it is not ready.
    """
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "astinuslab.forensics.isf_preflight._check_libvirt",
        lambda uri: "1.2.3",
    )
    monkeypatch.setattr(
        "astinuslab.forensics.isf_preflight.check_dwarf2json_tool",
        lambda: SimpleNamespace(
            ready=False,
            code="dwarf2json_hash_mismatch",
            detail="The configured dwarf2json file does not match its SHA-256 pin.",
        ),
    )

    info = check_host_prerequisites(libvirt_uri="qemu:///system")

    # The stable code is surfaced for diagnostics; the unsafe detail is not.
    assert info["dwarf2json"] == "dwarf2json_hash_mismatch"
    assert "SHA-256 pin" not in str(info)


def test_missing_host_tool_is_reported_without_dwarf2json_probe(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None if name == "rpm2cpio" else f"/usr/bin/{name}")
    monkeypatch.setattr(
        "astinuslab.forensics.isf_preflight.check_dwarf2json_tool",
        lambda: pytest.fail("dwarf2json must not mask an earlier host-tool diagnostic"),
    )

    with pytest.raises(PreflightError, match="rpm2cpio"):
        check_host_prerequisites(libvirt_uri="qemu:///system")
