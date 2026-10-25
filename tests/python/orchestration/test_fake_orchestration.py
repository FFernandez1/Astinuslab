"""A job run that never reaches the host.

``ASTINUSLAB_FAKE_ORCHESTRATION`` exists so the Jobs lifecycle can be driven
past admission. Without it a job run installs a base image through
``sudo libvirt_storage_admin.sh`` and defines a domain, so on any machine where
that sudo rule is configured -- the normal state of a machine that runs this
product -- executing a job from a test provisions a real VM. That is why every
browser test of Jobs has stopped at the moment of creation.

What the flag buys and what it deliberately does not:

* a ``manual_trigger`` job walks the real status graph to the capture hand-off,
  so stage progression, cancel and the capture trigger become reachable;
* a capture job runs the same boot and then **fails** where it would have
  produced evidence. Faking a boot is faking an observation; faking an
  acquisition is forging an artifact, and the fake does not do it. That
  boundary is the same one ``test_fake_libvirt_guard.py`` holds for
  ``_FakeDomain.coreDump``, and it is asserted here rather than assumed.

The refusal is the safety property. ``run_command`` does not return a canned
success for a privileged command; it raises. A path the fake failed to
anticipate therefore fails the test instead of reaching the host.
"""

from __future__ import annotations

import pytest

from astinuslab.orchestration import fake_runtime


@pytest.fixture(autouse=True)
def _available_host_capacity(monkeypatch):
    """Admission must not depend on free space on the developer host."""
    from astinuslab.jobs import capacity_host
    from astinuslab.jobs.capacity import GIB, HostCapacity

    monkeypatch.setattr(
        capacity_host,
        "read_host_capacity",
        lambda *a, **k: HostCapacity(65536, 60000, 1000 * GIB, 900 * GIB, {}, synthetic=True),
    )


@pytest.fixture()
def faked(in_memory_session_scope, monkeypatch):
    """Deps built with the flag on, pointed at an in-memory database."""
    import astinuslab.db as db_mod
    from astinuslab.core import fake_mode
    from astinuslab.orchestration.deps import JobRunDeps

    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", True)
    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    deps = JobRunDeps.default()
    deps.session_scope = in_memory_session_scope
    return deps


def _create_job(in_memory_session_scope, scenario_id: str, execution_mode: str) -> str:
    from astinuslab.api.schemas import JobCreate
    from astinuslab.core.config import settings
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs.job_services import create_job_service

    with in_memory_session_scope() as session:
        job = create_job_service(
            session,
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id=scenario_id,
                execution_mode=execution_mode,
            ),
        )
        JobRepository(session).update_job(
            job.id,
            image_path=str((settings.images_dir / f"{job.id}-target.qcow2").resolve()),
        )
        return str(job.id)


def _job(in_memory_session_scope, job_id: str):
    from astinuslab.db.repositories import ArtifactRepository, JobRepository

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        assert job is not None
        return {
            "status": job.status,
            "stage": job.current_stage,
            "guest_ip": job.guest_ip,
            "kernel": job.detected_kernel_release,
            "distro": job.detected_distro,
            "error": job.error_summary or "",
            "events": [event.state for event in repo.list_events(job_id)],
            "artifacts": len(ArtifactRepository(session).list_artifacts(job_id=job_id)),
        }


def test_a_live_session_job_reaches_the_capture_handoff(in_memory_session_scope, faked):
    from astinuslab.live_session.runner import run_live_session_job

    job_id = _create_job(in_memory_session_scope, "lab-session", "manual_trigger")
    run_live_session_job(job_id, deps=faked)

    job = _job(in_memory_session_scope, job_id)
    assert job["status"] == "waiting_for_capture"
    assert job["stage"] == "waiting_for_capture"
    # The record a real boot leaves behind, left behind by the fake one: this is
    # what the live page renders and what the compatibility guard reads.
    assert job["guest_ip"] == fake_runtime.FAKE_GUEST_IP
    assert job["kernel"] == fake_runtime.fake_guest_metadata()["kernel_release"]
    assert job["distro"] == fake_runtime.fake_guest_metadata()["distro_id"]
    # The real status graph, walked in order -- not a jump to the end state.
    # waiting_for_guest_access is recorded twice (address, then metadata), so
    # compare the sequence of transitions rather than the raw event list.
    boot_phases = ("preparing_image", "waiting_for_guest_access", "mounting_output_share")
    walked: list[str] = []
    for state in job["events"]:
        if state in boot_phases and (not walked or walked[-1] != state):
            walked.append(state)
    assert walked == list(boot_phases)


def test_a_capture_job_stops_where_it_would_produce_evidence(in_memory_session_scope, faked):
    from astinuslab.orchestration.runners.boot_capture_runner import run_immediate_job

    job_id = _create_job(in_memory_session_scope, "immediate-capture", "immediate")
    run_immediate_job(job_id, deps=faked)

    job = _job(in_memory_session_scope, job_id)
    assert job["status"] == "failed", "the fake must not carry a capture job to completion"
    assert job["artifacts"] == 0, "a faked run published an artifact"
    assert job["error"], "the failure has to say something"
    # It still got through the whole boot phase first -- the failure is at the
    # evidence boundary, not at the first thing that touches the host.
    assert "waiting_for_guest_access" in job["events"]


@pytest.mark.parametrize(
    "command",
    [
        ["sudo", "-n", "/usr/local/libexec/astinuslab/libvirt_storage_admin.sh", "install_base_image"],
        ["/usr/local/libexec/astinuslab/libvirt_storage_admin.sh", "ensure_dirs"],
        ["virsh", "-c", "qemu:///system", "list", "--all"],
        ["bash", "/opt/astinuslab/ops/host/mount_output_share_remote.sh", "192.0.2.10"],
        ["ssh", "analyst@192.0.2.10", "uname", "-r"],
    ],
)
def test_the_fake_refuses_a_command_that_would_reach_the_host(faked, command):
    with pytest.raises(fake_runtime.PrivilegedCommandRefused) as raised:
        faked.run_command(command)
    # The message has to name what was attempted, or a failing test tells the
    # next person nothing about which path escaped.
    assert command[0] in str(raised.value)


def test_an_ordinary_command_is_swallowed_rather_than_run(faked):
    assert faked.run_command(["true"]) == ""


def test_the_flag_leaves_the_production_wiring_alone(monkeypatch):
    from astinuslab.core import fake_mode
    from astinuslab.orchestration.deps import JobRunDeps

    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", False)
    deps = JobRunDeps.default()
    assert deps.run_command.__qualname__.startswith("JobRunDeps.default")
    assert deps.boot_and_prepare.__qualname__.startswith("JobRunDeps.default")


def test_the_flag_is_reported_to_the_operator(monkeypatch):
    from astinuslab.core import fake_mode

    monkeypatch.setattr(fake_mode, "FAKE_LIBVIRT", False)
    monkeypatch.setattr(fake_mode, "FAKE_PLAYGROUND", False)
    monkeypatch.setattr(fake_mode, "FAKE_ORCHESTRATION", True)
    assert fake_mode.fake_mode_active() is True
    assert fake_mode.fake_mode_flags()["fake_orchestration"] is True
    banner = fake_mode.fake_mode_banner()
    assert banner is not None and "fake_orchestration" in banner
