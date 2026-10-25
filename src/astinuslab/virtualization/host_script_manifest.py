"""Declarative contract for host shell scripts invoked by the backend.

Update REQUIRED_SCRIPTS when adding/renaming a script invocation. The preflight
(doctor/worker-start) checks presence and exec bits here instead of letting a
rename fail mid-job.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ..core import fake_mode
from ..core.config import Settings, settings
from .host_helpers import _ALLOWED_HELPERS, PRIVILEGED_HELPER_DIR

REQUIRED_SCRIPTS: tuple[str, ...] = (
    "acquire_disk_host.sh",
    "acquire_disk_remote.sh",
    "mount_output_share_remote.sh",
    "rebuild_cloud_image_libvirt_storage.sh",
    "run_guest_script.sh",
)

OPTIONAL_SCRIPTS: tuple[str, ...] = (
    "attach_virtiofs_to_vm.sh",
    "bake_workload.sh",
    "bootstrap_cloud_image_libvirt_storage.sh",
    "build_vol3_symbol_remote.sh",
    "check_guest_disk_space.sh",
    "check_job_vm.sh",
    "check_virtiofs_host_support.sh",
    "cleanup_job_vm.sh",
    "collect_guest_metadata.sh",
    "fix_libvirt_path_acl.sh",
    "install_sudoers_astinuslab.sh",
    "libvirt_storage_admin.sh",
    "network_isolation_admin.sh",
    "prebake_attacker_image.sh",
    "prepare_host_dump_share.sh",
    "prepare_target_image.sh",
    "publish_disk_to_host.sh",
    "rebuild_cloud_image_with_virtiofs.sh",
    "render_virtiofs_xml.sh",
    "save_workload_cache.sh",
    "setup_mounted_output_for_job.sh",
    "start_cloud_image_libvirt_storage.sh",
    "start_cloud_image_libvirt_storage_virtiofs.sh",
    "validate_virtiofs_mount.sh",
    "windows_build/poll_build.sh",
)

ACL_POLICY_PATH = Path("/etc/astinuslab/acl-policy.json")


def host_scripts_dir() -> Path:
    return settings.base_dir / "ops" / "host"


def check_host_scripts() -> list[dict[str, object]]:
    """Return {name, required, status} for each manifest script."""
    results: list[dict[str, object]] = []
    for name, required in [*((n, True) for n in REQUIRED_SCRIPTS), *((n, False) for n in OPTIONAL_SCRIPTS)]:
        path = host_scripts_dir() / name
        if not path.exists():
            status = "missing"
        elif not os.access(path, os.X_OK):
            status = "not_executable"
        else:
            status = "ok"
        results.append({"name": name, "required": required, "status": status})
    return results


def check_privileged_helpers() -> list[dict[str, object]]:
    """Check installed copies and passwordless-sudo coverage for allowed helpers."""
    results: list[dict[str, object]] = []
    for name in sorted(_ALLOWED_HELPERS):
        path = PRIVILEGED_HELPER_DIR / name
        installed = path.exists() and os.access(path, os.X_OK)
        sudo_ok = False
        if installed:
            try:
                proc = subprocess.run(
                    ["sudo", "-n", "-l", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                sudo_ok = proc.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                sudo_ok = False
        results.append({"name": name, "installed": installed, "sudo_ok": sudo_ok})
    return results


def preflight_problems(*, service_identity: str = "runner") -> list[str]:
    if service_identity != "runner":
        return []
    problems = [
        f"host script {r['name']}: {r['status']}" for r in check_host_scripts() if r["required"] and r["status"] != "ok"
    ]
    if fake_mode.FAKE_LIBVIRT:
        return problems
    policy = check_acl_policy_storage_root()
    if policy["code"] != "ready":
        problems.append(policy["message"])
    problems.extend(
        f"privileged helper {r['name']}: " + ("not installed" if not r["installed"] else "no passwordless sudo")
        for r in check_privileged_helpers()
        if not (r["installed"] and r["sudo_ok"])
    )
    return problems


def _acl_policy_roots() -> tuple[tuple[Path, ...], str]:
    try:
        policy = json.loads(ACL_POLICY_PATH.read_text(encoding="utf-8"))
        return tuple(Path(root).resolve() for root in policy.get("allowed_roots", [])), (
            "configured root is not covered by policy"
        )
    except (OSError, TypeError, ValueError) as exc:
        return (), f"policy unreadable: {exc}"


def _covered(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def check_acl_policy_storage_root(settings_obj=settings) -> dict[str, str]:
    """Return readiness status for configured storage versus installed policy."""
    if fake_mode.FAKE_LIBVIRT:
        return {"code": "not_applicable", "message": "ACL policy not applicable in fake libvirt mode"}
    configured = Path(settings_obj.storage_dir).resolve()
    policy_roots, error = _acl_policy_roots()
    if _covered(configured, policy_roots):
        return {"code": "ready", "message": "ACL policy covers configured storage root"}
    command = f"sudo ops/deploy/configure-privileged-roots.sh --install-dir {settings_obj.base_dir} --storage-root {configured}"
    return {
        "code": "acl_policy_storage_root_mismatch",
        "message": (
            f"acl_policy_storage_root_mismatch: configured root {configured}; "
            f"installed policy roots {list(map(str, policy_roots))} ({error}). Fix with: {command}"
        ),
    }


def check_acl_policy_playground_dirs(settings_obj: Settings = settings) -> dict[str, str]:
    """Return readiness status for Playground disk and capture directories versus installed policy.

    They are settings separate from ``storage_dir``. Registering a new storage root
    leaves them where they were, and the ACL helper then refuses every Playground
    launch while the storage-root check alone still reports ready.
    """
    if fake_mode.FAKE_LIBVIRT:
        return {"code": "not_applicable", "message": "ACL policy not applicable in fake libvirt mode"}
    policy_roots, error = _acl_policy_roots()
    uncovered = [
        f"{name}={path}"
        for name in ("playground_disk_dir", "playground_capture_dir")
        if not _covered(path := Path(getattr(settings_obj, name)).resolve(), policy_roots)
    ]
    if not uncovered:
        return {"code": "ready", "message": "ACL policy covers configured Playground directories"}
    return {
        "code": "acl_policy_playground_dir_mismatch",
        "message": (
            f"acl_policy_playground_dir_mismatch: {', '.join(uncovered)}; "
            f"installed policy roots {list(map(str, policy_roots))} ({error}). Fix by setting "
            "ASTINUSLAB_PLAYGROUND_DISK_DIR and ASTINUSLAB_PLAYGROUND_CAPTURE_DIR inside a registered root"
        ),
    }
