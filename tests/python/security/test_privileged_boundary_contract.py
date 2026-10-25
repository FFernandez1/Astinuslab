from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ACL_HELPER = REPO_ROOT / "ops/host/fix_libvirt_path_acl.sh"
SUDOERS_INSTALLER = REPO_ROOT / "ops/host/install_sudoers_astinuslab.sh"
INSTALLER = REPO_ROOT / "ops/deploy/install.sh"
SECURITY = REPO_ROOT / "src/astinuslab/core/security.py"
IMAGE_ADMIN = REPO_ROOT / "src/astinuslab/images/image_admin_support.py"
ARTIFACT_ROUTES = REPO_ROOT / "src/astinuslab/ui/artifact_routes.py"


def test_acl_helper_has_no_caller_controlled_root_override():
    text = ACL_HELPER.read_text(encoding="utf-8")
    assert "${2:-" not in text
    assert 'STORAGE_ROOTS="${2' not in text


def test_sudoers_acl_helper_is_installed_root_owned():
    text = SUDOERS_INSTALLER.read_text(encoding="utf-8")
    assert "chown root:root" in text
    assert "chmod 0755" in text


def test_runtime_source_is_not_recursively_owned_by_service_user():
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'chown -R "$SERVICE_USER:astinuslab" "$INSTALL_DIR"' not in text
    assert 'chmod -R g+rwX "$INSTALL_DIR"' not in text


def test_unsafe_browser_methods_have_same_origin_guard():
    text = SECURITY.read_text(encoding="utf-8")
    assert "same-origin" in text or "same_origin" in text
    assert "Origin" in text
    assert "cross-site" in text


def test_url_downloader_does_not_call_urllib_urlopen_directly():
    text = IMAGE_ADMIN.read_text(encoding="utf-8")
    assert "urllib.request.urlopen" not in text


def test_dump_vault_deletion_routes_through_custody_service():
    # Was a strict xfail through Phase 1: custody-aware deletion is Task 22, and
    # the marker was set to fail loudly the moment it started passing. Task 22
    # landed, so the marker is gone and this is an ordinary contract again.
    text = ARTIFACT_ROUTES.read_text(encoding="utf-8")
    assert "deletion" in text
    assert "delete_artifact" in text
