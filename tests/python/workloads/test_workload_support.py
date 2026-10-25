from __future__ import annotations

from pathlib import Path


def test_run_workload_install_passes_target_env(tmp_path):
    from astinuslab.workloads.workload_support import run_workload_install

    calls = {}
    script = tmp_path / "guest" / "workloads" / "w" / "install.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    class Settings:
        base_dir = tmp_path

    class Repo:
        def __init__(self, session):
            pass

        def transition_job_status(self, *args, **kwargs):
            pass

        def add_event(self, *args, **kwargs):
            pass

    class Scope:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    def session_scope():
        return Scope()

    def run_command(cmd, cwd, job_id, extra_env=None, timeout=3600.0):
        calls["extra_env"] = extra_env or {}
        return ""

    run_workload_install(
        session_scope=session_scope,
        repo_cls=Repo,
        get_active_key_paths=lambda: (Path("/tmp/key"), Path("/tmp/key.pub")),
        run_command=run_command,
        settings=Settings,
        check_workload_health=lambda job_id, workload, guest_ip: None,
        is_cancel_requested=lambda job_id: False,
        job_id="job-1",
        workload={"id": "w", "display_name": "W", "install_script": "guest/workloads/w/install.sh", "health_check": {}},
        guest_ip="192.0.2.10",
        guest_user="ubuntu",
        target_context={"distro": "ubuntu", "version": "22.04", "family": "debian", "arch": "x86_64"},
    )

    assert calls["extra_env"]["TARGET_DISTRO"] == "ubuntu"
    assert calls["extra_env"]["TARGET_VERSION"] == "22.04"
    assert calls["extra_env"]["TARGET_FAMILY"] == "debian"
    assert calls["extra_env"]["TARGET_ARCH"] == "x86_64"
