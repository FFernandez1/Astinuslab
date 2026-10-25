"""Host-free bindings for a job run, for the test tiers only.

``ASTINUSLAB_FAKE_LIBVIRT`` and ``ASTINUSLAB_FAKE_PLAYGROUND`` stop at the
Playground. Job orchestration keeps reaching for the host: it installs a base
image through ``sudo libvirt_storage_admin.sh``, defines a domain, starts
virtiofsd and SSHes into the guest. On a developer machine where that sudo rule
is configured -- which is the normal state of a machine that runs this product
-- executing a job from a test provisions a real VM with a real disk. That is
why every browser test of Jobs so far has had to stop at admission.

This module replaces the bindings on ``JobRunDeps`` that reach the host, and
nothing else. Everything downstream of them is the real thing: the real runner
walks the real status graph, writes the real events, and is torn down by the
real cleanup manager. Only the hypervisor and the guest are absent.

Two rules hold this to the line the rest of the codebase already draws:

* ``run_command`` refuses. It does not return a canned success for a privileged
  command -- it raises, loudly, naming what was attempted. A path this module
  failed to anticipate therefore fails the test rather than reaching the host,
  which is the only safety property that matters here.
* Nothing in this module writes an artifact or a manifest. Faking a boot is
  faking an observation; faking an acquisition is forging evidence, and this
  module does not do it. ``_FakeDomain`` deliberately has no ``coreDump`` for
  the same reason (see ``test_fake_libvirt_guard.py``), and a job run under
  this flag is expected to reach the capture hand-off and stop there.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..jobs import job_state_support, runtime_support

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime
    from .deps import JobRunDeps

logger = logging.getLogger(__name__)

# Anything that would cross into the host. Matched against the whole argv, so a
# helper invoked by absolute path is caught wherever it is installed.
_PRIVILEGED = ("sudo", "virsh", "virt-install", "qemu-img", "virtiofsd", "ssh", "scp")
_PRIVILEGED_HELPERS = (
    "libvirt_storage_admin.sh",
    "fix_libvirt_path_acl.sh",
    "network_isolation_admin.sh",
    "acquire_disk_host.sh",
    "mount_output_share_remote.sh",
    "run_guest_script.sh",
)

# A guest that does not exist still has to have an address and a kernel: the
# status graph records them, the live page renders them, and the compatibility
# guard reads them. Stable values, so two runs are comparable.
FAKE_GUEST_IP = "192.0.2.10"
FAKE_GUEST_METADATA = {
    "distro_id": "ubuntu",
    "distro_version": "22.04",
    "kernel_release": "5.15.0-fake-orchestration",
    "arch": "x86_64",
}


class PrivilegedCommandRefused(RuntimeError):
    """A job run under the fake asked the host to do something."""


def _refuse(cmd: list[str]) -> None:
    joined = " ".join(str(part) for part in cmd)
    raise PrivilegedCommandRefused(
        f"ASTINUSLAB_FAKE_ORCHESTRATION is set and this command would have reached the host: {joined}"
    )


def apply(deps: JobRunDeps) -> JobRunDeps:
    """Replace every binding on ``deps`` that reaches the host. Mutates and returns it."""

    def run_command(cmd, *args, **kwargs) -> str:
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        joined = " ".join(str(part) for part in argv)
        if any(str(argv[0]).endswith(binary) or binary in joined.split() for binary in _PRIVILEGED):
            _refuse(argv)
        if any(helper in joined for helper in _PRIVILEGED_HELPERS):
            _refuse(argv)
        logger.debug("fake orchestration: swallowed %s", joined)
        return ""

    def boot_and_prepare(job_id: str, *args, **kwargs) -> tuple[str, dict]:
        """Stand in for the whole boot phase, keeping its state effects.

        The real one stages cloud-init, installs a backing image through the
        privileged helper, defines a domain, waits for an address and collects
        guest metadata over SSH or the guest agent. All of that is the host.
        What is *not* the host is the record it leaves -- the guest address, the
        runtime row, the detected distro and kernel, and the two status
        transitions that carry the job from preparing_image to
        mounting_output_share. Those are what the live page renders and what the
        rest of the run reads, so they are performed here for real.
        """
        vm_name = deps.runtime_vm_name(job_id, "target")
        metadata = dict(FAKE_GUEST_METADATA)
        job_state_support.persist_guest_access_and_metadata(
            deps.session_scope,
            deps.job_repository_cls,
            deps.sync_runtime_record,
            job_id=job_id,
            vm_name=vm_name,
            guest_ip=FAKE_GUEST_IP,
            metadata=metadata,
        )
        return FAKE_GUEST_IP, metadata

    def boot_simple_vm(*args, **kwargs) -> str:
        job_id = kwargs.get("job_id") or (args[0] if args else "unknown")
        return deps.runtime_vm_name(str(job_id), "attacker")

    def teardown_vm(job_id: str) -> bool:
        role = "attacker" if job_id.endswith("-attacker") else "target"
        canonical = job_id[:-9] if role == "attacker" else job_id
        runtime_support.mark_runtime_destroyed(deps.session_scope, deps.job_repository_cls, canonical, role)
        return True

    def teardown_attacker_vm(job_id: str) -> None:
        teardown_vm(f"{job_id}-attacker")

    deps.run_command = run_command
    deps.require_noninteractive_sudo = lambda: None
    # Admission already reads host RAM and disk elsewhere; inside a run this
    # check only serves to abort on a busy developer machine.
    deps.check_host_disk_space = lambda job_id, required_gb=6: None
    deps.boot_and_prepare = boot_and_prepare
    deps.boot_simple_vm = boot_simple_vm
    deps.teardown_vm = teardown_vm
    deps.teardown_attacker_vm = teardown_attacker_vm
    deps.apply_network_isolation = lambda *args, **kwargs: False
    deps.remove_network_isolation = lambda *args, **kwargs: None
    deps.run_post_boot_script = lambda *args, **kwargs: None
    deps.create_lab_snapshot = lambda *args, **kwargs: "fake-orchestration-snapshot"
    deps.provision_workload = lambda *args, **kwargs: None
    deps.provision_stack = lambda *args, **kwargs: None
    deps.run_workload_install = lambda *args, **kwargs: None
    deps.check_workload_health = lambda *args, **kwargs: None
    deps.save_workload_cache = lambda *args, **kwargs: None
    deps.start_virtiofsd = lambda job_id, socket_path, share_dir, pid_file_path: 0
    deps.wait_for_guest_ssh = lambda *args, **kwargs: None
    deps.wait_for_guest_agent = lambda *args, **kwargs: None
    deps.parse_guest_ip = lambda *args, **kwargs: FAKE_GUEST_IP
    deps.verify_guest_disk_capacity = lambda *args, **kwargs: None
    deps.record_verified_ssh_host_key = lambda *args, **kwargs: None
    deps.collect_guest_metadata_via_qga = lambda vm_name, libvirt_uri: dict(FAKE_GUEST_METADATA)
    deps.collect_guest_metadata_via_ssh = lambda guest_ip, ssh_key, guest_user: dict(FAKE_GUEST_METADATA)
    deps.write_guest_metadata_json = lambda dump_dir, job_id, metadata: None
    return deps


def fake_guest_metadata() -> dict:
    """The metadata a faked guest reports. Copy, so a caller cannot edit the source."""
    return dict(FAKE_GUEST_METADATA)


def privileged_markers() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What ``run_command`` refuses. Exposed so a test asserts the real list."""
    return _PRIVILEGED, _PRIVILEGED_HELPERS
