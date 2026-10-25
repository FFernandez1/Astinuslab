from __future__ import annotations

import os
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[3]


def test_windows_build_poll_helper_is_portable_and_parameterized():
    script_path = ROOT / "ops" / "host" / "windows_build" / "poll_build.sh"
    script = script_path.read_text(encoding="utf-8")

    assert os.access(script_path, os.X_OK)
    # Any absolute home path, not one operator's: the guard must catch a leak
    # from a different checkout, and naming a user would embed one here.
    assert "/home/" not in script
    assert "REPO_ROOT" in script

    for option in [
        "--repo-root",
        "--vm-name",
        "--mac-file",
        "--ssh-key",
        "--uri",
        "--timeout",
        "--screenshot",
    ]:
        assert option in script

    assert "WINBUILD_VM_NAME" in script
    assert "WINBUILD_SSH_KEY" in script
    assert "LIBVIRT_URI" in script
    assert "$repo_root/storage/ssh_keys/key" in script


def test_windows_autounattend_example_is_sanitized_and_parseable():
    template = (ROOT / "guest" / "windows" / "Autounattend.example.xml").read_text(encoding="utf-8")

    ElementTree.fromstring(template)
    assert "{{ADMINISTRATOR_PASSWORD}}" in template
    assert "{{SSH_PUBLIC_KEY}}" in template
    assert "AstinusLab!Pass1" not in template
    assert "artifactlab/key" not in template
    assert "/home/" not in template


def test_generated_runtime_docs_define_windows_build_scratch_policy():
    generated = (ROOT / "docs" / "development" / "GENERATED_RUNTIME_FILES.md").read_text(encoding="utf-8")
    layout = (ROOT / "docs" / "development" / "REPOSITORY_LAYOUT.md").read_text(encoding="utf-8")

    assert "`build/`" in generated
    assert "`build/windows/`" in generated
    assert "`storage/tmp/windows-build/<id>/`" in generated
    assert "`ops/host/windows_build/`" in layout
    assert "`guest/windows/`" in layout
