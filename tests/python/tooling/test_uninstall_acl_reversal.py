"""Contract for reversing the traversal grants the installer made.

The installer walks the storage path to `/` adding `u:libvirt-qemu:x` to every
component so qemu can reach a capture, and that walk crosses directories it does
not own — an operator's home among them. Uninstall has to take back exactly what
it granted and nothing else, which means recording what it granted.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALL = ROOT / "ops/deploy/install.sh"
UNINSTALL = ROOT / "ops/deploy/uninstall.sh"
PRIVILEGED_STATE = ROOT / "ops/deploy/privileged_state.py"
pytestmark = [pytest.mark.unit, pytest.mark.host]


def run_bash(script: str, *, env=None, cwd=ROOT):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(["bash", "-c", script], cwd=cwd, env=merged, capture_output=True, text=True)


def run_root_mapped_bash(script: str, *, env=None, cwd=ROOT):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", "bash", "-c", script],
        cwd=cwd,
        env=merged,
        capture_output=True,
        text=True,
    )


def run_fakeroot_bash(script: str, *, env=None, cwd=ROOT):
    """Run a script in a view where ownership can be staged arbitrarily.

    ``run_root_mapped_bash`` cannot be used to stage an *unprivileged* owner.
    Inside a user namespace ``chown`` is refused with EPERM no matter how
    complete the capability set is, because the kernel checks CAP_CHOWN against
    the user namespace that owns the filesystem's superblock -- the initial one
    for anything under /tmp -- and not against the namespace we just created.
    A failed ``chown`` there leaves the file root-owned and the custody guard
    correctly does nothing, so the test passes over a condition it never built.

    fakeroot intercepts the stat family in the process and its children, so the
    parent still reads as root-owned (what the guard requires before it will
    look at the file at all) while the template reads as owned by someone else.
    """
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["fakeroot", "bash", "-c", script],
        cwd=cwd,
        env=merged,
        capture_output=True,
        text=True,
    )


def _require_fakeroot():
    if shutil.which("fakeroot") is None:
        pytest.skip("fakeroot is not available")


def _unsafe_custody_setup(template: Path, unsafe_custody: str) -> str:
    """Stage one flavour of unsafe custody, failing loudly if it does not take.

    The exit guard matters: the previous version of these tests staged the
    unprivileged case with a bare ``chown`` whose EPERM was discarded, which is
    what let them assert against a template that was still perfectly protected.
    """
    if unsafe_custody == "writable":
        template.chmod(0o666)
        return ":"
    return f"chown 1:1 {shlex.quote(str(template))} || exit 92"


def _require_acl_tools():
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("ACL tools are not available")
    if subprocess.run(["getent", "passwd", "libvirt-qemu"], capture_output=True).returncode != 0:
        pytest.skip("libvirt-qemu user is not available")


def _named_entry(path: Path) -> str | None:
    text = subprocess.run(["getfacl", "-cp", str(path)], capture_output=True, text=True, check=True).stdout
    for line in text.splitlines():
        if line.startswith("user:libvirt-qemu:"):
            return line
    return None


def _v2_record(path: Path, *, user: str = "libvirt-qemu") -> str:
    info = path.stat()
    return f"v2\t{user}\t{info.st_dev}\t{info.st_ino}\t{path}\n"


def _revoke_harness(manifest: Path) -> str:
    """uninstall.sh is a linear script with no source guard, so sourcing it
    would run the uninstall. Extract just the function under test."""
    text = UNINSTALL.read_text(encoding="utf-8")
    start = text.index("revoke_recorded_traversal_grants() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    return f"""
    ok() {{ echo "$@"; }}
    SCRIPT_DIR={shlex.quote(str(ROOT / "ops/deploy"))}
    TRAVERSAL_ACL_MANIFEST={shlex.quote(str(manifest))}
    {text[start:end]}
    revoke_recorded_traversal_grants
    """


def _function_body(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    marker = f"{name}() {{"
    assert marker in text, f"{name} must be defined"
    start = text.index(marker)
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _revoke_mac_harness(manifest: Path) -> str:
    function = _function_body(UNINSTALL, "revoke_recorded_mac_changes")
    return f"""
    ok() {{ :; }}
    warn() {{ echo "$*" >&2; }}
    SCRIPT_DIR={shlex.quote(str(ROOT / "ops/deploy"))}
    MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
    {function}
    revoke_recorded_mac_changes
    """


@pytest.mark.parametrize("unit_shape", ["single", "split"])
def test_uninstall_removes_masked_units_and_second_cleanup_is_idempotent(tmp_path, unit_shape):
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    suffix = "@.service" if unit_shape == "single" else ".service"
    for name in ("astinuslab-api", "astinuslab-runner", "astinuslab-analysis"):
        (unit_dir / f"{name}{suffix}").symlink_to("/dev/null")
    remove_units = _function_body(UNINSTALL, "remove_managed_systemd_units")
    explicit_split = _function_body(UNINSTALL, "explicit_split_units_present")

    result = run_bash(f"""
    ok() {{ :; }}
    systemctl() {{ return 1; }}
    SYSTEMD_UNIT_DIR={shlex.quote(str(unit_dir))}
    {explicit_split}
    {remove_units}
    remove_managed_systemd_units
    remove_managed_systemd_units
    if explicit_split_units_present; then exit 91; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert list(unit_dir.iterdir()) == []


def test_the_traversal_grant_records_only_the_components_it_added(tmp_path):
    """A component that already carried the entry was not ours to grant, so
    removing it later would take away an operator's own access."""
    _require_acl_tools()
    chain = tmp_path / "a" / "b" / "storage"
    chain.mkdir(parents=True)
    preexisting = tmp_path / "a"
    subprocess.run(["setfacl", "-m", "u:libvirt-qemu:x", str(preexisting)], check=True)
    manifest = tmp_path / "manifest"

    result = run_bash(f"""
    source {INSTALL}
    TRAVERSAL_ACL_MANIFEST={shlex.quote(str(manifest))}
    grant_qemu_traversal {shlex.quote(str(chain))} {shlex.quote(str(tmp_path))}
    """)

    assert result.returncode == 0, result.stderr
    lines = [line.split("\t") for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all(len(parts) == 5 and parts[0] == "v2" for parts in lines)
    recorded = [parts[-1] for parts in lines]
    assert str(chain) in recorded
    assert str(tmp_path / "a" / "b") in recorded
    assert str(preexisting) not in recorded, "an entry we did not add must not be recorded"
    assert _named_entry(preexisting) is not None, "and must still be there"


def test_rollback_removes_acl_recreated_from_a_stale_baseline_record(tmp_path):
    _require_acl_tools()
    target = tmp_path / "storage"
    target.mkdir()
    acl_manifest = tmp_path / "traversal.manifest"
    acl_manifest.write_text(_v2_record(target), encoding="utf-8")
    acl_manifest.chmod(0o600)
    mac_manifest = tmp_path / "mac.manifest"
    helper = ROOT / "ops" / "deploy" / "configure-privileged-roots.sh"

    result = run_bash(
        f"""
        source {INSTALL}
        source {helper}
        TRAVERSAL_ACL_MANIFEST={shlex.quote(str(acl_manifest))}
        MAC_POLICY_MANIFEST={shlex.quote(str(mac_manifest))}
        STORAGE_ROOT={shlex.quote(str(target))}
        snapshot_host_access_manifests
        grant_qemu_traversal {shlex.quote(str(target))} {shlex.quote(str(tmp_path))}
        test -n "$HOST_ACCESS_TRANSACTION_LOG"
        rollback_host_access_delta
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _named_entry(target) is None
    assert acl_manifest.read_text(encoding="utf-8") == _v2_record(target)


def test_the_grant_is_idempotent_across_reruns(tmp_path):
    _require_acl_tools()
    chain = tmp_path / "storage"
    chain.mkdir()
    manifest = tmp_path / "manifest"
    harness = f"""
    source {INSTALL}
    TRAVERSAL_ACL_MANIFEST={shlex.quote(str(manifest))}
    grant_qemu_traversal {shlex.quote(str(chain))} {shlex.quote(str(tmp_path))}
    """

    assert run_bash(harness).returncode == 0
    first = manifest.read_text(encoding="utf-8")
    assert run_bash(harness).returncode == 0

    assert manifest.read_text(encoding="utf-8") == first, "a rerun must not record the same grant twice"


def test_uninstall_removes_only_the_recorded_entries(tmp_path):
    _require_acl_tools()
    ours = tmp_path / "granted-by-us"
    theirs = tmp_path / "granted-by-operator"
    ours.mkdir()
    theirs.mkdir()
    for path in (ours, theirs):
        subprocess.run(["setfacl", "-m", "u:libvirt-qemu:x", str(path)], check=True)
    manifest = tmp_path / "manifest"
    manifest.write_text(_v2_record(ours), encoding="utf-8")
    manifest.chmod(0o600)

    result = run_bash(_revoke_harness(manifest))

    assert result.returncode == 0, result.stderr
    assert _named_entry(ours) is None, "the entry we recorded must be gone"
    assert _named_entry(theirs) is not None, "an entry we never recorded is not ours to remove"
    assert not manifest.exists(), "the manifest goes with the grants it described"


def test_uninstall_tolerates_a_recorded_path_that_no_longer_exists(tmp_path):
    _require_acl_tools()
    survivor = tmp_path / "still-here"
    survivor.mkdir()
    subprocess.run(["setfacl", "-m", "u:libvirt-qemu:x", str(survivor)], check=True)
    manifest = tmp_path / "manifest"
    gone = tmp_path / "gone"
    gone.mkdir()
    gone_record = _v2_record(gone)
    gone.rmdir()
    manifest.write_text(gone_record + _v2_record(survivor), encoding="utf-8")
    manifest.chmod(0o600)

    result = run_bash(_revoke_harness(manifest))

    assert result.returncode == 0, result.stderr
    assert _named_entry(survivor) is None


def test_uninstall_runs_the_revocation_and_refuses_an_orphaned_helper_directory():
    text = UNINSTALL.read_text(encoding="utf-8")
    support = PRIVILEGED_STATE.read_text(encoding="utf-8")

    assert "revoke_recorded_traversal_grants" in text
    # An orphan left behind means an unknown privileged file survived; saying
    # "removed" there would be a lie.
    assert "privileged helper directory still contains unknown files" in support
    assert "O_NOFOLLOW" in support


def test_uninstall_refuses_symlinked_helper_root_without_deleting_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    helper = outside / "acl_policy.py"
    helper.write_text("operator-owned\n", encoding="utf-8")
    linked_root = tmp_path / "libexec"
    linked_root.symlink_to(outside, target_is_directory=True)
    function = _function_body(UNINSTALL, "remove_installed_privileged_helpers")

    result = run_bash(f"""
        HELPER_INSTALL_DIR={shlex.quote(str(linked_root))}
        HELPER_SET=(acl_policy.py)
        {function}
        remove_installed_privileged_helpers
    """)

    assert result.returncode != 0
    assert helper.read_text(encoding="utf-8") == "operator-owned\n"
    assert linked_root.is_symlink()


def test_helper_install_refuses_symlinked_root_without_deleting_target(tmp_path):
    installer = ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "acl_policy.py"
    sentinel.write_text("operator-owned\n", encoding="utf-8")
    linked_root = tmp_path / "libexec"
    linked_root.symlink_to(outside, target_is_directory=True)
    install_root = tmp_path / "install"
    install_root.mkdir()

    result = run_bash(f"""
        source {installer}
        INSTALL_DIR={shlex.quote(str(install_root))}
        HELPER_INSTALL_DIR={shlex.quote(str(linked_root))}
        HELPER_SET=(acl_policy.py)
        install_helpers
    """)

    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "operator-owned\n"
    assert linked_root.is_symlink()


def test_uninstall_stops_and_disables_both_managed_unit_families(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    calls = tmp_path / "calls"
    function = _function_body(UNINSTALL, "stop_and_disable_managed_services")
    units = [
        "astinuslab-api.service",
        "astinuslab-runner.service",
        "astinuslab-analysis.service",
        "astinuslab-api@operator.service",
        "astinuslab-runner@operator.service",
        "astinuslab-analysis@operator.service",
    ]
    rendered_units = " ".join(shlex.quote(unit) for unit in units)

    result = run_bash(f"""
        SERVICE_UNITS=({rendered_units})
        STATE={shlex.quote(str(state))}
        CALLS={shlex.quote(str(calls))}
        for unit in "${{SERVICE_UNITS[@]}}"; do
          touch "$STATE/active-$unit" "$STATE/enabled-$unit"
        done
        ok() {{ :; }}
        systemctl() {{
          local action="$1" unit="${{2:-}}"
          case "$action" in
            show)
              case "$4" in
                ActiveState) [[ -e "$STATE/active-$unit" ]] && echo active || echo inactive ;;
                MainPID) [[ -e "$STATE/active-$unit" ]] && echo 1234 || echo 0 ;;
              esac
              ;;
            is-enabled) [[ -e "$STATE/enabled-${{3}}" ]] ;;
            stop) echo "stop ${{2}}" >> "$CALLS"; rm -f "$STATE/active-${{2}}" ;;
            disable) echo "disable ${{2}}" >> "$CALLS"; rm -f "$STATE/enabled-${{2}}" ;;
            *) return 2 ;;
          esac
        }}
        {function}
        stop_and_disable_managed_services
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        operation for unit in units for operation in (f"stop {unit}", f"disable {unit}")
    ]


def test_uninstall_stops_an_activating_managed_service(tmp_path):
    calls = tmp_path / "calls"
    function = _function_body(UNINSTALL, "stop_and_disable_managed_services")
    result = run_bash(f"""
        SERVICE_UNITS=(astinuslab-api@operator.service)
        active_state=activating
        main_pid=4321
        ok() {{ :; }}
        systemctl() {{
          local action="$1" unit="${{2:-}}"
          case "$action" in
            show)
              case "$4" in
                ActiveState) echo "$active_state" ;;
                MainPID) echo "$main_pid" ;;
              esac
              ;;
            stop)
              echo "stop $unit" >> {shlex.quote(str(calls))}
              active_state=inactive
              main_pid=0
              ;;
            is-enabled) return 1 ;;
            *) return 2 ;;
          esac
        }}
        {function}
        stop_and_disable_managed_services
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == ["stop astinuslab-api@operator.service"]


@pytest.mark.parametrize("unit_state", ["enabled", "enabled-runtime", "linked", "linked-runtime"])
def test_uninstall_detects_inactive_foreign_template_instance_in_any_linked_state(unit_state):
    function = _function_body(UNINSTALL, "foreign_template_instances")
    result = run_bash(f"""
        SERVICE_USER=operator
        systemctl() {{
          if [[ "$1" == list-unit-files ]]; then
            echo 'astinuslab-api@.service enabled'
            echo 'astinuslab-api@other.service {unit_state}'
          fi
        }}
        {function}
        foreign_template_instances
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["astinuslab-api@other.service"]

    assert "--state=enabled" not in function


def test_uninstall_does_not_treat_failed_managed_instance_bullet_as_a_unit():
    function = _function_body(UNINSTALL, "foreign_template_instances")
    result = run_bash(f"""
        SERVICE_USER=operator
        systemctl() {{
          if [[ "$1" == list-units ]]; then
            echo '● astinuslab-api@operator.service loaded failed failed managed'
          fi
        }}
        {function}
        foreign_template_instances
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    assert "--plain" in function


def test_legacy_vol3_cleanup_is_idempotent_when_install_root_is_already_missing(tmp_path):
    missing = tmp_path / "already-removed"

    result = subprocess.run(
        ["python3", str(PRIVILEGED_STATE), "clean-legacy-vol3-bin", str(missing)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["skipped"]


def test_uninstall_quiesces_services_before_revoking_mac_policy():
    text = UNINSTALL.read_text(encoding="utf-8")
    quiesce_call = text.index("stop_and_disable_managed_services \\\n")
    mac_revoke_call = text.index("mac_manifest_present=false")

    assert quiesce_call < mac_revoke_call


def test_the_installer_records_the_manifest_root_owned():
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("grant_qemu_traversal()") : text.index("configure_mac()")]

    assert "TRAVERSAL_ACL_MANIFEST" in body
    assert "/etc/astinuslab" in text, "the manifest belongs beside the other root-owned state"


def test_a_preexisting_entry_without_effective_execute_is_rejected(tmp_path):
    """An entry's presence is not proof qemu can traverse: it may lack `x` or
    be clamped by the ACL mask. The installer must preserve an operator-owned
    entry, but it must not report a usable host."""
    _require_acl_tools()
    chain = tmp_path / "storage"
    chain.mkdir()
    subprocess.run(["setfacl", "-m", "u:libvirt-qemu:r--,m::r--", str(chain)], check=True)
    manifest = tmp_path / "manifest"

    result = run_bash(f"""
    source {INSTALL}
    TRAVERSAL_ACL_MANIFEST={shlex.quote(str(manifest))}
    grant_qemu_traversal {shlex.quote(str(chain))} {shlex.quote(str(tmp_path))}
    """)

    assert result.returncode != 0
    assert "effective execute" in result.stderr
    assert not manifest.exists(), "an operator-owned entry must not become ours"


def test_a_manifest_write_failure_cannot_leave_an_unrecorded_grant(tmp_path):
    _require_acl_tools()
    chain = tmp_path / "storage"
    chain.mkdir()
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("occupied", encoding="utf-8")
    manifest = blocked_parent / "manifest"

    result = run_bash(f"""
    source {INSTALL}
    TRAVERSAL_ACL_MANIFEST={shlex.quote(str(manifest))}
    grant_qemu_traversal {shlex.quote(str(chain))} {shlex.quote(str(tmp_path))}
    """)

    assert result.returncode != 0
    assert _named_entry(chain) is None, "a grant without a durable record cannot be left behind"


def test_acl_crash_after_mutation_never_creates_a_false_ownership_record(tmp_path):
    _require_acl_tools()
    chain = tmp_path / "storage"
    chain.mkdir()
    manifest = tmp_path / "manifest"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_setfacl = shutil.which("setfacl")
    wrapper = fake_bin / "setfacl"
    wrapper.write_text(
        f'#!/bin/sh\n{shlex.quote(real_setfacl)} "$@"\nkill -KILL "$PPID"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = run_bash(
        f"""
        source {INSTALL}
        TRAVERSAL_ACL_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal {shlex.quote(str(chain))} {shlex.quote(str(tmp_path))}
        """,
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode != 0
    assert _named_entry(chain) is not None, "the injected crash occurs after the host mutation"
    assert not manifest.exists() or manifest.read_text(encoding="utf-8") == ""
    subprocess.run([real_setfacl, "-x", "u:libvirt-qemu", str(chain)], check=True)


def test_installer_refuses_to_launder_a_writable_acl_manifest(tmp_path):
    _require_acl_tools()
    chain = tmp_path / "storage"
    chain.mkdir()
    manifest = tmp_path / "manifest"
    attacker_record = "v2\tlibvirt-qemu\t1\t2\t/etc\n"
    manifest.write_text(attacker_record, encoding="utf-8")
    manifest.chmod(0o666)

    result = run_bash(f"""
    source {INSTALL}
    TRAVERSAL_ACL_MANIFEST={shlex.quote(str(manifest))}
    grant_qemu_traversal {shlex.quote(str(chain))} {shlex.quote(str(tmp_path))}
    """)

    assert result.returncode != 0
    assert manifest.read_text(encoding="utf-8") == attacker_record
    assert manifest.stat().st_mode & 0o777 == 0o666
    assert _named_entry(chain) is None


def test_installer_refuses_to_launder_a_writable_mac_manifest(tmp_path):
    manifest = tmp_path / "mac-policy.manifest"
    attacker_record = json.dumps({"version": 2, "kind": "apparmor", "target": "/etc/passwd", "value": "x"}) + "\n"
    manifest.write_text(attacker_record, encoding="utf-8")
    manifest.chmod(0o666)

    result = run_bash(f"""
    source {INSTALL}
    MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
    record_mac_change apparmor /etc/apparmor.d/libvirt/TEMPLATE.qemu '  "/srv/astinuslab/**" rwk,'
    """)

    assert result.returncode != 0
    assert manifest.read_text(encoding="utf-8") == attacker_record
    assert manifest.stat().st_mode & 0o777 == 0o666


def test_mac_manifest_append_failure_restores_the_previous_parseable_bytes(tmp_path):
    manifest = tmp_path / "mac-policy.manifest"
    baseline = (
        json.dumps(
            {"version": 2, "kind": "apparmor", "target": "/etc/apparmor.d/existing", "value": "existing"},
            sort_keys=True,
        )
        + "\n"
    )
    manifest.write_text(baseline, encoding="utf-8")
    manifest.chmod(0o600)
    oversized_value = "x" * 4096

    result = run_bash(f"""
        ulimit -f 1
        source {INSTALL}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        record_mac_change apparmor /etc/apparmor.d/new {shlex.quote(oversized_value)}
    """)

    assert result.returncode != 0
    assert manifest.read_text(encoding="utf-8") == baseline
    assert [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]


def test_host_access_transaction_append_failure_restores_exact_parseable_bytes(tmp_path):
    transaction_log = tmp_path / "host-access.transaction"
    baseline = json.dumps({"kind": "existing", "padding": "x" * 930}, sort_keys=True) + "\n"
    transaction_log.write_text(baseline, encoding="utf-8")
    transaction_log.chmod(0o600)
    oversized_value = "y" * 4096

    result = run_bash(f"""
        ulimit -f 1
        source {INSTALL}
        HOST_ACCESS_TRANSACTION_LOG={shlex.quote(str(transaction_log))}
        record_host_access_transaction apparmor /etc/apparmor.d/new {shlex.quote(oversized_value)}
    """)

    assert result.returncode != 0
    assert transaction_log.read_bytes() == baseline.encode()
    assert [json.loads(line) for line in transaction_log.read_text(encoding="utf-8").splitlines()]


def test_acl_transaction_append_failure_restores_exact_parseable_bytes(tmp_path):
    chain = tmp_path / "storage"
    chain.mkdir()
    transaction_log = tmp_path / "host-access.transaction"
    baseline = json.dumps({"kind": "existing", "padding": "x" * 930}, sort_keys=True) + "\n"
    transaction_log.write_text(baseline, encoding="utf-8")
    transaction_log.chmod(0o600)
    traversal_manifest = tmp_path / "traversal.manifest"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    acl_state = tmp_path / "acl.state"
    scripts = {
        "getent": "#!/bin/sh\nexit 0\n",
        "getfacl": ('#!/bin/sh\nif [ -e "$ACL_STATE" ]; then printf "user:libvirt-qemu:--x\\n"; fi\n'),
        "setfacl": ('#!/bin/sh\nif [ "$1" = -m ]; then : > "$ACL_STATE"; else rm -f -- "$ACL_STATE"; fi\n'),
    }
    for name, body in scripts.items():
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)

    result = run_bash(
        f"""
        ulimit -f 1
        source {INSTALL}
        TRAVERSAL_ACL_MANIFEST={shlex.quote(str(traversal_manifest))}
        HOST_ACCESS_TRANSACTION_LOG={shlex.quote(str(transaction_log))}
        grant_qemu_traversal {shlex.quote(str(chain))} {shlex.quote(str(tmp_path))}
        """,
        env={"ACL_STATE": str(acl_state), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode != 0
    assert transaction_log.read_bytes() == baseline.encode()
    assert [json.loads(line) for line in transaction_log.read_text(encoding="utf-8").splitlines()]


def test_uninstall_keeps_a_record_when_revocation_fails(tmp_path):
    _require_acl_tools()
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["setfacl", "-m", "u:libvirt-qemu:x", str(target)], check=True)
    manifest = tmp_path / "manifest"
    manifest.write_text(_v2_record(target), encoding="utf-8")
    manifest.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "setfacl"
    fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)

    result = run_bash(_revoke_harness(manifest), env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.returncode != 0
    assert manifest.exists(), "the retry record must survive a failed revocation"
    assert "could not revoke" in result.stderr


def test_uninstall_refuses_a_replaced_or_symlinked_recorded_path(tmp_path):
    _require_acl_tools()
    original = tmp_path / "original"
    original.mkdir()
    manifest = tmp_path / "manifest"
    manifest.write_text(_v2_record(original), encoding="utf-8")
    manifest.chmod(0o600)
    original.rename(tmp_path / "old-original")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    subprocess.run(["setfacl", "-m", "u:libvirt-qemu:x", str(replacement)], check=True)
    original.symlink_to(replacement, target_is_directory=True)

    result = run_bash(_revoke_harness(manifest))

    assert result.returncode != 0
    assert manifest.exists()
    assert _named_entry(replacement) is not None, "root cleanup must not follow the replacement"
    assert "changed since installation" in result.stderr


def test_uninstall_preserves_a_recorded_acl_that_an_operator_widened(tmp_path):
    _require_acl_tools()
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["setfacl", "-m", "u:libvirt-qemu:r-x", str(target)], check=True)
    manifest = tmp_path / "manifest"
    manifest.write_text(_v2_record(target), encoding="utf-8")
    manifest.chmod(0o600)

    result = run_bash(_revoke_harness(manifest))

    assert result.returncode != 0
    assert _named_entry(target) == "user:libvirt-qemu:r-x"
    assert manifest.exists(), "changed ownership evidence must remain retryable/manual"
    assert "changed since installation" in result.stderr


def test_privileged_root_rollback_preserves_an_acl_widened_during_the_transaction(tmp_path):
    _require_acl_tools()
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["setfacl", "-m", "u:libvirt-qemu:r-x", str(target)], check=True)
    acl_manifest = tmp_path / "traversal.manifest"
    acl_manifest.write_text(_v2_record(target), encoding="utf-8")
    acl_manifest.chmod(0o600)
    acl_before = tmp_path / "traversal.before"
    acl_before.write_text("", encoding="utf-8")
    mac_manifest = tmp_path / "mac.manifest"
    mac_before = tmp_path / "mac.before"
    mac_before.write_text("", encoding="utf-8")
    helper = ROOT / "ops" / "deploy" / "configure-privileged-roots.sh"

    result = run_bash(f"""
        source {helper}
        TRAVERSAL_ACL_MANIFEST={shlex.quote(str(acl_manifest))}
        ACL_MANIFEST_BEFORE={shlex.quote(str(acl_before))}
        ACL_MANIFEST_EXISTED=false
        MAC_POLICY_MANIFEST={shlex.quote(str(mac_manifest))}
        MAC_MANIFEST_BEFORE={shlex.quote(str(mac_before))}
        MAC_MANIFEST_EXISTED=false
        STORAGE_ROOT={shlex.quote(str(tmp_path))}
        rollback_host_access_delta
    """)

    assert result.returncode != 0
    assert _named_entry(target) == "user:libvirt-qemu:r-x"
    assert acl_manifest.exists()


def test_uninstall_refuses_a_group_or_other_writable_acl_manifest(tmp_path):
    _require_acl_tools()
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["setfacl", "-m", "u:libvirt-qemu:x", str(target)], check=True)
    manifest = tmp_path / "manifest"
    manifest.write_text(_v2_record(target), encoding="utf-8")
    manifest.chmod(0o666)

    result = run_bash(_revoke_harness(manifest))

    assert result.returncode != 0
    assert _named_entry(target) is not None
    assert manifest.exists()


def test_uninstall_refuses_a_group_or_other_writable_mac_manifest(tmp_path):
    template = tmp_path / "TEMPLATE.qemu"
    rule = '  "/srv/astinuslab/**" rwk,'
    template.write_text(f"profile test {{\n{rule}\n}}\n", encoding="utf-8")
    manifest = tmp_path / "mac-policy.manifest"
    manifest.write_text(
        json.dumps({"version": 2, "kind": "apparmor", "target": str(template), "value": rule}) + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o666)

    result = run_bash(_revoke_mac_harness(manifest))

    assert result.returncode != 0
    assert rule in template.read_text(encoding="utf-8")
    assert manifest.exists()


def test_snapshot_and_rollback_manifest_readers_reject_writable_authority_files():
    install = INSTALL.read_text(encoding="utf-8")
    helper = (ROOT / "ops" / "deploy" / "configure-privileged-roots.sh").read_text(encoding="utf-8")

    assert "snapshot_privileged_manifest" in install
    assert helper.count("info.st_mode & 0o022") >= 2


def test_a_custom_apparmor_rule_is_recorded_and_removed(tmp_path):
    storage = tmp_path / "custom-storage"
    storage.mkdir()
    template = tmp_path / "TEMPLATE.qemu"
    template.write_text("profile LIBVIRT_TEMPLATE {\n}\n", encoding="utf-8")
    template.chmod(0o644)
    manifest = tmp_path / "mac-manifest.jsonl"

    install_result = run_root_mapped_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        systemctl() {{ :; }}
        configure_mac
        """,
        env={"ASTINUSLAB_APPARMOR_TEMPLATE": str(template)},
    )

    assert install_result.returncode == 0, install_result.stdout + install_result.stderr
    rule = f'  "{storage}/**" rwk,'
    assert rule in template.read_text(encoding="utf-8")
    assert manifest.exists(), "the exact custom-root rule needs a durable ownership record"
    assert str(storage) in manifest.read_text(encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    systemctl.chmod(0o755)
    uninstall_result = run_root_mapped_bash(
        _revoke_mac_harness(manifest),
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert uninstall_result.returncode == 0, uninstall_result.stdout + uninstall_result.stderr
    assert rule not in template.read_text(encoding="utf-8")
    assert not manifest.exists()


def test_apparmor_install_refuses_a_symlinked_template(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("profile victim {\n}\n", encoding="utf-8")
    victim.chmod(0o644)
    template = tmp_path / "TEMPLATE.qemu"
    template.symlink_to(victim)
    manifest = tmp_path / "mac-policy.manifest"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    systemctl.chmod(0o755)

    result = run_root_mapped_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        configure_mac
        """,
        env={"ASTINUSLAB_APPARMOR_TEMPLATE": str(template), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode != 0
    assert victim.read_text(encoding="utf-8") == "profile victim {\n}\n"
    assert not manifest.exists()


@pytest.mark.parametrize("unsafe_custody", ["writable", "unprivileged"])
def test_apparmor_install_refuses_an_unprotected_template(tmp_path, unsafe_custody):
    storage = tmp_path / "storage"
    storage.mkdir()
    template = tmp_path / "TEMPLATE.qemu"
    original = "profile test {\n}\n"
    template.write_text(original, encoding="utf-8")
    template.chmod(0o644)
    manifest = tmp_path / "mac-policy.manifest"
    _require_fakeroot()
    unsafe_setup = _unsafe_custody_setup(template, unsafe_custody)

    result = run_fakeroot_bash(
        f"""
        {unsafe_setup}
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        systemctl() {{ exit 91; }}
        configure_mac
        """,
        env={"ASTINUSLAB_APPARMOR_TEMPLATE": str(template)},
    )

    # Assert on the refusal itself, not merely on a nonzero status: the
    # systemctl stub exits 91, so a status check alone passes just as well when
    # configure_mac sails through the custody guard and edits the template.
    assert "AppArmor template is not a protected root-owned file" in result.stderr, result.stderr
    assert template.read_text(encoding="utf-8") == original
    assert not manifest.exists()


def test_apparmor_install_refuses_template_replacement_before_rename(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    template = tmp_path / "TEMPLATE.qemu"
    displaced = tmp_path / "original-template"
    template.write_text("profile test {\n}\n", encoding="utf-8")
    template.chmod(0o644)
    manifest = tmp_path / "mac-policy.manifest"
    fake_modules = tmp_path / "fake-modules"
    fake_modules.mkdir()
    (fake_modules / "secrets.py").write_text(
        """import os
from pathlib import Path


def token_hex(_size):
    target = Path(os.environ["APPARMOR_RACE_TARGET"])
    if target.exists():
        target.rename(os.environ["APPARMOR_RACE_DISPLACED"])
        target.write_text("profile replacement {\\n}\\n", encoding="utf-8")
    return str(os.getpid())
""",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    systemctl.chmod(0o755)

    result = run_root_mapped_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        configure_mac
        """,
        env={
            "ASTINUSLAB_APPARMOR_TEMPLATE": str(template),
            "PYTHONPATH": str(fake_modules),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "APPARMOR_RACE_TARGET": str(template),
            "APPARMOR_RACE_DISPLACED": str(displaced),
        },
    )

    assert result.returncode != 0
    assert template.read_text(encoding="utf-8") == "profile replacement {\n}\n"
    assert '"' + str(storage) + '/**" rwk,' not in template.read_text(encoding="utf-8")


def test_apparmor_manifest_failure_immediately_undoes_inserted_rule(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    template = tmp_path / "TEMPLATE.qemu"
    template.write_text("profile test {\n}\n", encoding="utf-8")
    template.chmod(0o644)
    manifest = tmp_path / "manifest-is-a-directory"
    manifest.mkdir()

    result = run_root_mapped_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        configure_mac
        """,
        env={"ASTINUSLAB_APPARMOR_TEMPLATE": str(template)},
    )

    assert result.returncode != 0
    assert f'  "{storage}/**" rwk,' not in template.read_text(encoding="utf-8")


def test_apparmor_crash_after_mutation_never_preclaims_operator_ownership(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    template = tmp_path / "TEMPLATE.qemu"
    template.write_text("profile test {\n}\n", encoding="utf-8")
    template.chmod(0o644)
    manifest = tmp_path / "mac-policy.manifest"

    result = run_root_mapped_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        record_mac_change() {{ kill -KILL "$$"; }}
        configure_mac
        """,
        env={"ASTINUSLAB_APPARMOR_TEMPLATE": str(template)},
    )

    assert result.returncode != 0
    assert f'  "{storage}/**" rwk,' in template.read_text(encoding="utf-8")
    assert not manifest.exists()


def test_apparmor_record_survives_until_policy_and_libvirt_reload(tmp_path):
    template = tmp_path / "TEMPLATE.qemu"
    rule = '  "/srv/astinuslab/**" rwk,'
    template.write_text(f"profile test {{\n{rule}\n}}\n", encoding="utf-8")
    template.chmod(0o644)
    manifest = tmp_path / "mac-policy.manifest"
    record = {"version": 2, "kind": "apparmor", "target": str(template), "value": rule}
    manifest.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    manifest.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n[ "$2" != "libvirtd" ]\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    result = run_root_mapped_bash(
        _revoke_mac_harness(manifest),
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode != 0
    assert manifest.exists(), "the reload retry record must be durable"
    assert rule not in template.read_text(encoding="utf-8")


@pytest.mark.parametrize("unsafe_custody", ["writable", "unprivileged"])
def test_apparmor_uninstall_validation_failure_preserves_template_and_manifest(tmp_path, unsafe_custody):
    template = tmp_path / "TEMPLATE.qemu"
    rule = '  "/srv/astinuslab/**" rwk,'
    original = f"profile test {{\n{rule}\n}}\n"
    template.write_text(original, encoding="utf-8")
    template.chmod(0o644)
    manifest = tmp_path / "mac-policy.manifest"
    record = json.dumps({"version": 2, "kind": "apparmor", "target": str(template), "value": rule}) + "\n"
    manifest.write_text(record, encoding="utf-8")
    manifest.chmod(0o600)
    reload_calls = tmp_path / "reload.calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(reload_calls))}\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    _require_fakeroot()
    unsafe_setup = _unsafe_custody_setup(template, unsafe_custody)

    result = run_fakeroot_bash(
        f"{unsafe_setup}\n{_revoke_mac_harness(manifest)}",
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert "AppArmor template is not a protected root-owned file" in result.stderr, result.stderr
    assert template.read_text(encoding="utf-8") == original
    assert manifest.read_bytes() == record.encode()
    assert not reload_calls.exists()


def test_apparmor_cleanup_refuses_template_replacement_before_rename(tmp_path):
    template = tmp_path / "TEMPLATE.qemu"
    displaced = tmp_path / "original-template"
    rule = '  "/srv/astinuslab/**" rwk,'
    template.write_text(f"profile test {{\n{rule}\n}}\n", encoding="utf-8")
    template.chmod(0o644)
    manifest = tmp_path / "mac-policy.manifest"
    manifest.write_text(
        json.dumps({"version": 2, "kind": "apparmor", "target": str(template), "value": rule}) + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    fake_modules = tmp_path / "fake-modules"
    fake_modules.mkdir()
    (fake_modules / "secrets.py").write_text(
        """import os
from pathlib import Path

replaced = False


def token_hex(_size):
    global replaced
    target = Path(os.environ["APPARMOR_RACE_TARGET"])
    if not replaced:
        replaced = True
        target.rename(os.environ["APPARMOR_RACE_DISPLACED"])
        target.write_text("profile replacement {\\n}\\n", encoding="utf-8")
    return f"{os.getpid()}.{int(replaced)}"
""",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    systemctl.chmod(0o755)

    result = run_root_mapped_bash(
        _revoke_mac_harness(manifest),
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTHONPATH": str(fake_modules),
            "APPARMOR_RACE_TARGET": str(template),
            "APPARMOR_RACE_DISPLACED": str(displaced),
        },
    )

    assert result.returncode != 0
    assert template.read_text(encoding="utf-8") == "profile replacement {\n}\n"
    assert rule in displaced.read_text(encoding="utf-8")
    assert manifest.exists()


def test_a_preexisting_apparmor_rule_is_not_claimed_by_the_installer(tmp_path):
    storage = tmp_path / "operator-storage"
    storage.mkdir()
    rule = f'  "{storage}/**" rwk,'
    template = tmp_path / "TEMPLATE.qemu"
    template.write_text(f"profile LIBVIRT_TEMPLATE {{\n{rule}\n}}\n", encoding="utf-8")
    template.chmod(0o644)
    manifest = tmp_path / "mac-manifest.jsonl"

    result = run_root_mapped_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        systemctl() {{ :; }}
        configure_mac
        """,
        env={"ASTINUSLAB_APPARMOR_TEMPLATE": str(template)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not manifest.exists(), "an operator-owned rule must never enter our cleanup manifest"
    assert rule in template.read_text(encoding="utf-8")


def test_uninstall_never_infers_mac_ownership_when_manifest_is_absent():
    text = UNINSTALL.read_text(encoding="utf-8")
    tail = text[text.index("mac_manifest_present=false") : text.index("# ── Remove systemd unit files")]

    assert "sed -i" not in tail
    assert "semanage fcontext -d" not in tail
    assert "Preserving unrecorded AppArmor/SELinux policy" in tail


def test_a_custom_selinux_expression_is_recorded_and_removed(tmp_path):
    storage = tmp_path / "selinux-storage"
    storage.mkdir()
    manifest = tmp_path / "mac-manifest.jsonl"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "semanage.calls"
    selinux_state = tmp_path / "selinux.state"
    for name, body in {
        "getenforce": "#!/bin/sh\necho Enforcing\n",
        "restorecon": "#!/bin/sh\nexit 0\n",
        "semanage": (
            f'#!/bin/sh\necho "$*" >> {shlex.quote(str(calls))}\n'
            'if [ "$1 $2" = "fcontext -l" ] && [ -f "$SELINUX_STATE" ]; then '
            'printf "%s all files system_u:object_r:svirt_image_t:s0\\n" "$SELINUX_EXPRESSION"; fi\n'
            'if [ "$1 $2" = "fcontext -a" ]; then : > "$SELINUX_STATE"; fi\n'
            'if [ "$1 $2" = "fcontext -d" ]; then rm -f "$SELINUX_STATE"; fi\n'
        ),
    }.items():
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ASTINUSLAB_APPARMOR_TEMPLATE": str(tmp_path / "absent-template"),
        "SELINUX_STATE": str(selinux_state),
        "SELINUX_EXPRESSION": re.escape(str(storage)) + r"(/.*)?",
    }

    install_result = run_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        configure_mac
    """,
        env=env,
    )

    expression = re.escape(str(storage)) + r"(/.*)?"
    assert install_result.returncode == 0, install_result.stdout + install_result.stderr
    assert manifest.exists()
    manifest_text = manifest.read_text(encoding="utf-8")
    assert '"kind": "selinux"' in manifest_text
    record = json.loads(manifest_text)
    assert record["value"] == expression
    assert record["version"] == 2
    assert record["target"] == str(storage)
    assert record["selinux_type"] == "svirt_image_t"

    calls.write_text("", encoding="utf-8")
    uninstall_result = run_bash(
        _revoke_mac_harness(manifest),
        env=env,
    )

    assert uninstall_result.returncode == 0, uninstall_result.stdout + uninstall_result.stderr
    assert f"fcontext -d {expression}" in calls.read_text(encoding="utf-8")
    assert not manifest.exists()


def test_selinux_file_spec_escapes_regex_metacharacters_in_storage_root(tmp_path):
    storage = "/srv/astinuslab.case+one"
    manifest = tmp_path / "mac-policy.manifest"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "semanage.calls"
    state = tmp_path / "selinux.state"
    for name, body in {
        "getenforce": "#!/bin/sh\necho Enforcing\n",
        "restorecon": "#!/bin/sh\nexit 0\n",
        "semanage": (
            f'#!/bin/sh\necho "$*" >> {shlex.quote(str(calls))}\n'
            'if [ "$1 $2" = "fcontext -l" ] && [ -f "$SELINUX_STATE" ]; then '
            'printf "%s all files system_u:object_r:svirt_image_t:s0\\n" "$SELINUX_EXPRESSION"; fi\n'
            'if [ "$1 $2" = "fcontext -a" ]; then : > "$SELINUX_STATE"; fi\n'
            'if [ "$1 $2" = "fcontext -d" ]; then rm -f "$SELINUX_STATE"; fi\n'
        ),
    }.items():
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)

    result = run_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(storage)}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        configure_mac
        """,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ASTINUSLAB_APPARMOR_TEMPLATE": str(tmp_path / "absent-template"),
            "SELINUX_STATE": str(state),
            "SELINUX_EXPRESSION": re.escape(storage) + r"(/.*)?",
        },
    )

    expression = re.escape(storage) + r"(/.*)?"
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"fcontext -a -t svirt_image_t {expression}" in calls.read_text(encoding="utf-8")
    assert json.loads(manifest.read_text(encoding="utf-8"))["value"] == expression
    assert re.fullmatch(expression, storage + "/disk.qcow2")
    assert not re.fullmatch(expression, "/srv/astinuslabXcase+one/disk.qcow2")


def test_selinux_manifest_claim_is_persisted_only_after_verified_host_mutation():
    text = INSTALL.read_text(encoding="utf-8")
    block = text[
        text.index('if [[ "$selinux_rule_state" == absent ]]') : text.index(
            "restorecon -Rv", text.index('if [[ "$selinux_rule_state" == absent ]]')
        )
    ]

    assert block.index("semanage fcontext -a") < block.index("record_mac_change selinux")
    assert block.index('fcontext_list="$(semanage fcontext -l') < block.index("record_mac_change selinux")


def test_selinux_absent_mapping_is_relabelled_and_record_is_completed(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    expression = re.escape(str(storage)) + r"(/.*)?"
    manifest = tmp_path / "mac-policy.manifest"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "kind": "selinux",
                "target": str(storage),
                "value": expression,
                "selinux_type": "svirt_image_t",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    for name, body in {
        "semanage": f'#!/bin/sh\necho "$*" >> {shlex.quote(str(calls))}\nexit 0\n',
        "restorecon": f'#!/bin/sh\necho "$*" >> {shlex.quote(str(calls))}\nexit 0\n',
    }.items():
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)

    result = run_bash(_revoke_mac_harness(manifest), env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert not manifest.exists()
    logged = calls.read_text(encoding="utf-8")
    assert "fcontext -d" not in logged
    assert f"-Rv {storage}" in logged


def test_selinux_record_survives_failed_relabel(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    expression = re.escape(str(storage)) + r"(/.*)?"
    manifest = tmp_path / "mac-policy.manifest"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "kind": "selinux",
                "target": str(storage),
                "value": expression,
                "selinux_type": "svirt_image_t",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    semanage = fake_bin / "semanage"
    semanage.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    semanage.chmod(0o755)
    restorecon = fake_bin / "restorecon"
    restorecon.write_text("#!/bin/sh\nexit 31\n", encoding="utf-8")
    restorecon.chmod(0o755)

    result = run_bash(_revoke_mac_harness(manifest), env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.returncode != 0
    assert manifest.exists(), "relabel must succeed before the ownership record is discarded"


def test_a_failed_selinux_add_never_creates_an_ownership_record(tmp_path):
    storage = tmp_path / "selinux-storage"
    storage.mkdir()
    manifest = tmp_path / "mac-manifest.jsonl"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "getenforce": "#!/bin/sh\necho Enforcing\n",
        "restorecon": "#!/bin/sh\nexit 0\n",
        "semanage": '#!/bin/sh\ncase "$2" in -l) exit 0;; -a) exit 23;; esac\nexit 0\n',
    }.items():
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)

    result = run_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        configure_mac
        """,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ASTINUSLAB_APPARMOR_TEMPLATE": str(tmp_path / "absent-template"),
        },
    )

    assert result.returncode != 0
    assert not manifest.exists()


def test_selinux_host_policy_is_undone_when_manifest_persistence_fails(tmp_path):
    storage = tmp_path / "selinux-storage"
    storage.mkdir()
    manifest = tmp_path / "manifest-is-a-directory"
    manifest.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "semanage.calls"
    state = tmp_path / "selinux.state"
    expression = re.escape(str(storage)) + r"(/.*)?"
    for name, body in {
        "getenforce": "#!/bin/sh\necho Enforcing\n",
        "restorecon": "#!/bin/sh\nexit 0\n",
        "semanage": (
            f'#!/bin/sh\necho "$*" >> {shlex.quote(str(calls))}\n'
            'if [ "$1 $2" = "fcontext -l" ] && [ -f "$SELINUX_STATE" ]; then '
            'printf "%s all files system_u:object_r:svirt_image_t:s0\\n" "$SELINUX_EXPRESSION"; fi\n'
            'if [ "$1 $2" = "fcontext -a" ]; then : > "$SELINUX_STATE"; fi\n'
            'if [ "$1 $2" = "fcontext -d" ]; then rm -f "$SELINUX_STATE"; fi\n'
        ),
    }.items():
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)

    result = run_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        configure_mac
        """,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ASTINUSLAB_APPARMOR_TEMPLATE": str(tmp_path / "absent-template"),
            "SELINUX_STATE": str(state),
            "SELINUX_EXPRESSION": expression,
        },
    )

    assert result.returncode != 0
    assert not state.exists()
    logged = calls.read_text(encoding="utf-8").splitlines()
    assert logged[0] == "fcontext -l"
    assert logged[1] == f"fcontext -a -t svirt_image_t {expression}"
    assert logged[2] == "fcontext -l"
    assert logged[3] == f"fcontext -d {expression}"


def test_selinux_crash_after_mutation_never_preclaims_operator_ownership(tmp_path):
    storage = tmp_path / "selinux-storage"
    storage.mkdir()
    manifest = tmp_path / "mac-policy.manifest"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "selinux.state"
    expression = re.escape(str(storage)) + r"(/.*)?"
    for name, body in {
        "getenforce": "#!/bin/sh\necho Enforcing\n",
        "restorecon": "#!/bin/sh\nexit 0\n",
        "semanage": (
            '#!/bin/sh\nif [ "$1 $2" = "fcontext -l" ] && [ -f "$SELINUX_STATE" ]; then '
            'printf "%s all files system_u:object_r:svirt_image_t:s0\\n" "$SELINUX_EXPRESSION"; fi\n'
            'if [ "$1 $2" = "fcontext -a" ]; then : > "$SELINUX_STATE"; fi\n'
            'if [ "$1 $2" = "fcontext -d" ]; then rm -f "$SELINUX_STATE"; fi\n'
        ),
    }.items():
        script = fake_bin / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)

    result = run_bash(
        f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        MAC_POLICY_MANIFEST={shlex.quote(str(manifest))}
        grant_qemu_traversal() {{ :; }}
        record_mac_change() {{ kill -KILL "$$"; }}
        configure_mac
        """,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ASTINUSLAB_APPARMOR_TEMPLATE": str(tmp_path / "absent-template"),
            "SELINUX_STATE": str(state),
            "SELINUX_EXPRESSION": expression,
        },
    )

    assert result.returncode != 0
    assert state.exists(), "the injected crash occurs after semanage mutation and verification"
    assert not manifest.exists()
