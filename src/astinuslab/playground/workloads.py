from __future__ import annotations

import logging
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .. import catalog
from ..core.config import settings
from ..storage.keystore import get_active_key_paths
from ..workloads.workload_support import check_workload_health


@dataclass(frozen=True)
class PlaygroundWorkloadValidation:
    ok: bool
    status_code: int = 200
    detail: str = ""
    workload: dict[str, Any] | None = None
    compatibility_status: str = "unknown"
    compatibility_reason: str | None = None


def target_context_from_playground_image(image: dict[str, Any]) -> dict[str, Any]:
    distro = str(image.get("distro") or "unknown").lower()
    family = str(image.get("family") or "generic").lower()
    version = str(image.get("version") or "unknown").lower()
    arch = str(image.get("arch") or image.get("architecture") or "x86_64").lower()
    return {
        "id": image.get("id", "unknown"),
        "distro": distro,
        "version": version,
        "family": family,
        "arch": arch,
        "disk_gb": image.get("disk_gb"),
    }


def build_playground_service_outputs(
    workload: dict[str, Any], *, guest_ip: str | None, status: str
) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    for svc in workload.get("services", []):
        port = int(svc["port"])
        url_path = svc.get("url_path", "/")
        url = f"http://{guest_ip}:{port}{url_path}" if guest_ip else None
        services.append(
            {
                "name": svc["name"],
                "port": port,
                "url_path": url_path,
                "url": url,
                "credentials": svc.get("credentials"),
                "status": status,
            }
        )
    return services


def validate_playground_workload_selection(
    *,
    role: str,
    workload_id: str | None,
    image: dict[str, Any],
) -> PlaygroundWorkloadValidation:
    if not workload_id:
        return PlaygroundWorkloadValidation(ok=True)
    if role != "target":
        return PlaygroundWorkloadValidation(
            ok=False, status_code=400, detail="workload_id is supported for targets only"
        )

    workload = catalog.workload_by_id(workload_id)
    if workload is None:
        return PlaygroundWorkloadValidation(ok=False, status_code=400, detail=f"workload_id '{workload_id}' not found")

    compatibility = catalog.workload_compatibility_for_target(workload, target_context_from_playground_image(image))
    status = str(compatibility.get("compatibility_status") or "unknown")
    reason = compatibility.get("compatibility_reason")
    if status == "incompatible":
        detail = (
            f"Workload '{workload_id}' is incompatible with image '{image.get('id')}': {reason or 'no reason provided'}"
        )
        return PlaygroundWorkloadValidation(
            ok=False,
            status_code=400,
            detail=detail,
            workload=workload,
            compatibility_status=status,
            compatibility_reason=reason,
        )

    return PlaygroundWorkloadValidation(
        ok=True,
        workload=workload,
        compatibility_status=status,
        compatibility_reason=reason,
    )


def annotated_workloads_for_playground_image(image: dict[str, Any]) -> list[dict[str, Any]]:
    workloads = catalog.load_workloads()
    workload_map = {workload["id"]: workload for workload in workloads}
    target = target_context_from_playground_image(image)
    annotated: list[dict[str, Any]] = []
    for workload in workloads:
        compatibility = catalog.workload_compatibility_for_target(workload, target, workload_map=workload_map)
        annotated.append({**workload, **compatibility})
    return annotated


class _NoopRepo:
    def __init__(self, _session):
        pass

    def add_event(self, *_args, **_kwargs) -> None:
        return None


@contextmanager
def _noop_session_scope():
    yield None


def _run_command(cmd: list[str], *, cwd, extra_env: dict[str, str] | None = None) -> None:
    env = {**os.environ, **extra_env} if extra_env else None
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def install_playground_workload(
    *,
    workload: dict[str, Any],
    guest_ip: str,
    guest_user: str,
    target_context: dict[str, Any] | None = None,
) -> None:
    if workload.get("is_stack"):
        for component_id in workload.get("components", []):
            component = catalog.workload_by_id(component_id)
            if component is None:
                raise RuntimeError(f"Workload component not found: {component_id}")
            install_playground_workload(
                workload=component,
                guest_ip=guest_ip,
                guest_user=guest_user,
                target_context=target_context,
            )
        return

    install_script_rel = workload.get("install_script")
    if not install_script_rel:
        raise RuntimeError(f"Workload {workload['id']} has no install_script defined")
    script_path = settings.base_dir / install_script_rel
    if not script_path.exists():
        raise RuntimeError(f"Workload install script not found: {script_path}")

    ssh_priv, _ = get_active_key_paths()
    health_cfg = workload.get("health_check", {})
    timeout_s = int(health_cfg.get("timeout_seconds", 300)) + 300
    extra_env: dict[str, str] = {"GUEST_USER": guest_user}
    if target_context:
        for source_key, env_key in (
            ("distro", "TARGET_DISTRO"),
            ("version", "TARGET_VERSION"),
            ("family", "TARGET_FAMILY"),
            ("arch", "TARGET_ARCH"),
        ):
            if target_context.get(source_key):
                extra_env[env_key] = str(target_context[source_key])

    _run_command(
        [
            "bash",
            str(settings.base_dir / "ops" / "host" / "run_guest_script.sh"),
            guest_ip,
            str(ssh_priv),
            str(script_path),
            str(timeout_s),
        ],
        cwd=settings.base_dir,
        extra_env=extra_env,
    )
    check_workload_health(
        _noop_session_scope,
        _NoopRepo,
        lambda _job_id: False,
        logging.getLogger(__name__),
        time,
        "playground",
        workload,
        guest_ip,
    )


def best_effort_health_check(workload: dict[str, Any], guest_ip: str) -> None:
    """Wait for a prebaked workload's service to answer; never raise.

    A prebaked image already has the workload installed with its services
    enabled via systemd, so there is nothing to install — this only lets the
    service finish coming up before the VM is marked ready, and tolerates a
    slow first boot rather than failing the VM.
    """
    try:
        check_workload_health(
            _noop_session_scope,
            _NoopRepo,
            lambda _job_id: False,
            logging.getLogger(__name__),
            time,
            "playground",
            workload,
            guest_ip,
        )
    except Exception as exc:
        logging.getLogger(__name__).info("prebaked workload health check non-fatal: %s", exc)
