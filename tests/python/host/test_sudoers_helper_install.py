from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
HELPER_DIR = "/usr/local/libexec/astinuslab"


@pytest.mark.parametrize("existing_mode", [0o700, 0o755])
def test_new_helper_ancestors_ignore_private_umask_without_changing_existing_parents(tmp_path, existing_mode):
    script = ROOT / "ops/host/install_sudoers_astinuslab.sh"
    existing = tmp_path / "operator-owned"
    existing.mkdir(mode=existing_mode)
    existing.chmod(existing_mode)
    helper_dir = existing / "new-libexec" / "nested" / "astinuslab"
    program = f"""
    source {shlex.quote(str(script))}
    HELPER_INSTALL_DIR={shlex.quote(str(helper_dir))}
    umask 077
    prepare_helper_install_dir
    """

    result = subprocess.run(["bash", "-c", program], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert existing.stat().st_mode & 0o777 == existing_mode
    for path in (existing / "new-libexec", helper_dir.parent, helper_dir):
        assert path.stat().st_mode & 0o777 == 0o755, f"new helper ancestor cannot be traversed: {path}"


@pytest.mark.parametrize(
    ("caller_env", "explicit_user", "expected"),
    [
        ({}, "astinuslab-runner", "astinuslab-runner"),
        ({"USER": "operator"}, None, "operator"),
        ({"USER": "root", "SUDO_USER": "operator"}, None, "operator"),
        ({"USER": "root", "SUDO_USER": "operator"}, "astinuslab-runner", "astinuslab-runner"),
    ],
)
def test_sudoers_installer_accepts_explicit_identity_in_clean_environment(
    tmp_path, caller_env, explicit_user, expected
):
    script = ROOT / "ops/host/install_sudoers_astinuslab.sh"
    storage = tmp_path / "storage"
    storage.mkdir(mode=0o700)
    arguments = ["--install-dir", str(tmp_path), "--storage-root", str(storage)]
    if explicit_user:
        arguments.extend(["--run-user", explicit_user])
    # Argument parsing only: no root check, helper publication or sudoers write.
    program = f"""
    source {shlex.quote(str(script))}
    validate_storage_root_authority() {{ :; }}
    parse_args {shlex.join(arguments)}
    printf '%s\\n' "$RUN_USER"
    """
    result = subprocess.run(
        ["bash", "-c", program],
        env={"PATH": os.defpath, "HOME": str(tmp_path), **caller_env},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_sudoers_installer_requires_identity_when_environment_and_argument_are_absent(tmp_path):
    script = ROOT / "ops/host/install_sudoers_astinuslab.sh"
    result = subprocess.run(
        ["bash", str(script), "--install-dir", str(tmp_path)],
        env={"PATH": os.defpath, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--run-user cannot be empty" in result.stderr
    assert "unbound variable" not in result.stderr


def test_dev_sudoers_installer_installs_root_owned_libexec_helpers():
    text = (ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh").read_text(encoding="utf-8")

    assert 'INSTALL_DIR="${INSTALL_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"' in text
    assert f'HELPER_INSTALL_DIR="${{ASTINUSLAB_HELPER_INSTALL_DIR:-{HELPER_DIR}}}"' in text
    assert "prepare_helper_install_dir" in text
    assert (
        'render_helper_template "$source_helper" "$target_helper" "$INSTALL_DIR" "$install_dev" "$install_ino"' in text
    )
    assert 'chown root:root "$target_helper"' in text
    assert 'chmod 0755 "$target_helper"' in text
    assert 'text.replace("@@INSTALL_DIR@@", install_dir)' in text
    assert 'text.replace("@@INSTALL_DEV@@", install_dev)' in text
    assert 'text.replace("@@INSTALL_INO@@", install_ino)' in text
    assert 'HELPER_STORAGE="$HELPER_INSTALL_DIR/libvirt_storage_admin.sh"' in text
    assert 'HELPER_ACL="$HELPER_INSTALL_DIR/fix_libvirt_path_acl.sh"' in text
    assert 'for caller in "$RUN_USER" "${ADDITIONAL_RUN_USERS[@]}"' in text
    assert "NOPASSWD: %s, %s, %s" in text
    assert "--run-user USER" in text
    assert "--helper-source-dir PATH" in text


def test_run_user_validation_rejects_sudoers_injection():
    """A hostile --run-user is rejected inside parse_args, before require_root.

    Safe to run as any user: the installer exits during argument parsing and
    never reaches install_helpers, so nothing on the host is touched.
    """
    script = ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh"

    hostile = subprocess.run(
        ["bash", str(script), "--run-user", "evil ALL=(ALL) ALL #"],
        capture_output=True,
        text=True,
    )

    assert hostile.returncode != 0
    assert "unsupported characters" in hostile.stderr


@pytest.mark.parametrize("unsafe_kind", ["broad", "nested"])
def test_sudoers_policy_writer_rejects_unsafe_root_before_mutation(tmp_path, unsafe_kind):
    script = ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh"
    install = tmp_path / "install"
    install.mkdir()
    if unsafe_kind == "broad":
        storage = Path("/opt")
        expected = "not a registrable storage directory"
    else:
        install = ROOT
        storage = ROOT / "src" / "astinuslab"
        expected = "path-disjoint"
    policy = tmp_path / "policy.json"
    policy.write_text("sentinel\n", encoding="utf-8")
    helper_install = tmp_path / "libexec"
    sudoers = tmp_path / "sudoers"
    program = f"""
    source {script}
    POLICY_FILE={policy}
    HELPER_INSTALL_DIR={helper_install}
    SUDOERS_FILE={sudoers}
    parse_args --install-dir {install} --storage-root {storage} --run-user astinuslab-runner
    """

    result = subprocess.run(["bash", "-c", program], capture_output=True, text=True)

    assert result.returncode != 0
    assert expected in result.stderr
    assert policy.read_text(encoding="utf-8") == "sentinel\n"
    assert not helper_install.exists()
    assert not sudoers.exists()


def test_transitional_sudoers_keeps_old_identity_until_runner_verifies(tmp_path):
    script = ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh"
    sudoers = tmp_path / "sudoers"
    storage = tmp_path / "storage"
    storage.mkdir()
    program = f"""
    source {script}
    SUDOERS_FILE={sudoers}
    visudo() {{ :; }}
    validate_storage_root_authority() {{ :; }}
    parse_args --install-dir {tmp_path} --storage-root {storage} \
      --run-user astinuslab-runner --additional-run-user operator
    write_sudoers_rule
    """
    result = subprocess.run(["bash", "-c", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    rendered = sudoers.read_text(encoding="utf-8")
    assert "astinuslab-runner ALL=(root) NOPASSWD:" in rendered
    assert "operator ALL=(root) NOPASSWD:" in rendered


def test_split_policy_authorizes_only_callers_passed_by_root_installer():
    text = (ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh").read_text(encoding="utf-8")
    assert "--additional-run-user USER" in text
    assert '"allowed_callers": callers' in text


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="a valid --run-user passes parse_args; as root the installer would go on to "
    "write /usr/local/libexec/astinuslab and /etc/sudoers.d/astinuslab-runner",
)
def test_run_user_validation_accepts_a_strict_username():
    script = ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh"

    benign = subprocess.run(
        ["bash", str(script), "--run-user", "svc_astinuslab"],
        capture_output=True,
        text=True,
    )

    assert benign.returncode != 0
    assert "unsupported characters" not in benign.stderr
    assert "Run as root" in benign.stderr


def test_deploy_installer_delegates_sudoers_install_to_unified_helper():
    text = (ROOT / "ops" / "deploy" / "install.sh").read_text(encoding="utf-8")

    # The helper is rendered from the live release, but the identity it binds
    # stays the stable install root: that root's inode is baked into the storage
    # helper, so a release switch must not change it.
    assert '"$RELEASE_DIR/ops/host/install_sudoers_astinuslab.sh" \\' in text
    assert '--install-dir "$INSTALL_DIR"' in text
    assert '--run-user "$SERVICE_USER"' in text
    assert '--helper-source-dir "$RELEASE_DIR/ops/host"' in text
    assert 'HELPER_STORAGE="$INSTALL_DIR/ops/host/libvirt_storage_admin.sh"' not in text
    assert 'HELPER_ACL="$INSTALL_DIR/ops/host/fix_libvirt_path_acl.sh"' not in text


def test_installers_use_literal_python_rendering_instead_of_sed_escaping():
    deploy_text = (ROOT / "ops" / "deploy" / "install.sh").read_text(encoding="utf-8")
    helper_text = (ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh").read_text(encoding="utf-8")

    assert "render_template() {" in deploy_text
    assert "render_helper_template() {" in helper_text
    assert 'text.replace("/opt/astinuslab", install_dir)' in deploy_text
    assert 'text.replace("@@INSTALL_DIR@@", install_dir)' in helper_text
    assert "escaped_install_dir=" not in deploy_text
    assert "sed -i" not in helper_text


def test_installed_storage_helper_binds_install_root_identity():
    wrapper = (ROOT / "ops" / "host" / "libvirt_storage_admin.sh").read_text(encoding="utf-8")
    implementation = (ROOT / "ops" / "host" / "libvirt_storage_admin.py").read_text(encoding="utf-8")

    assert 'exec python3 -B "$SCRIPT_DIR/libvirt_storage_admin.py"' in wrapper
    assert '_INSTALL_ROOT = "@@INSTALL_DIR@@"' in implementation
    assert '_INSTALL_DEV = "@@INSTALL_DEV@@"' in implementation
    assert '_INSTALL_INO = "@@INSTALL_INO@@"' in implementation


def test_uninstall_removes_installed_libexec_helpers():
    text = (ROOT / "ops" / "deploy" / "uninstall.sh").read_text(encoding="utf-8")
    # The helper set and its root stay in the script, because they are what this
    # installation claims to own. The removal itself is
    # ops/deploy/privileged_state.py, so the descriptor-relative unlink is
    # asserted there rather than against a heredoc.
    removal = (ROOT / "ops" / "deploy" / "privileged_state.py").read_text(encoding="utf-8")
    assert "fix_libvirt_path_acl.py" in text
    assert "acl_policy.py" in text
    assert f'HELPER_INSTALL_DIR="${{ASTINUSLAB_HELPER_INSTALL_DIR:-{HELPER_DIR}}}"' in text
    assert "remove_installed_privileged_helpers" in text
    assert "remove-privileged-helpers" in text
    assert "for name in managed_names:" in removal
    assert "os.unlink(name, dir_fd=directory_fd)" in removal
    assert "os.rmdir(path)" in removal


def test_host_scripts_use_installed_privileged_helper_path():
    scripts = [
        ROOT / "ops" / "host" / "bootstrap_cloud_image_libvirt_storage.sh",
        ROOT / "ops" / "host" / "bake_workload.sh",
        ROOT / "ops" / "host" / "cleanup_job_vm.sh",
    ]
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert 'HELPER="/usr/local/libexec/astinuslab/libvirt_storage_admin.sh"' in text
        assert 'HELPER="$BASE_DIR/ops/host/libvirt_storage_admin.sh"' not in text
        assert '$(dirname "${BASH_SOURCE[0]}")" && pwd)/libvirt_storage_admin.sh' not in text


def test_task4_helper_set_is_shared_and_complete():
    installer = (ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh").read_text(encoding="utf-8")
    uninstaller = (ROOT / "ops" / "deploy" / "uninstall.sh").read_text(encoding="utf-8")
    expected = {
        "libvirt_storage_admin.sh",
        "libvirt_storage_admin.py",
        "fix_libvirt_path_acl.sh",
        "fix_libvirt_path_acl.py",
        "acl_policy.py",
        "network_isolation_admin.sh",
    }
    for text in (installer, uninstaller):
        start = text.index("HELPER_SET=(")
        body = text[start : text.index(")", start)]
        assert expected <= set(body.split())
    assert 'rm -rf -- "$existing_name"' in installer
    assert 'rmdir "$HELPER_INSTALL_DIR" 2>/dev/null || true' not in uninstaller


def test_installer_records_the_tolerated_storage_group_in_policy():
    """The ACL helper only tolerates group write for a group the root-owned
    policy names, so the installer has to record one whenever the storage root
    it is registering is group-writable - otherwise the supported 2770 layout
    would be refused at capture time."""
    text = (ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh").read_text(encoding="utf-8")

    assert "--storage-group" in text
    assert '"storage_groups": [group] if group else []' in text
    assert "0$(stat -c '%a' \"$STORAGE_ROOT\") & 0020" in text
    assert 'getent group "$STORAGE_GROUP"' in text


def test_installer_records_evidence_read_groups_separately_from_storage_validation():
    text = (ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh").read_text(encoding="utf-8")

    assert "--evidence-read-group GROUP" in text
    assert 'EVIDENCE_READ_GROUPS+=("$2")' in text
    assert '"evidence_read_groups": evidence_read_groups' in text
    assert '"storage_groups": [group] if group else []' in text


def test_acl_helper_probe_stays_inside_the_narrow_installed_helper_command():
    installer = (ROOT / "ops" / "host" / "install_sudoers_astinuslab.sh").read_text(encoding="utf-8")
    wrapper = (ROOT / "ops" / "host" / "fix_libvirt_path_acl.sh").read_text(encoding="utf-8")

    assert 'HELPER_ACL="$HELPER_INSTALL_DIR/fix_libvirt_path_acl.sh"' in installer
    assert "NOPASSWD: %s, %s, %s" in installer
    assert 'exec python3 -B "$SCRIPT_DIR/fix_libvirt_path_acl.py" "$1"' in wrapper
    assert '"$1" != "--probe"' in wrapper


def test_privileged_root_command_uses_acl_policy_authority_and_atomic_policy_write():
    text = (ROOT / "ops" / "deploy" / "configure-privileged-roots.sh").read_text(encoding="utf-8")
    assert "from acl_policy import validate_root" in text
    assert "from acl_policy import normalize_roots" in text
    assert "json.load(handle)" in text
    assert "os.fsync(handle.fileno())" in text
    # Cleanup state is script scope so the EXIT trap can still see it after main
    # returns; a local would make the trap abort under `set -u` and strand
    # temporary policy files in /etc.
    assert 'mv -f -- "$POLICY_TMP" "$POLICY_FILE"' in text
    assert "trap cleanup_artifacts EXIT" in text
    assert "local policy_tmp" not in text
    assert "systemctl restart" in text
    assert "--restart" in text


def test_unit_templates_have_no_fixed_storage_sandbox_root():
    for name in ("astinuslab-api", "astinuslab-runner", "astinuslab-analysis"):
        text = (ROOT / "ops/systemd" / f"{name}.service").read_text(encoding="utf-8")
        assert "@@STORAGE_ROOT@@" in text
        assert "ReadWritePaths=/opt/astinuslab/storage" not in text
    api = (ROOT / "ops/systemd/astinuslab-api.service").read_text(encoding="utf-8")
    assert "@@PROTECT_HOME@@" in api


def test_privileged_helpers_never_write_bytecode_into_the_helper_directory():
    """A root-run import leaves a root-owned __pycache__ beside the helpers:
    needless mutable state inside the trust boundary, and an orphan the
    installer then has to remove."""
    for name in ("fix_libvirt_path_acl.sh", "libvirt_storage_admin.sh"):
        text = (ROOT / "ops" / "host" / name).read_text(encoding="utf-8")
        assert "exec python3 -B " in text, f"{name} must run python with -B"


def test_orphan_removal_survives_a_directory_orphan(tmp_path):
    """rm -f on a directory exits 1, and the installer runs under set -e, so a
    stray __pycache__ in the helper directory would abort every later install."""
    helper_dir = tmp_path / "libexec"
    helper_dir.mkdir()
    (helper_dir / "__pycache__").mkdir()
    (helper_dir / "__pycache__" / "acl_policy.pyc").write_bytes(b"stale")
    (helper_dir / "libvirt_storage_admin.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    program = f"""
        set -euo pipefail
        HELPER_INSTALL_DIR={helper_dir}
        HELPER_SET=(libvirt_storage_admin.sh)
        for existing_name in "$HELPER_INSTALL_DIR"/*; do
          [[ -e "$existing_name" || -L "$existing_name" ]] || continue
          known=false
          for helper_name in "${{HELPER_SET[@]}}"; do
            [[ "$(basename "$existing_name")" == "$helper_name" ]] && known=true
          done
          $known || rm -rf -- "$existing_name"
        done
        echo SURVIVED
    """
    result = subprocess.run(["bash", "-c", program], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "SURVIVED" in result.stdout
    assert not (helper_dir / "__pycache__").exists()
    assert (helper_dir / "libvirt_storage_admin.sh").exists()
