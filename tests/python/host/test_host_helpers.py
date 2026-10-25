from __future__ import annotations

from pathlib import Path

import pytest


def test_privileged_helper_paths_are_under_usr_local_libexec():
    from astinuslab.virtualization.host_helpers import (
        fix_libvirt_path_acl_helper,
        libvirt_storage_admin_helper,
        network_isolation_admin_helper,
    )

    assert libvirt_storage_admin_helper() == Path("/usr/local/libexec/astinuslab/libvirt_storage_admin.sh")
    assert fix_libvirt_path_acl_helper() == Path("/usr/local/libexec/astinuslab/fix_libvirt_path_acl.sh")
    assert network_isolation_admin_helper() == Path("/usr/local/libexec/astinuslab/network_isolation_admin.sh")


def test_privileged_helper_path_rejects_unknown_helper_name():
    from astinuslab.virtualization.host_helpers import privileged_helper_path

    with pytest.raises(ValueError, match="Unsupported privileged helper"):
        privileged_helper_path("../libvirt_storage_admin.sh")


def test_allowed_helpers_set_matches_installer_helper_list():
    """Drift guard: the Python allowlist must match both install scripts'
    helper loops, otherwise a future helper addition can slip past the
    sudoers contract."""
    import re

    from astinuslab.virtualization.host_helpers import _ALLOWED_HELPERS

    repo_root = Path(__file__).resolve().parents[3]
    # Parse the HELPER_SET array, not the loop that consumes it: the loop now
    # expands "${HELPER_SET[@]}", so matching on it would only ever prove that
    # some literal list exists somewhere in the file - a comment would satisfy
    # it while the installed set drifted.
    pattern = re.compile(r"HELPER_SET=\(([^)]*)\)")

    sudoers_install_text = (repo_root / "ops" / "host" / "install_sudoers_astinuslab.sh").read_text(encoding="utf-8")
    match = pattern.search(sudoers_install_text)
    assert match, "could not locate HELPER_SET in install_sudoers_astinuslab.sh"
    sudoers_install_helpers = set(match.group(1).split())

    install_text = (repo_root / "ops" / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert 'install_sudoers_astinuslab.sh" \\' in install_text
    assert '--helper-source-dir "$RELEASE_DIR/ops/host"' in install_text

    support_files = {"libvirt_storage_admin.py", "fix_libvirt_path_acl.py", "acl_policy.py"}
    assert sudoers_install_helpers == _ALLOWED_HELPERS | support_files, (
        sudoers_install_helpers,
        _ALLOWED_HELPERS,
    )
    assert "libvirt_storage_admin.py" not in sudoers_install_text.split("NOPASSWD:", 1)[-1]


def test_ensure_evidence_dir_creates_dirs_the_acl_helper_will_walk(tmp_path):
    """The privileged helper refuses any group- or world-writable path
    component. A bare mkdir inherits the umask, and 0002 in a developer shell
    yields 0775, so every capture was denied before an ACL was ever applied."""
    import os

    from astinuslab.virtualization.host_helpers import ensure_evidence_dir

    root = tmp_path / "storage"
    root.mkdir()
    target = root / "dumps" / "job-1"

    ensure_evidence_dir(target, root)

    for path in (root / "dumps", target):
        assert path.is_dir()
        assert not os.stat(path).st_mode & 0o022, f"{path} is group or world writable"


def test_ensure_evidence_dir_tightens_a_preexisting_loose_ancestor(tmp_path):
    import os

    from astinuslab.virtualization.host_helpers import ensure_evidence_dir

    root = tmp_path / "storage"
    loose = root / "tmp"
    loose.mkdir(parents=True)
    os.chmod(loose, 0o775)

    ensure_evidence_dir(loose / "job-1", root)

    assert not os.stat(loose).st_mode & 0o022
    # read and execute survive; only the write bits are cleared
    assert os.stat(loose).st_mode & 0o055 == 0o055


def test_ensure_evidence_dir_never_touches_the_storage_root_itself(tmp_path):
    """The installed layout is storage 2770 so the service group can create
    SQLite WAL/SHM siblings, and the root-owned policy names that group. Taking
    its group-write bit away here would break the layout the installer builds."""
    import os

    from astinuslab.virtualization.host_helpers import ensure_evidence_dir

    root = tmp_path / "storage"
    root.mkdir()
    os.chmod(root, 0o2770)

    ensure_evidence_dir(root / "dumps" / "job-1", root)

    assert os.stat(root).st_mode & 0o7777 == 0o2770
